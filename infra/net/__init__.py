"""网络基础设施：HTTP 连接池、熔断器与多级请求缓存。"""

from __future__ import annotations

from infra.net.circuit_breaker import (
    ProviderCircuitBreaker,
    _CIRCUIT_BREAKERS,
    reset_all_circuit_breakers,
)
from infra.net.pool import (
    HTTPConnectionPool,
    USER_AGENT,
    _HTTP_POOL,
    _is_urlopen_mocked,
    http_get_bytes,
    http_get_json,
    http_get_text,
    http_post_form,
    http_post_json,
)
from infra.net.cache import (
    ALBUM_CACHE,
    ALBUM_META_CACHE,
    ARTIST_BYTES_CACHE,
    ARTIST_PIC_CACHE,
    CACHE_GEN,
    CACHE_LOCK,
    COVER_CACHE,
    INFLIGHT_ALBUM,
    INFLIGHT_ARTIST,
    INFLIGHT_SEARCH,
    INFLIGHT_SONG_SEARCH,
    KUGOU_INFO_DONE,
    PKL_CACHE,
    SEARCH_CACHE,
    SONG_META_CACHE,
    SONG_SEARCH_CACHE,
    bump_cache_gen,
    cached_call,
    get_cache_gen,
)
