"""跨平台用户配置持久化、默认音乐目录探测与界面辅助工具。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Iterable


def get_config_path() -> str:
    """返回用户配置文件路径（支持 MUSICMETADATA_CONFIG 环境变量覆盖）。"""
    override = (os.environ.get('MUSICMETADATA_CONFIG') or '').strip()
    if override:
        return os.path.abspath(override)
    if os.name == 'nt':
        base = os.environ.get('APPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(os.path.expanduser('~'), '.config')
    return os.path.join(base, 'MusicMetadata', 'config.json')


def normalize_folder_list(folders: Iterable[str] | None, *, existing_only: bool = False) -> list[str]:
    """标准化并去重音乐目录列表，保持原有顺序。"""
    out: list[str] = []
    seen: set[str] = set()
    for raw in folders or ():
        s = str(raw or '').strip()
        if not s:
            continue
        abs_p = os.path.abspath(s)
        if existing_only and not os.path.isdir(abs_p):
            continue
        norm = os.path.normcase(abs_p)
        if norm not in seen:
            seen.add(norm)
            out.append(abs_p)
    return out


def get_default_music_folders() -> list[str]:
    """探测系统默认音乐目录（如 ~/Music），不存在则返回空列表。"""
    candidates = [os.path.expanduser(os.path.join('~', 'Music'))]
    if os.name == 'nt':
        userprofile = os.environ.get('USERPROFILE') or ''
        if userprofile:
            candidates.append(os.path.join(userprofile, 'Music'))
    return normalize_folder_list(candidates, existing_only=True)


def load_user_config(path: str | None = None) -> dict:
    """读取用户 JSON 配置，缺失或损坏时返回空字典。"""
    cfg_path = path or get_config_path()
    try:
        with open(cfg_path, 'r', encoding='utf-8') as fp:
            data = json.load(fp)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def save_user_config(data: dict, path: str | None = None) -> bool:
    """保存用户 JSON 配置，自动创建父目录。"""
    cfg_path = path or get_config_path()
    try:
        parent = os.path.dirname(cfg_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(cfg_path, 'w', encoding='utf-8') as fp:
            json.dump(data, fp, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def has_foobar_env(env) -> bool:
    """判断当前环境是否检测到 foobar2000 可执行文件或媒体库配置。"""
    if env is None:
        return False
    return bool(getattr(env, 'fb_exe', '') or getattr(env, 'library_folders', None))


def ui_font(size: int = 9, bold: bool = False, mono: bool = False) -> tuple:
    """返回适配当前操作系统的界面字体元组（支持常规与等宽回落）。"""
    if mono:
        family = 'Consolas' if os.name == 'nt' else 'TkFixedFont'
    else:
        family = 'Microsoft YaHei UI' if os.name == 'nt' else 'TkDefaultFont'
    return (family, size, 'bold') if bold else (family, size)


def reveal_in_file_manager(path: str) -> None:
    """在系统文件管理器中定位并显示目标文件或目录。"""
    if not path or not os.path.exists(path):
        return
    abs_p = os.path.abspath(path)
    if os.name == 'nt':
        subprocess.Popen(['explorer', '/select,', os.path.normpath(abs_p)])
    elif sys.platform == 'darwin':
        subprocess.Popen(['open', '-R', abs_p])
    else:
        target_dir = abs_p if os.path.isdir(abs_p) else os.path.dirname(abs_p)
        subprocess.Popen(['xdg-open', target_dir])
