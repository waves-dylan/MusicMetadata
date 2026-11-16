"""foobar2000 媒体库集成：进程发现、metadb.sqlite 只读索引与命令行刷新。"""

from __future__ import annotations

import glob
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile

from core.constants import (
    CORE_TEXT_FIELDS,
    EXTRA_TEXT_FIELDS,
    FIELD_LABELS,
    PLAYER_TYPE_FOOBAR,
    STATUS_ERROR,
    STATUS_MISSING_DISK,
    STATUS_OK,
)
from core.models import LibraryTrack, PlayerEnv
from infra.integrations.base import BasePlayerAdapter, get_running_process_exes
from infra.scanner import (
    _append_note,
    _is_full_album_file,
    _is_temp_duplicate,
    _norm_cmp,
    is_under_folders,
    reconcile_album_consistency,
    scan_library as _generic_scan_library,
    touch_files_mtime,
    track_has_pending_work,
)


def get_running_foobar_exes() -> list[str]:
    """查询当前运行的 foobar2000.exe 路径列表。"""
    res = get_running_process_exes('foobar2000.exe')
    return res.get('foobar2000.exe', [])


def _detect_reload_command(fb_exe: str) -> str:
    try:
        with open(fb_exe, 'rb') as fp:
            if '从文件重新载入信息'.encode('utf-8') in fp.read():
                return '标签/从文件重新载入信息'
    except OSError:
        pass
    return 'Tagging/Reload info from file(s)'


def _resolve_fb_uri(uri: str, fb_dir: str) -> str:
    if '+' in uri and uri.split('+', 1)[0].isdigit():
        uri = uri.split('+', 1)[1]
    if uri.startswith('file://'):
        return os.path.normpath(uri[7:])
    if uri.startswith('file-relative://'):
        return os.path.normpath(os.path.join(fb_dir, uri[16:]))
    return uri


def _open_sqlite_ro(path: str) -> sqlite3.Connection:
    return sqlite3.connect('file:' + path.replace('\\', '/') + '?mode=ro', uri=True, timeout=2.0)


def parse_library_folders(profile_dir: str, fb_dir: str) -> tuple[list[str], list[str]]:
    """解析 library-v2.0/folders 或 content.sqlite，返回 (可访问目录, 不可访问目录)。"""
    folders, unavailable, seen = [], [], set()

    def add_folder(raw: str) -> None:
        abs_p = os.path.abspath(_resolve_fb_uri(raw.strip(), fb_dir))
        norm = os.path.normcase(abs_p)
        if norm not in seen:
            seen.add(norm)
            (folders if os.path.isdir(abs_p) else unavailable).append(abs_p)

    folders_file = os.path.join(profile_dir, 'library-v2.0', 'folders')
    if os.path.isfile(folders_file):
        try:
            with open(folders_file, 'rb') as fp:
                data = fp.read()
            if len(data) >= 4:
                count, pos = struct.unpack_from('<I', data, 0)[0], 4
                for _ in range(count):
                    if pos + 4 > len(data):
                        break
                    slen = struct.unpack_from('<I', data, pos)[0]
                    pos += 4
                    if pos + slen > len(data):
                        break
                    add_folder(data[pos:pos + slen].decode('utf-8', errors='replace'))
                    pos += slen + (4 if pos + slen + 4 <= len(data) else 0)
        except Exception:
            pass

    if not folders and not unavailable:
        for db_path in glob.glob(os.path.join(profile_dir, 'library-v2.0', '*', 'content.sqlite')):
            try:
                conn = _open_sqlite_ro(db_path)
                try:
                    row = conn.execute("SELECT value FROM config WHERE key='rootPath'").fetchone()
                    if row and row[0]:
                        add_folder(row[0])
                finally:
                    conn.close()
            except Exception:
                pass
    return folders, unavailable


def detect_foobar_env(custom_fb_dir: str = '') -> PlayerEnv:
    """自动发现 foobar2000 路径、profile 与媒体库目录。"""
    cands = ([custom_fb_dir] if custom_fb_dir and os.path.isdir(custom_fb_dir) else []) + [
        os.path.dirname(e) for e in get_running_foobar_exes()
    ] + [r'D:\Program Files\foobar2000', r'C:\Program Files\foobar2000', r'C:\Program Files (x86)\foobar2000', r'D:\foobar2000']

    fb_exe, fb_dir = '', ''
    for c in cands:
        exe = os.path.join(c, 'foobar2000.exe')
        if os.path.isfile(exe):
            fb_exe, fb_dir = exe, c
            break

    profile_dir = ''
    if fb_dir and (os.path.isfile(os.path.join(fb_dir, 'portable_mode_enabled')) or os.path.isdir(os.path.join(fb_dir, 'profile'))):
        p = os.path.join(fb_dir, 'profile')
        if os.path.isdir(p):
            profile_dir = p
    if not profile_dir and os.environ.get('APPDATA'):
        for sub in ('foobar2000-v2', 'foobar2000'):
            p = os.path.join(os.environ['APPDATA'], sub)
            if os.path.isdir(p):
                profile_dir = p
                break

    env = PlayerEnv(
        player_type=PLAYER_TYPE_FOOBAR,
        player_name='foobar2000',
        exe_path=fb_exe,
        app_dir=fb_dir,
        config_dir=profile_dir,
        reload_command=_detect_reload_command(fb_exe) if fb_exe else '标签/从文件重新载入信息',
    )
    if profile_dir:
        env.library_folders, env.unavailable_folders = parse_library_folders(profile_dir, fb_dir or profile_dir)
    return env


