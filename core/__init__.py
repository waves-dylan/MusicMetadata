"""MusicMetadata 核心包：包含数据模型、系统常量与配置管理。"""

from __future__ import annotations

from core.constants import *
from core.models import (
    FoobarEnv,
    FolderArtPlan,
    LibraryTrack,
    TrackArtTagPlan,
    TrackMatch,
)
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
