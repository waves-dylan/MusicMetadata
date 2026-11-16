"""Roon 音乐服务与媒体库集成：进程探测、安装目录定位与文件变更自动重载。"""

from __future__ import annotations

import os

from core.constants import PLAYER_TYPE_ROON
from core.models import PlayerEnv
from infra.integrations.base import BasePlayerAdapter, get_running_process_exes
from infra.scanner import touch_files_mtime


def get_running_roon_exes() -> list[str]:
    """查询当前运行的 Roon 或 RoonServer 进程路径列表。"""
    res = get_running_process_exes({'roon.exe', 'roonserver.exe', 'roonappliance.exe'})
    exes = []
    for paths in res.values():
        for p in paths:
            if p and p not in exes:
                exes.append(p)
    return exes


def detect_roon_env(custom_dir: str = '') -> PlayerEnv:
    """自动发现 Roon / RoonServer 安装路径与宿主环境。"""
    cands = [custom_dir] if custom_dir and os.path.isdir(custom_dir) else []

    for exe in get_running_roon_exes():
        d = os.path.dirname(exe)
        if d not in cands:
            cands.append(d)

    local_app_data = os.environ.get('LOCALAPPDATA', '')
    if local_app_data:
        cands.extend([
            os.path.join(local_app_data, 'RoonServer', 'Application'),
            os.path.join(local_app_data, 'Roon', 'Application'),
            os.path.join(local_app_data, 'RoonServer'),
            os.path.join(local_app_data, 'Roon'),
        ])

    prog_files = [
        os.environ.get('ProgramFiles', r'C:\Program Files'),
        os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'),
        r'D:\Roon',
        r'D:\RoonServer',
    ]
    for pf in prog_files:
        if pf:
            cands.append(os.path.join(pf, 'Roon'))
            cands.append(os.path.join(pf, 'RoonServer'))

    roon_exe, roon_dir = '', ''
    for c in cands:
        if not c or not os.path.isdir(c):
            continue
        for exe_name in ('Roon.exe', 'RoonServer.exe', 'RoonAppliance.exe'):
            p = os.path.join(c, exe_name)
            if os.path.isfile(p):
                roon_exe, roon_dir = p, c
                break
        if roon_exe:
            break

    # 确定配置与数据库目录
    config_dir = ''
    if local_app_data:
        for sub in ('RoonServer', 'Roon'):
            p = os.path.join(local_app_data, sub)
            if os.path.isdir(p):
                config_dir = p
                break
    if not config_dir and roon_dir:
        config_dir = roon_dir

    return PlayerEnv(
        player_type=PLAYER_TYPE_ROON,
        player_name='Roon',
        exe_path=roon_exe,
        app_dir=roon_dir,
        config_dir=config_dir,
        library_folders=[],
        unavailable_folders=[],
        reload_command='自动文件系统监听重载 (mtime)',
        extra_info={'is_server': 'roonserver' in roon_exe.lower()},
    )


def sync_files_to_roon(
    env: PlayerEnv,
    paths: list[str],
) -> tuple[int, bool]:
    """
    通过更新文件修改时间戳，触发 Roon Core 的后台文件系统监听自动重新载入与元数据分析。
    """
    valid_paths = [p for p in paths if p and os.path.isfile(p)]
    if not valid_paths:
        return 0, False

    touched = touch_files_mtime(valid_paths)
    return touched, touched > 0


class RoonAdapter(BasePlayerAdapter):
    """Roon 音乐服务与媒体库适配器。"""

    player_type: str = PLAYER_TYPE_ROON
    player_name: str = 'Roon'

    def detect_env(self, custom_dir: str = '') -> PlayerEnv:
        return detect_roon_env(custom_dir)

    def is_running(self) -> bool:
        return len(get_running_roon_exes()) > 0

    def get_library_folders(self, env: PlayerEnv) -> tuple[list[str], list[str]]:
        avail = [f for f in env.library_folders if os.path.isdir(f)]
        unavail = [f for f in env.library_folders if not os.path.isdir(f)]
        return avail, unavail

    def notify_reload(self, env: PlayerEnv, paths: list[str]) -> tuple[int, bool]:
        return sync_files_to_roon(env, paths)
