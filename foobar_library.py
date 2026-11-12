"""foobar2000 媒体库发现、metadb.sqlite 全字段核对与刷新同步。"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass, field

from art_repair import SUPPORTS_COVER_WRITE, FolderArtPlan, analyze_folder_art_and_tags, pick_canonical_album_date
from tag_io import AUDIO_EXTS, bump_mtime, has_valid_tracknumber, read_audio_info

STATUS_MISSING_DISK = 'missing_disk'
STATUS_STALE_FOOBAR = 'stale_foobar'
STATUS_OK = 'ok'
STATUS_ERROR = 'error'

CORE_TEXT_FIELDS = ('title', 'artist', 'albumartist', 'album', 'date')
EXTRA_TEXT_FIELDS = ('composer', 'genre', 'discnumber')
FIELD_LABELS = {
    'title': '标题', 'artist': '艺术家', 'albumartist': '专辑艺术家',
    'album': '专辑', 'date': '日期', 'composer': '作曲',
    'performer': '表演者', 'genre': '流派', 'tracknumber': '音轨号',
    'discnumber': '碟片编号', 'comment': '注释', 'lyrics': '歌词', 'cover': '封面',
}

_TEMP_COPY_RE = re.compile(r'^(.+)\.[a-z0-9_]{6,10}$', re.IGNORECASE)
_FULL_ALBUM_RE = re.compile(r'(专辑全长|full\s*album|整轨)', re.IGNORECASE)


@dataclass
class FoobarEnv:
    fb_exe: str = ''
    fb_dir: str = ''
    profile_dir: str = ''
    library_folders: list[str] = field(default_factory=list)
    unavailable_folders: list[str] = field(default_factory=list)
    reload_command: str = '标签/从文件重新载入信息'


@dataclass
class LibraryTrack:
    path: str
    filename: str
    folder: str
    title: str = ''
    artist: str = ''
    albumartist: str = ''
    album: str = ''
    date: str = ''
    composer: str = ''
    performer: str = ''
    genre: str = ''
    duration: float = 0.0
    disk_tracknumber: str = ''
    disk_track_int: int | None = None
    disk_discnumber: str = ''
    disk_disc_int: int | None = None
    comment: str = ''
    has_163_key: bool = False
    has_lyrics: bool = False
    has_embedded_lyrics: bool = False
    has_lrc_file: bool = False
    needs_lyrics: bool = False
    netease_meta: dict | None = None
    container: str | None = None
    extension_mismatch: bool = False
    error: str | None = None
    foobar_in_db: bool = False
    foobar_tracknumber: str = ''
    foobar_title: str = ''
    foobar_artist: str = ''
    foobar_albumartist: str = ''
    foobar_album: str = ''
    foobar_date: str = ''
    foobar_composer: str = ''
    foobar_performer: str = ''
    foobar_genre: str = ''
    foobar_discnumber: str = ''
    foobar_comment: str = ''
    foobar_size: int | None = None
    foobar_mtime: float = 0.0
    foobar_stale_reasons: list[str] = field(default_factory=list)
    disk_size: int = 0
    disk_mtime: float = 0.0
    status: str = STATUS_OK
    is_full_album: bool = False
    is_temp_duplicate: bool = False
    note: str = ''
    needs_tracknumber: bool = False
    needs_foobar_sync: bool = False
    needs_cover_fix: bool = False
    needs_cover_online: bool = False
    cover_status: str = '正常'
    cover_reasons: list[str] = field(default_factory=list)
    target_pic_bytes: bytes | None = None
    target_pic_mime: str = 'image/jpeg'
    needs_tag_fix: bool = False
    tag_updates: dict[str, str] = field(default_factory=dict)
    target_disc: int | None = None
    needs_date: bool = False
    missing_core_fields: list[str] = field(default_factory=list)
    missing_extra_fields: list[str] = field(default_factory=list)
    needs_extra_tags: bool = False
    folder_cover_action: str = 'ok'
    folder_plan: FolderArtPlan | None = None
    matched_track: int | None = None
    matched_disc: int | None = None
    matched_total: int | None = None
    matched_source: str = ''
    matched_album: str = ''
    match_note: str = ''
    write_status: str = ''

    def __post_init__(self):
        if self.status == STATUS_MISSING_DISK and not self.needs_tracknumber:
            self.needs_tracknumber = True
        elif self.status == STATUS_STALE_FOOBAR and not self.needs_foobar_sync:
            self.needs_foobar_sync = True


def get_running_foobar_exes() -> list[str]:
    """查询当前运行的 foobar2000.exe 路径列表。"""
    if os.name != 'nt':
        return []
    paths: list[str] = []
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
            return []
        try:
            ent = PROCESSENTRY32W(dwSize=ctypes.sizeof(PROCESSENTRY32W))
            ok = k32.Process32FirstW(snap, ctypes.byref(ent))
            while ok:
                if ent.szExeFile.lower() == 'foobar2000.exe':
                    hp = k32.OpenProcess(0x1000, False, ent.th32ProcessID)
                    if hp:
                        try:
                            buf, sz = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
                            if k32.QueryFullProcessImageNameW(hp, 0, buf, ctypes.byref(sz)) and buf.value not in paths:
                                paths.append(buf.value)
                        finally:
                            k32.CloseHandle(hp)
                ok = k32.Process32NextW(snap, ctypes.byref(ent))
        finally:
            k32.CloseHandle(snap)
    except Exception:
        pass
    return paths


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


def detect_foobar_env(custom_fb_dir: str = '') -> FoobarEnv:
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

    env = FoobarEnv(
        fb_exe=fb_exe, fb_dir=fb_dir, profile_dir=profile_dir,
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


def load_metadb_index(env: FoobarEnv) -> dict[str, dict]:
    """只读加载 metadb.sqlite，返回 {normcase_path: {...}}。"""
    result: dict[str, dict] = {}
    db_path = os.path.join(env.profile_dir, 'metadb.sqlite') if env.profile_dir else ''
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

    base_dir = env.fb_dir or env.profile_dir
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
            'performer': ', '.join(tags.get('performer') or []),
            'genre': ', '.join(tags.get('genre') or []),
            'discnumber': ', '.join(tags.get('discnumber') or []),
            'comment': ', '.join(tags.get('comment') or []),
        }
    return result


def _is_full_album_file(path: str, info: dict) -> bool:
    if float(info.get('duration') or 0.0) < 1200.0:
        return False
    return bool(_FULL_ALBUM_RE.search(info.get('title') or '') or _FULL_ALBUM_RE.search(os.path.splitext(os.path.basename(path))[0]))


def _is_temp_duplicate(path: str) -> bool:
    stem, ext = os.path.splitext(os.path.basename(path))
    m = _TEMP_COPY_RE.match(stem)
    return bool(m and os.path.isfile(os.path.join(os.path.dirname(path), m.group(1) + ext)))


def _norm_cmp(value: str) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def is_under_folders(path: str, folders: list[str]) -> bool:
    if not path or not folders:
        return False
    norm_p = os.path.normcase(os.path.abspath(path))
    return any(
        norm_p == (nr := os.path.normcase(os.path.abspath(r)).rstrip('\\/')) or norm_p.startswith(nr + os.sep)
        for r in folders if r
    )


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
        ('date', '日期'), ('composer', '作曲'), ('performer', '表演者'), ('genre', '流派'),
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


def track_has_pending_work(
    track: LibraryTrack,
    enable_track: bool = True, enable_art: bool = True, enable_tags: bool = True,
    enable_sync: bool = True, skip_full_album: bool = True, enable_extra_tags: bool = False,
    enabled_tag_fields: set[str] | frozenset[str] | None = None,
    enable_lyrics: bool = False,
) -> bool:
    if track.status == STATUS_ERROR or track.is_temp_duplicate:
        return False
    if enable_track and track.needs_tracknumber and not (skip_full_album and track.is_full_album):
        return True
    if enable_art and (track.needs_cover_fix or track.needs_cover_online or track.folder_cover_action in ('create', 'rewrite', 'remove')):
        return True
    if enable_lyrics and track.needs_lyrics and not (skip_full_album and track.is_full_album):
        return True
    if enable_tags:
        if enabled_tag_fields is None:
            has_planned = track.needs_tag_fix
            has_online = (
                bool(track.missing_core_fields)
                or track.needs_date
                or (enable_extra_tags and track.needs_extra_tags)
            )
        else:
            has_planned = (
                any(k in enabled_tag_fields for k in track.tag_updates)
                or (bool(track.target_disc) and 'discnumber' in enabled_tag_fields)
            )
            has_online = (
                any(f in enabled_tag_fields for f in track.missing_core_fields)
                or (track.needs_date and 'date' in enabled_tag_fields)
                or any(f in enabled_tag_fields for f in track.missing_extra_fields)
            )
        if has_planned or (has_online and not (skip_full_album and track.is_full_album)):
            return True
    if enable_sync and track.needs_foobar_sync:
        return True
    return False


def _append_note(track: LibraryTrack, item: str) -> None:
    if not item:
        return
    if not track.note:
        track.note = item
    elif item not in track.note:
        track.note = f'{track.note}；{item}'


def reconcile_album_consistency(tracks: list[LibraryTrack]) -> None:
    """跨目录按同专辑校对并统一不一致的 DATE 与混用的空/非空 DISCNUMBER。"""
    groups: dict[tuple[str, str], list[LibraryTrack]] = {}
    for tr in tracks:
        if tr.error or tr.is_temp_duplicate or tr.is_full_album:
            continue
        eff_alb = (tr.tag_updates.get('album') or tr.album or '').strip()
        if not eff_alb:
            continue
        eff_aa = (tr.tag_updates.get('albumartist') or tr.albumartist or tr.artist or '').strip()
        groups.setdefault((eff_aa.lower(), eff_alb.lower()), []).append(tr)

    for grp in groups.values():
        if len(grp) < 2:
            continue
        raw_dates = [(tr.date or '').strip() for tr in grp if (tr.date or '').strip()]
        if raw_dates:
            canon_date = pick_canonical_album_date(raw_dates)
            for tr in grp:
                cur_date = (tr.date or '').strip()
                if cur_date != canon_date:
                    tr.tag_updates['date'] = canon_date
                    tr.needs_tag_fix = True
                    tr.needs_date = False
                    if 'date' in tr.missing_core_fields:
                        tr.missing_core_fields = [f for f in tr.missing_core_fields if f != 'date']
                    _append_note(tr, f'统一专辑日期:{cur_date}->{canon_date}' if cur_date else f'补标签:date={canon_date}')
                elif 'date' in tr.tag_updates:
                    tr.tag_updates.pop('date', None)
                    tr.needs_tag_fix = bool(tr.tag_updates or tr.target_disc)

        disc_ints = [tr.disk_disc_int for tr in grp if tr.disk_disc_int and tr.disk_disc_int > 0]
        if disc_ints and set(disc_ints) == {1}:
            for tr in grp:
                if not (tr.disk_disc_int or (tr.disk_discnumber or '').strip()):
                    tr.target_disc = 1
                    tr.needs_tag_fix = True
                    if 'discnumber' in tr.missing_extra_fields:
                        tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f != 'discnumber']
                        tr.needs_extra_tags = bool(tr.missing_extra_fields)
                    _append_note(tr, '统一专辑碟号:1')


def scan_library(env: FoobarEnv, folders: list[str] | None = None, progress_cb=None, include_art_scan: bool = True) -> list[LibraryTrack]:
    """扫描媒体库并结合 metadb.sqlite、封面与标签健康度判定待修状态。"""
    target_folders = folders if folders is not None else env.library_folders
    metadb = load_metadb_index(env)
    metadb_loaded = bool(metadb)

    audio_paths, seen = [], set()
    last_walk_t = 0.0
    for root in target_folders:
        if not root or not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith('.')]
            if progress_cb:
                now = time.monotonic()
                if now - last_walk_t >= 0.08:
                    last_walk_t = now
                    progress_cb(len(audio_paths), 0, dp)
            for fn in sorted(fns):
                if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                    continue
                fp = os.path.abspath(os.path.join(dp, fn))
                norm = os.path.normcase(fp)
                if norm not in seen:
                    seen.add(norm)
                    audio_paths.append(fp)

    total = len(audio_paths)
    total_steps = total * (2 if include_art_scan else 1)
    infos_by_path, temp_dups, folder_clean = {}, {}, {}
    last_prog_t = 0.0
    for idx, fp in enumerate(audio_paths, 1):
        if progress_cb:
            now = time.monotonic()
            if idx == 1 or idx == total or idx % 5 == 0 or (now - last_prog_t) >= 0.08:
                last_prog_t = now
                progress_cb(idx, total_steps, fp)
        infos_by_path[fp] = read_audio_info(fp)
        temp_dup = _is_temp_duplicate(fp)
        temp_dups[fp] = temp_dup
        if not temp_dup:
            folder_clean.setdefault(os.path.dirname(fp), []).append(fp)

    folder_plans: dict[str, FolderArtPlan] = {}
    if include_art_scan:
        done_art = 0
        last_art_t = 0.0
        for folder, clean_paths in folder_clean.items():
            folder_plans[folder] = analyze_folder_art_and_tags(folder, clean_paths, pre_read_infos=infos_by_path)
            done_art += len(clean_paths)
            if progress_cb and total > 0:
                now = time.monotonic()
                if done_art == total or (now - last_art_t) >= 0.08:
                    last_art_t = now
                    progress_cb(min(total + done_art, total_steps), total_steps, clean_paths[-1])

    tracks: list[LibraryTrack] = []
    for fp in audio_paths:
        info = infos_by_path[fp]
        try:
            st = os.stat(fp)
            disk_size, disk_mtime = st.st_size, st.st_mtime
        except OSError:
            disk_size, disk_mtime = 0, 0.0

        norm = os.path.normcase(fp)
        fb = metadb.get(norm)
        full_album, temp_dup = _is_full_album_file(fp, info), temp_dups[fp]
        stale_reasons = find_foobar_cache_diffs(
            info, fb, disk_size, disk_mtime,
            in_monitored_library=is_under_folders(fp, env.library_folders),
            metadb_loaded=metadb_loaded, temp_dup=temp_dup,
        )

        f_plan = folder_plans.get(os.path.dirname(fp))
        t_plan = f_plan.track_plans.get(norm) if f_plan else None
        folder_cov_action = f_plan.cover_action if f_plan else 'ok'

        notes: list[str] = []
        if info.get('extension_mismatch'):
            notes.append(f"扩展名与真实格式({info.get('container')})不符")
        if full_album:
            notes.append('整轨全长文件(默认跳过)')
        if temp_dup:
            notes.append('疑似下载临时副本(正式文件已存在)')

        needs_track, needs_sync = False, False
        if info.get('error'):
            status = STATUS_ERROR
            notes.append(str(info['error']))
        elif not has_valid_tracknumber(info):
            status, needs_track = STATUS_MISSING_DISK, True
            if info.get('track_int') == 0:
                notes.append('音轨号为 0')
            if stale_reasons:
                needs_sync = True
                notes.append('foobar缓存:' + '、'.join(stale_reasons))
        elif stale_reasons:
            status, needs_sync = STATUS_STALE_FOOBAR, True
            notes.append('foobar缓存未刷新:' + '、'.join(stale_reasons))
        else:
            status = STATUS_OK

        cov_status = t_plan.cover_status if t_plan else '正常'
        cov_reasons = list(t_plan.cover_reasons) if t_plan else []
        needs_cov = bool(t_plan and t_plan.needs_cover_fix)
        needs_cov_online = bool(
            'no-cover-source' in cov_reasons
            and not info.get('error')
            and not temp_dup
            and info.get('container') in SUPPORTS_COVER_WRITE
        )
        if needs_cov_online:
            cov_status = '缺封面(待联网)'
        needs_tags = bool(t_plan and t_plan.needs_tag_fix)
        tag_updates = dict(t_plan.tag_updates) if t_plan else {}

        missing_core: list[str] = []
        missing_extra: list[str] = []
        if not info.get('error') and not temp_dup:
            for f in CORE_TEXT_FIELDS:
                if not (info.get(f) or '').strip() and f not in tag_updates:
                    missing_core.append(f)
            for f in EXTRA_TEXT_FIELDS:
                if f == 'discnumber':
                    if not (info.get('disc_int') or (info.get('discnumber') or '').strip()):
                        missing_extra.append(f)
                elif not (info.get(f) or '').strip() and f not in tag_updates:
                    missing_extra.append(f)
        needs_date = 'date' in missing_core
        needs_extra = bool(missing_extra)

        if needs_cov:
            notes.append('封面:' + ','.join(cov_reasons))
        elif cov_status != '正常':
            notes.append(cov_status)
        if folder_cov_action in ('create', 'rewrite', 'remove') and f_plan and f_plan.detail:
            notes.append(f_plan.detail)
        if needs_tags:
            notes.append('补标签:' + ','.join(f'{k}={v}' for k, v in tag_updates.items()))
        if missing_core:
            notes.append('缺核心:' + ','.join(FIELD_LABELS[f] for f in missing_core))
        if missing_extra:
            notes.append('缺扩展:' + ','.join(FIELD_LABELS[f] for f in missing_extra))

        tracks.append(LibraryTrack(
            path=fp, filename=os.path.basename(fp), folder=os.path.dirname(fp),
            title=info.get('title') or '', artist=info.get('artist') or '',
            albumartist=info.get('albumartist') or '', album=info.get('album') or '',
            date=info.get('date') or '', composer=info.get('composer') or '',
            performer=info.get('performer') or '', genre=info.get('genre') or '',
            duration=float(info.get('duration') or 0.0),
            disk_tracknumber=info.get('tracknumber') or '', disk_track_int=info.get('track_int'),
            disk_discnumber=info.get('discnumber') or '', disk_disc_int=info.get('disc_int'),
            comment=info.get('comment') or '', has_163_key=bool(info.get('has_163_key')),
            has_lyrics=bool(info.get('has_lyrics')),
            has_embedded_lyrics=bool(info.get('has_embedded_lyrics')),
            has_lrc_file=bool(info.get('has_lrc_file')),
            needs_lyrics=bool(not info.get('error') and not temp_dup and not info.get('has_lyrics')),
            netease_meta=info.get('netease_meta'), container=info.get('container'),
            extension_mismatch=bool(info.get('extension_mismatch')), error=info.get('error'),
            foobar_in_db=fb is not None, foobar_tracknumber=fb['tracknumber'] if fb else '',
            foobar_title=fb.get('title', '') if fb else '', foobar_artist=fb.get('artist', '') if fb else '',
            foobar_albumartist=fb.get('albumartist', '') if fb else '', foobar_album=fb.get('album', '') if fb else '',
            foobar_date=fb.get('date', '') if fb else '', foobar_composer=fb.get('composer', '') if fb else '',
            foobar_performer=fb.get('performer', '') if fb else '', foobar_genre=fb.get('genre', '') if fb else '',
            foobar_discnumber=fb.get('discnumber', '') if fb else '', foobar_comment=fb.get('comment', '') if fb else '',
            foobar_size=fb['size'] if fb else None,
            foobar_mtime=fb['mtime'] if fb else 0.0, foobar_stale_reasons=stale_reasons,
            disk_size=disk_size, disk_mtime=disk_mtime, status=status,
            is_full_album=full_album, is_temp_duplicate=temp_dup, note='；'.join(notes),
            needs_tracknumber=needs_track, needs_foobar_sync=needs_sync,
            needs_cover_fix=needs_cov, needs_cover_online=needs_cov_online,
            cover_status=cov_status, cover_reasons=cov_reasons,
            target_pic_bytes=t_plan.target_pic_bytes if t_plan else None,
            target_pic_mime=t_plan.target_pic_mime if t_plan else 'image/jpeg',
            needs_tag_fix=needs_tags, tag_updates=tag_updates, needs_date=needs_date,
            missing_core_fields=missing_core, missing_extra_fields=missing_extra,
            needs_extra_tags=needs_extra,
            folder_cover_action=folder_cov_action, folder_plan=f_plan,
        ))
    if include_art_scan:
        reconcile_album_consistency(tracks)
    return tracks


def sync_files_to_foobar(env: FoobarEnv, paths: list[str], foobar_mtimes: dict[str, float] | None = None) -> tuple[int, bool]:
    """推进修改时间并通知运行中的 foobar2000 载入元数据。"""
    valid_paths = [p for p in paths if p and os.path.isfile(p)]
    if not valid_paths:
        return 0, False

    touched = 0
    for p in valid_paths:
        prev = (foobar_mtimes or {}).get(os.path.normcase(os.path.abspath(p)), 0.0)
        try:
            bump_mtime(p, min_prev_mtime=prev)
            touched += 1
        except OSError:
            pass

    running = get_running_foobar_exes()
    reloaded = False
    if running and env.fb_exe and os.path.isfile(env.fb_exe):
        cmd_arg = f'/context_command:{env.reload_command}'
        for i in range(0, len(valid_paths), 20):
            try:
                subprocess.run(
                    [env.fb_exe, cmd_arg, *valid_paths[i:i + 20]],
                    timeout=3.0, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                )
                reloaded = True
            except Exception:
                break
        if touched > 0:
            reloaded = True
    return touched, reloaded
