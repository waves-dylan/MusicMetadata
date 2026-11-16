"""网易云音乐数据源提供者（支持官方接口与自定义 NeteaseCloudMusicApi 反代）。"""

from __future__ import annotations

import re

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    bump_cache_gen,
    cached_album,
    cached_artist_url,
    cached_search,
    cached_song_meta,
    cached_song_search,
    choose_album_detail,
    extract_artist_names,
    http_get_json,
    merge_lyrics,
    norm_core,
    normalize_release_date,
    parse_lyric_credits,
    set_album_date,
    set_album_meta,
    to_seconds,
)

_FULL_ALBUM_RE = re.compile(r'(专辑全长|full\s*album)', re.IGNORECASE)
_NETEASE_API_BASE = ''
_NETEASE_CB = ProviderCircuitBreaker(threshold=5)


def set_netease_api_base(api_base: str) -> str:
    global _NETEASE_API_BASE
    cleaned = str(api_base or '').strip().rstrip('/')
    if cleaned != _NETEASE_API_BASE:
        _NETEASE_API_BASE = cleaned
        bump_cache_gen()
    return _NETEASE_API_BASE


def get_netease_api_base() -> str:
    return _NETEASE_API_BASE


def build_netease_tracks(songs: list[dict]) -> list[dict]:
    raw = [s for s in (songs or []) if isinstance(s, dict)]
    out: list[dict] = []
    for idx, s in enumerate(raw):
        name = str(s.get('name') or '')
        dur = to_seconds(s.get('dt') or s.get('duration'))
        no = int(s.get('no') or 0)
        if _FULL_ALBUM_RE.search(name) and dur >= 1200.0:
            if no in (0, 1) or any(int(o.get('no') or 0) == 1 for o in raw[idx + 1:idx + 3]):
                continue
        cd = str(s.get('cd') or '1').strip()
        artists = extract_artist_names(s.get('ar') or s.get('artists'))
        out.append({'id': s.get('id'), 'name': name, 'duration': dur, 'disc': int(cd) if cd.isdigit() else 1, 'artist': artists})
    return build_tracks(out)


