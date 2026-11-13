"""咪咕音乐数据源提供者（Android APP 搜索接口 + 歌词/封面下载）。"""

from __future__ import annotations

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    cached_song_search,
    extract_artist_names,
    http_get_json,
)

_MIGU_UA = (
    'Mozilla/5.0 (Linux; Android 10; MI 9 Build/QKQ1.190825.002; wv) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/83.0.4103.106 Mobile Safari/537.36'
)
_MIGU_HEADERS = {
    'User-Agent': _MIGU_UA,
    'Referer': 'http://music.migu.cn/',
    'Accept': 'application/json, text/plain, */*',
}
_MIGU_CB = ProviderCircuitBreaker(threshold=5)


class MiGuProvider(MusicProvider):
    id = 'migu'
    label = '咪咕音乐'
    capabilities = frozenset({'cover', 'title', 'artist', 'album', 'lyrics'})
    supports_album_search = False
    _lyric_url_headers = {'User-Agent': _MIGU_UA, 'Referer': 'http://music.migu.cn/'}
    circuit_breaker = _MIGU_CB

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = http_get_json(
                'http://pd.musicapp.migu.cn/MIGUM2.0/v1.0/content/search_all.do',
                params={
                    'ua': 'Android_migu',
                    'version': '5.0.1',
                    'text': term,
                    'pageNo': 1,
                    'pageSize': 10,
                    'searchSwitch': '{"song":1,"album":0,"singer":0,"tagSong":0,"mvSong":0,"songlist":0,"bestShow":1}',
                },
                headers=_MIGU_HEADERS,
            )
            res = ((d or {}).get('songResultData') or {}).get('result') or []
            return [x for x in res if isinstance(x, dict)]
        return cached_song_search('migu_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            albs = s.get('albums') or []
            alb0 = albs[0] if albs and isinstance(albs[0], dict) else {}
            hits.append({
                'title': str(s.get('name') or ''),
                'artist': extract_artist_names(s.get('singers')),
                'duration': 0.0,
                'song_id': str(s.get('contentId') or s.get('id') or ''),
                'album_id': '',
                'album_name': str(alb0.get('name') or '').strip(),
                'release_date': '',
                'cover_url': self.normalize_cover_url(s),
                'genre': '',
                'lrc_url': str(s.get('lyricUrl') or s.get('trcUrl') or '').strip(),
            })
        return hits

    @staticmethod
    def normalize_cover_url(song: dict) -> str:
        imgs = song.get('imgItems') or []
        if not isinstance(imgs, list) or not imgs:
            return ''
        url = str((imgs[-1] if isinstance(imgs[-1], dict) else {}).get('img') or (imgs[0] if isinstance(imgs[0], dict) else {}).get('img') or '').strip()
        if url.startswith('//'):
            return 'https:' + url
        return url
