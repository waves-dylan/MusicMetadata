"""TheAudioDB 数据源提供者（支持专辑搜索、曲目信息、发行年份、封面与高清艺人写真）。"""

from __future__ import annotations

import os
import threading

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    cached_album,
    cached_artist_url,
    cached_search,
    choose_album_detail,
    http_get_json,
    norm_core,
    normalize_release_date,
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


class TheAudioDBProvider(MusicProvider):
    id = 'tadb'
    label = 'TheAudioDB'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre'})
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
                    'duration': dur,
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

    def search_song_hits(self, term: str) -> list[dict]:
        # TheAudioDB 主要作为专辑、封面与艺人写真源，单曲搜索返回空或基于 searchalbum
        return []
