"""MusicMetadata 主窗口图形界面组件与数据视图。"""

from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import traceback

from cli import build_arg_parser, run_cli
from core.config import (
    get_default_music_folders,
    has_player_env,
    is_player_active,
    load_user_config,
    normalize_folder_list,
    reveal_in_file_manager,
    save_user_config,
    ui_font,
)
from ui.track_detail import TrackDetailWidget
from core.constants import (
    CORE_TEXT_FIELDS,
    DELETABLE_FIELDS,
    EXTRA_TEXT_FIELDS,
    FIELD_LABELS,
    LYRICS_MODES,
    MAX_SIDE,
    PREFER_EXTERNAL_LRC_CONTAINERS,
    SETTABLE_FIELDS,
    STATUS_ERROR,
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_PLAYER,
    SUPPORTS_COVER_WRITE,
    SUPPORTS_EMBEDDED_LYRICS,
    PLAYER_TYPE_FOOBAR,
    PLAYER_TYPE_MUSICBEE,
    PLAYER_TYPE_ROON,
    PLAYER_TYPE_GENERIC,
)
from core.models import (
    FolderArtPlan,
    LibraryTrack,
    PlayerEnv,
    default_track_sort_key,
    natural_sort_key,
)
from infra.integrations import (
    detect_active_player,
    detect_foobar_env,
    detect_musicbee_env,
    get_running_foobar_exes,
    get_running_musicbee_exes,
    get_running_roon_exes,
    scan_library,
    sync_files_to_foobar,
    sync_files_to_musicbee,
)
from infra.scanner import track_has_pending_work, track_needs_lyrics
from pipelines import (
    FOLDER_COVER_LABELS,
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SacdExtractOptions,
    SetOptions,
    ZhConvertOptions,
    _album_date_group_key,
    _count_deletable_targets,
    _run_pool_cancellable,
    default_checked,
    execute_convert,
    execute_deletions,
    execute_repairs,
    execute_sacd_pipeline,
    execute_set_tags,
    execute_zh_convert,
    parse_delete_fields,
)
from providers import ONLINE_PROVIDERS, PROVIDER_LABELS
from services.art import (
    analyze_folder_art_and_tags,
    apply_folder_cover_file,
    apply_track_deletions,
    apply_track_repairs,
    apply_track_set_tags,
    convert_track_format,
    delete_folder_cover_file,
    inspect_deletable_fields,
    normalize_jpeg,
    parse_set_tags,
    pick_canonical_album_date,
)
from services.lookup import (
    TrackMatch,
    clear_caches,
    fetch_cover_bytes,
    get_discogs_token,
    get_fanart_keys,
    get_netease_api_base,
    get_spotify_credentials,
    get_tadb_api_key,
    parse_source_list,
    resolve_track_metadata,
    set_discogs_token,
    set_fanart_keys,
    set_netease_api_base,
    set_spotify_credentials,
    set_tadb_api_key,
)
from pathlib import Path
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg, has_valid_tracknumber, parse_filename_clues, read_audio_info
from ui.dialogs import (
    ArtistPickerDialog,
    ConvertFormatDialog,
    CoverPickerDialog,
    LyricPickerDialog,
    _extract_group_clues,
    DeleteMetadataDialog,
    FolderManagerDialog,
    SacdExtractDialog,
    SetMetadataDialog,
    SourceConfigDialog,
    ZhConvertDialog,
)




def _detect_player_env(*args, **kwargs) -> PlayerEnv:
    app_mod = sys.modules.get('app')
    if app_mod is not None:
        custom_act = getattr(app_mod, 'detect_active_player', None)
        if custom_act is not None and custom_act is not detect_active_player:
            res = custom_act(*args, **kwargs)
            return res[0] if isinstance(res, tuple) else res
        custom_fb = getattr(app_mod, 'detect_foobar_env', None)
        if custom_fb is not None and custom_fb is not detect_foobar_env:
            res = custom_fb(*args, **kwargs)
            return res[0] if isinstance(res, tuple) else res
    env, _ = detect_active_player()
    return env

_detect_foobar_env = _detect_player_env


def _get_default_music_folders(*args, **kwargs):
    fn = getattr(sys.modules.get('app'), 'get_default_music_folders', get_default_music_folders)
    return fn(*args, **kwargs)


def _scan_library(*args, **kwargs):
    fn = getattr(sys.modules.get('app'), 'scan_library', scan_library)
    return fn(*args, **kwargs)


class _MessageboxProxy:
    def __getattr__(self, name):
        app_mod = sys.modules.get('app')
        mb = getattr(app_mod, 'messagebox', messagebox) if app_mod is not None else messagebox
        return getattr(mb, name)


_msgbox = _MessageboxProxy()

def _format_status_badge(tr: LibraryTrack, *, include_lyrics: bool = False, lyrics_mode: str = 'auto') -> tuple[str, str]:
    if tr.status == STATUS_ERROR:
        return '读取异常', 'err'
    if tr.is_temp_duplicate:
        return '临时副本', 'warn'
    has_lrc_work = include_lyrics and track_needs_lyrics(tr, lyrics_mode)
    has_online_tags = bool(
        tr.needs_tracknumber or tr.needs_date or tr.missing_core_fields
        or tr.needs_extra_tags or has_lrc_work
    )
    if tr.is_full_album and has_online_tags and not (tr.needs_cover_fix or tr.needs_cover_online or tr.needs_tag_fix or tr.needs_player_sync):
        return '全长跳过', 'muted'
    tags = []
    if tr.needs_tracknumber:
        tags.append('缺音轨')
    if tr.needs_cover_fix or tr.needs_cover_online or tr.folder_cover_action in ('create', 'rewrite', 'remove'):
        tags.append('封面')
    if tr.needs_tag_fix or any(f != 'date' for f in tr.missing_core_fields):
        tags.append('缺标签')
    elif tr.needs_date and not tr.is_full_album:
        tags.append('缺日期')
    if tr.needs_extra_tags and not tr.is_full_album:
        tags.append('缺扩展')
    if has_lrc_work and not tr.is_full_album:
        if tr.needs_lyrics:
            tags.append('缺歌词')
        elif not tr.has_lrc_file:
            tags.append('缺外置歌词')
        elif not tr.has_embedded_lyrics:
            tags.append('缺内嵌歌词')
    if tr.needs_player_sync:
        tags.append('缓存过期')
    if not tags:
        return ('手动挑选', 'warn') if tr.cover_status.startswith('缺封面') else ('正常', 'ok')
    return ' / '.join(tags), ('err' if '缺音轨' in tags else ('warn' if any(t in tags for t in ('封面', '缺标签', '缺日期', '缺扩展', '缺歌词', '缺外置歌词', '缺内嵌歌词')) else 'info'))


def _format_cover_cell(tr: LibraryTrack) -> str:
    parts = [f'待修:{",".join(tr.cover_reasons)}' if tr.needs_cover_fix else (tr.cover_status if tr.cover_status != '正常' else '')]
    if tr.folder_cover_action in FOLDER_COVER_LABELS:
        parts.append(FOLDER_COVER_LABELS[tr.folder_cover_action])
    parts = [p for p in parts if p]
    return '；'.join(parts) if parts else '正常'


def _format_tag_sync_cell(tr: LibraryTrack, *, include_lyrics: bool = False, lyrics_mode: str = 'auto') -> str:
    parts = []
    reasons = tr.player_stale_reasons
    if tr.needs_player_sync:
        parts.append('缓存:' + ','.join(reasons) if reasons else '缓存过期')
    fix_items = [f'{k}={v}' for k, v in tr.tag_updates.items()] if tr.tag_updates else []
    if tr.target_disc:
        fix_items.append(f'discnumber={tr.target_disc}')
    if tr.needs_tag_fix and fix_items:
        parts.append('补:' + ','.join(fix_items))
    if not tr.is_full_album:
        pending = [f for f in (tr.missing_core_fields + tr.missing_extra_fields) if f not in tr.tag_updates]
        has_lrc_work = include_lyrics and track_needs_lyrics(tr, lyrics_mode)
        if has_lrc_work:
            if tr.needs_lyrics:
                pending.append('lyrics')
            elif not tr.has_lrc_file:
                parts.append('待补外置.lrc')
            elif not tr.has_embedded_lyrics:
                parts.append('待补内嵌歌词')
        if pending:
            labels = [FIELD_LABELS.get(f, f) for f in pending[:4]]
            more = f'等{len(pending)}项' if len(pending) > 4 else ''
            parts.append('待联网:' + ','.join(labels) + more)
    return '；'.join(parts) if parts else '正常'


_natural_sort_key = natural_sort_key


def _resource_path(rel_path: str) -> str:
    """解析资源文件路径，支持 PyInstaller 打包环境、源码多级子目录及工作目录。"""
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, rel_path)

    cur_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        cur_dir,                               # ui/
        os.path.dirname(cur_dir),              # ui/.. -> MusicMetadata/
        os.path.abspath(os.getcwd()),          # cwd
    ]
    for c in candidates:
        target = os.path.join(c, rel_path)
        if os.path.exists(target):
            return target

    return os.path.join(os.path.dirname(cur_dir), rel_path)


def _apply_window_icon(root: tk.Tk) -> tk.PhotoImage | None:
    """加载应用图标：Windows 优先使用原生多尺寸 .ico，成功后立即返回；非 Windows 或缺失时回退 .png。"""
    ico_path = _resource_path(os.path.join('assets', 'app_icon.ico'))
    if os.path.isfile(ico_path):
        try:
            root.iconbitmap(default=ico_path)
            return None
        except Exception:
            try:
                root.iconbitmap(ico_path)
                return None
            except Exception:
                pass

    png_path = _resource_path(os.path.join('assets', 'app_icon.png'))
    if os.path.isfile(png_path):
        try:
            photo = tk.PhotoImage(file=png_path)
            root.iconphoto(True, photo)
            return photo
        except Exception:
            pass
    return None


class ToolTip:
    """轻量悬浮提示气泡 (Tooltip)。"""
    def __init__(self, widget: tk.Widget, text_func: Any = '', delay: int = 350):
        self.widget = widget
        self.text_func = text_func
        self.delay = delay
        self.tip_window: tk.Toplevel | None = None
        self._after_id: str | None = None

        self.widget.bind('<Enter>', self._on_enter, add='+')
        self.widget.bind('<Leave>', self._on_leave, add='+')
        self.widget.bind('<ButtonPress>', self._on_leave, add='+')

    def _on_enter(self, _event=None):
        self._schedule()

    def _on_leave(self, _event=None):
        self._unschedule()
        self._hide()

    def _schedule(self):
        self._unschedule()
        self._after_id = self.widget.after(self.delay, self._show)

    def _unschedule(self):
        if self._after_id:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _show(self):
        text = self.text_func() if callable(self.text_func) else self.text_func
        if not text or not self.widget.winfo_exists():
            return
        self._hide()
        x = self.widget.winfo_rootx() + 10
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4

        self.tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        try:
            tw.wm_attributes('-topmost', True)
        except Exception:
            pass

        frame = tk.Frame(tw, bg='#0f172a', bd=1, relief='solid')
        frame.pack(fill='both', expand=True)
        lbl = tk.Label(
            frame, text=text, justify='left',
            bg='#0f172a', fg='#f8fafc',
            font=ui_font(8), padx=10, pady=6,
        )
        lbl.pack()
        tw.wm_geometry(f"+{x}+{y}")

    def _hide(self):
        if self.tip_window:
            try:
                self.tip_window.destroy()
            except Exception:
                pass
            self.tip_window = None