def _parse_metadb_info(blob: bytes | None) -> dict[str, list[str]]:
    if not blob or len(blob) < 24:
        return {}
    rest, idx, tags = blob[24:], 0, {}
    while idx < len(rest) and rest[idx] != 0:
        end_k = rest.find(b'\x00', idx)
        if end_k == -1:
            break
        k, idx, vals = rest[idx:end_k].decode('utf-8', errors='replace').lower(), end_k + 1, []
        while idx < len(rest):
            if rest[idx] == 0:
                idx += 1
                break
            end_v = rest.find(b'\x00', idx)
            if end_v == -1:
                break
            vals.append(rest[idx:end_v].decode('utf-8', errors='replace'))
            idx = end_v + 1
        if vals:
            tags[k] = vals
    return tags


def load_metadb_index(env: PlayerEnv) -> dict[str, dict]:
    """只读加载 metadb.sqlite，返回 {normcase_path: {...}}。"""
    result: dict[str, dict] = {}
    db_path = os.path.join(env.config_dir, 'metadb.sqlite') if env.config_dir else ''
    if not db_path or not os.path.isfile(db_path):
        return result

    temp_copy, conn, rows = None, None, []
    try:
        try:
            conn = _open_sqlite_ro(db_path)
            rows = conn.execute('SELECT name, info, size, lastModified FROM metadb').fetchall()
        except sqlite3.OperationalError:
            fd, temp_copy = tempfile.mkstemp(prefix='metadb_ro_', suffix='.sqlite')
            os.close(fd)
            shutil.copy2(db_path, temp_copy)
            conn = _open_sqlite_ro(temp_copy)
            rows = conn.execute('SELECT name, info, size, lastModified FROM metadb').fetchall()
    except Exception:
        rows = []
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        if temp_copy and os.path.exists(temp_copy):
            try:
                os.remove(temp_copy)
            except OSError:
                pass

    base_dir = env.app_dir or env.config_dir
    for name, info_blob, size, lm in rows:
        if not name or not name.startswith('0+'):
            continue
        norm = os.path.normcase(os.path.abspath(_resolve_fb_uri(name, base_dir)))
        tags = _parse_metadb_info(info_blob)
        tn_list = tags.get('tracknumber') or []
        result[norm] = {
            'tracknumber': tn_list[0].strip() if tn_list else '',
            'size': size,
            'mtime': (lm - 116444736000000000) / 10000000.0 if lm else 0.0,
            'title': ', '.join(tags.get('title') or []),
            'album': ', '.join(tags.get('album') or []),
            'artist': ', '.join(tags.get('artist') or []),
            'albumartist': ', '.join(tags.get('album artist') or tags.get('albumartist') or []),
            'date': ', '.join(tags.get('date') or []),
            'composer': ', '.join(tags.get('composer') or []),
            'lyricist': ', '.join(tags.get('lyricist') or tags.get('text writer') or tags.get('words') or []),
            'arranger': ', '.join(tags.get('arranger') or tags.get('arrangement') or []),
            'performer': ', '.join(tags.get('performer') or []),
            'genre': ', '.join(tags.get('genre') or []),
            'discnumber': ', '.join(tags.get('discnumber') or []),
            'comment': ', '.join(tags.get('comment') or tags.get('comment id3v1 comment') or []),
        }
    return result


