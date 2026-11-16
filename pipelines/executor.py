"""流水线并发调度器、可取消线程池与时间戳同步辅助。"""

from __future__ import annotations

import sys
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from core.config import is_player_active
from core.constants import PLAYER_TYPE_MUSICBEE, PLAYER_TYPE_ROON
from core.models import PlayerEnv
from infra.integrations.foobar import sync_files_to_foobar
from infra.integrations.musicbee import sync_files_to_musicbee
from infra.integrations.roon import sync_files_to_roon
from services.lookup import fetch_artist_bytes, fetch_cover_bytes, resolve_track_metadata
from tag_io import find_ffmpeg

_ORIG_HOOKS = {
    'resolve_track_metadata': resolve_track_metadata,
    'fetch_cover_bytes': fetch_cover_bytes,
    'fetch_artist_bytes': fetch_artist_bytes,
    'sync_files_to_foobar': sync_files_to_foobar,
    'sync_files_to_musicbee': sync_files_to_musicbee,
    'sync_files_to_roon': sync_files_to_roon,
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


def _sync_player_targets(
    env: PlayerEnv,
    synced_paths: list[str],
    foobar_mtimes: dict[str, float] | None,
    emit_log,
    *,
    log_noun: str = '变更文件',
) -> tuple[int, bool]:
    """统一同步已更新文件时间戳并在必要时触发播放器（foobar2000 / MusicBee / Roon）重载。"""
    unique = list(dict.fromkeys(synced_paths))
    p_type = getattr(env, 'player_type', None)
    if p_type == PLAYER_TYPE_MUSICBEE:
        emit_log(f'正在同步 {len(unique)} 个{log_noun}到 MusicBee（推进时间戳触发后台监控重载）...')
        sync_fn = resolve_hook('sync_files_to_musicbee', sync_files_to_musicbee)
        touched, reloaded = sync_fn(env, unique)
        emit_log(f'已更新时间戳 ({touched} 个)；MusicBee 监控服务已捕获文件变动。')
        return touched, reloaded

    if p_type == PLAYER_TYPE_ROON:
        emit_log(f'正在同步 {len(unique)} 个{log_noun}到 Roon（推进时间戳触发 Roon Core 重新分析）...')
        sync_fn = resolve_hook('sync_files_to_roon', sync_files_to_roon)
        touched, reloaded = sync_fn(env, unique)
        emit_log(f'已更新时间戳 ({touched} 个)；Roon Core 监控服务已捕获文件变动。')
        return touched, reloaded

    is_fb = (p_type == 'foobar') or is_player_active(env)
    emit_log(
        f'正在同步 {len(unique)} 个{log_noun}到 foobar2000...'
        if is_fb
        else f'正在更新 {len(unique)} 个{log_noun}的修改时间（便于播放器监听变更）...'
    )
    sync_fn = resolve_hook('sync_files_to_foobar', sync_files_to_foobar)
    touched, reloaded = sync_fn(env, unique, foobar_mtimes) if foobar_mtimes is not None else sync_fn(env, unique)
    if is_fb:
        if reloaded:
            emit_log(f'已同步 {len(unique)} 个{log_noun}到 foobar2000，并成功触发播放器重载元数据缓存。')
        else:
            emit_log(f'foobar2000 未在运行，未能触发缓存重载；请打开 foobar2000 后点击「仅刷新 foobar 缓存」。')
    else:
        emit_log(f'已更新时间戳 ({touched} 个)；若播放器未自动刷新，请在播放器内重新扫描目录。')
    return touched, reloaded

