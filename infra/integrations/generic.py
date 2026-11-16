"""通用本地媒体库适配器（未检测到 foobar 或 MusicBee 时的标准回退模式）。"""

from __future__ import annotations

import os

from core.constants import PLAYER_TYPE_GENERIC
from core.models import PlayerEnv
from infra.integrations.base import BasePlayerAdapter
from infra.scanner import touch_files_mtime


class GenericAdapter(BasePlayerAdapter):
    """通用本地文件媒体库适配器。"""

    player_type: str = PLAYER_TYPE_GENERIC
    player_name: str = '通用本地媒体库'

    def detect_env(self, custom_dir: str = '') -> PlayerEnv:
        music_folders = []
        if custom_dir and os.path.isdir(custom_dir):
            music_folders.append(os.path.abspath(custom_dir))
        else:
            default_music = os.path.expanduser('~/Music')
            if os.path.isdir(default_music):
                music_folders.append(os.path.abspath(default_music))

        return PlayerEnv(
            player_type=PLAYER_TYPE_GENERIC,
            player_name='通用本地媒体库',
            library_folders=music_folders,
            reload_command='更新文件修改时间',
        )

    def is_running(self) -> bool:
        return False

    def get_library_folders(self, env: PlayerEnv) -> tuple[list[str], list[str]]:
        avail = [f for f in env.library_folders if os.path.isdir(f)]
        unavail = [f for f in env.library_folders if not os.path.isdir(f)]
        return avail, unavail

    def notify_reload(self, env: PlayerEnv, paths: list[str]) -> tuple[int, bool]:
        valid_paths = [p for p in paths if p and os.path.isfile(p)]
        if not valid_paths:
            return 0, False
        touched = touch_files_mtime(valid_paths)
        return touched, touched > 0
