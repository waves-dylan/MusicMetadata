"""千千音乐（原百度音乐）数据源提供者（music.91q.com MD5 签名搜索 + 歌词/封面）。"""

from __future__ import annotations

import hashlib
import time

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    cached_song_search,
    extract_artist_names,
    http_get_json,
)

_QIANQIAN_APP_ID = '16073360'
_QIANQIAN_SECRET = '0b50b02fd0d73a9c4c8c3a781c30845f'
_QIANQIAN_HEADERS = {
    'Referer': 'https://music.91q.com/player',
    'Accept': '*/*',
    'From': 'web',
}


def sign_qianqian_params(params: dict[str, str], *, timestamp: str | None = None) -> dict[str, str]:
    signed = dict(params)
    signed['timestamp'] = timestamp or str(int(time.time()))
    raw = '&'.join(f'{k}={signed[k]}' for k in sorted(signed)) + _QIANQIAN_SECRET
    signed['sign'] = hashlib.md5(raw.encode('utf-8')).hexdigest()
    return signed


_BAIDU_CB = ProviderCircuitBreaker(threshold=5)


class BaiduProvider(MusicProvider):
    id = 'baidu'
    label = '千千音乐'
    capabilities = frozenset({'cover', 'title', 'artist', 'album', 'lyrics'})
    supports_album_search = False
    _lyric_url_headers = {'Referer': 'https://music.91q.com/player'}
    circuit_breaker = _BAIDU_CB

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            params = sign_qianqian_params({
                'word': term,
                'type': '1',
                'pageNo': '1',
                'pageSize': '10',
                'appid': _QIANQIAN_APP_ID,
            })
            ordered = {k: params[k] for k in sorted(params)}
            d = http_get_json('https://music.91q.com/v1/search', params=ordered, headers=_QIANQIAN_HEADERS)
            tracks = ((d or {}).get('data') or {}).get('typeTrack') or []
            return [x for x in tracks if isinstance(x, dict)]
        return cached_song_search('baidu_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            hits.append({
                'title': str(s.get('title') or ''),
                'artist': extract_artist_names(s.get('artist')),
                'duration': 0.0,
                'song_id': str(s.get('TSID') or '').strip(),
                'album_id': '',
                'album_name': str(s.get('albumTitle') or '').strip(),
                'release_date': '',
                'cover_url': str(s.get('pic') or '').strip(),
                'genre': '',
                'lrc_url': str(s.get('lyric') or '').strip(),
            })
        return hits
