"""网络与音源查询多级内存缓存管理器。"""

from __future__ import annotations

import threading

from infra.net.pool import _HTTP_POOL

CACHE_LOCK = threading.Lock()
CACHE_GEN = 0

ALBUM_CACHE: dict[tuple, list[dict]] = {}
ALBUM_META_CACHE: dict[tuple[str, str], dict] = {}
KUGOU_INFO_DONE: set[str] = set()
SONG_META_CACHE: dict[tuple[str, str], dict] = {}
SONG_SEARCH_CACHE: dict[tuple[str, str, str], list] = {}
COVER_CACHE: dict[str, bytes] = {}
ARTIST_PIC_CACHE: dict[tuple[str, str], str] = {}
ARTIST_BYTES_CACHE: dict[str, bytes] = {}
SEARCH_CACHE: dict[tuple, tuple[str, str, bool]] = {}
PKL_CACHE: dict[str, dict] = {}

INFLIGHT_ALBUM: dict[tuple, threading.Event] = {}
INFLIGHT_SEARCH: dict[tuple, threading.Event] = {}
INFLIGHT_SONG_SEARCH: dict[tuple, threading.Event] = {}
INFLIGHT_ARTIST: dict[tuple[str, str], threading.Event] = {}


def get_cache_gen() -> int:
    """获取当前全局缓存代次编号。"""
    with CACHE_LOCK:
        return CACHE_GEN


def bump_cache_gen() -> int:
    """递增全局缓存代次，并彻底清空所有内存缓存与 HTTP 连接池。"""
    global CACHE_GEN
    with CACHE_LOCK:
        CACHE_GEN += 1
        ALBUM_CACHE.clear()
        ALBUM_META_CACHE.clear()
        KUGOU_INFO_DONE.clear()
        SONG_META_CACHE.clear()
        SONG_SEARCH_CACHE.clear()
        COVER_CACHE.clear()
        ARTIST_PIC_CACHE.clear()
        ARTIST_BYTES_CACHE.clear()
        INFLIGHT_ARTIST.clear()
        SEARCH_CACHE.clear()
        PKL_CACHE.clear()
        _HTTP_POOL.clear()
        return CACHE_GEN


def cached_call(cache: dict, inflight: dict, key: tuple, fn):
    """线程安全缓存读写，并对同 key 并发请求去重（含负缓存）。"""
    while True:
        with CACHE_LOCK:
            if key in cache:
                return cache[key]
            ev = inflight.get(key)
            if ev is None:
                ev = threading.Event()
                inflight[key] = ev
                gen = CACHE_GEN
                break
        ev.wait()
    val_ready = False
    val = None
    try:
        val = fn()
        val_ready = True
        return val
    finally:
        with CACHE_LOCK:
            if val_ready and CACHE_GEN == gen:
                cache[key] = val
            inflight.pop(key, None)
            ev.set()
