"""跨平台用户配置持久化、默认音乐目录探测与界面辅助工具（向下兼容门面）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

from core.config import (
    get_config_path,
    get_default_music_folders,
    has_foobar_env,
    load_user_config,
    normalize_folder_list,
    reveal_in_file_manager,
    save_user_config,
    ui_font,
)

__all__ = [
    'get_config_path',
    'get_default_music_folders',
    'has_foobar_env',
    'load_user_config',
    'normalize_folder_list',
    'reveal_in_file_manager',
    'save_user_config',
    'ui_font',
    'json',
    'os',
    'subprocess',
    'sys',
]