def find_foobar_cache_diffs(
    info: dict, fb_entry: dict | None, disk_size: int, disk_mtime: float,
    in_monitored_library: bool, metadb_loaded: bool, temp_dup: bool = False,
) -> list[str]:
    """比对磁盘文件与 foobar metadb.sqlite 缓存，返回所有不一致原因。"""
    if info.get('error') or temp_dup:
        return []
    if fb_entry is None:
        return ['foobar 尚未索引'] if (metadb_loaded and in_monitored_library) else []

    reasons: list[str] = []
    if fb_entry.get('size') is not None and fb_entry['size'] != disk_size:
        reasons.append(f"大小不一致(fb:{fb_entry['size']}!=磁盘:{disk_size})")
    if float(fb_entry.get('mtime') or 0.0) > 0 and disk_mtime - float(fb_entry['mtime']) > 1.5:
        reasons.append('磁盘修改时间较新')

    disk_tn_int = info.get('track_int')
    fb_tn = str(fb_entry.get('tracknumber') or '')
    head = fb_tn.split('/')[0].strip()
    fb_tn_int = int(head) if head.isdigit() else None
    if disk_tn_int is not None and disk_tn_int > 0:
        if fb_tn_int is None or fb_tn_int <= 0:
            reasons.append(f"音轨号未同步(磁盘:{info.get('tracknumber')}/fb:空)")
        elif fb_tn_int != disk_tn_int:
            reasons.append(f"音轨号不一致(磁盘:{info.get('tracknumber')}/fb:{fb_tn})")

    for key, label in (
        ('title', '标题'), ('artist', '艺术家'), ('album', '专辑'), ('albumartist', '专辑艺术家'),
        ('date', '日期'), ('composer', '作曲'), ('lyricist', '作词'), ('arranger', '编曲'),
        ('performer', '表演者'), ('genre', '流派'),
    ):
        disk_val = _norm_cmp(info.get(key) or '')
        if disk_val and _norm_cmp(fb_entry.get(key) or '') != disk_val:
            reasons.append(f'{label}未同步')

    disk_dn_int = info.get('disc_int')
    fb_dn_head = str(fb_entry.get('discnumber') or '').split('/')[0].strip()
    fb_dn_int = int(fb_dn_head) if fb_dn_head.isdigit() else None
    if disk_dn_int is not None and disk_dn_int > 0 and fb_dn_int != disk_dn_int:
        reasons.append('碟片编号未同步')

    if not info.get('has_163_key') or info.get('container') in ('flac', 'ogg', 'opus'):
        disk_cmt = _norm_cmp(info.get('comment') or '')
        if disk_cmt and _norm_cmp(fb_entry.get('comment') or '') != disk_cmt:
            reasons.append('注释未同步')
    return reasons


def scan_library(
    env: PlayerEnv | None = None,
    folders: list[str] | None = None,
    progress_cb=None,
    include_art_scan: bool = True,
) -> list[LibraryTrack]:
    """扫描媒体库；存在 foobar 环境时附加 metadb.sqlite 缓存比对。"""
    return _generic_scan_library(
        env,
        folders=folders,
        progress_cb=progress_cb,
        include_art_scan=include_art_scan,
        metadb_loader=load_metadb_index,
        diff_finder=find_foobar_cache_diffs,
    )


def sync_files_to_foobar(
    env: PlayerEnv,
    paths: list[str],
    foobar_mtimes: dict[str, float] | None = None,
    *,
    auto_start: bool = True,
    batch_size: int = 60,
) -> tuple[int, bool]:
    """触发 foobar2000 重载元数据缓存；若未运行且允许自动启动，尝试唤起 foobar2000。"""
    valid_paths = [p for p in paths if p and os.path.isfile(p)]
    if not valid_paths:
        return 0, False

    running = get_running_foobar_exes()
    if not running and auto_start and env.exe_path and os.path.isfile(env.exe_path):
        try:
            subprocess.Popen([env.exe_path])
            import time
            for _ in range(15):
                time.sleep(0.2)
                running = get_running_foobar_exes()
                if running:
                    time.sleep(0.5)
                    break
        except Exception:
            running = []

    reloaded = False
    if running and env.exe_path and os.path.isfile(env.exe_path):
        cmd_arg = f'/context_command:{env.reload_command}'
        for i in range(0, len(valid_paths), batch_size):
            batch = valid_paths[i:i + batch_size]
            try:
                subprocess.run(
                    [env.exe_path, cmd_arg, *batch],
                    timeout=15.0, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                reloaded = True
            except Exception:
                pass

    touched = touch_files_mtime(valid_paths, foobar_mtimes) if not reloaded else 0
    return (len(valid_paths) if reloaded else touched), reloaded


class FoobarAdapter(BasePlayerAdapter):
    """foobar2000 播放器适配器。"""

    player_type: str = PLAYER_TYPE_FOOBAR
    player_name: str = 'foobar2000'

    def detect_env(self, custom_dir: str = '') -> PlayerEnv:
        return detect_foobar_env(custom_dir)

    def is_running(self) -> bool:
        return len(get_running_foobar_exes()) > 0

    def get_library_folders(self, env: PlayerEnv) -> tuple[list[str], list[str]]:
        return env.library_folders, env.unavailable_folders

    def notify_reload(self, env: PlayerEnv, paths: list[str]) -> tuple[int, bool]:
        return sync_files_to_foobar(env, paths)

