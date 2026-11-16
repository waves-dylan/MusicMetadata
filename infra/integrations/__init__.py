"""外部音乐播放器与媒体库宿主集成模块（foobar2000、MusicBee、Roon 与通用本地模式）。"""

from __future__ import annotations

import os

from core.constants import PLAYER_TYPE_FOOBAR, PLAYER_TYPE_GENERIC, PLAYER_TYPE_MUSICBEE, PLAYER_TYPE_ROON
from core.models import PlayerEnv
from infra.integrations.base import (
    BasePlayerAdapter,
    get_running_process_exes,
    sanitize_filename,
)
from infra.integrations.foobar import (
    FoobarAdapter,
    detect_foobar_env,
    find_foobar_cache_diffs,
    get_running_foobar_exes,
    load_metadb_index,
    parse_library_folders,
    scan_library,
    sync_files_to_foobar,
)
from infra.integrations.generic import GenericAdapter
from infra.integrations.musicbee import (
    MusicBeeAdapter,
    detect_musicbee_env,
    get_running_musicbee_exes,
    parse_musicbee_settings,
    sync_artist_pictures_to_musicbee,
    sync_files_to_musicbee,
)
from infra.integrations.roon import (
    RoonAdapter,
    detect_roon_env,
    get_running_roon_exes,
    sync_files_to_roon,
)


def get_player_adapter(player_type: str = PLAYER_TYPE_GENERIC) -> BasePlayerAdapter:
    """根据类型标识返回对应的播放器适配器实例。"""
    p = (player_type or '').strip().lower()
    if p in ('foobar', 'foobar2000', PLAYER_TYPE_FOOBAR):
        return FoobarAdapter()
    if p in ('musicbee', 'mb', PLAYER_TYPE_MUSICBEE):
        return MusicBeeAdapter()
    if p in ('roon', 'roonserver', PLAYER_TYPE_ROON):
        return RoonAdapter()
    return GenericAdapter()


def detect_active_player(
    preferred: str | None = None,
    custom_fb_dir: str = '',
    custom_mb_dir: str = '',
    custom_roon_dir: str = '',
) -> tuple[PlayerEnv, BasePlayerAdapter]:
    """
    智能探测并返回当前活跃的播放器环境与适配器。

    探测策略：
    1. 指定优先项时（foobar / musicbee / roon），直接探测该播放器。
    2. 自动模式下，优先探测当前正在运行的播放器进程。
    3. 若均未运行，探测本地已安装且配置了媒体库目录的环境。
    4. 均未安装时，回退到通用媒体库适配器。
    """
    pref = (preferred or '').strip().lower()

    if pref in ('foobar', 'foobar2000', PLAYER_TYPE_FOOBAR):
        adapter = FoobarAdapter()
        return adapter.detect_env(custom_fb_dir), adapter

    if pref in ('musicbee', 'mb', PLAYER_TYPE_MUSICBEE):
        adapter = MusicBeeAdapter()
        return adapter.detect_env(custom_mb_dir), adapter

    if pref in ('roon', 'roonserver', PLAYER_TYPE_ROON):
        adapter = RoonAdapter()
        return adapter.detect_env(custom_roon_dir), adapter

    # 自动探测运行中进程
    fb_running = len(get_running_foobar_exes()) > 0
    mb_running = len(get_running_musicbee_exes()) > 0
    roon_running = len(get_running_roon_exes()) > 0

    running_candidates = []
    if fb_running:
        running_candidates.append(('foobar', FoobarAdapter(), custom_fb_dir))
    if mb_running:
        running_candidates.append(('musicbee', MusicBeeAdapter(), custom_mb_dir))
    if roon_running:
        running_candidates.append(('roon', RoonAdapter(), custom_roon_dir))

    if len(running_candidates) == 1:
        _, adapter, cdir = running_candidates[0]
        return adapter.detect_env(cdir), adapter

    # 若未在运行或多个都在运行，探测安装与配置环境
    fb_adapter = FoobarAdapter()
    fb_env = fb_adapter.detect_env(custom_fb_dir)
    has_fb = bool(fb_env.exe_path or fb_env.config_dir or fb_env.library_folders)

    mb_adapter = MusicBeeAdapter()
    mb_env = mb_adapter.detect_env(custom_mb_dir)
    has_mb = bool(mb_env.exe_path or mb_env.config_dir or mb_env.library_folders)

    roon_adapter = RoonAdapter()
    roon_env = roon_adapter.detect_env(custom_roon_dir)
    has_roon = bool(roon_env.exe_path or roon_env.config_dir or roon_env.library_folders)

    installed = []
    if has_fb:
        installed.append((fb_env, fb_adapter))
    if has_mb:
        installed.append((mb_env, mb_adapter))
    if has_roon:
        installed.append((roon_env, roon_adapter))

    if len(installed) == 1:
        return installed[0]

    if has_fb and has_mb:
        if mb_env.library_folders and not fb_env.library_folders:
            return mb_env, mb_adapter
        return fb_env, fb_adapter

    if has_roon and not has_fb and not has_mb:
        return roon_env, roon_adapter

    if installed:
        return installed[0]

    # 均不存在，回退通用适配器
    generic_adapter = GenericAdapter()
    return generic_adapter.detect_env(), generic_adapter


__all__ = [
    'BasePlayerAdapter',
    'FoobarAdapter',
    'GenericAdapter',
    'MusicBeeAdapter',
    'RoonAdapter',
    'detect_active_player',
    'detect_foobar_env',
    'detect_musicbee_env',
    'detect_roon_env',
    'find_foobar_cache_diffs',
    'get_player_adapter',
    'get_running_foobar_exes',
    'get_running_musicbee_exes',
    'get_running_process_exes',
    'get_running_roon_exes',
    'load_metadb_index',
    'parse_library_folders',
    'parse_musicbee_settings',
    'sanitize_filename',
    'scan_library',
    'sync_artist_pictures_to_musicbee',
    'sync_files_to_foobar',
    'sync_files_to_musicbee',
    'sync_files_to_roon',
]
