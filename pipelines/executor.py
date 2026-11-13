"""流水线并发调度器、可取消线程池与时间戳同步辅助。"""

from __future__ import annotations

import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from core.models import FoobarEnv
from foobar_library import sync_files_to_foobar
from tag_io import find_ffmpeg
from track_lookup import fetch_artist_bytes, fetch_cover_bytes, resolve_track_metadata
from user_config import has_foobar_env

_ORIG_HOOKS = {
    'resolve_track_metadata': resolve_track_metadata,
    'fetch_cover_bytes': fetch_cover_bytes,
    'fetch_artist_bytes': fetch_artist_bytes,
    'sync_files_to_foobar': sync_files_to_foobar,
    'find_ffmpeg': find_ffmpeg,
}


def resolve_hook(name: str, current):
    """兼容 tests 直接 patch app.<symbol> 或模块本地符号的解析辅助。"""
    app_mod = sys.modules.get('app')
    if app_mod is not None and name in _ORIG_HOOKS:
        val = getattr(app_mod, name, current)
        if val is not _ORIG_HOOKS[name]:
            return val
    return current


def _run_pool_cancellable(items, submit_fn, on_result_fn, *, max_workers: int, is_cancel) -> bool:
    """并发执行任务，支持快速取消与即时逐项进度回调；返回是否被取消。"""
    if not items:
        return False
    workers = max(1, min(max_workers, len(items)))
    pool = ThreadPoolExecutor(max_workers=workers)
    cancelled = False
    it = iter(items)
    pending: dict = {}
    max_in_flight = workers * 3

    def _fill():
        while len(pending) < max_in_flight and not is_cancel():
            try:
                item = next(it)
            except StopIteration:
                break
            pending[pool.submit(submit_fn, item)] = item

    try:
        _fill()
        while pending:
            if is_cancel():
                cancelled = True
                break
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
                on_result_fn(pending.pop(fut), fut)
            if is_cancel():
                cancelled = True
                break
            _fill()
        if not cancelled and is_cancel():
            cancelled = True
    finally:
        for fut in pending:
            fut.cancel()
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)
    return cancelled


def _sync_foobar_targets(
    env: FoobarEnv,
    synced_paths: list[str],
    foobar_mtimes: dict[str, float] | None,
    emit_log,
    *,
    log_noun: str = '变更文件',
) -> tuple[int, bool]:
    """统一同步已更新文件时间戳并在必要时触发 foobar2000 重载。"""
    unique = list(dict.fromkeys(synced_paths))
    is_fb = has_foobar_env(env)
    emit_log(
        f'正在同步 {len(unique)} 个{log_noun}到 foobar2000...'
        if is_fb
        else f'正在更新 {len(unique)} 个{log_noun}的修改时间（便于播放器监听变更）...'
    )
    sync_fn = resolve_hook('sync_files_to_foobar', sync_files_to_foobar)
    touched, reloaded = sync_fn(env, unique, foobar_mtimes) if foobar_mtimes is not None else sync_fn(env, unique)
    if is_fb:
        emit_log(f'已更新时间戳 ({touched} 个)' + ('并触发运行中的 foobar2000 重载。' if reloaded else '；foobar 下次启动会自动读取。'))
    else:
        emit_log(f'已更新时间戳 ({touched} 个)；若播放器未自动刷新，请在播放器内重新扫描目录。')
    return touched, reloaded
