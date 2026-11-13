"""TheAudioDB 数据源提供者（支持专辑搜索、单曲直搜、曲目信息、发行年份、封面、流派、作曲者与高清艺人写真）。"""

from __future__ import annotations

import os
import re
import threading

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    cached_album,
    cached_artist_url,
    cached_search,
    cached_song_meta,
    cached_song_search,
    choose_album_detail,
    first_artist,
    http_get_json,
    norm_core,
    norm_full,
    normalize_release_date,
    parse_lyric_credits,
    set_album_meta,
    to_seconds,
)

_TADB_CB = ProviderCircuitBreaker(threshold=5)
_TADB_LOCK = threading.Lock()
_TADB_DEFAULT_KEY = '123'
_TADB_KEY = ''


def set_tadb_api_key(api_key: str) -> str:
    global _TADB_KEY
    k = str(api_key or '').strip()
    with _TADB_LOCK:
        _TADB_KEY = k
    return k


def get_tadb_api_key() -> str:
    with _TADB_LOCK:
        k = _TADB_KEY
    if not k:
        k = str(os.environ.get('THEAUDIODB_API_KEY') or os.environ.get('TADB_API_KEY') or '').strip()
    return k or _TADB_DEFAULT_KEY


def _tadb_get(endpoint: str, params=None):
    if _TADB_CB.is_open():
        return None
    api_key = get_tadb_api_key()
    url = f'https://www.theaudiodb.com/api/v1/json/{api_key}/{endpoint}'
    d = http_get_json(url, params=params, headers={'User-Agent': 'MusicMetadata/1.0'}, timeout=10)
    if d is None:
        _TADB_CB.record_failure()
        return None
    _TADB_CB.record_success()
    return d


def _pick_tadb_artist_pic(a: dict) -> str:
    if not isinstance(a, dict):
        return ''
    for k in ('strArtistThumb', 'strArtistFanart', 'strArtistWideThumb', 'strArtistFanart2', 'strArtistFanart3', 'strArtistCutout'):
        val = str(a.get(k) or '').strip()
        if val.startswith('http'):
            return val
    return ''


_COMPOSER_PATTERNS = (
    re.compile(
        r'(?:written\s+and\s+composed|composed\s+and\s+written|composed|written|music|song\s+written)\s+by\s+(?:the\s+)?'
        r'([A-Z][a-zA-Z0-9\s\.\-&/,\']+?)'
        r'(?:\s+(?:for|in|on|with|as\s+the|credited|and\s+first|and\s+produced|and\s+recorded|released|who|which)|,\s+(?:and\s+first|released|and\s+released)|[.;\(\)\n]|$)',
        re.IGNORECASE,
    ),
    re.compile(
        r'(?:written\s+and\s+performed|performed\s+and\s+written)\s+by\s+(?:the\s+)?'
        r'(?:(?:English|American|British|Canadian|Australian|French|German|Italian|Japanese|Swedish)\s+)?'
        r'(?:musician|singer|artist|composer)?\s*'
        r'([A-Z][a-zA-Z0-9\s\.\-&/,\']+?)'
        r'(?:\s+(?:for|in|on|with|as\s+the|credited|and\s+first|released)|[.;\(\)\n]|$)',
        re.IGNORECASE,
    ),
)


