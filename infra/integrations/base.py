"""播放器适配器抽象基类与通用系统/进程工具。"""

from __future__ import annotations

import ctypes
import os
import re
from abc import ABC, abstractmethod
from ctypes import wintypes

from core.constants import PLAYER_TYPE_GENERIC
from core.models import PlayerEnv
from infra.scanner import touch_files_mtime


def get_running_process_exes(target_names: set[str] | list[str] | str) -> dict[str, list[str]]:
    """根据进程名查询当前正在运行的进程完整路径字典 {exe_name_lower: [full_paths]}。"""
    if isinstance(target_names, str):
        target_set = {target_names.strip().lower()}
    else:
        target_set = {n.strip().lower() for n in target_names if n.strip()}

    result: dict[str, list[str]] = {n: [] for n in target_set}
    if os.name != 'nt' or not target_set:
        return result

    try:
        k32 = ctypes.WinDLL('kernel32', use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ('dwSize', wintypes.DWORD), ('cntUsage', wintypes.DWORD), ('th32ProcessID', wintypes.DWORD),
                ('th32DefaultHeapID', ctypes.c_size_t), ('th32ModuleID', wintypes.DWORD), ('cntThreads', wintypes.DWORD),
                ('th32ParentProcessID', wintypes.DWORD), ('pcPriClassBase', ctypes.c_long), ('dwFlags', wintypes.DWORD),
                ('szExeFile', wintypes.WCHAR * 260),
            ]

        snap = k32.CreateToolhelp32Snapshot(2, 0)
        if snap == wintypes.HANDLE(-1).value:
            return result
        try:
            ent = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
            ok = k32.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                name_lower = ent.szExeFile.lower()
                if name_lower in target_set:
                    hp = k32.OpenProcess(0x1000, False, ent.th32ProcessID)
                    if hp:
                        try:
                            buf = ctypes.create_unicode_buffer(1024)
                            sz = wintypes.DWORD(1024)
                            if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)):
                                full_p = buf.value
                                if full_p and full_p not in result[name_lower]:
                                    result[name_lower].append(full_p)
                        finally:
                            k32.CloseHandle(hp)
                ok = k32.Process32NextW(snap, ctypes.byref(ent))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return result


def sanitize_filename(name: str, replacement: str = '_') -> str:
    """清理 Windows 文件名中的非法字符（\\ / : * ? " < > |）并去除收尾空白/点。"""
    cleaned = re.sub(r'[\\/:*?"<>|]+', replacement, name)
    cleaned = cleaned.strip('. \t\r\n')
    return cleaned or 'unknown'


class BasePlayerAdapter(ABC):
    """音乐播放器适配器抽象基类。"""

    player_type: str = PLAYER_TYPE_GENERIC
    player_name: str = '通用本地媒体库'

    @abstractmethod
    def detect_env(self, custom_dir: str = '') -> PlayerEnv:
        """自动探测播放器安装、运行状态与配置环境。"""
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """检测播放器进程是否正在运行。"""
        pass

    @abstractmethod
    def get_library_folders(self, env: PlayerEnv) -> tuple[list[str], list[str]]:
        """获取播放器所监控或管理的媒体库目录，返回 (可访问目录列表, 不可访问目录列表)。"""
        pass

    @abstractmethod
    def notify_reload(self, env: PlayerEnv, paths: list[str]) -> tuple[int, bool]:
        """通知播放器刷新指定音轨的元数据与封面缓存。"""
        pass

    def sync_artist_pictures(self, env: PlayerEnv, artist_names: list[str] | str, jpeg_bytes: bytes) -> list[str]:
        """同步写入播放器专属的中央艺术家写真图库，返回写入成功的文件路径列表。默认不操作。"""
        return []
