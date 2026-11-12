"""酷狗音乐数据源提供者（支持专辑详情、无签名在线搜索与 Base64 歌词检索）。"""

from __future__ import annotations

import base64

from providers.base import (
    CACHE_LOCK,
    KUGOU_INFO_DONE,
    MusicProvider,
    build_tracks,
    cached_album,
    cached_song_meta,
    cached_song_search,
    get_album_meta,
    http_get_json,
    normalize_release_date,
    parse_lyric_credits,
    set_album_meta,
    to_seconds,
)


class KugouProvider(MusicProvider):
    id = 'kugou'
    label = '酷狗音乐'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'lyrics'})
    supports_album_search = False

    def get_album_info(self, album_id: str) -> dict:
        aid = str(album_id or '').strip()
        if not aid:
            return {}
        meta = get_album_meta('kugou', aid)
        with CACHE_LOCK:
            if aid in KUGOU_INFO_DONE or (meta.get('release_date') and meta.get('cover_url')):
                return meta
        d = http_get_json(
            'http://mobilecdnbj.kugou.com/api/v3/album/info',
            params={'albumid': aid},
            headers={'Referer': 'https://www.kugou.com/'},
        )
        info = (d or {}).get('data') if isinstance(d, dict) else {}
        with CACHE_LOCK:
            KUGOU_INFO_DONE.add(aid)
        if isinstance(info, dict):
            img = str(info.get('imgurl') or '').replace('{size}', '400')
            return set_album_meta(
                'kugou', aid,
                release_date=normalize_release_date(info.get('publishtime') or ''),
                album_name=str(info.get('albumname') or ''),
                albumartist=str(info.get('singername') or ''),
                cover_url=img,
            )
        return meta

    def get_album_date(self, album_id: str) -> str:
        return self.get_album_info(album_id).get('release_date', '')

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            items, page = [], 1
            while page <= 20:
                d = http_get_json(
                    'http://mobilecdnbj.kugou.com/api/v3/album/song',
                    params={'albumid': aid, 'page': page, 'pagesize': 100, 'version': '9108', 'plat': '0'},
                    headers={'Referer': 'https://www.kugou.com/'},
                )
                pl = (d or {}).get('data') or {}
                info = pl.get('info') or []
                for x in info:
                    if isinstance(x, dict):
                        fn = x.get('filename') or ''
                        name = fn.split(' - ', 1)[-1] if ' - ' in fn else (x.get('songname') or fn)
                        art = fn.split(' - ', 1)[0].strip() if ' - ' in fn else str(x.get('singername') or '')
                        items.append({'id': x.get('hash'), 'name': name, 'duration': to_seconds(x.get('duration')), 'artist': art})
                total = int(pl.get('total') or 0)
                page += 1
                if not info or len(info) < 100 or (total and len(items) >= total):
                    break
            return build_tracks(items)
        return cached_album('kugou', album_id, _fetch)

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = http_get_json(
                'http://mobilecdn.kugou.com/api/v3/search/song',
                params={'format': 'json', 'keyword': term, 'showtype': 1, 'page': 1, 'pagesize': 10},
                headers={'Accept': 'application/json, text/plain, */*'},
            )
            if not isinstance(d, dict) or d.get('status') not in (1, None):
                return []
            info = (d.get('data') or {}).get('info') or []
            return [x for x in info if isinstance(x, dict)]
        return cached_song_search('kugou_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            aid = str(s.get('album_id') or '').strip()
            hits.append({
                'title': str(s.get('songname') or ''),
                'artist': str(s.get('singername') or ''),
                'duration': to_seconds(s.get('timelen') or s.get('duration')),
                'song_id': str(s.get('hash') or ''),
                'album_id': '' if aid == '0' else aid,
                'album_name': str(s.get('album_name') or (s.get('albuminfo') or {}).get('name') or '').strip(),
                'release_date': '',
                'cover_url': str((s.get('trans_param') or {}).get('union_cover') or '').replace('{size}', '400'),
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_extra(self, song_hash: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        def _load(sh: str) -> dict:
            lyrics = ''
            dur_ms = int(duration * 1000) if duration > 0 else 99999
            sd = http_get_json(
                'http://lyrics.kugou.com/search',
                params={'ver': '1', 'man': 'yes', 'client': 'pc', 'keyword': title, 'duration': dur_ms, 'hash': sh},
            )
            cands = (sd or {}).get('candidates') if isinstance(sd, dict) else []
            if isinstance(cands, list) and cands:
                c0 = cands[0] if isinstance(cands[0], dict) else {}
                cid = str(c0.get('id') or '').strip()
                akey = str(c0.get('accesskey') or '').strip()
                if cid and akey:
                    dl = http_get_json(
                        'http://lyrics.kugou.com/download',
                        params={'ver': '1', 'client': 'pc', 'id': cid, 'accesskey': akey, 'fmt': 'lrc', 'charset': 'utf8'},
                    )
                    b64 = str((dl or {}).get('content') or '').strip() if isinstance(dl, dict) else ''
                    if b64:
                        try:
                            lyrics = base64.b64decode(b64).decode('utf-8', errors='replace').strip()
                        except Exception:
                            lyrics = ''
            return {'lyrics': lyrics, 'composer': parse_lyric_credits(lyrics) if lyrics else ''}

        return cached_song_meta('kugou', song_hash, _load)
