"""Spotify 嵌入式专辑数据源提供者（用于本地 .pkl 缓存关联的专辑曲目与发行日查询）。"""

from __future__ import annotations

import json
import re

from providers.base import (
    MusicProvider,
    build_tracks,
    cached_album,
    http_get_text,
    normalize_release_date,
    set_album_meta,
    to_seconds,
)


class SpotifyProvider(MusicProvider):
    id = 'spotify'
    label = 'Spotify'
    capabilities = frozenset({'track', 'date', 'title', 'album', 'albumartist'})
    supports_album_search = False

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            txt = http_get_text(f'https://open.spotify.com/embed/album/{aid}')
            m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt or '', re.S)
            if not m:
                return []
            try:
                ent = json.loads(m.group(1))['props']['pageProps']['state']['data']['entity']
                set_album_meta(
                    'spotify', aid,
                    release_date=normalize_release_date(ent.get('releaseDate') or ent.get('date')),
                    album_name=str(ent.get('title') or ent.get('name') or ''),
                    albumartist=str(ent.get('subtitle') or ''),
                )
                return build_tracks([
                    {'id': (t.get('uri') or '').rsplit(':', 1)[-1], 'name': t.get('name') or t.get('title') or '', 'duration': to_seconds((t.get('duration') or {}).get('totalMilliseconds'))}
                    for t in (ent.get('trackList') or [])
                ])
            except Exception:
                return []
        return cached_album('spotify', album_id, _fetch)