class MainWindow:
    _COLS = ('chk', 'status', 'title', 'artist', 'album', 'track', 'cover', 'tagsync', 'note')
    _HEADS = ('☐ 选择', '综合状态', '歌曲名', '艺术家', '专辑', '音轨号', '封面状态', '标签检查', '说明 / 结果')
    _WIDTHS = (65, 95, 260, 150, 200, 100, 110, 150, 230)
    _MIN_WIDTHS = (60, 80, 160, 110, 130, 75, 90, 120, 160)
    _SEARCH_TYPES = (
        '全部',
        '歌曲名',
        '艺术家',
        '专辑名',
        '专辑艺术家',
        '作曲',
        '作词',
        '编曲',
        '流派',
        '音轨号',
        '文件路径',
        '说明/备注',
    )
    _PROG_LABELS = {
        'fix_prog': '处理中',
        'del_prog': '删除元数据',
        'set_prog': '设置标签',
        'conv_prog': '转换格式',
        'zh_prog': '简繁转换',
        'sacd_prog': 'SACD提取',
    }
    _PREFER_PRESETS: dict[str, tuple[str, ...]] = {
        '默认(免梯直连)': (),
        '华语/国内优先': ('netease', 'qq', 'kugou', 'migu', 'baidu'),
        '欧美/国际优先': ('itunes', 'deezer', 'ytmusic', 'lrclib', 'mb', 'spotify', 'tadb', 'fanart', 'discogs'),
        '网易云优先': ('netease',),
        'QQ音乐优先': ('qq',),
        'Apple/iTunes优先': ('itunes',),
        'MusicBrainz优先': ('mb',),
        'YouTube Music优先': ('ytmusic', 'lrclib'),
        'Deezer优先': ('deezer', 'lrclib'),
        'TheAudioDB优先': ('tadb',),
        'Fanart.tv优先': ('fanart',),
        'Discogs优先': ('discogs',),
        '酷狗优先': ('kugou',),
        '咪咕优先': ('migu',),
        '千千优先': ('baidu',),
    }

    def __init__(self, root: tk.Tk, auto_scan: bool = False):
        self.root = root
        self.root.title('MusicMetadata')
        self._icon_photo = _apply_window_icon(self.root)
        self._user_config = load_user_config()

        sw = max(1024, self.root.winfo_screenwidth())
        sh = max(720, self.root.winfo_screenheight())
        target_w = min(1400, max(1080, sw - 80))
        target_h = min(860, max(680, sh - 100))
        pos_x = max(0, (sw - target_w) // 2)
        pos_y = max(0, (sh - target_h) // 2)
        default_geo = f'{target_w}x{target_h}+{pos_x}+{pos_y}'

        saved_geo = str(self._user_config.get('window_geometry') or '')
        if saved_geo:
            try:
                parts = saved_geo.replace('+', 'x').replace('-', 'x').split('x')
                if len(parts) >= 2 and int(parts[0]) >= 900 and int(parts[1]) >= 500:
                    self.root.geometry(saved_geo)
                else:
                    self.root.geometry(default_geo)
            except Exception:
                self.root.geometry(default_geo)
        else:
            self.root.geometry(default_geo)

        self.root.minsize(1020, 580)
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.report_callback_exception = self._on_tkinter_exception
        saved_folders = normalize_folder_list(self._user_config.get('custom_folders'), existing_only=True)
        self.env = PlayerEnv()
        self.tracks: list[LibraryTrack] = []
        self.visible_tracks: list[LibraryTrack] = []
        self.custom_folders: list[str] | None = saved_folders or None
        self.checked_paths: set[str] = set()
        self._sort_col: str | None = None
        self._sort_reverse: bool = False
        self._base_headings: dict[str, str] = {c: h for c, h in zip(self._COLS, self._HEADS)}
        self._busy = False
        self._has_scanned = False
        self._prog_indet = False
        self._cancel_flag = False
        self._ev_q: queue.Queue = queue.Queue()

        self.layout_mode = self._user_config.get('layout_mode', 'bottom')
        self.var_layout_mode = tk.StringVar(value=self.layout_mode)

        self.var_track = tk.BooleanVar(value=True)
        self.var_art = tk.BooleanVar(value=True)
        self.var_cover = self.var_art
        self.var_artist_art = tk.BooleanVar(value=bool(self._user_config.get('enable_artist_art', False)))
        self.var_date = tk.BooleanVar(value=True)
        self.var_albumartist = tk.BooleanVar(value=True)
        self.var_artist = tk.BooleanVar(value=True)
        self.var_composer = tk.BooleanVar(value=bool(self._user_config.get('enable_composer', True)))
        self.var_lyricist = tk.BooleanVar(value=bool(self._user_config.get('enable_lyricist', True)))
        self.var_arranger = tk.BooleanVar(value=bool(self._user_config.get('enable_arranger', True)))
        self.var_genre = tk.BooleanVar(value=True)
        self.var_disc = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=True)
        self.var_sync_mb_artist = tk.BooleanVar(value=bool(self._user_config.get('sync_musicbee_artist', True)))
        self.var_lyrics = tk.BooleanVar(value=bool(self._user_config.get('enable_lyrics', False)))
        self.var_reverse_cover_cascade = tk.BooleanVar(value=bool(self._user_config.get('enable_reverse_cover_cascade', False)))
        saved_lrc_mode = self._user_config.get('lyrics_mode', 'auto')
        self.var_lyrics_mode = tk.StringVar(value=saved_lrc_mode if saved_lrc_mode in LYRICS_MODES else 'auto')
        saved_preset = self._user_config.get('prefer_preset', '默认(免梯直连)')
        if saved_preset == '默认':
            saved_preset = '默认(免梯直连)'
        valid_presets = set(self._PREFER_PRESETS.keys()) | {'自定义'}
        self.var_prefer_preset = tk.StringVar(value=saved_preset if saved_preset in valid_presets else '默认(免梯直连)')
        self._custom_prefer_sources: tuple[str, ...] = tuple(self._user_config.get('custom_prefer_sources') or ())
        self._only_sources: tuple[str, ...] = tuple(self._user_config.get('only_sources') or ())
        self._skip_sources: tuple[str, ...] = tuple(self._user_config.get('skip_sources') or ())
        self._netease_api: str = str(self._user_config.get('netease_api') or '')
        if self._netease_api:
            set_netease_api_base(self._netease_api)
        self._spotify_client_id, self._spotify_client_secret = get_spotify_credentials()
        self._tadb_api_key: str = str(self._user_config.get('tadb_api_key') or '')
        if self._tadb_api_key:
            set_tadb_api_key(self._tadb_api_key)
        self._fanart_api_key: str = str(self._user_config.get('fanart_api_key') or '')
        self._fanart_client_key: str = str(self._user_config.get('fanart_client_key') or '')
        if self._fanart_api_key or self._fanart_client_key:
            set_fanart_keys(self._fanart_api_key, self._fanart_client_key)
        self._discogs_token: str = str(self._user_config.get('discogs_token') or '')
        if self._discogs_token:
            set_discogs_token(self._discogs_token)
        self.var_only_pending = tk.BooleanVar(value=True)
        self.var_skip_full = tk.BooleanVar(value=True)
        self.var_search = tk.StringVar(value='')
        saved_search_type = str(self._user_config.get('search_type') or '全部')
        if saved_search_type not in self._SEARCH_TYPES:
            saved_search_type = '全部'
        self.var_search_type = tk.StringVar(value=saved_search_type)
        self.var_search_info = tk.StringVar(value='')
        self._search_after_id: str | None = None
        init_env_msg = (
            f'已加载上次保存的 {len(self.custom_folders)} 个目录；点击「扫描媒体库」开始检查。'
            if self.custom_folders
            else '未自动扫描音乐目录；请点击下方「扫描媒体库」或右上角「选择目录...」开始扫描。'
        )
        self.var_env = tk.StringVar(value=init_env_msg)
        self.var_paths = tk.StringVar(value=('📂 待扫描目录: ' + '；'.join(self.custom_folders)) if self.custom_folders else '')
        self._paths_tooltip_text = ('待扫描目录列表:\n  ' + '\n  '.join(self.custom_folders)) if self.custom_folders else ''
        self.var_summary = tk.StringVar(value='尚未扫描，请点击「扫描媒体库」开始检查。')
        self.var_status = tk.StringVar(value='就绪：点击「扫描媒体库」或「选择目录...」开始扫描')

        self._ev_handlers = {
            'scan_done': lambda p: self._on_scan_finished(*p),
            'fix_row': lambda p: self._on_fix_row_updated(*p),
            'del_row': lambda p: self._on_delete_row_updated(*p),
            'set_row': lambda p: self._on_set_row_updated(*p),
            'conv_row': lambda p: self._on_convert_row_updated(*p),
            'zh_row': lambda p: self._on_zh_row_updated(*p),
            'fix_done': lambda p: self._on_fix_finished(p),
            'del_done': lambda p: self._on_delete_finished(p),
            'set_done': lambda p: self._on_set_finished(p),
            'conv_done': lambda p: self._on_convert_finished(p),
            'zh_done': lambda p: self._on_zh_finished(p),
            'sacd_done': lambda p: self._on_sacd_finished(p),
            'log': self._log,
        }

        self._build_ui()
        self._init_player_mode()
        self.root.after(40, self._poll_queue)
        if auto_scan:
            self.start_scan()

    def _build_ui(self):
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('.', font=ui_font(9))
        style.configure('Treeview', rowheight=24)
        style.configure('Treeview.Heading', font=ui_font(9, bold=True))
        self.root.configure(bg='#f5f7fa')

        # 挂载顶部系统菜单栏 (方案 C)
        self._setup_menu_bar()

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)

        # 顶部环境与统计卡 (数据源与扫描管理闭环，分层响应式结构)
        top = ttk.LabelFrame(outer, text='媒体库状态', padding=(10, 6))
        top.pack(fill='x', pady=(0, 8))

        # 第 1 行：核心状态与控制按钮 (按钮高优先级 side='right' 常驻，避免窄窗口被挤压)
        r1 = ttk.Frame(top)
        r1.pack(fill='x')

        self.btn_rescan = ttk.Button(r1, text='扫描媒体库', command=self.start_scan)
        self.btn_rescan.pack(side='right', padx=(6, 0))

        self.btn_manage_dirs = ttk.Button(r1, text='管理目录...', command=self._manage_custom_folders)
        self.btn_manage_dirs.pack(side='right', padx=(6, 0))

        self.btn_pick_dir = ttk.Button(r1, text='选择目录...', command=self._pick_custom_folder)
        self.btn_pick_dir.pack(side='right', padx=(6, 0))

        self.btn_reset_lib = ttk.Button(r1, text='使用媒体库', command=self._reset_to_library)

        self.lbl_env = ttk.Label(r1, textvariable=self.var_env, anchor='w')
        self.lbl_env.pack(side='left', fill='x', expand=True)
        ToolTip(self.lbl_env, lambda: self.var_env.get())

        # 第 2 行：详细监控路径展示与气泡提示 (方案 2 分层解耦，独享整行横向空间)
        r_paths = ttk.Frame(top)
        r_paths.pack(fill='x', pady=(2, 0))

        self.lbl_paths = ttk.Label(
            r_paths, textvariable=self.var_paths,
            font=ui_font(8), foreground='#64748b', anchor='w', cursor='hand2',
        )
        self.lbl_paths.pack(side='left', fill='x', expand=True)
        self.lbl_paths.bind('<Button-1>', lambda _e: self._manage_custom_folders())
        ToolTip(self.lbl_paths, lambda: self._paths_tooltip_text)

        # 第 3 行：统计摘要信息
        self.lbl_summary = ttk.Label(top, textvariable=self.var_summary, foreground='#334155', font=ui_font(9, bold=True))
        self.lbl_summary.pack(fill='x', pady=(4, 0))

        # 操作按钮栏 (高频快捷工具栏)
        acts = ttk.Frame(outer)
        acts.pack(fill='x', pady=(0, 6))

        # 核心主操作组
        self.btn_one_click = tk.Button(
            acts, text='🚀 一键修复已选项目', command=self._on_one_click,
            bg='#2563eb', fg='#ffffff', activebackground='#1d4ed8', activeforeground='#ffffff',
            relief='flat', padx=14, pady=5, font=ui_font(10, bold=True), cursor='hand2',
        )
        self.btn_one_click.pack(side='left', padx=(0, 6))

        self.btn_sync_only = ttk.Button(acts, text='仅刷新播放器缓存', command=self._on_sync_only)
        self.btn_sync_only.pack(side='left', padx=(0, 6))

        self.btn_stop = ttk.Button(acts, text='⏹ 停止', command=self._on_stop)

        # 垂直分隔线 1
        self.sep_act1 = ttk.Separator(acts, orient='vertical')
        self.sep_act1.pack(side='left', fill='y', padx=(4, 8), pady=2)

        # 交互精修核对组
        self.btn_pick_cover = ttk.Button(acts, text='🖼️ 挑选封面...', command=self._on_pick_cover)
        self.btn_pick_cover.pack(side='left', padx=(0, 6))
        self.btn_pick_artist = ttk.Button(acts, text='👤 挑选写真...', command=self._on_pick_artist)
        self.btn_pick_artist.pack(side='left', padx=(0, 6))
        self.btn_pick_lyric = ttk.Button(acts, text='🎵 挑选歌词...', command=self._on_pick_lyric)
        self.btn_pick_lyric.pack(side='left', padx=(0, 6))

        # 垂直分隔线 2
        self.sep_act2 = ttk.Separator(acts, orient='vertical')
        self.sep_act2.pack(side='left', fill='y', padx=(4, 8), pady=2)

        # 扩展工具与实用工具箱组
        self.btn_sacd = ttk.Button(acts, text='⚡ SACD提取...', command=self._on_sacd_extract)
        self.btn_sacd.pack(side='left', padx=(0, 6))

        self.btn_tools_menu = ttk.Menubutton(acts, text='🛠️ 更多工具 ▾')
        self.menu_more_tools = tk.Menu(self.btn_tools_menu, tearoff=0)
        self.menu_more_tools.add_command(label='✏️ 设置标签...', command=self._on_set_metadata)
        self.menu_more_tools.add_command(label='🌐 简繁汉字转换...', command=self._on_convert_zh)
        self.menu_more_tools.add_command(label='🎼 音频格式转换...', command=self._on_convert_format)
        self.menu_more_tools.add_separator()
        self.menu_more_tools.add_command(label='🧹 删除部分元数据...', command=self._on_delete_metadata)
        self.btn_tools_menu['menu'] = self.menu_more_tools
        self.btn_tools_menu.pack(side='left', padx=(0, 6))

        # 兼容性/自动化测试保留实例 (不 pack 进主界面，保持 API 与测试兼容)
        self.btn_set_meta = ttk.Button(acts, text='设置标签...', command=self._on_set_metadata)
        self.btn_delete_meta = ttk.Button(acts, text='删除部分元数据...', command=self._on_delete_metadata)
        self.btn_convert = ttk.Button(acts, text='格式转换...', command=self._on_convert_format)
        self.btn_zh_convert = ttk.Button(acts, text='简繁转换...', command=self._on_convert_zh)

        # 修复项选择（8 个独立元数据修复按钮 + 快捷全选/清空 + 辅助选项）
        opts_card = ttk.LabelFrame(outer, text='修复项选择', padding=(8, 4))
        opts_card.pack(fill='x', pady=(0, 6))

        opts_fields = ttk.Frame(opts_card)
        opts_fields.pack(fill='x')
        self.field_vars: dict[str, tk.BooleanVar] = {
            'cover': self.var_art,
            'date': self.var_date,
            'albumartist': self.var_albumartist,
            'artist': self.var_artist,
            'composer': self.var_composer,
            'lyricist': self.var_lyricist,
            'arranger': self.var_arranger,
            'genre': self.var_genre,
            'tracknumber': self.var_track,
            'discnumber': self.var_disc,
        }
        self.field_buttons: dict[str, ttk.Checkbutton] = {}
        for key, label in (
            ('cover', '封面'),
            ('date', '日期'),
            ('albumartist', '专辑艺术家'),
            ('artist', '艺术家'),
            ('composer', '作曲'),
            ('lyricist', '作词'),
            ('arranger', '编曲'),
            ('genre', '流派'),
            ('tracknumber', '音轨号'),
            ('discnumber', '碟片编号'),
        ):
            btn = ttk.Checkbutton(opts_fields, text=label, variable=self.field_vars[key], command=self._on_option_changed)
            btn.pack(side='left', padx=(0, 10))
            self.field_buttons[key] = btn

        self.btn_fields_none = ttk.Button(opts_fields, text='全不选', width=7, command=lambda: self._set_all_fields(False))
        self.btn_fields_none.pack(side='right')
        self.btn_fields_all = ttk.Button(opts_fields, text='全选', width=6, command=lambda: self._set_all_fields(True))
        self.btn_fields_all.pack(side='right', padx=(0, 4))

        opts_misc = ttk.Frame(opts_card)
        opts_misc.pack(fill='x', pady=(4, 0))
        self.chk_sync = ttk.Checkbutton(opts_misc, text='刷新播放器缓存', variable=self.var_sync, command=self._on_option_changed)
        self.chk_sync.pack(side='left', padx=(0, 8))
        self.chk_artist_art = ttk.Checkbutton(opts_misc, text='获取艺术家图', variable=self.var_artist_art, command=self._on_pref_option_changed)
        self.chk_artist_art.pack(side='left', padx=(0, 8))
        self.chk_sync_mb_artist = ttk.Checkbutton(opts_misc, text='同步MusicBee写真库', variable=self.var_sync_mb_artist, command=self._on_pref_option_changed)
        self.chk_lyrics = ttk.Checkbutton(opts_misc, text='补全歌词', variable=self.var_lyrics, command=self._on_pref_option_changed)
        self.chk_lyrics.pack(side='left', padx=(0, 4))
        self.cmb_lyrics_mode = ttk.Combobox(
            opts_misc, textvariable=self.var_lyrics_mode,
            values=list(LYRICS_MODES), state='readonly', width=6,
        )
        self.cmb_lyrics_mode.pack(side='left', padx=(0, 8))
        self.cmb_lyrics_mode.bind('<<ComboboxSelected>>', lambda _e: self._on_pref_option_changed())
        ttk.Label(opts_misc, text='音源:').pack(side='left')
        self.cmb_prefer_source = ttk.Combobox(
            opts_misc, textvariable=self.var_prefer_preset,
            values=list(self._PREFER_PRESETS.keys()) + ['自定义'],
            state='readonly', width=14,
        )
        self.cmb_prefer_source.pack(side='left', padx=(4, 4))
        self.cmb_prefer_source.bind('<<ComboboxSelected>>', lambda _e: self._save_user_state())
        self.btn_source_cfg = ttk.Button(opts_misc, text='数据源...', command=self._on_source_config)
        self.btn_source_cfg.pack(side='left', padx=(0, 8))
        self.chk_reverse_cover = ttk.Checkbutton(
            opts_misc, text='单曲反哺封面', variable=self.var_reverse_cover_cascade, command=self._on_reverse_cover_cascade_clicked,
        )
        self.chk_reverse_cover.pack(side='left', padx=(0, 8))
        # 兼容性/测试保留实例（移入系统菜单栏「视图与过滤」，不占界面横向空间）
        self.chk_skip_full = ttk.Checkbutton(opts_misc, text='默认跳过整轨全长', variable=self.var_skip_full, command=self._on_option_changed)

        # 底部固定进度与状态栏（先按 side='bottom' 布局，确保窗口较矮时不被分割区挤压隐藏）
        bottom_bar = ttk.Frame(outer)
        bottom_bar.pack(side='bottom', fill='x', pady=(6, 0))
        self.lbl_status = ttk.Label(bottom_bar, textvariable=self.var_status, anchor='w', foreground='#475569')
        self.lbl_status.pack(side='bottom', fill='x')
        self.progress = ttk.Progressbar(bottom_bar, mode='determinate', maximum=100)

        # 主分割区：工作区 (表格 + 元数据详情) + 运行日志
        self.paned_main = ttk.PanedWindow(outer, orient='vertical')
        self.paned_main.pack(fill='both', expand=True)
        self.paned = self.paned_main  # 兼容引用

        # 工作区动态可拖拽分栏容器 (使用 tk.PanedWindow 支持原生态水平/垂直方向动态切换与拖拽)
        self.work_paned = tk.PanedWindow(
            self.paned_main,
            orient='vertical',
            sashwidth=4,
            sashrelief='groove',
            bg='#cbd5e1',
            bd=0,
        )
        self.paned_main.add(self.work_paned, weight=5)
        self.work_area = self.work_paned  # 保持向下兼容引用

        self.tbl_frame = ttk.Frame(self.work_paned)

        # 搜索与过滤工具栏
        search_bar = ttk.Frame(self.tbl_frame, padding=(0, 0, 0, 6))
        search_bar.grid(row=0, column=0, columnspan=2, sticky='ew')

        ttk.Label(search_bar, text='搜索类型:', font=ui_font(9)).pack(side='left', padx=(0, 4))
        self.cmb_search_type = ttk.Combobox(
            search_bar, textvariable=self.var_search_type,
            values=list(self._SEARCH_TYPES), state='readonly', width=10,
        )
        self.cmb_search_type.pack(side='left', padx=(0, 8))
        self.cmb_search_type.bind('<<ComboboxSelected>>', lambda _e: self._on_search_type_changed())

        ttk.Label(search_bar, text='关键字:', font=ui_font(9)).pack(side='left', padx=(0, 4))
        self.ent_search = ttk.Entry(
            search_bar,
            textvariable=self.var_search,
            width=24,
            font=ui_font(9),
        )
        self.ent_search.pack(side='left', padx=(0, 6))
        self.ent_search.bind('<KeyRelease-Return>', self._on_search_return)
        self.ent_search.bind('<Return>', lambda _e: 'break' if self._busy else None)
        self.ent_search.bind('<KeyRelease>', self._on_entry_key_release)
        self.ent_search.bind('<Escape>', lambda _e: self._on_clear_search())
        self.ent_search.bind('<Button-1>', self._on_search_button1, add='+')
        self.ent_search.bind('<Button-2>', lambda _e: 'break' if self._busy else None, add='+')
        self.ent_search.bind('<Button-3>', lambda _e: 'break' if self._busy else None, add='+')
        self.ent_search.bind('<KeyPress>', lambda _e: 'break' if self._busy else None, add='+')
        self.ent_search.bind('<FocusIn>', self._on_search_focus_in, add='+')
        self.var_search.trace_add('write', self._on_search_query_changed)

        self.btn_search = ttk.Button(search_bar, text='搜索', width=6, command=self._apply_search_immediate)
        self.btn_search.pack(side='left', padx=(0, 4))

        self.btn_clear_search = ttk.Button(search_bar, text='清空', width=6, command=self._on_clear_search)
        self.btn_clear_search.pack(side='left', padx=(0, 10))

        self.lbl_search_info = ttk.Label(search_bar, textvariable=self.var_search_info, foreground='#475569', font=ui_font(9))
        self.lbl_search_info.pack(side='left')

        # 表格选择快捷控制与提示
        self.btn_toggle_all = ttk.Button(search_bar, text='全选 / 取消', command=self._toggle_select_all)
        self.btn_toggle_all.pack(side='right', padx=(8, 0))

        self.chk_only_pending = ttk.Checkbutton(
            search_bar, text='只显示待处理曲目', variable=self.var_only_pending, command=self._refresh_table,
        )
        self.chk_only_pending.pack(side='right', padx=(10, 8))

        self.lbl_search_hint = ttk.Label(
            search_bar, text='快捷键: Ctrl+F 聚焦 / Esc 清空',
            foreground='#94a3b8', font=ui_font(8),
        )
        self.lbl_search_hint.pack(side='right')

        self.tree = ttk.Treeview(self.tbl_frame, columns=self._COLS, show='headings', selectmode='extended')
        for col, head, width, min_w in zip(self._COLS, self._HEADS, self._WIDTHS, self._MIN_WIDTHS):
            if col == 'chk':
                self.tree.heading(col, text=head, command=self._toggle_select_all)
            else:
                self.tree.heading(col, text=head, command=lambda c=col: self._on_heading_click(c))
            self.tree.column(
                col,
                width=width,
                minwidth=min_w,
                anchor='center' if col in ('chk', 'status', 'track', 'cover') else 'w',
                stretch=(col in ('title', 'artist', 'album', 'note')),
            )
        for tag, fg in (('err', '#dc2626'), ('warn', '#b45309'), ('info', '#2563eb'), ('ok', '#15803d'), ('muted', '#64748b')):
            self.tree.tag_configure(tag, foreground=fg)

        vsb = ttk.Scrollbar(self.tbl_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(self.tbl_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=1, column=0, sticky='nsew')
        vsb.grid(row=1, column=1, sticky='ns')
        hsb.grid(row=2, column=0, sticky='ew')
        self.tbl_frame.rowconfigure(0, weight=0)
        self.tbl_frame.rowconfigure(1, weight=1)
        self.tbl_frame.rowconfigure(2, weight=0)
        self.tbl_frame.columnconfigure(0, weight=1)
        self.tbl_frame.columnconfigure(1, weight=0)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Button-3>', self._on_tree_right_click)
        self.tree.bind('<Double-1>', self._on_tree_double_click)
        self.tree.bind('<space>', self._on_tree_space)
        self.tree.bind('<<TreeviewSelect>>', lambda _e: self._on_tree_selection_changed())

        # 挂载元数据详情展示组件 (直接作为 work_paned 的子组件，绝无层级覆盖问题)
        self.detail_widget = TrackDetailWidget(
            self.work_paned,
            orient='horizontal',
            on_pick_cover=self._on_pick_cover,
            on_reveal_file=reveal_in_file_manager,
        )

        # 底部原汁原味黑色运行日志面板 (完全独立)
        self.log_frame = ttk.Frame(self.paned_main)
        self.log_box = tk.Text(
            self.log_frame, height=6, bg='#0f172a', fg='#e2e8f0', relief='flat',
            font=ui_font(9, mono=True), state='disabled', wrap='word',
        )
        l_sb = ttk.Scrollbar(self.log_frame, orient='vertical', command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=l_sb.set)
        self.log_box.pack(side='left', fill='both', expand=True)
        l_sb.pack(side='right', fill='y')
        self.paned_main.add(self.log_frame, weight=1)

        # 初始化并应用布局形态
        self.h_paned = None
        self._apply_layout_mode(self.layout_mode)

        self._setup_shortcuts()

    def _setup_menu_bar(self):
        menubar = tk.Menu(self.root)
        self.root.configure(menu=menubar)

        # 1. 文件/媒体库 (F)
        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label='🔄 重新扫描媒体库', accelerator='Ctrl+R', command=self.start_scan)
        file_menu.add_separator()
        file_menu.add_command(label='📂 选择工作目录...', accelerator='Ctrl+O', command=self._pick_custom_folder)
        file_menu.add_command(label='📁 管理自定义目录...', command=self._manage_custom_folders)
        file_menu.add_command(label='🏠 恢复默认播放器媒体库', command=self._reset_to_library)
        file_menu.add_separator()
        file_menu.add_command(label='⚡ 仅刷新播放器缓存', accelerator='F6', command=self._on_sync_only)
        file_menu.add_separator()
        file_menu.add_command(label='❌ 退出', accelerator='Alt+F4', command=self._on_close)
        menubar.add_cascade(label='文件 / 媒体库 (F)', menu=file_menu, underline=9)

        # 2. 编辑与挑选 (E)
        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label='🚀 一键修复已选项目', accelerator='Ctrl+Enter', command=self._on_one_click)
        edit_menu.add_command(label='⏹ 停止当前任务', accelerator='Esc', command=self._on_stop)
        edit_menu.add_separator()
        edit_menu.add_command(label='☑️ 全选曲目', accelerator='Ctrl+A', command=lambda: self._set_all_selection(True))
        edit_menu.add_command(label='◻️ 取消全选', accelerator='Ctrl+D', command=lambda: self._set_all_selection(False))
        edit_menu.add_command(label='🔄 反向选择', accelerator='Ctrl+I', command=self._invert_selection)
        edit_menu.add_separator()
        edit_menu.add_command(label='🖼️ 挑选专辑封面 (cover.jpg)...', accelerator='Ctrl+Shift+C', command=self._on_pick_cover)
        edit_menu.add_command(label='👤 挑选歌手写真...', accelerator='Ctrl+Shift+A', command=self._on_pick_artist)
        edit_menu.add_command(label='🎵 挑选/更换歌词 (lrc)...', accelerator='Ctrl+Shift+L', command=self._on_pick_lyric)
        edit_menu.add_separator()
        edit_menu.add_command(label='✏️ 批量设置标签...', accelerator='Ctrl+E', command=self._on_set_metadata)
        menubar.add_cascade(label='编辑与挑选 (E)', menu=edit_menu, underline=6)

        # 3. 音频工具 (T)
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label='⚡ SACD 镜像提取...', command=self._on_sacd_extract)
        tools_menu.add_command(label='🎼 音频格式转换...', command=self._on_convert_format)
        tools_menu.add_separator()
        tools_menu.add_command(label='🌐 简繁汉字转换...', accelerator='Ctrl+T', command=self._on_convert_zh)
        tools_menu.add_command(label='🧹 删除部分元数据...', command=self._on_delete_metadata)
        tools_menu.add_separator()
        tools_menu.add_command(label='⚙️ 配置元数据音源...', command=self._on_source_config)
        menubar.add_cascade(label='音频工具 (T)', menu=tools_menu, underline=5)

        # 4. 视图与过滤 (V)
        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label='🔍 快速聚焦搜索', accelerator='Ctrl+F', command=self._on_ctrl_f)
        view_menu.add_command(label='✖️ 清空搜索与过滤', command=self._on_clear_search)
        view_menu.add_separator()
        view_menu.add_checkbutton(label='只显示待处理曲目', variable=self.var_only_pending, command=self._refresh_table)
        view_menu.add_checkbutton(label='默认跳过整轨全长文件', variable=self.var_skip_full, command=self._on_option_changed)
        view_menu.add_separator()
        view_menu.add_radiobutton(label='底部元数据面板 (默认)', variable=self.var_layout_mode, value='bottom', command=lambda: self._apply_layout_mode('bottom'))
        view_menu.add_radiobutton(label='右侧元数据面板', variable=self.var_layout_mode, value='right', command=lambda: self._apply_layout_mode('right'))
        view_menu.add_radiobutton(label='隐藏元数据面板', variable=self.var_layout_mode, value='hidden', command=lambda: self._apply_layout_mode('hidden'))
        view_menu.add_separator()
        view_menu.add_command(label='📊 刷新曲目状态列表', accelerator='F5', command=self._refresh_table)
        menubar.add_cascade(label='视图与过滤 (V)', menu=view_menu, underline=6)

        # 5. 帮助 (H)
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label='⌨️ 快捷键速查表...', command=self._show_shortcuts_dialog)
        help_menu.add_separator()
        help_menu.add_command(label='ℹ️ 关于 MusicMetadata...', command=self._show_about_dialog)
        menubar.add_cascade(label='帮助 (H)', menu=help_menu, underline=3)

        self.menubar = menubar

    def _setup_shortcuts(self):
        self.root.bind('<Control-f>', self._on_ctrl_f)
        self.root.bind('<Control-F>', self._on_ctrl_f)
        self.root.bind('<Control-r>', lambda _e: self.start_scan())
        self.root.bind('<Control-R>', lambda _e: self.start_scan())
        self.root.bind('<F5>', lambda _e: self.start_scan())
        self.root.bind('<Control-o>', lambda _e: self._pick_custom_folder())
        self.root.bind('<Control-O>', lambda _e: self._pick_custom_folder())
        self.root.bind('<F6>', lambda _e: self._on_sync_only())
        self.root.bind('<Control-Return>', lambda _e: self._on_one_click())
        self.root.bind('<Control-a>', self._on_shortcut_select_all)
        self.root.bind('<Control-A>', self._on_shortcut_select_all)
        self.root.bind('<Control-d>', lambda _e: self._set_all_selection(False))
        self.root.bind('<Control-D>', lambda _e: self._set_all_selection(False))
        self.root.bind('<Control-i>', lambda _e: self._invert_selection())
        self.root.bind('<Control-I>', lambda _e: self._invert_selection())
        self.root.bind('<Control-Shift-C>', lambda _e: self._on_pick_cover())
        self.root.bind('<Control-Shift-c>', lambda _e: self._on_pick_cover())
        self.root.bind('<Control-Shift-A>', lambda _e: self._on_pick_artist())
        self.root.bind('<Control-Shift-a>', lambda _e: self._on_pick_artist())
        self.root.bind('<Control-Shift-L>', lambda _e: self._on_pick_lyric())
        self.root.bind('<Control-Shift-l>', lambda _e: self._on_pick_lyric())
        self.root.bind('<Control-e>', self._on_shortcut_set_meta)
        self.root.bind('<Control-E>', self._on_shortcut_set_meta)
        self.root.bind('<Control-t>', self._on_shortcut_convert_zh)
        self.root.bind('<Control-T>', self._on_shortcut_convert_zh)
        self.root.bind('<Control-p>', self._on_toggle_detail_panel)
        self.root.bind('<Control-P>', self._on_toggle_detail_panel)

    def _apply_layout_mode(self, mode: str):
        if mode not in ('bottom', 'right', 'hidden'):
            mode = 'bottom'
        self.layout_mode = mode
        if hasattr(self, 'var_layout_mode'):
            self.var_layout_mode.set(mode)
        self._user_config['layout_mode'] = mode
        self._save_user_state()

        if mode == 'hidden':
            try:
                self.work_paned.forget(self.detail_widget)
            except Exception:
                pass
            panes = [str(p) for p in self.work_paned.panes()]
            if str(self.tbl_frame) not in panes:
                self.work_paned.add(self.tbl_frame, stretch='always')
            else:
                self.work_paned.paneconfigure(self.tbl_frame, stretch='always')
        elif mode == 'bottom':
            self.detail_widget.set_orientation('horizontal')
            self.work_paned.configure(orient='vertical')
            panes = [str(p) for p in self.work_paned.panes()]
            if str(self.tbl_frame) not in panes:
                self.work_paned.add(self.tbl_frame, stretch='always')
            else:
                self.work_paned.paneconfigure(self.tbl_frame, stretch='always')
            if str(self.detail_widget) not in panes:
                self.work_paned.add(self.detail_widget, stretch='never', height=135)
            else:
                self.work_paned.paneconfigure(self.detail_widget, stretch='never', height=135, width=0)
        elif mode == 'right':
            self.detail_widget.set_orientation('vertical')
            self.work_paned.configure(orient='horizontal')
            panes = [str(p) for p in self.work_paned.panes()]
            if str(self.tbl_frame) not in panes:
                self.work_paned.add(self.tbl_frame, stretch='always')
            else:
                self.work_paned.paneconfigure(self.tbl_frame, stretch='always')
            if str(self.detail_widget) not in panes:
                self.work_paned.add(self.detail_widget, stretch='never', width=285)
            else:
                self.work_paned.paneconfigure(self.detail_widget, stretch='never', width=285, height=0)

        self._on_tree_selection_changed()

    def _on_tree_selection_changed(self, event=None):
        if not hasattr(self, 'detail_widget') or self.layout_mode == 'hidden':
            return
        selected_iids = self.tree.selection()
        if not selected_iids:
            self.detail_widget.set_empty()
            return
        tracks_map = {t.path: t for t in self.tracks}
        selected_tracks = [tracks_map[iid] for iid in selected_iids if iid in tracks_map]
        if selected_tracks:
            self.detail_widget.update_selection(selected_tracks)
        else:
            self.detail_widget.set_empty()

    def _on_toggle_detail_panel(self, event=None):
        next_mode = 'hidden' if self.layout_mode != 'hidden' else 'bottom'
        self._apply_layout_mode(next_mode)
        return 'break'

    def _set_all_selection(self, checked: bool):
        if self._busy or not self.visible_tracks:
            return
        if checked:
            for tr in self.visible_tracks:
                self.checked_paths.add(tr.path)
        else:
            for tr in self.visible_tracks:
                self.checked_paths.discard(tr.path)
        self._refresh_table()

    def _invert_selection(self):
        if self._busy or not self.visible_tracks:
            return
        for tr in self.visible_tracks:
            if tr.path in self.checked_paths:
                self.checked_paths.discard(tr.path)
            else:
                self.checked_paths.add(tr.path)
        self._refresh_table()

    def _on_shortcut_select_all(self, event=None):
        widget = self.root.focus_get()
        if isinstance(widget, (tk.Entry, ttk.Entry)):
            widget.select_range(0, 'end')
            widget.icursor('end')
            return 'break'
        if isinstance(widget, tk.Text):
            widget.tag_add('sel', '1.0', 'end')
            return 'break'
        self._set_all_selection(True)
        return 'break'

    def _on_shortcut_set_meta(self, event=None):
        widget = self.root.focus_get()
        if isinstance(widget, (tk.Entry, ttk.Entry, tk.Text)):
            return None
        self._on_set_metadata()
        return 'break'

    def _on_shortcut_convert_zh(self, event=None):
        widget = self.root.focus_get()
        if isinstance(widget, (tk.Entry, ttk.Entry, tk.Text)):
            return None
        self._on_convert_zh()
        return 'break'

    def _show_shortcuts_dialog(self):
        text = (
            "【全局与媒体库】\n"
            "  Ctrl + R / F5      重新扫描媒体库\n"
            "  Ctrl + O           选择工作目录\n"
            "  F6                 仅刷新播放器缓存\n"
            "  Ctrl + Enter       执行一键修复\n"
            "  Esc                清空搜索 / 停止任务\n\n"
            "【曲目选择】\n"
            "  Ctrl + A           全选曲目 (输入框内全选文本)\n"
            "  Ctrl + D           取消全选\n"
            "  Ctrl + I           反向选择\n"
            "  Space (空格键)     切换当前选定曲目勾选\n\n"
            "【快捷精修】\n"
            "  Ctrl + Shift + C   挑选专辑封面\n"
            "  Ctrl + Shift + A   挑选歌手写真\n"
            "  Ctrl + Shift + L   挑选/更换歌词\n"
            "  Ctrl + E           批量设置标签\n"
            "  Ctrl + T           简繁汉字转换\n"
            "  Ctrl + P           切换元数据详情面板显示/隐藏\n"
            "  Ctrl + F           快速聚焦搜索框"
        )
        _msgbox.showinfo('快捷键速查', text)

    def _show_about_dialog(self):
        _msgbox.showinfo(
            '关于 MusicMetadata',
            "MusicMetadata 音乐元数据整理工具\n\n"
            "专为高品质音乐库打造，支持封面自动修复与高清挑选、\n"
            "多音源歌词与艺术家写真补全、SACD 提取分割、标签清洗及播放器缓存同步。\n\n"
            "兼容 MusicBee / foobar2000 / Roon 媒体库生态。"
        )

    def _set_all_fields(self, checked: bool):
        if self._busy:
            return
        for var in self.field_vars.values():
            var.set(checked)
        self._on_option_changed()

    def _current_options(self, sync_only: bool = False) -> FixOptions:
        any_tag = any(
            v.get() for v in (
                self.var_date, self.var_albumartist, self.var_artist,
                self.var_composer, self.var_lyricist, self.var_arranger,
                self.var_genre, self.var_disc,
            )
        )
        any_extra = any(v.get() for v in (self.var_composer, self.var_lyricist, self.var_arranger, self.var_genre, self.var_disc))
        preset = self.var_prefer_preset.get()
        prefer = self._custom_prefer_sources if preset == '自定义' else self._PREFER_PRESETS.get(preset, ())
        return FixOptions(
            enable_track=self.var_track.get(),
            enable_art=self.var_art.get(),
            enable_artist_art=self.var_artist_art.get(),
            enable_tags=any_tag,
            enable_extra_tags=any_extra,
            enable_date=self.var_date.get(),
            enable_albumartist=self.var_albumartist.get(),
            enable_artist=self.var_artist.get(),
            enable_composer=self.var_composer.get(),
            enable_lyricist=self.var_lyricist.get(),
            enable_arranger=self.var_arranger.get(),
            enable_genre=self.var_genre.get(),
            enable_disc=self.var_disc.get(),
            enable_lyrics=self.var_lyrics.get(),
            lyrics_mode=self.var_lyrics_mode.get() or 'auto',
            prefer_sources=prefer,
            only_sources=self._only_sources,
            skip_sources=self._skip_sources,
            netease_api=self._netease_api or None,
            spotify_client_id=self._spotify_client_id or None,
            spotify_client_secret=self._spotify_client_secret or None,
            tadb_api_key=self._tadb_api_key or None,
            fanart_api_key=self._fanart_api_key or None,
            fanart_client_key=self._fanart_client_key or None,
            discogs_token=self._discogs_token or None,
            sync_musicbee_artist=self.var_sync_mb_artist.get(),
            musicbee_artist_dir=getattr(self.env, 'artist_pictures_dir', None),
            enable_sync=self.var_sync.get(),
            sync_only=sync_only,
            enable_reverse_cover_cascade=self.var_reverse_cover_cascade.get(),
        )

    def _log(self, text: str):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', f"[{time.strftime('%H:%M:%S')}] {text}\n")
        if int(self.log_box.index('end-1c').split('.')[0]) > 1500:
            self.log_box.delete('1.0', '200.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def _on_tkinter_exception(self, exc, val, tb):
        """捕获未处理的 Tkinter 回调异常，输出至主界面日志框并弹出友好提示。"""
        err_msg = ''.join(traceback.format_exception(exc, val, tb)).strip()
        self._log(f'[界面异常] {val}\n{err_msg}')
        try:
            _msgbox.showerror('执行异常', f'操作执行异常：\n{val}\n\n详细信息已记录至下方日志区。')
        except Exception:
            pass

    def _set_busy(self, busy: bool, text: str = ''):
        self._busy = busy
        state = 'disabled' if busy else 'normal'
        self.btn_one_click.configure(state=state, bg='#93c5fd' if busy else '#2563eb')
        for b in (
            self.btn_sync_only, self.btn_set_meta, self.btn_pick_cover, self.btn_pick_artist, self.btn_pick_lyric, self.btn_delete_meta,
            self.btn_convert, self.btn_zh_convert, self.btn_sacd, self.btn_tools_menu, self.btn_rescan, self.btn_pick_dir,
            self.btn_manage_dirs, self.btn_reset_lib, self.btn_toggle_all,
            self.btn_fields_all, self.btn_fields_none,
            self.btn_search, self.btn_clear_search,
            self.chk_sync, self.chk_artist_art, self.chk_sync_mb_artist, self.chk_lyrics, self.btn_source_cfg,
            self.chk_skip_full, self.chk_only_pending,
            *self.field_buttons.values(),
        ):
            try:
                b.configure(state=state)
            except Exception:
                pass
        if hasattr(self, 'menubar') and self.menubar:
            m_state = 'disabled' if busy else 'normal'
            for label in ('文件 / 媒体库 (F)', '编辑与挑选 (E)', '音频工具 (T)'):
                try:
                    self.menubar.entryconfig(label, state=m_state)
                except Exception:
                    pass
        for cmb in (self.cmb_lyrics_mode, self.cmb_prefer_source, self.cmb_search_type):
            cmb.configure(state='disabled' if busy else 'readonly')
        if hasattr(self, 'ent_search') and self.ent_search.winfo_exists():
            if busy:
                try:
                    self.ent_search.selection_clear()
                except Exception:
                    pass
                self.ent_search.configure(
                    state='disabled',
                    cursor='arrow',
                    takefocus=0,
                )
            else:
                self.ent_search.configure(
                    state='normal',
                    cursor='',
                    takefocus=1,
                )
        if busy:
            if hasattr(self, '_search_after_id') and self._search_after_id:
                try:
                    self.root.after_cancel(self._search_after_id)
                except Exception:
                    pass
                self._search_after_id = None
            try:
                if hasattr(self, 'tree') and self.tree.winfo_exists():
                    self.tree.focus_set()
                elif hasattr(self, 'root') and self.root.winfo_exists():
                    self.root.focus_set()
            except Exception:
                pass
        if text:
            self.var_status.set(text)

    def _show_progress(self, show: bool, maximum: int = 100, value: int = 0):
        if show:
            if self.progress.winfo_manager() != 'pack':
                self.progress.pack(side='top', fill='x', pady=(0, 4), before=self.lbl_status)
            if maximum <= 0:
                if not self._prog_indet:
                    self.progress.configure(mode='indeterminate', maximum=100, value=0)
                    self.progress.start(14)
                    self._prog_indet = True
            else:
                if self._prog_indet:
                    self.progress.stop()
                    self._prog_indet = False
                self.progress.configure(mode='determinate', maximum=maximum, value=value)
        else:
            if self._prog_indet:
                self.progress.stop()
                self._prog_indet = False
            if self.progress.winfo_manager() == 'pack':
                self.progress.pack_forget()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._ev_q.get_nowait()
                if kind == 'scan_prog':
                    idx, tot, fp = payload
                    if tot <= 0:
                        self._show_progress(True, 0, 0)
                        name = os.path.basename(fp) or fp
                        self.var_status.set(f'扫描目录 ({idx} 个音频): {name}' if idx else f'扫描目录: {name}')
                    else:
                        self._show_progress(True, tot, idx)
                        self.var_status.set(f'扫描检查 ({idx}/{tot}): {os.path.basename(fp)}')
                elif kind in self._PROG_LABELS:
                    idx, tot, name = payload
                    self._show_progress(True, tot, idx)
                    self.var_status.set(f'{self._PROG_LABELS[kind]} ({idx}/{tot}): {name}')
                elif kind in self._ev_handlers:
                    self._ev_handlers[kind](payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(40, self._poll_queue)

    def _on_close(self):
        if hasattr(self, '_search_after_id') and self._search_after_id:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None
        try:
            self._save_user_state()
        except Exception:
            pass
        self.root.destroy()

    def _on_reverse_cover_cascade_clicked(self):
        if self.var_reverse_cover_cascade.get():
            msg = (
                "【高置信度反向封面反哺 功能说明与风险提示】\n\n"
                "● 作用机制：\n"
                "在一键修复全自动批处理时，若单曲原始音频缺失确切专辑名，"
                "系统将根据歌词与物理时长（Score >= 180.0）智能锁定的正规录音室大碟，"
                "自动为其生成同目录下的 cover.jpg。\n\n"
                "● 发烧友安全忠告：\n"
                "若您的音乐文件为特定首版黑胶或 SACD 珍藏版本，对大碟版本有极其严苛的手工要求，"
                "推荐保持关闭，改为在「挑选与切换歌词」或「挑选封面」弹窗中肉眼确认后联动写入。\n\n"
                "是否确认开启此项全自动封面反哺？"
            )
            confirmed = _msgbox.askyesno("开启单曲高分反哺封面警告", msg, icon='warning', parent=self.root)
            if not confirmed:
                self.var_reverse_cover_cascade.set(False)
                return
        self._save_user_state()
        self._on_option_changed()

    def _save_user_state(self):
        cfg = dict(self._user_config)
        cfg['custom_folders'] = list(self.custom_folders or [])
        cfg['enable_artist_art'] = bool(self.var_artist_art.get())
        cfg['enable_reverse_cover_cascade'] = bool(self.var_reverse_cover_cascade.get())
        cfg['enable_composer'] = bool(self.var_composer.get())
        cfg['enable_lyricist'] = bool(self.var_lyricist.get())
        cfg['enable_arranger'] = bool(self.var_arranger.get())
        cfg['enable_lyrics'] = bool(self.var_lyrics.get())
        cfg['sync_musicbee_artist'] = bool(self.var_sync_mb_artist.get())
        cfg['lyrics_mode'] = self.var_lyrics_mode.get() or 'auto'
        cfg['prefer_preset'] = self.var_prefer_preset.get() or '默认(免梯直连)'
        if hasattr(self, 'var_search_type'):
            cfg['search_type'] = self.var_search_type.get() or '全部'
        cfg['custom_prefer_sources'] = list(self._custom_prefer_sources)
        cfg['only_sources'] = list(self._only_sources)
        cfg['skip_sources'] = list(self._skip_sources)
        cfg['netease_api'] = self._netease_api
        cfg['tadb_api_key'] = self._tadb_api_key
        cfg['fanart_api_key'] = self._fanart_api_key
        cfg['fanart_client_key'] = self._fanart_client_key
        cfg['discogs_token'] = self._discogs_token
        if self.root.winfo_exists():
            try:
                cfg['window_geometry'] = self.root.geometry()
            except Exception:
                pass
        self._user_config = cfg
        save_user_config(cfg)

    def _init_player_mode(self):
        """启动时轻量检测当前运行中的播放器进程（零 IO 开销，耗时<15ms），秒级自适应首屏控件。"""
        try:
            active_type = ''
            if get_running_musicbee_exes():
                active_type = PLAYER_TYPE_MUSICBEE
            elif get_running_roon_exes():
                active_type = PLAYER_TYPE_ROON
            elif get_running_foobar_exes():
                active_type = PLAYER_TYPE_FOOBAR

            if active_type:
                self._apply_player_mode(True, active_type)
                p_label = 'MusicBee' if active_type == PLAYER_TYPE_MUSICBEE else ('Roon' if active_type == PLAYER_TYPE_ROON else 'foobar2000')
                if self.custom_folders:
                    self.var_env.set(f'已识别 {p_label}，已加载上次保存的 {len(self.custom_folders)} 个目录；点击「扫描媒体库」开始检查。')
                else:
                    self.var_env.set(f'已就绪：已检测到运行中的 {p_label}；点击下方「扫描媒体库」开始检查。')
            else:
                self._apply_player_mode(False)
                if self.custom_folders:
                    self.var_env.set(f'已加载上次保存的 {len(self.custom_folders)} 个目录；点击「扫描媒体库」开始检查。')
                else:
                    self.var_env.set('未自动扫描音乐目录；请点击下方「扫描媒体库」或右上角「选择目录...」开始扫描。')
        except Exception:
            pass

    def _apply_player_mode(self, is_active: bool, player_type: str = PLAYER_TYPE_FOOBAR):
        if is_active:
            if player_type == PLAYER_TYPE_MUSICBEE:
                p_label = 'MusicBee'
            elif player_type == PLAYER_TYPE_ROON:
                p_label = 'Roon'
            else:
                p_label = 'foobar'
            self.btn_sync_only.configure(text=f'仅刷新 {p_label} 缓存')
            self.chk_sync.configure(text=f'刷新 {p_label} 缓存')
            if self.btn_sync_only.winfo_manager() != 'pack':
                self.btn_sync_only.pack(side='left', padx=(0, 6), before=self.sep_act1)
            if self.chk_sync.winfo_manager() != 'pack':
                self.chk_sync.pack(side='left', padx=(0, 8), before=self.chk_artist_art)
            if player_type == PLAYER_TYPE_MUSICBEE:
                self._base_headings['track'] = '音轨号'
                self._base_headings['tagsync'] = '标签/监控检查'
                if self.chk_sync_mb_artist.winfo_manager() != 'pack':
                    self.chk_sync_mb_artist.pack(side='left', padx=(0, 8), after=self.chk_artist_art)
            elif player_type == PLAYER_TYPE_ROON:
                self._base_headings['track'] = '音轨号'
                self._base_headings['tagsync'] = '标签/Roon监控检查'
                if self.chk_sync_mb_artist.winfo_manager() == 'pack':
                    self.chk_sync_mb_artist.pack_forget()
            else:
                self._base_headings['track'] = '音轨号'
                self._base_headings['tagsync'] = '缓存/标签检查'
                if self.chk_sync_mb_artist.winfo_manager() == 'pack':
                    self.chk_sync_mb_artist.pack_forget()
        else:
            if self.btn_sync_only.winfo_manager() == 'pack':
                self.btn_sync_only.pack_forget()
            if self.chk_sync.winfo_manager() == 'pack':
                self.chk_sync.pack_forget()
            if self.chk_sync_mb_artist.winfo_manager() == 'pack':
                self.chk_sync_mb_artist.pack_forget()
            self._base_headings['track'] = '音轨号'
            self._base_headings['tagsync'] = '标签检查'
        self._update_headings()

        if self.custom_folders and self.env.library_folders:
            if self.btn_reset_lib.winfo_manager() != 'pack':
                self.btn_reset_lib.pack(side='right', padx=(6, 0), before=self.lbl_env)
        elif self.btn_reset_lib.winfo_manager() == 'pack':
            self.btn_reset_lib.pack_forget()

    def _pick_custom_folder(self):
        if self._busy:
            return
        folder = filedialog.askdirectory(title='选择要扫描的音乐文件夹')
        if folder:
            self.custom_folders = [os.path.abspath(folder)]
            self._save_user_state()
            self.start_scan()

    def _manage_custom_folders(self):
        if self._busy:
            return
        dlg = FolderManagerDialog(self.root, self.custom_folders or [])
        self._last_folder_dialog = dlg
        self.root.wait_window(dlg.top)
        if dlg.result is None:
            return
        self.custom_folders = dlg.result or None
        self._save_user_state()
        self.start_scan()

    def _reset_to_library(self):
        if self._busy:
            return
        self.custom_folders = None
        self._save_user_state()
        if self.btn_reset_lib.winfo_manager() == 'pack':
            self.btn_reset_lib.pack_forget()
        self.start_scan()

    def start_scan(self):
        if self._busy:
            return
        self._set_busy(True, '正在扫描音乐目录、封面与标签元数据...')
        self._show_progress(True, 0, 0)
        custom = list(self.custom_folders) if self.custom_folders else None

        def _worker():
            try:
                env = _detect_player_env()
                fallback_folders: list[str] | None = None
                scan_folders = custom
                if scan_folders is None and not env.library_folders:
                    fallback_folders = _get_default_music_folders() or None
                    scan_folders = fallback_folders
                tracks = _scan_library(
                    env,
                    folders=scan_folders,
                    progress_cb=lambda i, t, f: self._ev_q.put(('scan_prog', (i, t, f))),
                    include_art_scan=True,
                )
                self._ev_q.put(('scan_done', (env, tracks, fallback_folders)))
            except Exception as exc:
                import traceback
                self._ev_q.put(('log', f'扫描发生异常: {exc}\n{traceback.format_exc()}'))
                self._ev_q.put(('scan_done', (_detect_player_env(), [], None)))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_player_state_label(self, fallback_folders: list[str] | None = None):
        env = self.env
        if not env:
            return
        is_active = is_player_active(env)
        is_mb = getattr(env, 'player_type', None) == PLAYER_TYPE_MUSICBEE
        is_roon = getattr(env, 'player_type', None) == PLAYER_TYPE_ROON
        is_fb = getattr(env, 'player_type', None) == PLAYER_TYPE_FOOBAR
        fallback_folders = fallback_folders or getattr(self, '_last_fallback_folders', None)

        raw_paths: list[str] = []
        if self.custom_folders:
            n_dirs = len(self.custom_folders)
            prefix = f'当前扫描自定义目录（共 {n_dirs} 个目录）' if is_active else f'当前扫描目录（共 {n_dirs} 个目录）'
            self.var_env.set(prefix)
            raw_paths = list(self.custom_folders)
        elif is_mb:
            mb_state = '运行中' if get_running_musicbee_exes() else '未运行'
            art_desc = '，已连接写真库' if getattr(env, 'artist_pictures_dir', '') else ''
            if env.library_folders or env.unavailable_folders:
                n_dirs = len(env.library_folders)
                wdesc = f'，{len(env.unavailable_folders)} 个不可访问' if env.unavailable_folders else ''
                self.var_env.set(f'已识别 MusicBee 媒体库（{n_dirs} 个监控目录，MusicBee {mb_state}{art_desc}{wdesc}）')
                raw_paths = list(env.library_folders) + list(env.unavailable_folders)
            elif fallback_folders:
                self.var_env.set(f'已识别 MusicBee 播放器（MusicBee {mb_state}{art_desc}，未读取到监控目录，已自动扫描系统音乐目录）')
                raw_paths = list(fallback_folders)
            else:
                self.var_env.set(f'已识别 MusicBee 播放器（MusicBee {mb_state}{art_desc}，未读取到监控目录，请点击右上角「选择目录...」指定文件夹）。')
        elif is_roon:
            roon_state = '运行中' if get_running_roon_exes() else '未运行'
            if env.library_folders:
                self.var_env.set(f'已识别 Roon 音乐系统（{len(env.library_folders)} 个目录，Roon {roon_state}）')
                raw_paths = list(env.library_folders)
            elif fallback_folders:
                self.var_env.set(f'已识别 Roon 音乐系统（Roon {roon_state}，未读取到监控目录，已自动扫描系统音乐目录）')
                raw_paths = list(fallback_folders)
            else:
                fdesc = env.config_dir or '已连接 Roon 宿主'
                self.var_env.set(f'已识别 Roon 音乐系统（Roon {roon_state}）')
                raw_paths = [fdesc] if fdesc else []
        elif is_fb:
            fb_state = '运行中' if get_running_foobar_exes() else '未运行'
            if env.library_folders or env.unavailable_folders:
                n_dirs = len(env.library_folders)
                wdesc = f'，{len(env.unavailable_folders)} 个不可访问' if env.unavailable_folders else ''
                self.var_env.set(f'已识别 foobar2000 媒体库（{n_dirs} 个目录，foobar {fb_state}{wdesc}）')
                raw_paths = list(env.library_folders) + list(env.unavailable_folders)
            elif fallback_folders:
                self.var_env.set(f'已识别 foobar2000（foobar {fb_state}，未读取到媒体库目录，已自动扫描系统音乐目录）')
                raw_paths = list(fallback_folders)
            else:
                self.var_env.set(f'已识别 foobar2000（foobar {fb_state}，未读取到媒体库目录，请点击右上角「选择目录...」指定文件夹）。')
        elif fallback_folders:
            self.var_env.set('未检测到外部音乐播放器，已自动扫描系统音乐目录')
            raw_paths = list(fallback_folders)
        else:
            self.var_env.set('未检测到外部音乐播放器或默认音乐目录，请点击右上角「选择目录...」或「管理目录...」指定文件夹。')

        self._current_library_paths = raw_paths
        if raw_paths:
            path_str = '；'.join(raw_paths)
            self.var_paths.set(f'📂 监控路径: {path_str}')
            tip_lines = [f'当前监控/扫描路径 (共 {len(raw_paths)} 个):']
            for idx, p in enumerate(raw_paths, 1):
                tip_lines.append(f'  {idx}. {p}')
            tip_lines.append('\n💡 提示：点击此处或右上角「管理目录...」可增删自定义扫描路径')
            self._paths_tooltip_text = '\n'.join(tip_lines)
        else:
            self.var_paths.set('')
            self._paths_tooltip_text = ''

    def _on_scan_finished(self, env: PlayerEnv, tracks: list[LibraryTrack], fallback_folders: list[str] | None = None):
        self.env, self.tracks = env, list(tracks)
        self.tracks.sort(key=default_track_sort_key)
        self._has_scanned = True
        self.btn_rescan.configure(text='重新扫描')
        self._show_progress(False)
        self._set_busy(False)
        is_player = has_player_env(env)
        p_type = getattr(env, 'player_type', None)
        is_mb = (p_type == PLAYER_TYPE_MUSICBEE) and bool(getattr(env, 'exe_path', '') or getattr(env, 'config_dir', '') or getattr(env, 'library_folders', []))
        is_roon = (p_type == PLAYER_TYPE_ROON) and bool(getattr(env, 'exe_path', '') or getattr(env, 'config_dir', '') or getattr(env, 'library_folders', []))
        is_fb = (p_type == PLAYER_TYPE_FOOBAR) and is_player
        is_active = is_mb or is_fb or is_roon
        active_type = PLAYER_TYPE_MUSICBEE if is_mb else (PLAYER_TYPE_ROON if is_roon else PLAYER_TYPE_FOOBAR)
        self._apply_player_mode(is_active, active_type)

        self._last_fallback_folders = fallback_folders
        self._update_player_state_label(fallback_folders)

        opts = self._current_options()
        self.checked_paths = {tr.path for tr in tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._update_summary()
        self._refresh_table()

        miss = sum(1 for t in tracks if t.needs_tracknumber and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        cov = sum(1 for t in tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in tracks if t.needs_date and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        tagc = sum(1 for t in tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate))
        stale = sum(1 for t in tracks if t.needs_player_sync)
        if is_mb:
            p_label = 'MusicBee'
        elif is_roon:
            p_label = 'Roon'
        else:
            p_label = 'foobar'
        sync_log = f' | {p_label} 缓存待刷新 {stale} 首' if (is_active or stale > 0) else ''
        self._log(f'扫描完成：共 {len(tracks)} 首 | 缺音轨 {miss} 首 | 封面待修 {cov} 首 (目录 cover.jpg {dcov} 个) | 标签待补 {tagc} 首 (缺日期 {datec} 首){sync_log}。')
        self.var_status.set(
            f'扫描完成，可按需勾选模块后点击「一键修复已选项目」或「仅刷新 {p_label} 缓存」'
            if is_active
            else '扫描完成，可按需勾选模块后点击「一键修复已选项目」'
        )

    def _update_summary(self):
        miss = sum(1 for t in self.tracks if t.needs_tracknumber and not t.is_full_album and not t.is_temp_duplicate)
        cov = sum(1 for t in self.tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in self.tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in self.tracks if t.needs_date and not t.is_full_album and not t.is_temp_duplicate)
        tagc = sum(1 for t in self.tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not t.is_full_album and not t.is_temp_duplicate))
        lmode = self.var_lyrics_mode.get() or 'auto'
        lrcc = sum(1 for t in self.tracks if track_needs_lyrics(t, lmode) and not t.is_full_album and not t.is_temp_duplicate)
        stale = sum(1 for t in self.tracks if t.needs_player_sync)
        inc_art = self.var_artist_art.get()
        artist_cnt = sum(1 for t in self.tracks if (t.needs_artist_art or t.folder_artist_action in ('create', 'rewrite', 'nodata')) and not t.is_full_album and not t.is_temp_duplicate)
        dart = len({t.folder for t in self.tracks if t.folder_artist_action in ('create', 'rewrite', 'remove', 'nodata')})
        inc_lrc = self.var_lyrics.get()
        ok_cnt = sum(1 for t in self.tracks if not track_has_pending_work(t, True, True, True, True, True, enable_extra_tags=True, enable_lyrics=inc_lrc, enable_artist_art=inc_art, lyrics_mode=lmode))
        parts = [
            f'共 {len(self.tracks)} 首', f'缺音轨号 {miss}',
            f'封面待修 {cov}' + (f' (目录cover {dcov})' if dcov else ''),
            f'标签待补 {tagc}' + (f' (缺日期 {datec})' if datec else ''),
        ]
        if inc_art:
            parts.append(f'缺艺术家图 {artist_cnt}' + (f' (目录artist {dart})' if dart else ''))
        if inc_lrc:
            parts.append(f'缺歌词 {lrcc}')
        if is_player_active(self.env) or stale > 0:
            p_name = getattr(self.env, 'player_name', '播放器')
            parts.append(f'{p_name}待刷新 {stale}')
        parts.append(f'正常 {ok_cnt}')
        self.var_summary.set('  |  '.join(parts))

    def _on_pref_option_changed(self):
        self._save_user_state()
        self._on_option_changed()

    def _on_option_changed(self):
        if self._busy:
            return
        opts = self._current_options()
        self.checked_paths = {tr.path for tr in self.tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._update_summary()
        self._refresh_table()

    def _row_values(self, tr: LibraryTrack) -> tuple[tuple[str, ...], str]:
        inc_lrc = self.var_lyrics.get()
        lmode = self.var_lyrics_mode.get() or 'auto'
        st_text, st_tag = _format_status_badge(tr, include_lyrics=inc_lrc, lyrics_mode=lmode)
        chk = '☑' if tr.path in self.checked_paths else '☐'
        title = tr.title or os.path.splitext(tr.filename)[0]
        if tr.needs_player_sync and tr.disk_tracknumber:
            tn_disp = f"{tr.disk_tracknumber} / 缓存:{getattr(tr, 'foobar_tracknumber', '') or '空'}"
        elif tr.disk_track_int == 0:
            tn_disp = '0 (无效)'
        else:
            tn_disp = tr.disk_tracknumber or '无'
        note = tr.match_note or tr.note or tr.path
        return (chk, st_text, title, tr.artist, tr.album, tn_disp, _format_cover_cell(tr), _format_tag_sync_cell(tr, include_lyrics=inc_lrc, lyrics_mode=lmode), note), st_tag

    def _refresh_table(self):
        opts = self._current_options()
        skip_full = self.var_skip_full.get()
        if self.var_only_pending.get():
            allowed = opts.active_tag_fields()
            self.visible_tracks = [
                t for t in self.tracks
                if t.status == STATUS_ERROR or t.is_temp_duplicate or (opts.enable_track and t.is_full_album and t.needs_tracknumber)
                or track_has_pending_work(
                    t, opts.enable_track, opts.enable_art, opts.enable_tags, opts.enable_sync, skip_full,
                    enable_extra_tags=(opts.enable_tags and opts.enable_extra_tags),
                    enabled_tag_fields=allowed,
                    enable_lyrics=opts.enable_lyrics,
                    enable_artist_art=opts.enable_artist_art,
                    lyrics_mode=opts.lyrics_mode,
                )
            ]
        else:
            self.visible_tracks = list(self.tracks)

        # 搜索与字段过滤
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            stype = self.var_search_type.get() if hasattr(self, 'var_search_type') else '全部'
            self.visible_tracks = [t for t in self.visible_tracks if self._match_track_search(t, query, stype)]

        if self._sort_col:
            sort_fn = self._get_sort_key(self._sort_col)
            self.visible_tracks.sort(key=sort_fn, reverse=self._sort_reverse)
        else:
            self.visible_tracks.sort(key=default_track_sort_key)

        self.tree.delete(*self.tree.get_children())
        for tr in self.visible_tracks:
            vals, tag = self._row_values(tr)
            self.tree.insert('', 'end', iid=tr.path, values=vals, tags=(tag,))
        self._update_header_chk_state()
        self._update_search_info()
        if hasattr(self, 'detail_widget'):
            self._on_tree_selection_changed()

    _populate_tree = _refresh_table

    def _match_track_search(self, tr: LibraryTrack, query: str, search_type: str) -> bool:
        q = (query or '').strip().lower()
        if not q:
            return True

        tokens = q.split()
        if not tokens:
            return True

        title = (tr.title or os.path.splitext(tr.filename)[0] or '').lower()
        artist = (tr.artist or '').lower()
        album = (tr.album or '').lower()
        albumartist = (tr.albumartist or '').lower()
        composer = (tr.composer or '').lower()
        lyricist = (tr.lyricist or '').lower()
        arranger = (tr.arranger or '').lower()
        genre = (tr.genre or '').lower()
        track_str = str(tr.disk_tracknumber or '').lower()
        track_int_str = str(tr.disk_track_int) if tr.disk_track_int is not None else ''
        tracknumber = f"{track_str} {track_int_str}".strip()
        file_path = f"{tr.filename or ''} {tr.path or ''} {tr.folder or ''}".lower()
        note = f"{tr.match_note or ''} {tr.note or ''}".lower()

        t_type = (search_type or '全部').strip()

        if t_type in ('歌曲名', 'title', '歌曲', '曲目'):
            target = title
        elif t_type in ('艺术家', 'artist', '歌手'):
            target = artist
        elif t_type in ('专辑名', 'album', '专辑'):
            target = album
        elif t_type in ('专辑艺术家', 'albumartist', 'album_artist'):
            target = albumartist
        elif t_type in ('作曲', 'composer'):
            target = composer
        elif t_type in ('作词', 'lyricist'):
            target = lyricist
        elif t_type in ('编曲', 'arranger'):
            target = arranger
        elif t_type in ('流派', 'genre'):
            target = genre
        elif t_type in ('音轨号', 'track', 'tracknumber', '音轨'):
            target = tracknumber
        elif t_type in ('文件路径', '文件名/路径', '文件名', 'path', 'filename'):
            target = file_path
        elif t_type in ('说明/备注', '说明', '备注', 'note'):
            target = note
        else:  # '全部' 或其他
            target = f"{title} {artist} {album} {albumartist} {composer} {lyricist} {arranger} {genre} {tracknumber} {file_path} {note}"

        return all(tok in target for tok in tokens)

    def _update_search_info(self):
        if not hasattr(self, 'var_search_info'):
            return
        total = len(self.tracks)
        visible = len(self.visible_tracks)
        q = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if not self._has_scanned and total == 0:
            self.var_search_info.set('')
            return
        if q:
            self.var_search_info.set(f'已筛选: {visible} / {total} 首')
        elif hasattr(self, 'var_only_pending') and self.var_only_pending.get() and visible != total:
            self.var_search_info.set(f'待处理: {visible} / {total} 首')
        else:
            self.var_search_info.set(f'共 {total} 首')

    def _on_ctrl_f(self, event=None):
        if self._busy:
            return 'break'
        if hasattr(self, 'ent_search') and self.ent_search.winfo_exists():
            try:
                self.ent_search.focus_set()
                self.ent_search.selection_range(0, 'end')
                self.ent_search.icursor('end')
                self.ent_search.update_idletasks()
            except Exception:
                pass
        return 'break'

    def _on_clear_search(self):
        if self._busy:
            return
        if hasattr(self, '_search_after_id') and self._search_after_id:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None
        if hasattr(self, 'var_search'):
            self.var_search.set('')
        self._refresh_table()
        if hasattr(self, 'ent_search') and self.ent_search.winfo_exists():
            try:
                self.ent_search.focus_set()
                self.ent_search.update_idletasks()
            except Exception:
                pass

    def _on_search_return(self, event=None):
        if self._busy:
            return 'break'
        self._apply_search_immediate()
        return 'break'

    def _on_search_button1(self, event=None):
        if self._busy:
            return 'break'

    def _on_search_focus_in(self, event=None):
        if self._busy:
            try:
                if hasattr(self, 'tree') and self.tree.winfo_exists():
                    self.tree.focus_set()
                elif hasattr(self, 'root') and self.root.winfo_exists():
                    self.root.focus_set()
            except Exception:
                pass
            return 'break'

    def _on_entry_key_release(self, event=None):
        if self._busy:
            return 'break'
        return None

    def _apply_search_from_return(self):
        if self._busy:
            return
        self._apply_search_immediate()

    def _on_search_type_changed(self):
        if self._busy:
            return
        self._save_user_state()
        self._apply_search_immediate()

    def _on_search_query_changed(self, *args):
        if self._busy:
            return
        if hasattr(self, '_search_after_id') and self._search_after_id:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None
        if hasattr(self, 'root') and self.root.winfo_exists():
            self._search_after_id = self.root.after(200, self._apply_search_debounce)

    def _apply_search_debounce(self):
        self._search_after_id = None
        if self._busy:
            return
        if not hasattr(self, 'root') or not self.root.winfo_exists():
            return
        self._refresh_table()

    def _apply_search_immediate(self):
        if self._busy:
            return
        if hasattr(self, '_search_after_id') and self._search_after_id:
            try:
                self.root.after_cancel(self._search_after_id)
            except Exception:
                pass
            self._search_after_id = None
        self._refresh_table()

    def _quick_search(self, query: str, search_type: str = '全部'):
        if self._busy:
            return
        if hasattr(self, 'var_search_type'):
            if search_type in self._SEARCH_TYPES:
                self.var_search_type.set(search_type)
            else:
                self.var_search_type.set('全部')
        if hasattr(self, 'var_search'):
            self.var_search.set(query)
        self._apply_search_immediate()

    def _update_headings(self):
        if not hasattr(self, 'tree'):
            return
        for col in self._COLS:
            if col == 'chk':
                continue
            base = self._base_headings.get(col, col)
            if self._sort_col == col:
                arrow = ' ▼' if self._sort_reverse else ' ▲'
                self.tree.heading(col, text=f'{base}{arrow}')
            else:
                self.tree.heading(col, text=base)

    def _on_heading_click(self, col: str):
        if self._busy:
            return
        if col == 'chk':
            return
        if self._sort_col == col:
            if not self._sort_reverse:
                self._sort_reverse = True
            else:
                self._sort_col = None
                self._sort_reverse = False
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._update_headings()
        self._refresh_table()

    def _get_sort_key(self, col: str):
        inc_lrc = self.var_lyrics.get() if hasattr(self, 'var_lyrics') else False
        lmode = self.var_lyrics_mode.get() if hasattr(self, 'var_lyrics_mode') else 'auto'

        if col == 'title':
            return lambda tr: (
                _natural_sort_key(tr.title or os.path.splitext(tr.filename)[0]),
                _natural_sort_key(tr.artist or ''),
                _natural_sort_key(tr.album or ''),
                tr.disk_disc_int or 1,
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
            )
        elif col == 'artist':
            return lambda tr: (
                _natural_sort_key(tr.artist or ''),
                _natural_sort_key(tr.album or ''),
                tr.disk_disc_int or 1,
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
                _natural_sort_key(tr.title or os.path.splitext(tr.filename)[0]),
            )
        elif col == 'album':
            return lambda tr: (
                _natural_sort_key(tr.album or ''),
                _natural_sort_key(tr.artist or ''),
                tr.disk_disc_int or 1,
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
                _natural_sort_key(tr.title or os.path.splitext(tr.filename)[0]),
            )
        elif col == 'track':
            return lambda tr: (
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
                tr.disk_disc_int if (tr.disk_disc_int is not None and tr.disk_disc_int > 0) else 1,
                _natural_sort_key(tr.disk_tracknumber or ''),
                _natural_sort_key(tr.album or ''),
                _natural_sort_key(tr.title or os.path.splitext(tr.filename)[0]),
            )
        elif col == 'status':
            return lambda tr: (
                _format_status_badge(tr, include_lyrics=inc_lrc, lyrics_mode=lmode)[0],
                _natural_sort_key(tr.artist or ''),
                _natural_sort_key(tr.album or ''),
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
            )
        elif col == 'cover':
            return lambda tr: (
                _format_cover_cell(tr),
                _natural_sort_key(tr.artist or ''),
                _natural_sort_key(tr.album or ''),
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
            )
        elif col == 'tagsync':
            return lambda tr: (
                _format_tag_sync_cell(tr, include_lyrics=inc_lrc, lyrics_mode=lmode),
                _natural_sort_key(tr.artist or ''),
                _natural_sort_key(tr.album or ''),
                tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999,
            )
        elif col == 'note':
            return lambda tr: (
                _natural_sort_key(tr.match_note or tr.note or tr.path or ''),
                _natural_sort_key(tr.title or os.path.splitext(tr.filename)[0]),
            )
        return lambda tr: tr.path

    def _update_header_chk_state(self):
        if not hasattr(self, 'tree'):
            return
        if not self.visible_tracks:
            self.tree.heading('chk', text='☐ 选择')
            return
        checked_count = sum(1 for tr in self.visible_tracks if tr.path in self.checked_paths)
        total = len(self.visible_tracks)
        if checked_count == 0:
            head_text = '☐ 选择'
        elif checked_count == total:
            head_text = '☑ 选择'
        else:
            head_text = '▣ 选择'
        self.tree.heading('chk', text=head_text)

    def _toggle_row(self, iid: str):
        if not iid or not self.tree.exists(iid):
            return
        if iid in self.checked_paths:
            self.checked_paths.discard(iid)
        else:
            self.checked_paths.add(iid)
        vals = list(self.tree.item(iid, 'values'))
        vals[0] = '☑' if iid in self.checked_paths else '☐'
        self.tree.item(iid, values=vals)
        self._update_header_chk_state()
        self._update_search_info()
        if hasattr(self, 'detail_widget'):
            self._on_tree_selection_changed()

    def _on_tree_click(self, event):
        if self._busy:
            return
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle_row(iid)

    def _on_tree_space(self, _event):
        if self._busy:
            return
        for iid in self.tree.selection():
            self._toggle_row(iid)

    def _on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        col_str = self.tree.identify_column(event.x)
        if col_str == '#6':
            self._on_pick_cover()
            return
        if col_str in ('#7', '#8'):
            tr = next((x for x in self.tracks if x.path == iid), None)
            if tr and (tr.needs_lyrics or not tr.has_lyrics):
                self._on_pick_lyric()
                return
        if os.path.exists(iid):
            reveal_in_file_manager(iid)

    def _copy_to_clipboard(self, text: str, hint: str = ''):
        if not text:
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.root.update_idletasks()
            pv = text.replace('\r', ' ').replace('\n', ' ').strip()
            if len(pv) > 40:
                pv = pv[:38] + '...'
            msg = f"已复制{hint}到剪贴板: {pv}" if hint else f"已复制到剪贴板: {pv}"
            self.var_status.set(msg)
        except Exception as e:
            self._log(f"复制到剪贴板失败: {e}")

    def _on_tree_right_click(self, event):
        if self._busy:
            return
        iid = self.tree.identify_row(event.y)
        if not iid or not self.tree.exists(iid):
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)

        tr = next((t for t in self.tracks if t.path == iid), None)
        vals = list(self.tree.item(iid, 'values') or ())

        # 提取当前曲目各字段信息
        val_title = (tr.title if (tr and tr.title) else (vals[2] if len(vals) > 2 else '')) or ''
        val_artist = (tr.artist if tr else (vals[3] if len(vals) > 3 else '')) or ''
        val_album = (tr.album if tr else (vals[4] if len(vals) > 4 else '')) or ''
        val_track = ''
        if tr:
            val_track = tr.disk_tracknumber or ''
        elif len(vals) > 5 and vals[5] and vals[5] not in ('无', '0 (无效)'):
            val_track = str(vals[5])
        val_status = str(vals[1]) if len(vals) > 1 and vals[1] else (tr.status if tr else '')
        val_cover = str(vals[6]) if len(vals) > 6 and vals[6] else ''
        val_tagsync = str(vals[7]) if len(vals) > 7 and vals[7] else ''
        val_note = (tr.match_note or tr.note if tr else '') or (str(vals[8]) if len(vals) > 8 and vals[8] else '')
        val_path = iid

        # 各列映射关系：key -> (名称, 值)
        col_meta: dict[str, tuple[str, str]] = {
            'chk': ('文件路径', val_path),
            'status': ('综合状态', val_status),
            'title': ('歌曲名', val_title),
            'artist': ('艺术家', val_artist),
            'album': ('专辑', val_album),
            'track': ('音轨号', val_track),
            'cover': ('封面状态', val_cover),
            'tagsync': ('标签检查', val_tagsync),
            'note': ('说明', val_note),
        }

        # 视点击位置解析所点击的列
        col_str = self.tree.identify_column(event.x)
        clicked_col_key: str | None = None
        if col_str and col_str.startswith('#'):
            try:
                c_idx = int(col_str[1:]) - 1
                if 0 <= c_idx < len(self._COLS):
                    clicked_col_key = self._COLS[c_idx]
            except (ValueError, IndexError):
                clicked_col_key = None

        menu = tk.Menu(self.root, tearoff=0)

        # 1. 媒体资源挑选与标签编辑
        menu.add_command(label='🖼️ 挑选专辑封面 (cover.jpg)...', command=self._on_pick_cover)
        target_art = val_artist or (tr.artist if tr else '') or ''
        art_label = f'👤 为艺术家「{target_art}」挑选写真 (artist.jpg)...' if target_art else '👤 挑选艺术家写真 (artist.jpg)...'
        menu.add_command(label=art_label, command=self._on_pick_artist)
        menu.add_command(label='🎵 挑选/更换歌词 (lrc)...', command=self._on_pick_lyric)
        menu.add_command(label='✏️ 设置标签...', command=self._on_set_metadata)
        menu.add_command(label='🌐 简繁汉字转换...', command=self._on_convert_zh)
        menu.add_separator()

        # 2. 文件系统快捷操作
        menu.add_command(label='📂 在文件管理器中显示', command=lambda: reveal_in_file_manager(iid))
        menu.add_separator()

        # 3. 根据右键点击的列位置动态展示“复制 [当前列]”功能及快速筛选
        target_name, target_val = col_meta.get(clicked_col_key, ('文件路径', val_path))
        if target_val:
            pv = str(target_val).replace('\r', ' ').replace('\n', ' ').strip()
            if len(pv) > 22:
                pv = pv[:20] + '...'
            menu.add_command(
                label=f'📋 复制{target_name}: {pv}',
                command=lambda v=str(target_val), n=target_name: self._copy_to_clipboard(v, n),
            )
            # 联动字段搜索功能：支持右键直接按当前字段值快速筛选
            if clicked_col_key in ('title', 'artist', 'album', 'note'):
                search_type_map = {
                    'title': '歌曲名',
                    'artist': '艺术家',
                    'album': '专辑名',
                    'note': '说明/备注',
                }
                s_type = search_type_map.get(clicked_col_key, '全部')
                menu.add_command(
                    label=f'🔍 筛选此{target_name}: {pv}',
                    command=lambda v=str(target_val), st=s_type: self._quick_search(v, st),
                )
        else:
            menu.add_command(label=f'📋 复制{target_name} (空)', state='disabled')

        # 级联子菜单：提供完整字段及路径快速复制
        sub_copy = tk.Menu(menu, tearoff=0)

        val_composer = (tr.composer if tr else '') or ''
        val_lyricist = (tr.lyricist if tr else '') or ''
        val_arranger = (tr.arranger if tr else '') or ''
        val_genre = (tr.genre if tr else '') or ''
        val_date = (tr.date if tr else '') or ''

        # 1. 核心与扩展标签信息
        for n, v in (
            ('歌曲名', val_title), ('艺术家', val_artist), ('专辑', val_album),
            ('音轨号', val_track), ('发行年份', val_date),
            ('作曲', val_composer), ('作词', val_lyricist), ('编曲', val_arranger),
            ('流派', val_genre),
        ):
            if v:
                pv = str(v).replace('\r', ' ').replace('\n', ' ').strip()
                if len(pv) > 24:
                    pv = pv[:22] + '...'
                sub_copy.add_command(label=f'复制{n}: {pv}', command=lambda val=str(v), name=n: self._copy_to_clipboard(val, name))
            else:
                sub_copy.add_command(label=f'复制{n} (空)', state='disabled')

        sub_copy.add_separator()

        # 2. 文件及目录路径
        file_name = os.path.basename(iid)
        dir_name = os.path.dirname(iid)
        for label_prefix, raw_val, hint_name in (
            ('复制完整文件路径', val_path, '文件路径'),
            ('复制文件名', file_name, '文件名'),
            ('复制所在目录路径', dir_name, '目录路径'),
        ):
            pv = str(raw_val).replace('\r', ' ').replace('\n', ' ').strip()
            if len(pv) > 28:
                pv = pv[:26] + '...'
            sub_copy.add_command(label=f'{label_prefix}: {pv}', command=lambda v=raw_val, h=hint_name: self._copy_to_clipboard(v, h))

        sub_copy.add_separator()

        # 3. 状态与说明字段
        for n, v in (('综合状态', val_status), ('封面状态', val_cover), ('标签检查', val_tagsync), ('说明', val_note)):
            if v:
                pv = str(v).replace('\r', ' ').replace('\n', ' ').strip()
                if len(pv) > 24:
                    pv = pv[:22] + '...'
                sub_copy.add_command(label=f'复制{n}: {pv}', command=lambda val=str(v), name=n: self._copy_to_clipboard(val, name))
            else:
                sub_copy.add_command(label=f'复制{n} (空)', state='disabled')

        sub_copy.add_separator()

        # 4. 整行表格数据 (制表符分隔，便于直接粘贴进 Excel / 表格文本)
        row_cells = [str(x) for x in vals[1:] if x is not None]
        if row_cells:
            row_tsv = '\t'.join(row_cells)
            sub_copy.add_command(
                label='复制整行数据 (制表符分隔)',
                command=lambda v=row_tsv: self._copy_to_clipboard(v, '整行数据'),
            )

        # 5. 多曲目勾选时的批量复制扩展
        if len(self.checked_paths) > 1 and iid in self.checked_paths:
            sub_copy.add_separator()
            query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
            if query:
                checked_tracks = [t for t in self.visible_tracks if t.path in self.checked_paths]
            else:
                checked_tracks = [t for t in self.visible_tracks if t.path in self.checked_paths]
                if not checked_tracks:
                    checked_tracks = [t for t in self.tracks if t.path in self.checked_paths]
            chk_count = len(checked_tracks)

            if clicked_col_key and clicked_col_key in ('title', 'artist', 'album', 'track'):
                col_field_attr = 'disk_tracknumber' if clicked_col_key == 'track' else clicked_col_key
                batch_vals = [getattr(t, col_field_attr, '') or '' for t in checked_tracks]
                if any(batch_vals):
                    sub_copy.add_command(
                        label=f'复制已勾选的{target_name} (共 {chk_count} 首)',
                        command=lambda lines='\n'.join(batch_vals), n=target_name: self._copy_to_clipboard(lines, f'{chk_count}首{n}'),
                    )

            checked_paths_text = '\n'.join(t.path for t in checked_tracks)
            sub_copy.add_command(
                label=f'复制已勾选的完整路径 (共 {chk_count} 首)',
                command=lambda p=checked_paths_text: self._copy_to_clipboard(p, f'{chk_count}首路径'),
            )

        menu.add_cascade(label='📋 更多复制', menu=sub_copy)

        self._last_context_menu = menu
        menu.post(event.x_root, event.y_root)

    def _on_pick_cover(self):
        if self._busy:
            return
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            checked_list = [t for t in self.visible_tracks if t.path in self.checked_paths]
        else:
            checked_list = [t for t in self.tracks if t.path in self.checked_paths]
        sel = self.tree.selection()

        if not checked_list:
            if sel:
                target_path = sel[0]
                t = next((x for x in (self.visible_tracks if query else self.tracks) if x.path == target_path), None)
                if t:
                    checked_list = [t]
            elif len(self.visible_tracks) == 1:
                checked_list = [self.visible_tracks[0]]

        if not checked_list:
            if query and any(t.path in self.checked_paths for t in self.tracks):
                _msgbox.showinfo('提示', '当前搜索结果中没有勾选任何曲目。\n\n（搜索视图外有已勾选项；如需挑选全量曲目，请先清空搜索框。）')
            else:
                _msgbox.showinfo('提示', '请先勾选或在列表中选中目标曲目。')
            return

        # 2. 按目录与专辑进行智能分组聚合 (Album Groups)
        folder_groups: dict[str, list[LibraryTrack]] = {}
        for tr in checked_list:
            folder_groups.setdefault(tr.folder, []).append(tr)

        album_groups: list[dict] = []
        for fld, f_tracks in folder_groups.items():
            rep_t = next((t for t in f_tracks if sel and t.path == sel[0]), f_tracks[0])
            inf_t, inf_a, inf_alb = _extract_group_clues(rep_t)
            album_groups.append({
                'folder': fld,
                'rep_track': rep_t,
                'tracks': f_tracks,
                'album_name': inf_alb or rep_t.album or '',
                'artist': inf_a or rep_t.artist or '',
                'title': inf_t or rep_t.title or '',
                'saved': False,
            })

        # 视觉同步：高亮首个专辑的代表曲目
        first_rep = album_groups[0]['rep_track']
        if self.tree.exists(first_rep.path):
            self.tree.selection_set(first_rep.path)
            self.tree.focus(first_rep.path)
            self.tree.see(first_rep.path)

        def _apply_picked_cover_for_group(grp: dict, res: dict):
            cov_url = res.get('cover_url')
            if not cov_url:
                return
            alb_name = res.get('album_name') or ''
            rdate = res.get('release_date') or ''
            action_tracks = grp.get('tracks') or [grp['rep_track']]

            def _apply_worker():
                self._log(f'正在下载选中的专辑封面: 《{alb_name}》...')
                data = fetch_cover_bytes(cov_url)
                if not data:
                    self._log(f'[封面下载失败] 无法获取图片数据: {cov_url}')
                    return
                norm_cov = normalize_jpeg(data, MAX_SIDE)
                if not norm_cov:
                    self._log('[封面处理失败] 图片解码/规范化失败')
                    return

                # 1. 为所在目录写入/更新外置封面（优先已有的 folder.jpg / cover.jpg 就地随俗）
                if res.get('export_cover', True):
                    target_name = 'folder.jpg' if (os.path.isfile(os.path.join(grp['folder'], 'folder.jpg')) and not os.path.isfile(os.path.join(grp['folder'], 'cover.jpg'))) else 'cover.jpg'
                    cov_file = os.path.join(grp['folder'], target_name)
                    try:
                        with open(cov_file, 'wb') as fp:
                            fp.write(norm_cov)
                        self._log(f'[目录封面] {grp["folder"]} -> 已保存 {target_name} (来源: 《{alb_name}》)')
                    except OSError as err:
                        self._log(f'[目录封面写入失败] {err}')

                # 2. 同步更新元数据（若选中的候选专辑名带有“复黑王/BTB/首版”及歌手后缀，智能纯净化为标准专辑名）
                updated_fields = {}
                clean_alb_to_save = alb_name
                if alb_name:
                    c_alb = re.sub(r'^(复黑王|环球复黑王|btb|首版|经典|lp|cd)\s*', '', alb_name, flags=re.I).strip()
                    art_n = grp.get('artist') or ''
                    if art_n:
                        c_alb = re.sub(rf'\s*{re.escape(art_n)}$', '', c_alb, flags=re.I).strip()
                    clean_alb_to_save = c_alb or alb_name

                if res.get('update_meta', True):
                    if clean_alb_to_save:
                        updated_fields['album'] = clean_alb_to_save
                    if rdate:
                        updated_fields['date'] = rdate

                for target_t in action_tracks:
                    if clean_alb_to_save:
                        target_t.album = clean_alb_to_save
                    if rdate:
                        target_t.date = rdate
                    if target_t.container in SUPPORTS_COVER_WRITE or updated_fields:
                        try:
                            repair_kw = {
                                'cover_bytes': norm_cov if target_t.container in SUPPORTS_COVER_WRITE else None,
                                'cover_mime': 'image/jpeg',
                                'tag_updates': updated_fields if updated_fields else None,
                            }
                            ok, msg = apply_track_repairs(target_t.path, **repair_kw)
                            if ok:
                                self._log(f'[音频更新] {target_t.filename} -> {msg or "已更新元数据与内嵌封面"}')
                        except Exception as exc:
                            self._log(f'[音频更新提示] {target_t.filename} -> {exc}')

                # 3. 刷新涉及目录所有曲目的封面状态（外置 cover.jpg 已就位）
                for t in self.tracks:
                    if t.folder == grp['folder']:
                        t.needs_cover_fix = False
                        t.needs_cover_online = False
                        t.cover_status = '正常'
                        if t.folder_plan:
                            t.folder_plan.cover_action = 'ok'
                            t.folder_plan.cover_jpeg = norm_cov
                        t.folder_cover_action = 'ok'

                self._ev_q.put(('refresh', None))

            threading.Thread(target=_apply_worker, daemon=True).start()

        preset = self.var_prefer_preset.get()
        prefer = self._custom_prefer_sources if preset == '自定义' else self._PREFER_PRESETS.get(preset, ())
        mb_dir = getattr(self.env, 'artist_pictures_dir', '') if hasattr(self, 'env') else ''

        def _on_cover_wizard_artist_applied(applied_grp: dict, res_info: dict):
            art_name = res_info.get('artist') or applied_grp.get('artist') or ''
            self._log(f'[向导写真联动] 艺术家「{art_name}」写真配置完成并落盘')
            self._ev_q.put(('refresh', None))

        CoverPickerDialog(
            self.root,
            album_groups,
            on_apply_group=_apply_picked_cover_for_group,
            on_apply_artist=_on_cover_wizard_artist_applied,
            musicbee_artist_dir=mb_dir,
            prefer_sources=prefer,
        )

    def _on_pick_artist(self):
        """为已勾选或选中的曲目可视化挑选艺术家写真 (artist.jpg)。"""
        if self._busy:
            return
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            checked_list = [t for t in self.visible_tracks if t.path in self.checked_paths]
        else:
            checked_list = [t for t in self.tracks if t.path in self.checked_paths]
        sel = self.tree.selection()

        if not checked_list:
            if sel:
                target_path = sel[0]
                t = next((x for x in (self.visible_tracks if query else self.tracks) if x.path == target_path), None)
                if t:
                    checked_list = [t]
            elif len(self.visible_tracks) == 1:
                checked_list = [self.visible_tracks[0]]

        if not checked_list:
            if query and any(t.path in self.checked_paths for t in self.tracks):
                _msgbox.showinfo('提示', '当前搜索结果中没有勾选任何曲目。\n\n（搜索视图外有已勾选项；如需挑选全量曲目，请先清空搜索框。）')
            else:
                _msgbox.showinfo('提示', '请先勾选或在列表中选中目标曲目。')
            return

        from services.lookup.resolver import first_artist
        artist_groups_dict: dict[str, list[LibraryTrack]] = {}
        for tr in checked_list:
            art = (first_artist(tr.artist) or tr.artist or '').strip()
            if not art and tr.albumartist:
                art = (first_artist(tr.albumartist) or tr.albumartist or '').strip()
            if not art:
                clues = parse_filename_clues(tr.filename)
                art = (clues.get('inferred_artist') or '').strip()
            if not art:
                art = '未知艺术家'
            artist_groups_dict.setdefault(art, []).append(tr)

        artist_groups: list[dict] = []
        for art, art_tracks in artist_groups_dict.items():
            rep_t = art_tracks[0]
            artist_groups.append({
                'artist': art,
                'folder': rep_t.folder,
                'tracks': art_tracks,
                'hint': rep_t.title or '',
                'saved': False,
            })

        preset = self.var_prefer_preset.get()
        prefer = self._custom_prefer_sources if preset == '自定义' else self._PREFER_PRESETS.get(preset, ())
        mb_dir = getattr(self.env, 'artist_pictures_dir', '') if hasattr(self, 'env') else ''

        def _on_artist_applied(applied_grp: dict, res_info: dict):
            art_name = res_info.get('artist') or applied_grp.get('artist') or ''
            self._log(f'[艺人写真应用] 艺术家「{art_name}」写真配置完成并落盘')
            self._ev_q.put(('refresh', None))

        ArtistPickerDialog(
            self.root,
            artist_groups,
            musicbee_artist_dir=mb_dir,
            on_apply=_on_artist_applied,
            prefer_sources=prefer,
        )

    def _on_pick_lyric(self):
        if self._busy:
            return
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            checked_list = [t for t in self.visible_tracks if t.path in self.checked_paths]
        else:
            checked_list = [t for t in self.tracks if t.path in self.checked_paths]
        sel = self.tree.selection()

        if not checked_list:
            if sel:
                target_path = sel[0]
                t = next((x for x in (self.visible_tracks if query else self.tracks) if x.path == target_path), None)
                if t:
                    checked_list = [t]
            elif len(self.visible_tracks) == 1:
                checked_list = [self.visible_tracks[0]]

        if not checked_list:
            if query and any(t.path in self.checked_paths for t in self.tracks):
                _msgbox.showinfo('提示', '当前搜索结果中没有勾选任何曲目。\n\n（搜索视图外有已勾选项；如需挑选全量曲目，请先清空搜索框。）')
            else:
                _msgbox.showinfo('提示', '请先勾选或在列表中选中目标曲目。')
            return

        def _apply_picked_lyric_for_track(tr: LibraryTrack, res: dict):
            extra_msg = '及大碟封面' if res.get('cover_saved') else ''
            self._log(f'[歌词挑选应用] {tr.filename} -> 歌词{extra_msg}及元数据写入成功')
            tr.needs_lyrics = False
            tr.has_lyrics = True
            if res.get('cover_saved'):
                tr.cover_status = '正常'
                tr.needs_cover_online = False
                tr.needs_cover_fix = False
                tr.folder_cover_action = 'ok'
                if tr.folder_plan:
                    tr.folder_plan.cover_action = 'ok'
            if self.var_sync.get() and self._player_env and getattr(self._player_env, 'player_type', None) in ('musicbee', 'foobar'):
                try:
                    if self._player_env.player_type == 'musicbee':
                        sync_files_to_musicbee(self._player_env.exe_path, [tr.path])
                    elif self._player_env.player_type == 'foobar':
                        sync_files_to_foobar(self._player_env.exe_path, [tr.path])
                except Exception:
                    pass
            self._ev_q.put(('refresh', None))

        preset = self.var_prefer_preset.get()
        prefer = self._custom_prefer_sources if preset == '自定义' else self._PREFER_PRESETS.get(preset, ())
        LyricPickerDialog(
            self.root,
            checked_list,
            on_apply=_apply_picked_lyric_for_track,
            prefer_sources=prefer,
            auto_search=True,
        )

    def _toggle_select_all(self):
        if self._busy:
            return
        if not self.visible_tracks:
            return
        all_checked = all(tr.path in self.checked_paths for tr in self.visible_tracks)
        for tr in self.visible_tracks:
            (self.checked_paths.discard if all_checked else self.checked_paths.add)(tr.path)
        self._refresh_table()

    def _start_worker(self, busy_label: str, total: int, worker_fn):
        if self._busy:
            return
        self._cancel_flag = False
        self._set_busy(True, busy_label)
        self.btn_stop.configure(state='normal')
        self.btn_stop.pack(side='left', padx=(0, 6), before=self.sep_act1)
        self._show_progress(True, total, 0)
        threading.Thread(target=worker_fn, daemon=True).start()

    def _run_batch_task(self, prefix: str, targets: list[LibraryTrack], opts, executor, busy_label: str):
        def _worker():
            try:
                summary = executor(
                    self.env, targets, opts,
                    progress_cb=lambda i, t, n: self._ev_q.put((f'{prefix}_prog', (i, t, n))),
                    row_cb=lambda p, d: self._ev_q.put((f'{prefix}_row', (p, d))),
                    log_cb=lambda m: self._ev_q.put(('log', m)),
                    is_cancelled_cb=lambda: self._cancel_flag,
                )
                self._ev_q.put((f'{prefix}_done', summary))
            except Exception as exc:
                import traceback
                self._ev_q.put(('log', f'执行任务异常中断: {exc}\n{traceback.format_exc()}'))
                self._ev_q.put((f'{prefix}_done', {'error': str(exc), 'cancelled': False}))

        self._start_worker(busy_label, len(targets), _worker)

    def _finish_worker(self):
        self.btn_stop.pack_forget()
        self._show_progress(False)
        self._set_busy(False)
        self._update_summary()
        self._update_header_chk_state()

    def _get_target_tracks_for_action(self, action_name: str = '处理') -> list[LibraryTrack] | None:
        """
        根据当前视图状态安全提取目标曲目：
        1. 当处于搜索筛选状态 (var_search 非空) 时，严格遵循「所见即所选」原则，仅在当前可见视图中提取已勾选曲目；
           如果当前搜索视图内未勾选任何曲目，则予以友好提示并拦截操作，防止后台误伤全局已勾选曲目。
        2. 当处于全量/默认视图时，提取全库中已勾选的曲目。
        返回待处理曲目列表；如果已给出提示并需中断操作，返回 None。
        """
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            targets = [t for t in self.visible_tracks if t.path in self.checked_paths]
            if not targets:
                hidden_count = sum(1 for t in self.tracks if t.path in self.checked_paths)
                if hidden_count > 0:
                    _msgbox.showinfo(
                        '提示',
                        f'当前搜索结果中没有勾选任何曲目。\n\n'
                        f'（当前搜索视图外另有 {hidden_count} 首曲目处于勾选状态；如需{action_name}全量勾选曲目，请先清空搜索框。）',
                        parent=self.root,
                    )
                else:
                    _msgbox.showinfo('提示', '当前搜索结果中没有已勾选的曲目。', parent=self.root)
                return None
            return targets

        targets = [t for t in self.tracks if t.path in self.checked_paths]
        if not targets:
            _msgbox.showinfo(
                '提示',
                f'当前没有已勾选的曲目。\n如需对正常曲目{action_name}，请先取消勾选“只显示待处理曲目”，再勾选目标曲目。',
                parent=self.root,
            )
            return None
        return targets

    def _open_target_dialog(self, dialog_cls, attr_name: str, action_desc: str, on_ok):
        if self._busy:
            return
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        targets = self._get_target_tracks_for_action(action_desc)
        if not targets:
            return
        dlg = dialog_cls(self.root, targets, default_sync=self.var_sync.get(), has_player=has_player_env(self.env))
        setattr(self, attr_name, dlg)
        self.root.wait_window(dlg.top)
        if dlg.result is not None:
            on_ok(targets, dlg.result)

    def _update_tree_row(self, path: str, tr: LibraryTrack | None, payload: dict, *, default_tn: str = '无'):
        if not self.tree.exists(path):
            return
        inc_lrc = self.var_lyrics.get()
        vals = list(self.tree.item(path, 'values'))
        vals[0] = '☑' if path in self.checked_paths else '☐'
        if payload.get('status_text'):
            vals[1] = payload['status_text']
        if tr is not None:
            vals[2] = tr.title or os.path.splitext(tr.filename)[0]
            vals[3] = tr.artist
            vals[4] = tr.album
            vals[5] = tr.disk_tracknumber or default_tn
            vals[6] = _format_cover_cell(tr)
            vals[7] = _format_tag_sync_cell(tr, include_lyrics=inc_lrc, lyrics_mode=self.var_lyrics_mode.get() or 'auto')
        if payload.get('source_note'):
            vals[8] = payload['source_note']
        self.tree.item(path, values=vals, tags=(payload.get('color', 'info'),))

    def _sync_track_from_info(self, tr: LibraryTrack, new_info: dict, *, pop_tags=(), cover_deleted: bool = False, artist_cover_deleted: bool = False, synced: bool = False):
        for f in ('title', 'artist', 'albumartist', 'album', 'date', 'composer', 'lyricist', 'arranger', 'performer', 'genre', 'comment'):
            setattr(tr, f, new_info.get(f) or '')
        tr.has_163_key = bool(new_info.get('has_163_key'))
        tr.has_lyrics = bool(new_info.get('has_lyrics'))
        tr.has_embedded_lyrics = bool(new_info.get('has_embedded_lyrics'))
        tr.has_lrc_file = bool(new_info.get('has_lrc_file'))
        tr.needs_lyrics = not tr.has_lyrics
        tr.netease_meta = new_info.get('netease_meta')
        tr.disk_tracknumber = new_info.get('tracknumber') or ''
        tr.disk_track_int = new_info.get('track_int')
        tr.disk_discnumber = new_info.get('discnumber') or ''
        tr.disk_disc_int = new_info.get('disc_int')
        tr.needs_tracknumber = not has_valid_tracknumber(new_info)
        for k in pop_tags:
            tr.tag_updates.pop(k, None)
        if 'discnumber' in pop_tags:
            tr.target_disc = None
        if cover_deleted:
            tr.needs_cover_fix = False
            tr.cover_status = '缺封面'
            tr.cover_reasons = ['no-cover-source']
            tr.target_pic_bytes = None
        if artist_cover_deleted:
            tr.has_artist_art = False
            tr.needs_artist_art = True
            tr.artist_status = '缺艺术家图'
            tr.artist_reasons = ['no-artist-art']
            tr.target_artist_bytes = None
        self._recompute_track_missing(tr)
        if synced or not is_player_active(self.env):
            tr.needs_player_sync = False
            tr.player_stale_reasons = []
        else:
            tr.needs_player_sync = True
            tr.player_stale_reasons = ['磁盘修改时间较新']
        tr.status = STATUS_MISSING_DISK if tr.needs_tracknumber else (STATUS_STALE_PLAYER if tr.needs_player_sync else STATUS_OK)

    def _start_fix(self, targets: list[LibraryTrack], opts: FixOptions, busy_label: str):
        self._run_batch_task('fix', targets, opts, execute_repairs, busy_label)

    def _on_one_click(self):
        if self._busy:
            return
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        opts = self._current_options(sync_only=False)
        if not (opts.enable_track or opts.enable_art or opts.enable_tags or opts.enable_lyrics or opts.enable_sync):
            _msgbox.showinfo('提示', '请至少勾选一项要执行的修复功能。')
            return
        targets = self._get_target_tracks_for_action('修复')
        if not targets:
            return

        if opts.enable_sync and getattr(self.env, 'player_type', '') == PLAYER_TYPE_FOOBAR:
            if not get_running_foobar_exes() and self.env.exe_path and os.path.isfile(self.env.exe_path):
                ans = _msgbox.askyesnocancel(
                    'foobar2000 缓存同步提示',
                    '检测到已勾选「刷新 foobar 缓存」，但 foobar2000 当前未在运行。\n\n'
                    '・点击【是】：立即启动 foobar2000 并在修复后实时重载缓存\n'
                    '・点击【否】：仅修复文件元数据，本次跳过播放器缓存刷新\n'
                    '・点击【取消】：取消本次操作',
                    parent=self.root,
                )
                if ans is None:
                    return
                if ans:
                    try:
                        subprocess.Popen([self.env.exe_path])
                        self.var_status.set('正在启动 foobar2000...')
                        self.root.update_idletasks()
                        for _ in range(15):
                            time.sleep(0.2)
                            if get_running_foobar_exes():
                                time.sleep(0.5)
                                break
                        self._update_player_state_label()
                    except Exception as err:
                        self._log(f'启动 foobar2000 失败：{err}，本次将跳过播放器缓存同步。')
                        opts.enable_sync = False
                else:
                    opts.enable_sync = False

        self._start_fix(targets, opts, f'正在修复已选曲目元数据 (共 {len(targets)} 首)...')

    def _on_sync_only(self):
        if self._busy:
            return
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        p_name = getattr(self.env, 'player_name', '播放器')
        query = (self.var_search.get() or '').strip() if hasattr(self, 'var_search') else ''
        if query:
            targets = [t for t in self.visible_tracks if t.path in self.checked_paths] or [t for t in self.visible_tracks if t.needs_player_sync]
            if not targets:
                _msgbox.showinfo('提示', f'当前搜索结果中没有勾选或需要刷新 {p_name} 缓存的曲目。', parent=self.root)
                return
        else:
            targets = [t for t in self.tracks if t.path in self.checked_paths] or [t for t in self.tracks if t.needs_player_sync]
            if not targets:
                _msgbox.showinfo('提示', f'当前没有检测到需要刷新 {p_name} 缓存的曲目；如需强制刷新，请取消“只显示待处理曲目”并勾选歌曲。')
                return

        if getattr(self.env, 'player_type', '') == PLAYER_TYPE_FOOBAR:
            if not get_running_foobar_exes() and self.env.exe_path and os.path.isfile(self.env.exe_path):
                ans = _msgbox.askyesno(
                    '启动 foobar2000',
                    '检测到 foobar2000 当前未在运行。\n'
                    '刷新媒体库缓存需要 foobar2000 处于运行状态以执行元数据重载。\n\n'
                    '是否立即启动 foobar2000 并同步缓存？',
                    parent=self.root,
                )
                if not ans:
                    self._log('已取消刷新 foobar 缓存：foobar2000 未启动。')
                    return
                try:
                    subprocess.Popen([self.env.exe_path])
                    self.var_status.set('正在启动 foobar2000...')
                    self.root.update_idletasks()
                    for _ in range(15):
                        time.sleep(0.2)
                        if get_running_foobar_exes():
                            time.sleep(0.5)
                            break
                    self._update_player_state_label()
                except Exception as err:
                    _msgbox.showerror('启动失败', f'启动 foobar2000 失败：{err}')
                    return

        self._start_fix(targets, FixOptions(enable_track=False, enable_art=False, enable_tags=False, enable_sync=True, sync_only=True), f'正在刷新 {p_name} 缓存 (共 {len(targets)} 首)...')

    def _on_stop(self):
        self._cancel_flag = True
        self.btn_stop.configure(state='disabled')
        self.var_status.set('正在停止...')

    def _on_source_config(self):
        if self._busy:
            return
        dlg = SourceConfigDialog(
            self.root,
            prefer_sources=self._custom_prefer_sources or self._PREFER_PRESETS.get(self.var_prefer_preset.get(), ()),
            only_sources=self._only_sources,
            skip_sources=self._skip_sources,
            netease_api=self._netease_api,
            spotify_client_id=self._spotify_client_id,
            spotify_client_secret=self._spotify_client_secret,
            tadb_api_key=self._tadb_api_key,
            fanart_api_key=self._fanart_api_key,
            fanart_client_key=self._fanart_client_key,
            discogs_token=self._discogs_token,
        )
        self._last_source_dialog = dlg
        self.root.wait_window(dlg.top)
        if dlg.result is None:
            return
        self._custom_prefer_sources = dlg.result['prefer_sources']
        self._only_sources = dlg.result['only_sources']
        self._skip_sources = dlg.result['skip_sources']
        self._netease_api = dlg.result['netease_api']
        self._spotify_client_id = dlg.result['spotify_client_id']
        self._spotify_client_secret = dlg.result['spotify_client_secret']
        self._tadb_api_key = dlg.result.get('tadb_api_key', '')
        self._fanart_api_key = dlg.result.get('fanart_api_key', '')
        self._fanart_client_key = dlg.result.get('fanart_client_key', '')
        self._discogs_token = dlg.result.get('discogs_token', '')
        set_netease_api_base(self._netease_api)
        set_spotify_credentials(self._spotify_client_id, self._spotify_client_secret)
        set_tadb_api_key(self._tadb_api_key)
        set_fanart_keys(self._fanart_api_key, self._fanart_client_key)
        set_discogs_token(self._discogs_token)
        self.var_prefer_preset.set('自定义' if (self._custom_prefer_sources or self._only_sources or self._skip_sources) else '默认(免梯直连)')
        self._save_user_state()
        sp_state = '已配置' if (self._spotify_client_id and self._spotify_client_secret) else '未配置(仅缓存)'
        dg_state = '已配置' if self._discogs_token else '未配置'
        self._log(
            f"已更新数据源配置：优先={','.join(self._custom_prefer_sources) or '默认(免梯直连)'} | "
            f"仅用={','.join(self._only_sources) or '不限'} | "
            f"跳过={','.join(self._skip_sources) or '无'} | "
            f"网易云API={self._netease_api or '官方直连'} | "
            f"Spotify API={sp_state} | Discogs={dg_state}"
        )

    def _on_fix_row_updated(self, path: str, payload: dict):
        tr = next((t for t in self.tracks if t.path == path), None)
        is_ok = payload.get('color') == 'ok'
        if tr is not None:
            if payload.get('matched_track'):
                tr.matched_track = payload['matched_track']
            if payload.get('source_note'):
                tr.match_note = payload['source_note']
            if is_ok:
                if payload.get('fixed_track'):
                    tr.needs_tracknumber = False
                if payload.get('fixed_cover'):
                    tr.needs_cover_fix, tr.needs_cover_online, tr.cover_status, tr.cover_reasons = False, False, '正常', []
                if payload.get('fixed_artist_art'):
                    tr.has_artist_art = True
                    tr.needs_artist_art, tr.artist_status, tr.artist_reasons = False, '正常', []
                if payload.get('fixed_lyrics'):
                    tr.has_lyrics = True
                    tr.needs_lyrics = False
                    lmode = payload.get('lyrics_mode', 'auto')
                    if lmode in ('lrc', 'both') or (lmode == 'auto' and tr.container in PREFER_EXTERNAL_LRC_CONTAINERS) or tr.container not in SUPPORTS_EMBEDDED_LYRICS:
                        tr.has_lrc_file = True
                    if (lmode in ('embed', 'both') or (lmode == 'auto' and tr.container not in PREFER_EXTERNAL_LRC_CONTAINERS)) and tr.container in SUPPORTS_EMBEDDED_LYRICS:
                        tr.has_embedded_lyrics = True
                if payload.get('fixed_tags'):
                    applied_tags = payload.get('written_tags') or tr.tag_updates
                    for k, v in applied_tags.items():
                        attr = 'album' if k == 'album' else k
                        if hasattr(tr, attr):
                            setattr(tr, attr, v)
                        tr.tag_updates.pop(k, None)
                    tr.missing_core_fields = [f for f in tr.missing_core_fields if f not in applied_tags]
                    tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f not in applied_tags]
                    if tr.date:
                        tr.needs_date = False
                if payload.get('current_disc'):
                    tr.disk_discnumber = str(payload['current_disc'])
                    tr.disk_disc_int = int(payload['current_disc'])
                    tr.target_disc = None
                    tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f != 'discnumber']
                tr.needs_tag_fix = bool(tr.tag_updates or tr.target_disc)
                tr.needs_extra_tags = bool(tr.missing_extra_fields)
                if payload.get('fixed_sync') or not tr.needs_player_sync:
                    tr.needs_player_sync = False
                    tr.player_stale_reasons = []
                if payload.get('fixed_folder_cover'):
                    tr.folder_cover_action = 'ok'
                if payload.get('current_track'):
                    tr.disk_tracknumber = str(payload['current_track'])
                    tr.disk_track_int = int(payload['current_track'])
                if not any(k in payload for k in ('fixed_track', 'fixed_disc', 'fixed_cover', 'fixed_artist_art', 'fixed_tags', 'fixed_lyrics', 'fixed_sync', 'fixed_folder_cover')):
                    tr.needs_tracknumber = tr.needs_player_sync = False
                if not tr.needs_tracknumber and not tr.needs_player_sync:
                    tr.status = STATUS_OK
                self.checked_paths.discard(path)

        self._update_tree_row(path, tr, payload, default_tn='-')

    def _on_fix_finished(self, summary: dict):
        if summary.get('error'):
            self._finish_worker()
            err_msg = f"修复过程发生异常中断：{summary['error']}"
            self._log(err_msg)
            self.var_status.set(err_msg)
            _msgbox.showerror('执行出错', err_msg)
            return
        opts = self._current_options()
        for tr in self.tracks:
            if tr.folder_plan is not None:
                tr.folder_cover_action = tr.folder_plan.cover_action
                tr.folder_artist_action = tr.folder_plan.artist_action
                if not track_has_pending_work(
                    tr, opts.enable_track, opts.enable_art, opts.enable_tags, opts.enable_sync,
                    self.var_skip_full.get(), enable_extra_tags=opts.enable_extra_tags,
                    enabled_tag_fields=opts.active_tag_fields(),
                    enable_lyrics=opts.enable_lyrics,
                    enable_artist_art=opts.enable_artist_art,
                ):
                    self.checked_paths.discard(tr.path)
        self._finish_worker()

        if summary.get('sync_only'):
            p_name = getattr(self.env, 'player_name', '播放器')
            if summary.get('reloaded'):
                msg = f"缓存刷新完成：共处理 {summary['synced_stale']} 首，已成功触发 {p_name} 重载元数据缓存。"
            else:
                msg = f"缓存刷新完成：共处理 {summary['synced_stale']} 首，推进时间戳 {summary['touched']} 个。"
        else:
            date_part = f"（含日期 {summary['date_fixed']}）" if summary.get('date_fixed') else ''
            lrc_part = f" / 歌词 {summary['lyrics_fixed']}" if summary.get('lyrics_fixed') else ''
            art_part = f" / 艺术家图 {summary['artist_fixed']}" if summary.get('artist_fixed') else ''
            folder_art_part = f"，目录 artist.jpg {summary['folder_artist_ok']} 个" if summary.get('folder_artist_ok') else ''
            date_miss = f"、未匹配日期 {summary['date_unmatched']} 首" if summary.get('date_unmatched') else ''
            lrc_miss = f"、未匹配歌词 {summary['lyrics_unmatched']} 首" if summary.get('lyrics_unmatched') else ''
            msg = (
                f"修复完成：文件写入 {summary['written_ok']} 首（封面 {summary['cover_fixed']}{art_part} / 标签 {summary['tags_fixed']}{date_part}{lrc_part}），"
                f"目录 cover.jpg {summary['folder_cover_ok']} 个{folder_art_part}，纯缓存同步 {summary['synced_stale']} 首，"
                f"未匹配音轨 {summary['unmatched']} 首{date_miss}{lrc_miss}，失败 {summary['written_fail'] + summary['folder_cover_fail'] + summary.get('folder_artist_fail', 0)} 项。"
            )
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)

    @staticmethod
    def _recompute_track_missing(tr: LibraryTrack):
        if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
            return
        missing_core = [
            f for f in CORE_TEXT_FIELDS
            if not (getattr(tr, f, '') or '').strip() and f not in tr.tag_updates
        ]
        missing_extra: list[str] = []
        for f in EXTRA_TEXT_FIELDS:
            if f == 'discnumber':
                if not (tr.disk_disc_int or (tr.disk_discnumber or '').strip()) and not tr.target_disc:
                    missing_extra.append(f)
            elif not (getattr(tr, f, '') or '').strip() and f not in tr.tag_updates:
                missing_extra.append(f)
        tr.missing_core_fields = missing_core
        tr.missing_extra_fields = missing_extra
        tr.needs_date = 'date' in missing_core
        tr.needs_extra_tags = bool(missing_extra)
        tr.needs_tag_fix = bool(tr.tag_updates or tr.target_disc)

    def _on_set_metadata(self):
        self._open_target_dialog(SetMetadataDialog, '_last_set_dialog', '修改标签', self._start_set_tags)

    def _start_set_tags(self, targets: list[LibraryTrack], set_opts: SetOptions):
        self._run_batch_task('set', targets, set_opts, execute_set_tags, f'正在设置已选曲目元数据 (共 {len(targets)} 首)...')

    def _on_set_row_updated(self, path: str, payload: dict):
        tr = next((t for t in self.tracks if t.path == path), None)
        if tr is not None:
            if payload.get('source_note'):
                tr.match_note = payload['source_note']
            if payload.get('color') == 'ok' and 'new_info' in payload:
                self._sync_track_from_info(
                    tr, payload['new_info'],
                    pop_tags=payload.get('set_tags') or (),
                    synced=bool(payload.get('synced')),
                )
                self.checked_paths.discard(path)
        self._update_tree_row(path, tr, payload)

    def _on_set_finished(self, summary: dict):
        self._finish_worker()
        if summary.get('error'):
            err_msg = f"设置元数据发生异常中断：{summary['error']}"
            self._log(err_msg)
            self.var_status.set(err_msg)
            _msgbox.showerror('执行出错', err_msg)
            return
        prefix = '[预演] 设置完成' if summary.get('dry_run') else '设置完成'
        msg = f"{prefix}：修改文件 {summary['set_ok']} 首，跳过 {summary['set_skipped']} 首，失败 {summary['set_fail']} 项。"
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)
        _msgbox.showinfo('预演完成' if summary.get('dry_run') else '完成', msg)

    def _on_convert_format(self):
        self._open_target_dialog(ConvertFormatDialog, '_last_convert_dialog', '转换格式', self._start_convert)

    def _start_convert(self, targets: list[LibraryTrack], conv_opts: ConvertOptions):
        self._run_batch_task('conv', targets, conv_opts, execute_convert, f'正在转换格式 -> .{conv_opts.target_format} (共 {len(targets)} 首)...')

    def _on_convert_row_updated(self, path: str, payload: dict):
        if payload.get('color') == 'ok':
            self.checked_paths.discard(path)
        self._update_tree_row(path, None, payload)

    def _on_convert_finished(self, summary: dict):
        self._finish_worker()
        if summary.get('error'):
            self._log(summary['error'])
            self.var_status.set(summary['error'])
            _msgbox.showerror('转换失败', summary['error'])
            return
        prefix = '[预演] 转换完成' if summary.get('dry_run') else '转换完成'
        msg = f"{prefix}：成功 {summary['conv_ok']} 首，跳过 {summary['conv_skipped']} 首，失败 {summary['conv_fail']} 项。"
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)
        _msgbox.showinfo('预演完成' if summary.get('dry_run') else '完成', msg)

    def _on_convert_zh(self):
        self._open_target_dialog(ZhConvertDialog, '_last_zh_convert_dialog', '简繁转换', self._start_zh_convert)

    def _start_zh_convert(self, targets: list[LibraryTrack], zh_opts: ZhConvertOptions):
        dir_name = '繁转简' if zh_opts.direction == 't2s' else '简转繁'
        self._run_batch_task('zh', targets, zh_opts, execute_zh_convert, f'正在执行简繁转换 ({dir_name}) -> 共 {len(targets)} 首...')

    def _on_zh_row_updated(self, path: str, payload: dict):
        if payload.get('color') == 'ok':
            self.checked_paths.discard(path)
        tr = next((t for t in self.tracks if t.path == path), None)
        self._update_tree_row(path, tr, payload)

    def _on_zh_finished(self, summary: dict):
        self._finish_worker()
        if summary.get('error'):
            self._log(summary['error'])
            self.var_status.set(summary['error'])
            _msgbox.showerror('转换失败', summary['error'])
            return
        prefix = '[预演] 简繁转换完成' if summary.get('dry_run') else '简繁转换完成'
        msg = f"{prefix}：成功修改 {summary['zh_ok']} 首，无需转换 {summary['zh_skipped']} 首，失败 {summary['zh_fail']} 项（共变更 {summary.get('diff_count', 0)} 处字段）。"
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)
        _msgbox.showinfo('预演完成' if summary.get('dry_run') else '完成', msg)

    def _on_sacd_extract(self):
        if self._busy:
            return
        dlg = SacdExtractDialog(self.root, default_lyrics_mode=self.var_lyrics_mode.get() or 'both')
        self.root.wait_window(dlg.top)
        if dlg.result:
            iso_path, sacd_opts = dlg.result
            self._start_sacd_extract(iso_path, sacd_opts)

    def _start_sacd_extract(self, iso_path: str, sacd_opts: SacdExtractOptions):
        iso_name = Path(iso_path).name
        self._set_busy(True, f'正在提取 SACD ISO -> {iso_name}...')
        self._cancel_flag = False

        def _worker():
            try:
                summary = execute_sacd_pipeline(
                    self.env, iso_path, sacd_opts,
                    progress_cb=lambda i, t, n: self._ev_q.put(('sacd_prog', (i, t, n))),
                    log_cb=lambda m: self._ev_q.put(('log', m)),
                    is_cancelled_cb=lambda: self._cancel_flag,
                )
                self._ev_q.put(('sacd_done', summary))
            except Exception as exc:
                import traceback
                self._ev_q.put(('log', f'SACD 提取异常: {exc}\n{traceback.format_exc()}'))
                self._ev_q.put(('sacd_done', {'success': False, 'error': str(exc)}))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_sacd_finished(self, summary: dict):
        self._finish_worker()
        if not summary.get('success'):
            err = summary.get('error', 'SACD 提取失败')
            self._log(f"[SACD 失败] {err}")
            self.var_status.set(f"SACD 提取失败: {err}")
            _msgbox.showerror('SACD 提取失败', err)
            return

        out_dir = summary.get('output_dir', '')
        count = summary.get('track_count', 0)
        prefix = '[预演] SACD 分析完成' if summary.get('dry_run') else 'SACD 分轨与修复完成'
        msg = f"{prefix}！共处理 {count} 首 DSF 曲目。\n输出目录: {out_dir}"
        self._log(msg)
        self.var_status.set(f"SACD 完成: {count} 首曲目 -> {out_dir}")
        _msgbox.showinfo('SACD 完成', msg)

        # 提取完成后自动导入曲目库
        if not summary.get('dry_run') and out_dir:
            try:
                from infra.scanner.local_scanner import scan_library
                new_tracks = scan_library(self.env, folders=[out_dir], include_art_scan=True)
                if new_tracks:
                    existing_paths = {t.path for t in self.tracks}
                    added_tracks = [t for t in new_tracks if t.path not in existing_paths]
                    if added_tracks:
                        self.tracks.extend(added_tracks)
                        self._populate_tree()
                        self._update_summary()
            except Exception as exc:
                self._log(f"刷新曲目列表失败: {exc}")

    def _on_delete_metadata(self):
        self._open_target_dialog(DeleteMetadataDialog, '_last_delete_dialog', '删除元数据', self._start_delete)

    def _start_delete(self, targets: list[LibraryTrack], del_opts: DeleteOptions):
        def _worker():
            try:
                summary = execute_deletions(
                    self.env, targets, del_opts,
                    progress_cb=lambda i, t, n: self._ev_q.put(('del_prog', (i, t, n))),
                    row_cb=lambda p, d: self._ev_q.put(('del_row', (p, d))),
                    log_cb=lambda m: self._ev_q.put(('log', m)),
                    is_cancelled_cb=lambda: self._cancel_flag,
                )
                reanalyzed: dict[str, FolderArtPlan] = {}
                if not del_opts.dry_run and (summary.get('deleted_ok', 0) > 0 or summary.get('folder_cover_deleted', 0) > 0):
                    mod_set = set(summary.get('modified_paths') or ())
                    touched_folders = {
                        tr.folder for tr in targets
                        if tr.folder and (tr.path in mod_set or (del_opts.delete_folder_cover and 'cover' in del_opts.fields))
                    }
                    for folder in touched_folders:
                        clean_paths = [
                            t.path for t in self.tracks
                            if t.folder == folder and not t.is_temp_duplicate and t.status != STATUS_ERROR
                        ]
                        if clean_paths:
                            reanalyzed[folder] = analyze_folder_art_and_tags(folder, clean_paths)
                summary['reanalyzed_folders'] = reanalyzed
                self._ev_q.put(('del_done', summary))
            except Exception as exc:
                import traceback
                self._ev_q.put(('log', f'删除任务异常中断: {exc}\n{traceback.format_exc()}'))
                self._ev_q.put(('del_done', {'error': str(exc), 'cancelled': False}))

        self._start_worker(f'正在删除已选曲目元数据 (共 {len(targets)} 首)...', len(targets), _worker)

    def _on_delete_row_updated(self, path: str, payload: dict):
        tr = next((t for t in self.tracks if t.path == path), None)
        if tr is not None:
            if payload.get('source_note'):
                tr.match_note = payload['source_note']
            if payload.get('color') == 'ok' and 'new_info' in payload:
                deleted_fields = set(payload.get('deleted_fields') or ())
                self._sync_track_from_info(
                    tr, payload['new_info'],
                    pop_tags=deleted_fields,
                    cover_deleted=('cover' in deleted_fields),
                    artist_cover_deleted=('artist_cover' in deleted_fields),
                    synced=bool(payload.get('synced')),
                )
                self.checked_paths.discard(path)
        self._update_tree_row(path, tr, payload)

    def _on_delete_finished(self, summary: dict):
        if summary.get('error'):
            self._finish_worker()
            err_msg = f"删除元数据发生异常中断：{summary['error']}"
            self._log(err_msg)
            self.var_status.set(err_msg)
            _msgbox.showerror('执行出错', err_msg)
            return
        reanalyzed: dict[str, FolderArtPlan] = summary.get('reanalyzed_folders') or {}
        if reanalyzed:
            for tr in self.tracks:
                f_plan = reanalyzed.get(tr.folder)
                if f_plan is None:
                    continue
                tr.folder_plan = f_plan
                tr.folder_cover_action = f_plan.cover_action
                t_plan = f_plan.track_plans.get(os.path.normcase(os.path.abspath(tr.path)))
                if t_plan is not None:
                    tr.cover_reasons = list(t_plan.cover_reasons)
                    tr.needs_cover_fix = bool(t_plan.needs_cover_fix)
                    tr.needs_cover_online = bool(
                        'no-cover-source' in tr.cover_reasons
                        and tr.status != STATUS_ERROR
                        and not tr.is_temp_duplicate
                        and tr.container in SUPPORTS_COVER_WRITE
                    )
                    if tr.needs_cover_online:
                        tr.cover_status = '缺封面(待联网)'
                    elif 'no-cover-source' in tr.cover_reasons:
                        tr.cover_status = '缺封面(手动挑选)'
                    else:
                        tr.cover_status = t_plan.cover_status
                    tr.target_pic_bytes = t_plan.target_pic_bytes
                    tr.target_pic_mime = t_plan.target_pic_mime
                    tr.tag_updates = dict(t_plan.tag_updates)
                    self._recompute_track_missing(tr)
                if self.tree.exists(tr.path):
                    vals = list(self.tree.item(tr.path, 'values'))
                    vals[6] = _format_cover_cell(tr)
                    vals[7] = _format_tag_sync_cell(tr, include_lyrics=self.var_lyrics.get(), lyrics_mode=self.var_lyrics_mode.get() or 'auto')
                    self.tree.item(tr.path, values=vals)

        self._finish_worker()

        prefix = '[预演] 删除完成' if summary.get('dry_run') else '删除完成'
        fc_part = f"，删除目录 cover.jpg {summary['folder_cover_deleted']} 个" if summary.get('folder_cover_deleted') else ''
        fa_part = f"，删除目录 artist.jpg {summary['folder_artist_deleted']} 个" if summary.get('folder_artist_deleted') else ''
        msg = (
            f"{prefix}：修改文件 {summary['deleted_ok']} 首{fc_part}{fa_part}，"
            f"跳过 {summary['deleted_skipped']} 首，失败 {summary['deleted_fail'] + summary['folder_cover_fail'] + summary.get('folder_artist_fail', 0)} 项。"
        )
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)
        _msgbox.showinfo('预演完成' if summary.get('dry_run') else '完成', msg)

