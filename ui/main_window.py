"""MusicMetadata 主窗口图形界面组件与数据视图。"""

from __future__ import annotations

import ctypes
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from art_repair import (
    DELETABLE_FIELDS,
    LYRICS_MODES,
    MAX_SIDE,
    SETTABLE_FIELDS,
    SUPPORTS_COVER_WRITE,
    FolderArtPlan,
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
from cli import build_arg_parser, run_cli
from ui.dialogs import ConvertFormatDialog, DeleteMetadataDialog, FolderManagerDialog, SetMetadataDialog, SourceConfigDialog
from foobar_library import (
    CORE_TEXT_FIELDS,
    EXTRA_TEXT_FIELDS,
    FIELD_LABELS,
    STATUS_ERROR,
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
    FoobarEnv,
    LibraryTrack,
    detect_foobar_env,
    get_running_foobar_exes,
    scan_library,
    sync_files_to_foobar,
    track_has_pending_work,
)
from providers import ONLINE_PROVIDERS, PROVIDER_LABELS
from runner import (
    FOLDER_COVER_LABELS,
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SetOptions,
    _album_date_group_key,
    _count_deletable_targets,
    _run_pool_cancellable,
    default_checked,
    execute_convert,
    execute_deletions,
    execute_repairs,
    execute_set_tags,
    parse_delete_fields,
)
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg, has_valid_tracknumber, read_audio_info
from track_lookup import (
    TrackMatch,
    clear_caches,
    fetch_cover_bytes,
    get_fanart_keys,
    get_netease_api_base,
    get_spotify_credentials,
    get_tadb_api_key,
    parse_source_list,
    resolve_track_metadata,
    set_fanart_keys,
    set_netease_api_base,
    set_spotify_credentials,
    set_tadb_api_key,
)
from user_config import (
    get_default_music_folders,
    has_foobar_env,
    load_user_config,
    normalize_folder_list,
    reveal_in_file_manager,
    save_user_config,
    ui_font,
)




def _detect_foobar_env(*args, **kwargs):
    fn = getattr(sys.modules.get('app'), 'detect_foobar_env', detect_foobar_env)
    return fn(*args, **kwargs)


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

def _format_status_badge(tr: LibraryTrack, *, include_lyrics: bool = False) -> tuple[str, str]:
    if tr.status == STATUS_ERROR:
        return '读取异常', 'err'
    if tr.is_temp_duplicate:
        return '临时副本', 'warn'
    has_online_tags = bool(
        tr.needs_tracknumber or tr.needs_date or tr.missing_core_fields
        or tr.needs_extra_tags or (include_lyrics and tr.needs_lyrics)
    )
    if tr.is_full_album and has_online_tags and not (tr.needs_cover_fix or tr.needs_cover_online or tr.needs_tag_fix or tr.needs_foobar_sync):
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
    if include_lyrics and tr.needs_lyrics and not tr.is_full_album:
        tags.append('缺歌词')
    if tr.needs_foobar_sync:
        tags.append('缓存过期')
    if not tags:
        return ('缺封面无源', 'warn') if tr.cover_status.startswith('缺封面') else ('正常', 'ok')
    return ' / '.join(tags), ('err' if '缺音轨' in tags else ('warn' if any(t in tags for t in ('封面', '缺标签', '缺日期', '缺扩展', '缺歌词')) else 'info'))


def _format_cover_cell(tr: LibraryTrack) -> str:
    parts = [f'待修:{",".join(tr.cover_reasons)}' if tr.needs_cover_fix else (tr.cover_status if tr.cover_status != '正常' else '')]
    if tr.folder_cover_action in FOLDER_COVER_LABELS:
        parts.append(FOLDER_COVER_LABELS[tr.folder_cover_action])
    parts = [p for p in parts if p]
    return '；'.join(parts) if parts else '正常'


def _format_tag_sync_cell(tr: LibraryTrack, *, include_lyrics: bool = False) -> str:
    parts = []
    if tr.needs_foobar_sync:
        parts.append('fb:' + ','.join(tr.foobar_stale_reasons) if tr.foobar_stale_reasons else 'fb缓存过期')
    fix_items = [f'{k}={v}' for k, v in tr.tag_updates.items()] if tr.tag_updates else []
    if tr.target_disc:
        fix_items.append(f'discnumber={tr.target_disc}')
    if tr.needs_tag_fix and fix_items:
        parts.append('补:' + ','.join(fix_items))
    if not tr.is_full_album:
        pending = [f for f in (tr.missing_core_fields + tr.missing_extra_fields) if f not in tr.tag_updates]
        if include_lyrics and tr.needs_lyrics:
            pending.append('lyrics')
        if pending:
            labels = [FIELD_LABELS.get(f, f) for f in pending[:4]]
            more = f'等{len(pending)}项' if len(pending) > 4 else ''
            parts.append('待联网:' + ','.join(labels) + more)
    return '；'.join(parts) if parts else '正常'


def _resource_path(rel_path: str) -> str:
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


def _apply_window_icon(root: tk.Tk) -> tk.PhotoImage | None:
    """加载应用图标，优先使用多尺寸 .ico，缺失时回退 .png。"""
    ico_path = _resource_path(os.path.join('assets', 'app_icon.ico'))
    if os.path.isfile(ico_path):
        try:
            root.iconbitmap(default=ico_path)
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


class MainWindow:
    _COLS = ('chk', 'status', 'title', 'artist', 'album', 'track', 'cover', 'tagsync', 'note')
    _HEADS = ('选择', '综合状态', '歌曲名', '艺术家', '专辑', '音轨号', '封面状态', '标签检查', '说明 / 结果')
    _WIDTHS = (44, 108, 175, 115, 145, 105, 145, 165, 240)
    _PROG_LABELS = {
        'fix_prog': '处理中',
        'del_prog': '删除元数据',
        'set_prog': '设置标签',
        'conv_prog': '转换格式',
    }
    _PREFER_PRESETS: dict[str, tuple[str, ...]] = {
        '默认': (),
        '网易云优先': ('netease',),
        'QQ音乐优先': ('qq',),
        'Apple/iTunes优先': ('itunes',),
        'MusicBrainz优先': ('mb',),
        '欧美/国际优先': ('itunes', 'deezer', 'ytmusic', 'lrclib', 'mb', 'spotify', 'tadb', 'fanart'),
        'YouTube Music优先': ('ytmusic', 'lrclib'),
        'Deezer优先': ('deezer', 'lrclib'),
        'TheAudioDB优先': ('tadb',),
        'Fanart.tv优先': ('fanart',),
        '酷狗优先': ('kugou',),
        '咪咕优先': ('migu',),
        '千千优先': ('baidu',),
    }

    def __init__(self, root: tk.Tk, auto_scan: bool = False):
        self.root = root
        self.root.title('MusicMetadata')
        self._icon_photo = _apply_window_icon(self.root)
        self.root.geometry('1220x760')
        self.root.minsize(920, 560)

        self._user_config = load_user_config()
        saved_folders = normalize_folder_list(self._user_config.get('custom_folders'), existing_only=True)
        self.env = FoobarEnv()
        self.tracks: list[LibraryTrack] = []
        self.visible_tracks: list[LibraryTrack] = []
        self.custom_folders: list[str] | None = saved_folders or None
        self.checked_paths: set[str] = set()
        self._busy = False
        self._has_scanned = False
        self._prog_indet = False
        self._cancel_flag = False
        self._ev_q: queue.Queue = queue.Queue()

        self.var_track = tk.BooleanVar(value=True)
        self.var_art = tk.BooleanVar(value=True)
        self.var_cover = self.var_art
        self.var_artist_art = tk.BooleanVar(value=bool(self._user_config.get('enable_artist_art', False)))
        self.var_date = tk.BooleanVar(value=True)
        self.var_albumartist = tk.BooleanVar(value=True)
        self.var_artist = tk.BooleanVar(value=True)
        self.var_composer = tk.BooleanVar(value=True)
        self.var_genre = tk.BooleanVar(value=True)
        self.var_disc = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=True)
        self.var_lyrics = tk.BooleanVar(value=bool(self._user_config.get('enable_lyrics', False)))
        saved_lrc_mode = self._user_config.get('lyrics_mode', 'auto')
        self.var_lyrics_mode = tk.StringVar(value=saved_lrc_mode if saved_lrc_mode in LYRICS_MODES else 'auto')
        saved_preset = self._user_config.get('prefer_preset', '默认')
        valid_presets = set(self._PREFER_PRESETS.keys()) | {'自定义'}
        self.var_prefer_preset = tk.StringVar(value=saved_preset if saved_preset in valid_presets else '默认')
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
        self.var_only_pending = tk.BooleanVar(value=True)
        self.var_skip_full = tk.BooleanVar(value=True)
        init_env_msg = (
            f'已加载上次保存的 {len(self.custom_folders)} 个目录；点击「扫描媒体库」开始检查。'
            if self.custom_folders
            else '未自动扫描音乐目录；请点击下方「扫描媒体库」或右上角「选择目录...」开始扫描。'
        )
        self.var_env = tk.StringVar(value=init_env_msg)
        self.var_summary = tk.StringVar(value='尚未扫描，请点击「扫描媒体库」开始检查。')
        self.var_status = tk.StringVar(value='就绪：点击「扫描媒体库」或「选择目录...」开始扫描')

        self._ev_handlers = {
            'scan_done': lambda p: self._on_scan_finished(*p),
            'fix_row': lambda p: self._on_fix_row_updated(*p),
            'del_row': lambda p: self._on_delete_row_updated(*p),
            'set_row': lambda p: self._on_set_row_updated(*p),
            'conv_row': lambda p: self._on_convert_row_updated(*p),
            'fix_done': lambda p: self._on_fix_finished(p),
            'del_done': lambda p: self._on_delete_finished(p),
            'set_done': lambda p: self._on_set_finished(p),
            'conv_done': lambda p: self._on_convert_finished(p),
            'log': self._log,
        }

        self._build_ui()
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

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)

        # 顶部环境与统计卡
        top = ttk.LabelFrame(outer, text='媒体库状态', padding=(10, 6))
        top.pack(fill='x', pady=(0, 8))
        r1 = ttk.Frame(top)
        r1.pack(fill='x')
        ttk.Label(r1, textvariable=self.var_env, anchor='w').pack(side='left', fill='x', expand=True)
        self.btn_reset_lib = ttk.Button(r1, text='使用媒体库', command=self._reset_to_library)
        self.btn_manage_dirs = ttk.Button(r1, text='管理目录...', command=self._manage_custom_folders)
        self.btn_manage_dirs.pack(side='right', padx=(6, 0))
        self.btn_pick_dir = ttk.Button(r1, text='选择目录...', command=self._pick_custom_folder)
        self.btn_pick_dir.pack(side='right', padx=(6, 0))
        ttk.Label(top, textvariable=self.var_summary, foreground='#334155', font=ui_font(9, bold=True)).pack(fill='x', pady=(4, 0))

        # 操作按钮栏
        acts = ttk.Frame(outer)
        acts.pack(fill='x', pady=(0, 6))
        self.btn_one_click = tk.Button(
            acts, text='一键修复已选项目', command=self._on_one_click,
            bg='#2563eb', fg='#ffffff', activebackground='#1d4ed8', activeforeground='#ffffff',
            relief='flat', padx=14, pady=5, font=ui_font(10, bold=True), cursor='hand2',
        )
        self.btn_one_click.pack(side='left', padx=(0, 8))
        self.btn_sync_only = ttk.Button(acts, text='仅刷新 foobar 缓存', command=self._on_sync_only)
        self.btn_sync_only.pack(side='left', padx=(0, 6))
        self.btn_set_meta = ttk.Button(acts, text='设置标签...', command=self._on_set_metadata)
        self.btn_set_meta.pack(side='left', padx=(0, 6))
        self.btn_delete_meta = ttk.Button(acts, text='删除部分元数据...', command=self._on_delete_metadata)
        self.btn_delete_meta.pack(side='left', padx=(0, 6))
        self.btn_convert = ttk.Button(acts, text='格式转换...', command=self._on_convert_format)
        self.btn_convert.pack(side='left', padx=(0, 6))
        self.btn_rescan = ttk.Button(acts, text='扫描媒体库', command=self.start_scan)
        self.btn_rescan.pack(side='left', padx=(0, 6))
        self.btn_toggle_all = ttk.Button(acts, text='全选 / 取消', command=self._toggle_select_all)
        self.btn_toggle_all.pack(side='left', padx=(0, 6))
        self.btn_stop = ttk.Button(acts, text='停止', command=self._on_stop)

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
        self.chk_sync = ttk.Checkbutton(opts_misc, text='刷新 foobar 缓存', variable=self.var_sync, command=self._on_option_changed)
        self.chk_sync.pack(side='left', padx=(0, 10))
        self.chk_artist_art = ttk.Checkbutton(opts_misc, text='获取艺术家图', variable=self.var_artist_art, command=self._on_pref_option_changed)
        self.chk_artist_art.pack(side='left', padx=(0, 10))
        self.chk_lyrics = ttk.Checkbutton(opts_misc, text='补全歌词', variable=self.var_lyrics, command=self._on_pref_option_changed)
        self.chk_lyrics.pack(side='left', padx=(0, 4))
        self.cmb_lyrics_mode = ttk.Combobox(
            opts_misc, textvariable=self.var_lyrics_mode,
            values=list(LYRICS_MODES), state='readonly', width=7,
        )
        self.cmb_lyrics_mode.pack(side='left', padx=(0, 10))
        self.cmb_lyrics_mode.bind('<<ComboboxSelected>>', lambda _e: self._save_user_state())
        ttk.Label(opts_misc, text='音源:').pack(side='left')
        self.cmb_prefer_source = ttk.Combobox(
            opts_misc, textvariable=self.var_prefer_preset,
            values=list(self._PREFER_PRESETS.keys()) + ['自定义'],
            state='readonly', width=16,
        )
        self.cmb_prefer_source.pack(side='left', padx=(4, 4))
        self.cmb_prefer_source.bind('<<ComboboxSelected>>', lambda _e: self._save_user_state())
        self.btn_source_cfg = ttk.Button(opts_misc, text='数据源...', command=self._on_source_config)
        self.btn_source_cfg.pack(side='left', padx=(0, 10))
        self.chk_skip_full = ttk.Checkbutton(opts_misc, text='默认跳过整轨全长', variable=self.var_skip_full, command=self._on_option_changed)
        self.chk_skip_full.pack(side='right')
        self.chk_only_pending = ttk.Checkbutton(opts_misc, text='只显示待处理曲目', variable=self.var_only_pending, command=self._refresh_table)
        self.chk_only_pending.pack(side='right', padx=(0, 12))

        # 底部固定进度与状态栏（先按 side='bottom' 布局，确保窗口较矮时不被分割区挤压隐藏）
        bottom_bar = ttk.Frame(outer)
        bottom_bar.pack(side='bottom', fill='x', pady=(6, 0))
        self.lbl_status = ttk.Label(bottom_bar, textvariable=self.var_status, anchor='w', foreground='#475569')
        self.lbl_status.pack(side='bottom', fill='x')
        self.progress = ttk.Progressbar(bottom_bar, mode='determinate', maximum=100)

        # 主分割区：表格 + 日志
        paned = ttk.PanedWindow(outer, orient='vertical')
        paned.pack(fill='both', expand=True)

        tbl_frame = ttk.Frame(paned)
        self.tree = ttk.Treeview(tbl_frame, columns=self._COLS, show='headings', selectmode='browse')
        for col, head, width in zip(self._COLS, self._HEADS, self._WIDTHS):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, minwidth=36, anchor='center' if col in ('chk', 'status', 'track') else 'w', stretch=(col == 'note'))
        for tag, fg in (('err', '#dc2626'), ('warn', '#b45309'), ('info', '#2563eb'), ('ok', '#15803d'), ('muted', '#64748b')):
            self.tree.tag_configure(tag, foreground=fg)

        vsb = ttk.Scrollbar(tbl_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tbl_frame.rowconfigure(0, weight=1)
        tbl_frame.columnconfigure(0, weight=1)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Double-1>', self._on_tree_double_click)
        self.tree.bind('<space>', self._on_tree_space)
        paned.add(tbl_frame, weight=3)

        log_frame = ttk.Frame(paned)
        self.log_box = tk.Text(
            log_frame, height=8, bg='#0f172a', fg='#e2e8f0', relief='flat',
            font=ui_font(9, mono=True), state='disabled', wrap='word',
        )
        l_sb = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=l_sb.set)
        self.log_box.pack(side='left', fill='both', expand=True)
        l_sb.pack(side='right', fill='y')
        paned.add(log_frame, weight=1)

    def _set_all_fields(self, checked: bool):
        for var in self.field_vars.values():
            var.set(checked)
        self._on_option_changed()

    def _current_options(self, sync_only: bool = False) -> FixOptions:
        any_tag = any(
            v.get() for v in (
                self.var_date, self.var_albumartist, self.var_artist,
                self.var_composer, self.var_genre, self.var_disc,
            )
        )
        any_extra = any(v.get() for v in (self.var_composer, self.var_genre, self.var_disc))
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
            enable_sync=self.var_sync.get(),
            sync_only=sync_only,
        )

    def _log(self, text: str):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', f"[{time.strftime('%H:%M:%S')}] {text}\n")
        if int(self.log_box.index('end-1c').split('.')[0]) > 1500:
            self.log_box.delete('1.0', '200.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def _set_busy(self, busy: bool, text: str = ''):
        self._busy = busy
        state = 'disabled' if busy else 'normal'
        self.btn_one_click.configure(state=state, bg='#93c5fd' if busy else '#2563eb')
        for b in (
            self.btn_sync_only, self.btn_set_meta, self.btn_delete_meta, self.btn_convert,
            self.btn_rescan, self.btn_pick_dir, self.btn_manage_dirs, self.btn_reset_lib,
            self.btn_toggle_all, self.btn_fields_all, self.btn_fields_none,
            self.chk_sync, self.chk_artist_art, self.chk_lyrics, self.btn_source_cfg,
            self.chk_skip_full, self.chk_only_pending,
            *self.field_buttons.values(),
        ):
            b.configure(state=state)
        for cmb in (self.cmb_lyrics_mode, self.cmb_prefer_source):
            cmb.configure(state='disabled' if busy else 'readonly')
        if busy:
            try:
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

    def _save_user_state(self):
        cfg = dict(self._user_config)
        cfg['custom_folders'] = list(self.custom_folders or [])
        cfg['enable_artist_art'] = bool(self.var_artist_art.get())
        cfg['enable_lyrics'] = bool(self.var_lyrics.get())
        cfg['lyrics_mode'] = self.var_lyrics_mode.get() or 'auto'
        cfg['prefer_preset'] = self.var_prefer_preset.get() or '默认'
        cfg['custom_prefer_sources'] = list(self._custom_prefer_sources)
        cfg['only_sources'] = list(self._only_sources)
        cfg['skip_sources'] = list(self._skip_sources)
        cfg['netease_api'] = self._netease_api
        cfg['tadb_api_key'] = self._tadb_api_key
        cfg['fanart_api_key'] = self._fanart_api_key
        cfg['fanart_client_key'] = self._fanart_client_key
        self._user_config = cfg
        save_user_config(cfg)

    def _apply_player_mode(self, is_fb: bool):
        if is_fb:
            if self.btn_sync_only.winfo_manager() != 'pack':
                self.btn_sync_only.pack(side='left', padx=(0, 6), before=self.btn_set_meta)
            if self.chk_sync.winfo_manager() != 'pack':
                self.chk_sync.pack(side='left', padx=(0, 10), before=self.chk_artist_art)
            self.tree.heading('track', text='音轨号(磁盘/fb)')
            self.tree.heading('tagsync', text='缓存/标签检查')
        else:
            if self.btn_sync_only.winfo_manager() == 'pack':
                self.btn_sync_only.pack_forget()
            if self.chk_sync.winfo_manager() == 'pack':
                self.chk_sync.pack_forget()
            self.tree.heading('track', text='音轨号')
            self.tree.heading('tagsync', text='标签检查')

        if self.custom_folders and self.env.library_folders:
            if self.btn_reset_lib.winfo_manager() != 'pack':
                self.btn_reset_lib.pack(side='right', padx=(6, 0), before=self.btn_manage_dirs)
        elif self.btn_reset_lib.winfo_manager() == 'pack':
            self.btn_reset_lib.pack_forget()

    def _pick_custom_folder(self):
        folder = filedialog.askdirectory(title='选择要扫描的音乐文件夹')
        if folder:
            self.custom_folders = [os.path.abspath(folder)]
            self._save_user_state()
            self.start_scan()

    def _manage_custom_folders(self):
        dlg = FolderManagerDialog(self.root, self.custom_folders or [])
        self._last_folder_dialog = dlg
        self.root.wait_window(dlg.top)
        if dlg.result is None:
            return
        self.custom_folders = dlg.result or None
        self._save_user_state()
        self.start_scan()

    def _reset_to_library(self):
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
                env = _detect_foobar_env()
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
                self._ev_q.put(('scan_done', (_detect_foobar_env(), [], None)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_finished(self, env: FoobarEnv, tracks: list[LibraryTrack], fallback_folders: list[str] | None = None):
        self.env, self.tracks = env, tracks
        self._has_scanned = True
        self.btn_rescan.configure(text='重新扫描')
        self._show_progress(False)
        self._set_busy(False)
        is_fb = has_foobar_env(env)
        self._apply_player_mode(is_fb)

        if self.custom_folders:
            prefix = '当前扫描自定义目录：' if is_fb else '当前扫描目录：'
            self.var_env.set(prefix + '；'.join(self.custom_folders))
        elif env.library_folders or env.unavailable_folders:
            fb_state = '运行中' if get_running_foobar_exes() else '未运行'
            fdesc = '；'.join(env.library_folders) if env.library_folders else '无'
            wdesc = f'（不可访问：{"；".join(env.unavailable_folders)}）' if env.unavailable_folders else ''
            self.var_env.set(f'已识别 foobar2000 媒体库（{len(env.library_folders)} 个目录，foobar {fb_state}）：{fdesc}{wdesc}')
        elif fallback_folders:
            self.var_env.set('未检测到 foobar2000，已自动扫描系统音乐目录：' + '；'.join(fallback_folders))
        else:
            self.var_env.set('未检测到 foobar2000 或默认音乐目录，请点击右上角「选择目录...」或「管理目录...」指定文件夹。')

        opts = self._current_options()
        self.checked_paths = {tr.path for tr in tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._update_summary()
        self._refresh_table()

        miss = sum(1 for t in tracks if t.needs_tracknumber and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        cov = sum(1 for t in tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in tracks if t.needs_date and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        tagc = sum(1 for t in tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate))
        stale = sum(1 for t in tracks if t.needs_foobar_sync)
        fb_log = f' | foobar 缓存待刷新 {stale} 首' if (is_fb or stale > 0) else ''
        self._log(f'扫描完成：共 {len(tracks)} 首 | 缺音轨 {miss} 首 | 封面待修 {cov} 首 (目录 cover.jpg {dcov} 个) | 标签待补 {tagc} 首 (缺日期 {datec} 首){fb_log}。')
        self.var_status.set(
            '扫描完成，可按需勾选模块后点击「一键修复已选项目」或「仅刷新 foobar 缓存」'
            if is_fb
            else '扫描完成，可按需勾选模块后点击「一键修复已选项目」'
        )

    def _update_summary(self):
        miss = sum(1 for t in self.tracks if t.needs_tracknumber and not t.is_full_album and not t.is_temp_duplicate)
        cov = sum(1 for t in self.tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in self.tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in self.tracks if t.needs_date and not t.is_full_album and not t.is_temp_duplicate)
        tagc = sum(1 for t in self.tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not t.is_full_album and not t.is_temp_duplicate))
        lrcc = sum(1 for t in self.tracks if t.needs_lyrics and not t.is_full_album and not t.is_temp_duplicate)
        stale = sum(1 for t in self.tracks if t.needs_foobar_sync)
        inc_art = self.var_artist_art.get()
        artist_cnt = sum(1 for t in self.tracks if t.needs_artist_art and not t.is_full_album and not t.is_temp_duplicate)
        dart = len({t.folder for t in self.tracks if t.folder_artist_action in ('create', 'rewrite', 'remove')})
        inc_lrc = self.var_lyrics.get()
        ok_cnt = sum(1 for t in self.tracks if not track_has_pending_work(t, True, True, True, True, True, enable_extra_tags=True, enable_lyrics=inc_lrc, enable_artist_art=inc_art))
        parts = [
            f'共 {len(self.tracks)} 首', f'缺音轨号 {miss}',
            f'封面待修 {cov}' + (f' (目录cover {dcov})' if dcov else ''),
            f'标签待补 {tagc}' + (f' (缺日期 {datec})' if datec else ''),
        ]
        if inc_art:
            parts.append(f'缺艺术家图 {artist_cnt}' + (f' (目录artist {dart})' if dart else ''))
        if inc_lrc:
            parts.append(f'缺歌词 {lrcc}')
        if has_foobar_env(self.env) or stale > 0:
            parts.append(f'foobar待刷新 {stale}')
        parts.append(f'正常 {ok_cnt}')
        self.var_summary.set('  |  '.join(parts))

    def _on_pref_option_changed(self):
        self._save_user_state()
        self._on_option_changed()

    def _on_option_changed(self):
        opts = self._current_options()
        self.checked_paths = {tr.path for tr in self.tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._update_summary()
        self._refresh_table()

    def _row_values(self, tr: LibraryTrack) -> tuple[tuple[str, ...], str]:
        inc_lrc = self.var_lyrics.get()
        st_text, st_tag = _format_status_badge(tr, include_lyrics=inc_lrc)
        chk = '☑' if tr.path in self.checked_paths else '☐'
        title = tr.title or os.path.splitext(tr.filename)[0]
        if tr.needs_foobar_sync and tr.disk_tracknumber:
            tn_disp = f"{tr.disk_tracknumber} / fb:{tr.foobar_tracknumber or '空'}"
        elif tr.disk_track_int == 0:
            tn_disp = '0 (无效)'
        else:
            tn_disp = tr.disk_tracknumber or '无'
        note = tr.match_note or tr.note or tr.path
        return (chk, st_text, title, tr.artist, tr.album, tn_disp, _format_cover_cell(tr), _format_tag_sync_cell(tr, include_lyrics=inc_lrc), note), st_tag

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
                )
            ]
        else:
            self.visible_tracks = list(self.tracks)

        self.tree.delete(*self.tree.get_children())
        for tr in self.visible_tracks:
            vals, tag = self._row_values(tr)
            self.tree.insert('', 'end', iid=tr.path, values=vals, tags=(tag,))

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

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle_row(iid)

    def _on_tree_space(self, _event):
        for iid in self.tree.selection():
            self._toggle_row(iid)

    def _on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and os.path.exists(iid):
            reveal_in_file_manager(iid)

    def _toggle_select_all(self):
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
        self.btn_stop.pack(side='left')
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

    def _open_target_dialog(self, dialog_cls, attr_name: str, action_desc: str, on_ok):
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        targets = [t for t in self.tracks if t.path in self.checked_paths]
        if not targets:
            _msgbox.showinfo(
                '提示',
                f'当前没有已勾选的曲目。\n如需对正常曲目{action_desc}，请先取消勾选“只显示待处理曲目”，再勾选目标曲目。',
            )
            return
        dlg = dialog_cls(self.root, targets, default_sync=self.var_sync.get(), has_foobar=has_foobar_env(self.env))
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
            vals[7] = _format_tag_sync_cell(tr, include_lyrics=inc_lrc)
        if payload.get('source_note'):
            vals[8] = payload['source_note']
        self.tree.item(path, values=vals, tags=(payload.get('color', 'info'),))

    def _sync_track_from_info(self, tr: LibraryTrack, new_info: dict, *, pop_tags=(), cover_deleted: bool = False, artist_cover_deleted: bool = False, synced: bool = False):
        for f in ('title', 'artist', 'albumartist', 'album', 'date', 'composer', 'performer', 'genre', 'comment'):
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
        if synced or not has_foobar_env(self.env):
            tr.needs_foobar_sync, tr.foobar_stale_reasons = False, []
        else:
            tr.needs_foobar_sync = True
            tr.foobar_stale_reasons = ['磁盘修改时间较新']
        tr.status = STATUS_MISSING_DISK if tr.needs_tracknumber else (STATUS_STALE_FOOBAR if tr.needs_foobar_sync else STATUS_OK)

    def _start_fix(self, targets: list[LibraryTrack], opts: FixOptions, busy_label: str):
        self._run_batch_task('fix', targets, opts, execute_repairs, busy_label)

    def _on_one_click(self):
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        opts = self._current_options(sync_only=False)
        if not (opts.enable_track or opts.enable_art or opts.enable_tags or opts.enable_lyrics or opts.enable_sync):
            _msgbox.showinfo('提示', '请至少勾选一项要执行的修复功能。')
            return
        targets = [t for t in self.tracks if t.path in self.checked_paths]
        if not targets:
            _msgbox.showinfo('提示', '当前没有已勾选的待处理曲目。\n如果刚添加新歌，请点击“重新扫描”。')
            return
        self._start_fix(targets, opts, f'正在修复已选曲目元数据 (共 {len(targets)} 首)...')

    def _on_sync_only(self):
        if not self._has_scanned:
            _msgbox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        targets = [t for t in self.tracks if t.path in self.checked_paths] or [t for t in self.tracks if t.needs_foobar_sync]
        if not targets:
            _msgbox.showinfo('提示', '当前没有检测到需要刷新 foobar 缓存的曲目；如需强制刷新，请取消“只显示待处理曲目”并勾选歌曲。')
            return
        self._start_fix(targets, FixOptions(enable_track=False, enable_art=False, enable_tags=False, enable_sync=True, sync_only=True), f'正在刷新 foobar 缓存 (共 {len(targets)} 首)...')

    def _on_stop(self):
        self._cancel_flag = True
        self.btn_stop.configure(state='disabled')
        self.var_status.set('正在停止...')

    def _on_source_config(self):
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
        set_netease_api_base(self._netease_api)
        set_spotify_credentials(self._spotify_client_id, self._spotify_client_secret)
        set_tadb_api_key(self._tadb_api_key)
        set_fanart_keys(self._fanart_api_key, self._fanart_client_key)
        self.var_prefer_preset.set('自定义' if (self._custom_prefer_sources or self._only_sources or self._skip_sources) else '默认')
        self._save_user_state()
        sp_state = '已配置' if (self._spotify_client_id and self._spotify_client_secret) else '未配置(仅缓存)'
        self._log(
            f"已更新数据源配置：优先={','.join(self._custom_prefer_sources) or '默认'} | "
            f"仅用={','.join(self._only_sources) or '不限'} | "
            f"跳过={','.join(self._skip_sources) or '无'} | "
            f"网易云API={self._netease_api or '官方直连'} | "
            f"Spotify API={sp_state}"
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
                    if lmode in ('lrc', 'both') or (lmode == 'auto' and tr.container in ('wav', 'ape', 'wv')):
                        tr.has_lrc_file = True
                    if lmode in ('embed', 'both') or (lmode == 'auto' and tr.container not in ('wav', 'ape', 'wv')):
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
                if payload.get('fixed_sync') or not tr.needs_foobar_sync:
                    tr.needs_foobar_sync, tr.foobar_stale_reasons = False, []
                if payload.get('fixed_folder_cover'):
                    tr.folder_cover_action = 'ok'
                if payload.get('current_track'):
                    tr.disk_tracknumber = str(payload['current_track'])
                    tr.disk_track_int = int(payload['current_track'])
                if not any(k in payload for k in ('fixed_track', 'fixed_disc', 'fixed_cover', 'fixed_artist_art', 'fixed_tags', 'fixed_lyrics', 'fixed_sync', 'fixed_folder_cover')):
                    tr.needs_tracknumber = tr.needs_foobar_sync = False
                if not tr.needs_tracknumber and not tr.needs_foobar_sync:
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
        if summary['written_ok'] > 0 or summary['synced_stale'] > 0 or summary['folder_cover_ok'] > 0:
            _msgbox.showinfo('完成', msg)

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
                    tr.cover_status = '缺封面(待联网)' if tr.needs_cover_online else t_plan.cover_status
                    tr.target_pic_bytes = t_plan.target_pic_bytes
                    tr.target_pic_mime = t_plan.target_pic_mime
                    tr.tag_updates = dict(t_plan.tag_updates)
                    self._recompute_track_missing(tr)
                if self.tree.exists(tr.path):
                    vals = list(self.tree.item(tr.path, 'values'))
                    vals[6] = _format_cover_cell(tr)
                    vals[7] = _format_tag_sync_cell(tr, include_lyrics=self.var_lyrics.get())
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
