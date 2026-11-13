"""LRCLIB 开源歌词数据源提供者（免 Key，优先提供精准同步 LRC 时间轴歌词）。"""

from __future__ import annotations

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    cached_song_meta,
    cached_song_search,
    http_get_json,
    parse_lyric_credits,
    to_seconds,
)

_LRCLIB_CB = ProviderCircuitBreaker(threshold=5)
_LRCLIB_HEADERS = {'User-Agent': 'MusicMetadata/1.0 (https://github.com/waves-dylan/MusicMetadata)'}


def _lrclib_get(url: str, params=None):
    if _LRCLIB_CB.is_open():
        return None
    d = http_get_json(url, params=params, headers=_LRCLIB_HEADERS, timeout=10)
    if d is None:
        _LRCLIB_CB.record_failure()
        return None
    _LRCLIB_CB.record_success()
    return d


class LrclibProvider(MusicProvider):
    id = 'lrclib'
    label = 'LRCLIB'
    capabilities = frozenset({'lyrics'})
    supports_album_search = False
    circuit_breaker = _LRCLIB_CB

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = _lrclib_get('https://lrclib.net/api/search', params={'q': term})
            if not isinstance(d, list):
                return []
            valid = [
                x for x in d
                if isinstance(x, dict) and (str(x.get('syncedLyrics') or '').strip() or str(x.get('plainLyrics') or '').strip())
            ]
            # 优先将带时间轴的同步歌词排在前面
            valid.sort(key=lambda x: 0 if str(x.get('syncedLyrics') or '').strip() else 1)
            return valid

        return cached_song_search('lrclib_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            sid = str(s.get('id') or '').strip()
            synced = str(s.get('syncedLyrics') or '').strip()
            plain = str(s.get('plainLyrics') or '').strip()
            lyrics = synced or plain
            if sid and lyrics:
                cached_song_meta(
                    'lrclib',
                    sid,
                    lambda _cid, lrc=lyrics: {
                        'lyrics': lrc,
                        'composer': parse_lyric_credits(lrc),
                    },
                )
            hits.append({
                'title': str(s.get('trackName') or s.get('name') or ''),
                'artist': str(s.get('artistName') or ''),
                'duration': to_seconds(s.get('duration')),
                'song_id': sid,
                'album_id': '',
                'album_name': str(s.get('albumName') or ''),
                'release_date': '',
                'cover_url': '',
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_extra(self, song_id: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        if not need_lyrics or not song_id:
            return {}

        def _load(sid: str) -> dict:
            d = _lrclib_get(f'https://lrclib.net/api/get/{sid}')
            if not isinstance(d, dict):
                return {}
            lyrics = str(d.get('syncedLyrics') or d.get('plainLyrics') or '').strip()
            return {'lyrics': lyrics, 'composer': parse_lyric_credits(lyrics) if lyrics else ''}

        return cached_song_meta('lrclib', song_id, _load)