class NetEaseProvider(MusicProvider):
    id = 'netease'
    label = '网易云音乐'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'composer', 'discnumber', 'lyrics'})
    supports_album_search = True
    circuit_breaker = _NETEASE_CB

    @property
    def api_base(self) -> str:
        return _NETEASE_API_BASE

    def _headers(self) -> dict[str, str]:
        return {} if self.api_base else {'Referer': 'https://music.163.com/'}

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            if self.api_base:
                d = http_get_json(f'{self.api_base}/search', params={'keywords': term, 'type': 10, 'limit': 10})
            else:
                d = http_get_json(
                    'https://music.163.com/api/search/get',
                    params={'s': term, 'type': 10, 'limit': 10, 'offset': 0},
                    headers=self._headers(),
                )
            a, artist_ok = choose_album_detail(
                ((d or {}).get('result') or {}).get('albums') or [],
                album_name, singer, lambda x: x.get('name'), lambda x: (x.get('artist') or {}).get('name'),
            )
            if a and a.get('id'):
                aid = str(a['id'])
                set_album_date('netease', aid, normalize_release_date(a.get('publishTime'), is_ms=True))
                return aid, str(a.get('name') or album_name), artist_ok
            return '', '', False
        return cached_search('netease_alb', term, album_name, singer, _do)

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            urls = (
                (f'{self.api_base}/album?id={aid}',)
                if self.api_base
                else (f'https://music.163.com/api/v1/album/{aid}', f'https://music.163.com/api/album/{aid}')
            )
            for url in urls:
                d = http_get_json(url, headers=self._headers())
                if isinstance(d, dict):
                    alb_obj = d.get('album') if isinstance(d.get('album'), dict) else {}
                    aa = extract_artist_names(alb_obj.get('artists'))
                    if not aa and isinstance(alb_obj.get('artist'), dict):
                        aa = str(alb_obj['artist'].get('name') or '').strip()
                    set_album_meta(
                        'netease', aid,
                        release_date=normalize_release_date(alb_obj.get('publishTime') or d.get('publishTime'), is_ms=True),
                        album_name=str(alb_obj.get('name') or ''),
                        albumartist=aa,
                        cover_url=str(alb_obj.get('picUrl') or ''),
                    )
                    songs = d.get('songs') or (alb_obj.get('songs') or [])
                    if songs:
                        return build_netease_tracks(songs)
            return []
        return cached_album('netease', album_id, _fetch)

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            if self.api_base:
                d = http_get_json(f'{self.api_base}/search', params={'keywords': term, 'type': 1, 'limit': 15})
            else:
                d = http_get_json(
                    'https://music.163.com/api/cloudsearch/pc',
                    params={'s': term, 'type': 1, 'limit': 15, 'offset': 0},
                    headers=self._headers(),
                )
                if not d or not ((d.get('result') or {}).get('songs')):
                    # 备选回退至旧版接口
                    d = http_get_json(
                        'https://music.163.com/api/search/get',
                        params={'s': term, 'type': 1, 'limit': 10, 'offset': 0},
                        headers=self._headers(),
                    )
            return ((d or {}).get('result') or {}).get('songs') or []
        return cached_song_search('netease_song', self.api_base or 'default', term, _fetch)

    def search_artist_candidates(self, artist_name: str, *, track_hint: str = '') -> list[dict]:
        name = str(artist_name or '').strip()
        if not name:
            return []

        try:
            if self.api_base:
                d = http_get_json(f'{self.api_base}/search', params={'keywords': name, 'type': 100, 'limit': 10})
            else:
                d = http_get_json(
                    'https://music.163.com/api/search/get',
                    params={'s': name, 'type': 100, 'limit': 10, 'offset': 0},
                    headers=self._headers(),
                )
        except Exception:
            return []

        artists = ((d or {}).get('result') or {}).get('artists') or []
        if not isinstance(artists, list):
            return []

        results: list[dict] = []
        seen_pics: set[str] = set()
        for a in artists:
            if not isinstance(a, dict):
                continue
            pic = str(a.get('picUrl') or a.get('img1v1Url') or '').strip()
            if not pic or not pic.startswith('http') or 'default_avatar' in pic:
                continue
            pic = pic.replace('http://', 'https://')
            if pic in seen_pics:
                continue
            seen_pics.add(pic)
            aname = str(a.get('name') or '').strip() or name
            album_size = a.get('albumSize') or 0
            alias = [str(x).strip() for x in (a.get('alias') or []) if str(x).strip()]
            clues = f'专辑 {album_size} 张'
            if alias:
                clues += f' · 别名: {", ".join(alias[:2])}'
            trans = str(a.get('trans') or '').strip()
            if trans and trans not in alias:
                clues += f' ({trans})'
            results.append({
                'source': self.label,
                'platform': self.id,
                'artist_name': aname,
                'pic_url': pic,
                'aspect_type': 'square',
                'resolution': '1000×1000',
                'width': 1000,
                'height': 1000,
                'clues': clues,
                'raw_meta': a,
            })
        return results

    def search_artist(self, artist_name: str) -> str:
        name = str(artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            cands = self.search_artist_candidates(name)
            if not cands:
                return ''
            sk = norm_core(name)
            for c in cands:
                if sk and norm_core(c['artist_name']) == sk:
                    return c['pic_url']
            return cands[0]['pic_url']

        return cached_artist_url('netease', name, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            if not isinstance(s, dict):
                continue
            alb = s.get('al') or s.get('album') or {}
            pic_url = str(alb.get('picUrl') or '')
            if pic_url.startswith('http://'):
                pic_url = 'https://' + pic_url[7:]
            pub_time = s.get('publishTime') or alb.get('publishTime')
            hits.append({
                'title': str(s.get('name') or ''),
                'artist': extract_artist_names(s.get('ar') or s.get('artists'), sep=' '),
                'duration': to_seconds(s.get('dt') or s.get('duration')),
                'song_id': str(s.get('id') or ''),
                'album_id': str(alb['id']) if alb.get('id') else '',
                'album_name': str(alb.get('name') or ''),
                'release_date': normalize_release_date(pub_time, is_ms=True),
                'cover_url': pic_url,
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_detail(self, song_id: str) -> dict | None:
        sid = str(song_id or '').strip()
        if not sid:
            return None
        if self.api_base:
            d = http_get_json(f'{self.api_base}/song/detail', params={'ids': sid})
        else:
            d = http_get_json(
                'https://music.163.com/api/song/detail',
                params={'id': sid, 'ids': f'[{sid}]'},
                headers=self._headers(),
            )
        det = ((d or {}).get('songs') or [None])[0]
        return det if isinstance(det, dict) else None

    def get_song_extra(self, song_id: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        def _load(sid: str) -> dict:
            if self.api_base:
                d = http_get_json(f'{self.api_base}/lyric', params={'id': sid})
            else:
                d = http_get_json(
                    'https://music.163.com/api/song/lyric',
                    params={'id': sid, 'lv': 1, 'tv': 1},
                    headers=self._headers(),
                )
            orig_lrc = str(((d or {}).get('lrc') or {}).get('lyric') or '') if isinstance(d, dict) else ''
            trans_lrc = str(((d or {}).get('tlyric') or {}).get('lyric') or '') if isinstance(d, dict) else ''
            merged = merge_lyrics(orig_lrc, trans_lrc) if orig_lrc else ''
            comp = parse_lyric_credits(orig_lrc or merged)
            return {'composer': comp, 'lyrics': merged}

        return cached_song_meta('netease', song_id, _load)
