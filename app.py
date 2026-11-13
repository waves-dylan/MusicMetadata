"""MusicMetadata — 通用本地音乐库曲目元数据修复工具（主程序入口与统一调度）。"""

from __future__ import annotations

import ctypes
import os
import sys
import tkinter as tk
from tkinter import messagebox

from cli import build_arg_parser, run_cli
from core.models import FoobarEnv, LibraryTrack, TrackMatch
from ui.dialogs import (
    ConvertFormatDialog,
    DeleteMetadataDialog,
    FolderManagerDialog,
    SetMetadataDialog,
    SourceConfigDialog,
)
from art_repair import (
    analyze_folder_art_and_tags,
    pick_canonical_album_date,
)
from foobar_library import (
    detect_foobar_env,
    get_running_foobar_exes,
    scan_library,
    sync_files_to_foobar,
)
from runner import (
    FOLDER_COVER_LABELS,
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SetOptions,
    default_checked,
    execute_convert,
    execute_deletions,
    execute_repairs,
    execute_set_tags,
    parse_delete_fields,
)
from tag_io import find_ffmpeg
from track_lookup import (
    fetch_cover_bytes,
    resolve_track_metadata,
)
from ui.main_window import (
    MainWindow,
    _apply_window_icon,
    _format_cover_cell,
    _format_status_badge,
    _format_tag_sync_cell,
    _resource_path,
)
from user_config import get_default_music_folders

# 向后兼容导出（供外部脚本与自动化测试 patch app.<symbol> 使用）
__all__ = [
    'ConvertFormatDialog',
    'ConvertOptions',
    'DeleteMetadataDialog',
    'DeleteOptions',
    'FixOptions',
    'FoobarEnv',
    'FolderManagerDialog',
    'LibraryTrack',
    'MainWindow',
    'SetMetadataDialog',
    'SetOptions',
    'SourceConfigDialog',
    'TrackMatch',
    '_apply_window_icon',
    '_format_cover_cell',
    '_format_status_badge',
    '_format_tag_sync_cell',
    '_resource_path',
    'detect_foobar_env',
    'execute_convert',
    'execute_deletions',
    'execute_repairs',
    'execute_set_tags',
    'fetch_cover_bytes',
    'find_ffmpeg',
    'get_default_music_folders',
    'get_running_foobar_exes',
    'main',
    'messagebox',
    'resolve_track_metadata',
    'scan_library',
    'sync_files_to_foobar',
]


def main():
    args = build_arg_parser().parse_args()
    if args.auto or args.dry_run or args.sync_only or args.delete or args.set_tags or args.convert:
        sys.exit(run_cli(args))

    if os.name == 'nt':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('foobar.musicdl.MusicMetadata')
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
