"""在线音乐元数据检索与多数据源聚合服务包。"""

from __future__ import annotations

from track_lookup import (
    TrackMatch,
    clear_caches,
    fetch_artist_bytes,
    fetch_cover_bytes,
    get_fanart_keys,
    get_netease_api_base,
    get_spotify_credentials,
    get_tadb_api_key,
    parse_source_list,
    resolve_artist_pic_url,
    resolve_release_date,
    resolve_track_metadata,
    resolve_track_number,
    set_fanart_keys,
    set_netease_api_base,
    set_spotify_credentials,
    set_tadb_api_key,
)

__all__ = [
    'TrackMatch',
    'clear_caches',
    'fetch_artist_bytes',
    'fetch_cover_bytes',
    'get_fanart_keys',
    'get_netease_api_base',
    'get_spotify_credentials',
    'get_tadb_api_key',
    'parse_source_list',
    'resolve_artist_pic_url',
    'resolve_release_date',
    'resolve_track_metadata',
    'resolve_track_number',
    'set_fanart_keys',
    'set_netease_api_base',
    'set_spotify_credentials',
    'set_tadb_api_key',
]
