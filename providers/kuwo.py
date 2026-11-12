"""酷我音乐专辑数据源提供者（用于本地 .pkl 缓存关联的专辑曲目与发行日查询）。"""

from __future__ import annotations

import ast

from providers.base import (
    MusicProvider,
    build_tracks,
    cached_album,
    http_get_text,
    normalize_release_date,
    set_album_meta,
    to_seconds,
)


class KuwoProvider(MusicProvider):
    id = 'kuwo'
    label = '酷我音乐'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist'})
    supports_album_search = False

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            txt = http_get_text(
                'http://search.kuwo.cn/r.s',
                params={'stype': 'albuminfo', 'albumid': aid, 'vipver': '1', 'encoding': 'utf8', 'rformat': 'json'},
            )
            if not txt:
                return []
            try:
                data = ast.literal_eval(txt.strip())
                if isinstance(data, dict):
                    set_album_meta(
                        'kuwo', aid,
                        release_date=normalize_release_date(data.get('pub') or data.get('releaseDate')),
                        album_name=str(data.get('name') or ''),
                        albumartist=str(data.get('artist') or ''),
                        cover_url=str(data.get('hts_img') or data.get('img') or ''),
                    )
                items = (data.get('musiclist') or []) if isinstance(data, dict) else []
                return build_tracks([
                    {'id': x.get('musicrid'), 'name': x.get('songname'), 'duration': to_seconds(x.get('duration')), 'artist': str(x.get('artist') or '')}
                    for x in items if isinstance(x, dict)
                ])
            except Exception:
                return []
        return cached_album('kuwo', album_id, _fetch)