def _clean_composer(name: str) -> str:
    if not name:
        return ''
    cleaned = re.sub(
        r'\b(?:guitarist|vocalist|singer|pianist|musician|artist|bassist|drummer|frontman|songwriter|producer)\s+',
        '',
        name,
        flags=re.IGNORECASE,
    ).strip()
    cleaned = re.sub(r'\s+and\s+', ', ', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s*,\s*', ', ', cleaned)
    return cleaned.strip(' ,.-;')


def _extract_tadb_composer(track: dict) -> str:
    if not isinstance(track, dict):
        return ''
    # 1. 优先尝试从内嵌歌词头部 (strTrackLyrics) 提取制作人/作曲者
    lrc = str(track.get('strTrackLyrics') or '').strip()
    if lrc:
        comp = parse_lyric_credits(lrc)
        if comp:
            return comp

    # 2. 从多语言简介 (strDescriptionEN / CN / DE 等) 中正则提取曲作者
    for k in ('strDescriptionEN', 'strDescriptionCN', 'strDescriptionDE', 'strDescriptionFR'):
        desc = str(track.get(k) or '').strip()
        if not desc:
            continue
        for pat in _COMPOSER_PATTERNS:
            m = pat.search(desc)
            if m:
                raw_name = _clean_composer(m.group(1))
                if raw_name and len(raw_name) <= 80 and not any(
                    w in raw_name.lower() for w in ('song', 'album', 'record', 'single', 'label', 'track')
                ):
                    return raw_name

    # 3. 古典乐或原声配乐分类下，艺人通常即为作曲家
    genre = str(track.get('strGenre') or '').strip().lower()
    if genre in {'classical', 'ost', 'score', 'soundtrack', 'film score', 'composer'} or 'classical' in genre:
        artist = str(track.get('strArtist') or '').strip()
        if artist:
            return artist

    return ''


class TheAudioDBProvider(MusicProvider):
    id = 'tadb'
    label = 'TheAudioDB'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'composer', 'discnumber'})
    supports_album_search = True
    circuit_breaker = _TADB_CB

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            # 优先使用 searchalbum.php?s={singer}&a={album_name}
            d = _tadb_get('searchalbum.php', params={'s': singer, 'a': album_name})
            cands = (d or {}).get('album') or [] if isinstance(d, dict) else []
            if not cands and term:
                # 回退按关键词搜索
                d = _tadb_get('searchalbum.php', params={'s': term})
                cands = (d or {}).get('album') or [] if isinstance(d, dict) else []
            if not cands:
                return '', '', False

            a, artist_ok = choose_album_detail(
                cands,
                album_name,
                singer,
                lambda x: x.get('strAlbum'),
                lambda x: x.get('strArtist'),
            )
            if a and a.get('idAlbum'):
                aid = str(a['idAlbum'])
                cov = str(a.get('strAlbumThumb') or '').strip()
                cov = cov if cov.startswith('http') else ''
                date_str = normalize_release_date(str(a.get('intYearReleased') or ''))
                set_album_meta(
                    'tadb',
                    aid,
                    release_date=date_str,
                    cover_url=cov,
                    album_name=str(a.get('strAlbum') or ''),
                    albumartist=str(a.get('strArtist') or ''),
                    artist_name=str(a.get('strArtist') or ''),
                    genre=str(a.get('strGenre') or ''),
                )
                return aid, cov, artist_ok
            return '', '', False

        return cached_search('tadb', term, album_name, singer, _do)

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            d = _tadb_get('track.php', params={'m': aid})
            items = (d or {}).get('track') or [] if isinstance(d, dict) else []
            raw_tracks = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                ms = item.get('intDuration')
                dur = (float(ms) / 1000.0) if ms is not None and str(ms).isdigit() else 0.0
                raw_tracks.append({
                    'name': str(item.get('strTrack') or ''),
                    'track': int(item['intTrackNumber']) if str(item.get('intTrackNumber') or '').isdigit() else None,
                    'disc': int(item['intCD']) if str(item.get('intCD') or '').isdigit() else 1,
                    'duration': dur,
                    'id': str(item.get('idTrack') or '').strip() or None,
                    'composer': _extract_tadb_composer(item),
                    'genre': str(item.get('strGenre') or '').strip(),
                    'cover_url': str(item.get('strTrackThumb') or '').strip() if str(item.get('strTrackThumb') or '').startswith('http') else '',
                })
            return build_tracks(raw_tracks)

        return cached_album('tadb', album_id, _fetch)

    def search_artist(self, artist_name: str) -> str:
        name = (artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            d = _tadb_get('search.php', params={'s': name})
            artists = (d or {}).get('artists') or [] if isinstance(d, dict) else []
            if not artists:
                return ''
            sk = norm_core(name)
            for a in artists:
                if not isinstance(a, dict):
                    continue
                if sk and norm_core(a.get('strArtist') or '') == sk:
                    pic = _pick_tadb_artist_pic(a)
                    if pic:
                        return pic
            first = artists[0]
            if isinstance(first, dict):
                return _pick_tadb_artist_pic(first)
            return ''

        return cached_artist_url('tadb', name, _fetch)

    def search_songs(self, term: str, *, title: str = '', artist: str = '') -> list[dict]:
        clean_term = str(term or '').strip()
        clean_title = str(title or '').strip()
        clean_artist = str(artist or '').strip()
        if not clean_term and not (clean_title and clean_artist):
            return []

        cache_scope = f'{clean_title}@@@{clean_artist}'

        def _fetch() -> list[dict]:
            candidates: list[tuple[str, str]] = []
            if clean_title and clean_artist:
                candidates.append((clean_artist, clean_title))
                fa = first_artist(clean_artist)
                if fa and fa != clean_artist:
                    candidates.append((fa, clean_title))

            if clean_term:
                if ' - ' in clean_term:
                    p0, p1 = [x.strip() for x in clean_term.split(' - ', 1)]
                    if p0 and p1:
                        candidates.append((p0, p1))
                        candidates.append((p1, p0))
                elif ' ' in clean_term:
                    words = clean_term.split()
                    candidates.append((words[0], ' '.join(words[1:])))
                    candidates.append((words[-1], ' '.join(words[:-1])))
                    if len(words) >= 3:
                        candidates.append((' '.join(words[:2]), ' '.join(words[2:])))
                        candidates.append((' '.join(words[:-2]), ' '.join(words[-2:])))
                else:
                    candidates.append((clean_term, clean_term))

            seen_cands: set[tuple[str, str]] = set()
            uniq_cands: list[tuple[str, str]] = []
            for s, t in candidates:
                pair = (s.strip(), t.strip())
                if pair[0] and pair[1] and pair not in seen_cands:
                    seen_cands.add(pair)
                    uniq_cands.append(pair)

            seen_track_ids: set[str] = set()
            results: list[dict] = []
            for s, t in uniq_cands:
                d = _tadb_get('searchtrack.php', params={'s': s, 't': t})
                tracks = (d or {}).get('track') or [] if isinstance(d, dict) else []
                for tr in tracks:
                    if not isinstance(tr, dict):
                        continue
                    tid = str(tr.get('idTrack') or '').strip()
                    if tid and tid in seen_track_ids:
                        continue
                    if tid:
                        seen_track_ids.add(tid)
                    results.append(tr)
                if results:
                    break
            return results

        return cached_song_search('tadb_song', cache_scope, clean_term or f'{clean_title} {clean_artist}', _fetch)

    def search_song_hits(self, term: str, *, title: str = '', artist: str = '') -> list[dict]:
        hits: list[dict] = []
        for tr in self.search_songs(term, title=title, artist=artist):
            ms = tr.get('intDuration')
            dur = (float(ms) / 1000.0) if ms is not None and str(ms).isdigit() else 0.0
            cov = str(tr.get('strTrackThumb') or '').strip()
            cov = cov if cov.startswith('http') else ''
            comp = _extract_tadb_composer(tr)
            genre = str(tr.get('strGenre') or '').strip()
            lyrics = str(tr.get('strTrackLyrics') or '').strip()
            cd = int(tr['intCD']) if str(tr.get('intCD') or '').isdigit() else 1
            tn = int(tr['intTrackNumber']) if str(tr.get('intTrackNumber') or '').isdigit() else None
            hits.append({
                'title': str(tr.get('strTrack') or ''),
                'artist': str(tr.get('strArtist') or ''),
                'duration': dur,
                'song_id': str(tr.get('idTrack') or '').strip(),
                'album_id': str(tr.get('idAlbum') or '').strip(),
                'album_name': str(tr.get('strAlbum') or '').strip(),
                'release_date': '',
                'cover_url': cov,
                'genre': genre,
                'composer': comp,
                'disc': cd,
                'track': tn,
                'lyrics': lyrics,
                'lrc_url': '',
            })
        return hits

    def get_song_extra(self, song_id: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        sid = str(song_id or '').strip()
        if not sid:
            return {}

        def _fetch(_id: str) -> dict:
            d = _tadb_get('track.php', params={'h': _id})
            items = (d or {}).get('track') or [] if isinstance(d, dict) else []
            if items and isinstance(items[0], dict):
                tr = items[0]
                return {
                    'composer': _extract_tadb_composer(tr),
                    'genre': str(tr.get('strGenre') or '').strip(),
                    'lyrics': str(tr.get('strTrackLyrics') or '').strip(),
                }
            return {}

        return cached_song_meta('tadb_song_meta', sid, _fetch)
