"""挑选与切换专辑封面模态对话框（支持单专辑挑选与多专辑流水线向导模式）。"""

from __future__ import annotations

import io
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageTk

from core.config import ui_font
from core.constants import MAX_SIDE
from core.models import LibraryTrack
from providers.base import norm_core
from services.art.image_ops import normalize_jpeg
from services.lookup import fetch_cover_bytes, search_candidate_album_covers
from services.lookup.resolver import _VA_ARTISTS
from tag_io import parse_filename_clues
from ui.dialogs.base import _ModalDialog, _msgbox


def _extract_group_clues(track: LibraryTrack) -> tuple[str, str, str]:
    """智能提取单曲或专辑组的歌曲名、歌手名与专辑名。"""
    raw_fn = os.path.splitext(track.filename)[0]
    clues = parse_filename_clues(track.filename)
    inferred_t = (track.title or '').strip()
    if not inferred_t and track.tag_updates and track.tag_updates.get('title'):
        inferred_t = track.tag_updates['title'].strip()
    if not inferred_t:
        inferred_t = (clues.get('inferred_title') or '').strip()
    if not inferred_t:
        inferred_t = re.sub(r'^\d+[\s\.\-_]+', '', raw_fn).strip() or raw_fn

    inferred_a = (track.artist or '').strip()
    if not inferred_a and track.tag_updates and (track.tag_updates.get('artist') or track.tag_updates.get('albumartist')):
        inferred_a = (track.tag_updates.get('artist') or track.tag_updates.get('albumartist') or '').strip()
    if not inferred_a:
        inferred_a = (clues.get('inferred_artist') or '').strip()
    if not inferred_a and track.folder:
        f_base = os.path.basename(track.folder).strip()
        if f_base and f_base.lower() not in ('music', 'download', 'downloads', 'songs', 'audio', 'tracks', '已下载音乐'):
            inferred_a = f_base

    inferred_alb = (track.album or '').strip()
    if not inferred_alb and track.tag_updates and track.tag_updates.get('album'):
        alb_cand = track.tag_updates['album'].strip()
        # 核心防呆：如果目录推断出的专辑名与歌手名相同（例如父文件夹名为歌手名"邓丽君"），绝不能当成专辑名！
        if not inferred_a or alb_cand.lower() != inferred_a.lower():
            inferred_alb = alb_cand

    # 再次兜底防呆：如果专辑名与歌手名完全一致，说明该目录为歌手分类目录而非专缉名，清空 inferred_alb
    if inferred_alb and inferred_a and inferred_alb.lower() == inferred_a.lower():
        inferred_alb = ''

    return inferred_t, inferred_a, inferred_alb


class CoverPickerDialog(_ModalDialog):
    """挑选与应用专辑封面的可视化弹窗（融合流水线向导与下拉任意切换模式）。"""

    def __init__(
        self,
        parent: tk.Misc,
        album_groups: list[dict] | LibraryTrack,
        *,
        target_tracks: list[LibraryTrack] | None = None,
        on_apply: Callable[[LibraryTrack, dict], None] | None = None,
        on_apply_group: Callable[[dict, dict], None] | None = None,
        on_apply_artist: Callable[[dict, dict], None] | None = None,
        musicbee_artist_dir: str = '',
        prefer_sources: tuple[str, ...] | list[str] | None = None,
        auto_search: bool = True,
    ):
        # 兼容单曲入参与多专辑组入参
        if isinstance(album_groups, LibraryTrack):
            t = album_groups
            inf_t, inf_a, inf_alb = _extract_group_clues(t)
            self.album_groups: list[dict] = [{
                'folder': t.folder,
                'rep_track': t,
                'tracks': list(target_tracks) if target_tracks else [t],
                'album_name': inf_alb,
                'artist': inf_a,
                'title': inf_t,
                'saved': False,
            }]
        else:
            self.album_groups = list(album_groups) if album_groups else []

        if not self.album_groups:
            raise ValueError("album_groups 不能为空")

        self.on_apply = on_apply
        self.on_apply_group = on_apply_group
        self.on_apply_artist = on_apply_artist
        self.musicbee_artist_dir = str(musicbee_artist_dir or '').strip()
        self._wizard_session_memory: dict[str, bytes] = {}
        self.prefer_sources = prefer_sources
        self.auto_search = auto_search
        self._current_index = 0
        self._saved_flags: dict[int, bool] = {i: False for i in range(len(self.album_groups))}
        self._candidates: list[dict] = []
        self._img_cache: dict[str, ImageTk.PhotoImage] = {}
        self._search_thread_id = 0
        self._is_searching = False
        self._search_start_time = 0.0
        self._timer_after_id = None
        self._plat_status_labels: dict[str, ttk.Label] = {}
        self._view_mode = 'dash'

        self.is_multi = len(self.album_groups) > 1
        first_grp = self.album_groups[0]
        init_desc = self._compute_group_desc(first_grp)

        super().__init__(
            parent,
            '选择专辑封面 (多专辑向导)' if self.is_multi else '选择专辑封面',
            None,
            resizable=True,
        )
        self.top.geometry('820x630')
        self.top.minsize(700, 500)

        self.lbl_intro = ttk.Label(self.outer, text=init_desc, wraplength=760)
        self.lbl_intro.pack(anchor='w', pady=(0, 6))

        def _on_outer_configure(event):
            if self.lbl_intro.winfo_exists():
                self.lbl_intro.configure(wraplength=max(500, event.width - 24))
        self.outer.bind('<Configure>', _on_outer_configure)

        # 1. 顶部向导导航栏 (仅在多专辑时呈现，实现方案 B 的自由下拉与翻页)
        if self.is_multi:
            nav_bar = ttk.Frame(self.outer)
            nav_bar.pack(fill='x', pady=(0, 6))
            ttk.Label(nav_bar, text='专辑切换:', font=ui_font(9, bold=True)).pack(side='left', padx=(0, 6))

            self.var_combo_text = tk.StringVar(master=self.top)
            self.cmb_album = ttk.Combobox(
                nav_bar, textvariable=self.var_combo_text, state='readonly', width=46,
            )
            self.cmb_album.pack(side='left', padx=(0, 8))
            self.cmb_album.bind('<<ComboboxSelected>>', self._on_combo_select)

            self.btn_nav_prev = ttk.Button(nav_bar, text='◀ 上一个', width=8, command=self._on_prev_album)
            self.btn_nav_prev.pack(side='left', padx=(0, 4))
            self.btn_nav_next = ttk.Button(nav_bar, text='下一个 ▶', width=8, command=self._on_next_album)
            self.btn_nav_next.pack(side='left')

            self._refresh_combobox_items()

        # 2. 检索词设置栏
        top_bar = ttk.Frame(self.outer)
        top_bar.pack(fill='x', pady=(0, 6))
        ttk.Label(top_bar, text='检索关键词:').pack(side='left', padx=(0, 6))

        # 伴随式艺人写真联动胶囊 (优先 pack side='right'，杜绝因输入框 expand 而被挤压或溢出视窗)
        self.btn_artist_capsule = tk.Button(
            top_bar,
            text='👤 挑选艺人写真: 检测中...',
            font=ui_font(8, bold=True),
            relief='groove',
            bd=1,
            padx=8,
            pady=2,
            cursor='hand2',
            command=self._on_click_artist_capsule,
        )
        self.btn_artist_capsule.pack(side='right', padx=(8, 0))

        self.btn_search = ttk.Button(top_bar, text='重新检索', command=self._trigger_search)
        self.btn_search.pack(side='right', padx=(8, 0))

        self.var_query = tk.StringVar(master=self.top)
        self.ent_query = ttk.Entry(top_bar, textvariable=self.var_query, width=32)
        self.ent_query.pack(side='left', fill='x', expand=True)
        self.ent_query.bind('<Return>', lambda _e: self._trigger_search())

        # 5. 底部按钮动作栏 (先 pack 到底部，确保空间不足时绝不被遮挡或挤压)
        self.btn_bar = ttk.Frame(self.outer)
        self.btn_bar.pack(fill='x', side='bottom', pady=(8, 0))

        if self.is_multi:
            # 方案 A + B 复合向导按钮群
            btn_close = ttk.Button(self.btn_bar, text='完成/退出', command=self.top.destroy)
            btn_close.pack(side='left')

            self.btn_skip = ttk.Button(self.btn_bar, text='跳过此专辑 ⏩', command=self._on_skip_album)
            self.btn_skip.pack(side='right', padx=(6, 0))

            self.btn_apply_only = ttk.Button(self.btn_bar, text='仅应用本专辑', command=lambda: self._apply_current_group(advance=False))
            self.btn_apply_only.pack(side='right', padx=(6, 0))

            self.btn_confirm = tk.Button(
                self.btn_bar,
                text='应用并下一个 ▶',
                bg='#059669',
                fg='white',
                activebackground='#047857',
                activeforeground='white',
                font=ui_font(9, bold=True),
                relief='flat',
                padx=12,
                pady=4,
                cursor='hand2',
                command=lambda: self._apply_current_group(advance=True),
            )
            self.btn_confirm.pack(side='right')
        else:
            # 单专辑模式
            btn_cancel = ttk.Button(self.btn_bar, text='取消', width=8, command=self.top.destroy)
            btn_cancel.pack(side='right')

            self.btn_confirm = tk.Button(
                self.btn_bar,
                text='应用为此专辑封面',
                bg='#059669',
                fg='white',
                activebackground='#047857',
                activeforeground='white',
                font=ui_font(9, bold=True),
                relief='flat',
                padx=12,
                pady=4,
                cursor='hand2',
                command=lambda: self._apply_current_group(advance=False),
            )
            self.btn_confirm.pack(side='right', padx=(0, 8))

        # 4. 选项区域 (紧贴底部动作栏上方)
        opts_bar = ttk.Frame(self.outer)
        opts_bar.pack(fill='x', side='bottom', pady=(0, 6))
        self.var_export_cover = tk.BooleanVar(master=self.top, value=True)
        self.chk_export = ttk.Checkbutton(opts_bar, text='为所在目录生成/覆盖外置封面 (cover/folder.jpg)', variable=self.var_export_cover)
        self.chk_export.pack(side='left')

        self.var_update_meta = tk.BooleanVar(master=self.top, value=True)
        self.chk_meta = ttk.Checkbutton(opts_bar, text='同步更新曲目的专辑名与发行年份', variable=self.var_update_meta)
        self.chk_meta.pack(side='left', padx=(16, 0))

        # 3. 中间主体区 (左侧候选列表 + 右侧封面大图预览，最后 pack 并自适应吸纳所有剩余空间)
        body = ttk.Frame(self.outer)
        body.pack(fill='both', expand=True, pady=(0, 6))

        # 3.1 左侧列表
        left_frame = ttk.LabelFrame(body, text='候选专辑版本', padding=6)
        left_frame.pack(side='left', fill='both', expand=True, padx=(0, 8))

        cols = ('badge', 'album', 'year', 'source')
        self.tree = ttk.Treeview(left_frame, columns=cols, show='headings', selectmode='browse')
        self.tree.heading('badge', text='推荐')
        self.tree.heading('album', text='专辑名称')
        self.tree.heading('year', text='年份')
        self.tree.heading('source', text='来源平台')

        self.tree.column('badge', width=65, minwidth=50, stretch=False, anchor='center')
        self.tree.column('album', width=200, minwidth=140)
        self.tree.column('year', width=75, minwidth=60, stretch=False, anchor='center')
        self.tree.column('source', width=80, minwidth=60, stretch=False, anchor='center')

        t_sb = ttk.Scrollbar(left_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=t_sb.set)
        self.tree.pack(side='left', fill='both', expand=True)
        t_sb.pack(side='right', fill='y')
        self.tree.bind('<<TreeviewSelect>>', self._on_select_cand)
        self.tree.bind('<Double-1>', lambda _e: self._on_double_click_confirm())

        # 3.2 右侧区域：支持在“实时检索进度看板”与“封面大图预览”之间平滑自适应切换
        self.right_frame = ttk.LabelFrame(body, text='封面预览与状态看板', padding=8)
        self.right_frame.pack(side='right', fill='both', padx=(0, 0))

        # 3.2.1 顶部视图模式双分栏切换卡（等宽自适应布局，杜绝字符裁剪与表情符号异常间距）
        top_switch = ttk.Frame(self.right_frame)
        top_switch.pack(fill='x', pady=(0, 6))

        self.btn_switch_preview = ttk.Button(
            top_switch, text='封面大图', command=self._show_preview_view,
        )
        self.btn_switch_preview.pack(side='left', fill='x', expand=True, padx=(0, 2))

        self.btn_switch_dash = ttk.Button(
            top_switch, text='● 检索看板', command=self._show_dashboard_view,
        )
        self.btn_switch_dash.pack(side='left', fill='x', expand=True, padx=(2, 0))

        self.lbl_view_title = ttk.Label(top_switch)

        # 3.2.2 实时进度看板面板 (Dashboard Panel)
        self.dashboard_panel = ttk.Frame(self.right_frame)

        self.lbl_dash_headline = ttk.Label(
            self.dashboard_panel, text='🔍 全网大碟多源检索中...', font=ui_font(9, bold=True), foreground='#1e293b',
        )
        self.lbl_dash_headline.pack(anchor='w', pady=(2, 2))

        self.lbl_dash_target = ttk.Label(
            self.dashboard_panel, text='', font=ui_font(8), foreground='#64748b', wraplength=260,
        )
        self.lbl_dash_target.pack(anchor='w', pady=(0, 4))

        self.prog_bar = ttk.Progressbar(self.dashboard_panel, mode='indeterminate', length=250)
        self.prog_bar.pack(fill='x', pady=(4, 8))

        # 平台监控卡片
        self.plat_frame = ttk.LabelFrame(self.dashboard_panel, text='各平台音源实时状态', padding=6)
        self.plat_frame.pack(fill='x', pady=(0, 6))

        init_plats = [
            ('qq', '🎵 QQ音乐'),
            ('netease', '🎶 网易云音乐'),
            ('itunes', '🍏 Apple Music'),
        ]
        for pid, pname in init_plats:
            row = ttk.Frame(self.plat_frame)
            row.pack(fill='x', pady=2)
            ttk.Label(row, text=pname, font=ui_font(8)).pack(side='left')
            st_lbl = ttk.Label(row, text='⚪ 等待检索', font=ui_font(8), foreground='#94a3b8')
            st_lbl.pack(side='right')
            self._plat_status_labels[pid] = st_lbl

        # 梯队与指标卡片
        metrics_frame = ttk.LabelFrame(self.dashboard_panel, text='检索梯队与指标', padding=6)
        metrics_frame.pack(fill='x', pady=(0, 6))

        self.lbl_dash_tier = ttk.Label(
            metrics_frame, text='🎯 策略: 正在规划检索梯队...', font=ui_font(8), foreground='#334155', wraplength=250,
        )
        self.lbl_dash_tier.pack(anchor='w', pady=(1, 2))

        m_row = ttk.Frame(metrics_frame)
        m_row.pack(fill='x', pady=(2, 1))
        self.lbl_dash_count = ttk.Label(m_row, text='📦 候选大碟: 0 张', font=ui_font(8, bold=True), foreground='#059669')
        self.lbl_dash_count.pack(side='left')
        self.lbl_dash_time = ttk.Label(m_row, text='⏱️ 耗时: 0.0s', font=ui_font(8), foreground='#64748b')
        self.lbl_dash_time.pack(side='right')

        # 底部动态微日志
        self.lbl_dash_log = ttk.Label(
            self.dashboard_panel, text='▶ 准备开始多源并发探测...', font=ui_font(8), foreground='#475569', wraplength=260,
        )
        self.lbl_dash_log.pack(anchor='w', fill='x', pady=(4, 0))

        # 3.2.3 封面大图预览面板 (Preview Panel)
        self.preview_panel = ttk.Frame(self.right_frame)

        self.lbl_preview = ttk.Label(self.preview_panel, text='暂无大图预览', anchor='center')
        self.lbl_preview.pack(fill='both', expand=True, padx=4, pady=4)
        self.lbl_preview_info = ttk.Label(self.preview_panel, text='', font=ui_font(8), foreground='#64748b', anchor='center')
        self.lbl_preview_info.pack(fill='x', pady=(4, 0))

        # 默认挂载进度看板
        self.dashboard_panel.pack(fill='both', expand=True)

        def _on_right_configure(event):
            w = max(240, event.width - 24)
            for lbl in (self.lbl_dash_target, self.lbl_dash_tier, self.lbl_dash_log):
                if lbl.winfo_exists():
                    lbl.configure(wraplength=w)
        self.right_frame.bind('<Configure>', _on_right_configure)

        self.btn_confirm.configure(state='disabled')

        # 初始化载入第一个专辑并触发检索
        self._load_group(0, auto_search=auto_search)

    # -------------------------------------------------------------------------
    # 状态与向导辅助
    # -------------------------------------------------------------------------

    def _compute_group_desc(self, grp: dict) -> str:
        alb = (grp.get('album_name') or '').strip()
        art = (grp.get('artist') or '').strip()
        title = (grp.get('title') or '').strip()
        cnt = len(grp.get('tracks') or [])
        if not alb or (art and alb.lower() == art.lower()):
            hint = f'《{title}》' if title else f'《{grp["rep_track"].filename}》'
        else:
            hint = f'《{alb}》'
        art_hint = f' ({art})' if art else ''
        if cnt > 1:
            return f'正在为 {hint}{art_hint} 检索全网候选大碟封面（将应用于已选 {cnt} 首曲目及所在目录外置封面）：'
        return f'正在为 {hint}{art_hint} 检索全网候选大碟封面（将生成所在目录外置封面）：'

    def _refresh_combobox_items(self):
        if not self.is_multi:
            return
        items = []
        for i, g in enumerate(self.album_groups):
            alb = (g.get('album_name') or '').strip()
            art = (g.get('artist') or '').strip() or '群星'
            title = (g.get('title') or '').strip()
            if not alb or (art and alb.lower() == art.lower()):
                display_name = title or g['rep_track'].filename
            else:
                display_name = alb
            cnt = len(g.get('tracks') or [])
            st = ' [已保存 ✓]' if self._saved_flags.get(i) else ' [待挑选]'
            items.append(f'{i + 1}/{len(self.album_groups)}: {art} - 《{display_name}》 ({cnt}首){st}')
        self.cmb_album['values'] = items
        if 0 <= self._current_index < len(items):
            self.cmb_album.current(self._current_index)

    def _load_group(self, idx: int, auto_search: bool | None = None):
        if not (0 <= idx < len(self.album_groups)):
            return
        if auto_search is None:
            auto_search = self.auto_search
        self._current_index = idx
        grp = self.album_groups[idx]
        rep_t = grp['rep_track']

        # 更新弹窗提示与多曲目文案
        self.lbl_intro.configure(text=self._compute_group_desc(grp))
        cnt = len(grp.get('tracks') or [])
        meta_txt = f'同步更新已选 {cnt} 首曲目的专辑名与发行年份' if cnt > 1 else '同步更新曲目的专辑名与发行年份'
        self.chk_meta.configure(text=meta_txt)

        # 智能提取默认检索词
        inf_t, inf_a, inf_alb = _extract_group_clues(rep_t)
        artist_name = inf_a or grp.get('artist') or ''

        # 核心判断：优先使用明确的专辑名；若专辑名缺失或与歌手名完全一致（误把歌手目录当专辑名），使用歌曲名！
        if inf_alb and (not artist_name or inf_alb.lower() != artist_name.lower()):
            core_name = inf_alb
        else:
            core_name = inf_t or grp.get('title') or os.path.splitext(rep_t.filename)[0]

        clean_core = re.sub(r'^\d+[\s\.\-_]+', '', core_name).strip() or core_name

        # 防呆：如果 clean_core 处理后依然是歌手名（例如歌名误填为歌手名）
        if artist_name and clean_core.lower() == artist_name.lower():
            fallback_title = inf_t or (parse_filename_clues(rep_t.filename).get('inferred_title') or '').strip()
            if fallback_title and fallback_title.lower() != artist_name.lower():
                clean_core = fallback_title

        # 关键词组装：彻底杜绝 "邓丽君 邓丽君" 重复词
        if clean_core and artist_name and clean_core.lower() != artist_name.lower():
            if clean_core.lower().endswith(artist_name.lower()) or clean_core.lower().startswith(artist_name.lower()):
                query_str = clean_core
            else:
                query_str = f'{clean_core} {artist_name}'.strip()
        else:
            query_str = clean_core or artist_name

        self.var_query.set(query_str)

        if self.is_multi:
            self._refresh_combobox_items()
            self.btn_nav_prev.configure(state='normal' if idx > 0 else 'disabled')
            self.btn_nav_next.configure(state='normal' if idx < len(self.album_groups) - 1 else 'disabled')
            # 最后一个专辑将“应用并下一个”变更为“应用并完成”
            if idx == len(self.album_groups) - 1:
                self.btn_confirm.configure(text='应用并完成退出 ✔')
            else:
                self.btn_confirm.configure(text='应用并下一个 ▶')

        # 更新伴随式艺人写真胶囊状态
        self._update_artist_capsule()

        # 触发联网检索
        if auto_search:
            self._trigger_search()

    def _update_artist_capsule(self):
        """根据当前专辑艺人的本地文件、MusicBee中央库及向导记忆池更新伴随胶囊状态。"""
        if not hasattr(self, 'btn_artist_capsule') or not self.top.winfo_exists():
            return
        grp = self.album_groups[self._current_index]
        rep_t = grp['rep_track']
        _, inf_a, _ = _extract_group_clues(rep_t)
        art = (inf_a or grp.get('artist') or '').strip()
        tracks = grp.get('tracks') or []
        folder = grp.get('folder') or rep_t.folder or ''

        # 1. 合辑 / 多艺人判定
        distinct_artists = {t.artist for t in tracks if t.artist}
        if len(distinct_artists) > 1 or norm_core(art) in _VA_ARTISTS:
            self.btn_artist_capsule.configure(
                text='👤 挑选艺人写真: 合辑保护 (多歌手)',
                bg='#f3f4f6',
                fg='#6b7280',
                activebackground='#f3f4f6',
                activeforeground='#6b7280',
            )
            return

        # 2. 会话记忆池判定 (本次向导已配置)
        if art in self._wizard_session_memory or norm_core(art) in self._wizard_session_memory:
            self.btn_artist_capsule.configure(
                text=f'👤 挑选艺人写真: 本次已配置 ✓ ({art})',
                bg='#ecfdf5',
                fg='#059669',
                activebackground='#d1fae5',
                activeforeground='#047857',
            )
            return

        # 3. 本地磁盘 / MusicBee 判定
        has_local = False
        if folder and os.path.isdir(folder):
            if os.path.isfile(os.path.join(folder, 'artist.jpg')) or os.path.isfile(os.path.join(folder, 'artist.png')):
                has_local = True
        if not has_local and self.musicbee_artist_dir and art:
            mb_path = os.path.join(self.musicbee_artist_dir, f'{art}.jpg')
            if os.path.isfile(mb_path):
                has_local = True

        if has_local:
            self.btn_artist_capsule.configure(
                text='👤 挑选艺人写真: 本地已就绪 ✓',
                bg='#f0fdf4',
                fg='#16a34a',
                activebackground='#dcfce7',
                activeforeground='#15803d',
            )
        else:
            self.btn_artist_capsule.configure(
                text='👤 挑选艺人写真: 缺失 ⚠️ | 顺手精选',
                bg='#fffbeb',
                fg='#b45309',
                activebackground='#fef3c7',
                activeforeground='#92400e',
            )

    def _on_click_artist_capsule(self):
        """点击伴随胶囊拉起艺术家写真挑选器。"""
        grp = self.album_groups[self._current_index]
        rep_t = grp['rep_track']
        _, inf_a, _ = _extract_group_clues(rep_t)
        art = (inf_a or grp.get('artist') or '').strip()
        tracks = grp.get('tracks') or []
        folder = grp.get('folder') or rep_t.folder or ''

        distinct_artists = {t.artist for t in tracks if t.artist}
        if len(distinct_artists) > 1 or norm_core(art) in _VA_ARTISTS:
            _msgbox().showinfo(
                '多艺人合辑保护',
                '当前目录包含多位不同歌手的曲目或为群星合辑，为防止艺术家写真串图霸占目录，系统已对目录外置 artist.jpg 进行物理隔离保护。\n\n如需为特定单曲挑选内嵌写真，可在主列表选中该曲目后右键进行独立挑选。',
                parent=self.top,
            )
            return

        if not art:
            _msgbox().showwarning('缺少艺人名', '当前专辑未识别到有效艺术家名称，无法进行写真检索。', parent=self.top)
            return

        from ui.dialogs.artist_dlg import ArtistPickerDialog

        def _on_art_applied(applied_grp: dict, res_info: dict):
            jpg = res_info.get('jpeg_bytes')
            if jpg:
                self._wizard_session_memory[art] = jpg
                self._wizard_session_memory[norm_core(art)] = jpg
            self._update_artist_capsule()
            if self.on_apply_artist:
                try:
                    self.on_apply_artist(applied_grp, res_info)
                except Exception:
                    pass

        ArtistPickerDialog(
            self.top,
            art,
            folder=folder,
            target_tracks=tracks,
            musicbee_artist_dir=self.musicbee_artist_dir,
            on_apply=_on_art_applied,
            prefer_sources=self.prefer_sources,
        )

    def _collect_missing_artist_groups(self) -> dict[str, dict]:
        """盘点当前多专辑批次中尚无写真的独立艺人组。"""
        missing: dict[str, dict] = {}
        for grp in self.album_groups:
            rep_t = grp['rep_track']
            _, inf_a, _ = _extract_group_clues(rep_t)
            art = (inf_a or grp.get('artist') or '').strip()
            if not art or norm_core(art) in _VA_ARTISTS or art in missing:
                continue
            tracks = grp.get('tracks') or []
            if len({t.artist for t in tracks if t.artist}) > 1:
                continue
            if art in self._wizard_session_memory or norm_core(art) in self._wizard_session_memory:
                continue
            folder = grp.get('folder') or rep_t.folder or ''
            has_local = False
            if folder and os.path.isdir(folder):
                if os.path.isfile(os.path.join(folder, 'artist.jpg')) or os.path.isfile(os.path.join(folder, 'artist.png')):
                    has_local = True
            if not has_local and self.musicbee_artist_dir and art:
                mb_path = os.path.join(self.musicbee_artist_dir, f'{art}.jpg')
                if os.path.isfile(mb_path):
                    has_local = True
            if not has_local:
                missing[art] = {
                    'artist': art,
                    'folder': folder,
                    'tracks': tracks,
                    'hint': grp.get('title') or rep_t.title or '',
                    'saved': False,
                }
        return missing

    def _on_combo_select(self, _event=None):
        idx = self.cmb_album.current()
        if idx >= 0 and idx != self._current_index:
            self._load_group(idx)

    def _on_prev_album(self):
        if self._current_index > 0:
            self._load_group(self._current_index - 1)

    def _on_next_album(self):
        if self._current_index < len(self.album_groups) - 1:
            self._load_group(self._current_index + 1)

    def _on_skip_album(self):
        if self._current_index < len(self.album_groups) - 1:
            self._load_group(self._current_index + 1)
        else:
            _msgbox.showinfo('完成', '已到达最后一个专辑。')

    # -------------------------------------------------------------------------
    # 视图切换与实时看板辅助
    # -------------------------------------------------------------------------

    def _show_dashboard_view(self):
        self._view_mode = 'dash'
        self.preview_panel.pack_forget()
        self.dashboard_panel.pack(fill='both', expand=True)
        self.btn_switch_dash.configure(text='● 检索看板')
        self.btn_switch_preview.configure(text='封面大图')
        if hasattr(self, 'lbl_view_title'):
            self.lbl_view_title.configure(text='实时检索看板')

    def _show_preview_view(self):
        self._view_mode = 'preview'
        self.dashboard_panel.pack_forget()
        self.preview_panel.pack(fill='both', expand=True)
        self.btn_switch_preview.configure(text='● 封面大图')
        self.btn_switch_dash.configure(text='检索看板')
        if hasattr(self, 'lbl_view_title'):
            self.lbl_view_title.configure(text='封面大图预览')

    def _tick_timer(self):
        if not self._is_searching or not self.top.winfo_exists():
            return
        elapsed = time.time() - self._search_start_time
        self.lbl_dash_time.configure(text=f'⏱️ 耗时: {elapsed:.1f}s')
        self._timer_after_id = self.top.after(100, self._tick_timer)

    def _stop_timer(self):
        self._is_searching = False
        if self._timer_after_id:
            try:
                self.top.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None

    def _on_cancel(self):
        self._stop_timer()
        super()._on_cancel()

    def _handle_progress_evt(self, evt: dict, tid: int):
        if not self.top.winfo_exists() or tid != self._search_thread_id:
            return
        etype = evt.get('type')
        if etype == 'tier_start':
            tname = evt.get('tier_name') or evt.get('kind', '')
            tidx = evt.get('tier_idx', 1)
            ttotal = evt.get('total_tiers', 1)
            self.lbl_dash_tier.configure(text=f'🎯 梯队 [{tidx}/{ttotal}]: {tname}')
            self.lbl_dash_log.configure(text=f'▶ 开始第 {tidx} 梯队检索: 《{evt.get("term", "")}》')
        elif etype == 'platform_start':
            plat = evt.get('platform')
            plabel = evt.get('platform_label', plat)
            if plat in self._plat_status_labels:
                self._plat_status_labels[plat].configure(text='⏳ 检索中...', foreground='#0284c7')
            self.lbl_dash_log.configure(text=f'▶ 正在从 {plabel} 检索《{evt.get("term", "")}》...')
        elif etype == 'platform_done':
            plat = evt.get('platform')
            plabel = evt.get('platform_label', plat)
            added = evt.get('added', 0)
            if plat in self._plat_status_labels:
                if added > 0:
                    self._plat_status_labels[plat].configure(text=f'🟢 命中 {added} 张', foreground='#059669')
                else:
                    cur_text = self._plat_status_labels[plat].cget('text')
                    if not cur_text.startswith('🟢'):
                        self._plat_status_labels[plat].configure(text='⚪ 未收录', foreground='#94a3b8')
            cnt = evt.get('found_count', 0)
            self.lbl_dash_count.configure(text=f'📦 候选大碟: {cnt} 张')
            if added > 0:
                self.lbl_dash_log.configure(text=f'✓ {plabel} 成功捕获 {added} 张候选封面')
            else:
                self.lbl_dash_log.configure(text=f'ℹ {plabel} 本轮无新候选')
        elif etype == 'early_stop':
            self.lbl_dash_log.configure(text='★ 已命中足够丰富的高品质首版大碟，提前完成检索')
        elif etype == 'scoring':
            cnt = evt.get('found_count', 0)
            self.lbl_dash_log.configure(text=f'★ 正在对 {cnt} 个候选大碟执行首版/BTB/年份智能加权...')
        elif etype == 'done':
            cnt = evt.get('final_count', 0)
            self.lbl_dash_count.configure(text=f'📦 候选大碟: {cnt} 张')

    # -------------------------------------------------------------------------
    # 检索逻辑
    # -------------------------------------------------------------------------

    def _trigger_search(self):
        term = self.var_query.get().strip()
        if not term:
            return
        self._search_thread_id += 1
        my_tid = self._search_thread_id

        self.btn_search.configure(state='disabled')
        self.btn_confirm.configure(state='disabled')
        self.tree.delete(*self.tree.get_children())

        # 激活实时看板并重置统计
        self._stop_timer()
        self._is_searching = True
        self._search_start_time = time.time()
        self._tick_timer()

        self.prog_bar.configure(mode='indeterminate', value=0)
        self.prog_bar.start(12)

        self.lbl_dash_headline.configure(text='🔍 全网大碟多源检索中...', foreground='#1e293b')
        self.lbl_dash_target.configure(text=f'检索关键词: 《{term}》')
        self.lbl_dash_tier.configure(text='🎯 正在根据关键词规划检索梯队...')
        self.lbl_dash_count.configure(text='📦 候选大碟: 0 张')
        self.lbl_dash_time.configure(text='⏱️ 耗时: 0.0s')
        self.lbl_dash_log.configure(text='▶ 正在初始化音源连接...')

        for pid, lbl in self._plat_status_labels.items():
            lbl.configure(text='⚪ 等待检索', foreground='#94a3b8')

        self.btn_switch_preview.configure(state='disabled')
        self._show_dashboard_view()

        cur_grp = self.album_groups[self._current_index]
        rep_t = cur_grp['rep_track']

        def _worker():
            try:
                clean_term = re.sub(r'^\d+[\s\.\-_]+', '', term).strip() or term
                art_hint = (cur_grp.get('artist') or '').strip()
                alb_hint = (cur_grp.get('album_name') or '').strip()
                title_hint = (cur_grp.get('title') or '').strip()

                if art_hint and clean_term.lower().endswith(art_hint.lower()) and len(clean_term) > len(art_hint):
                    core_part = clean_term[:-len(art_hint)].strip()
                    cand_art = art_hint
                elif ' ' in clean_term:
                    parts = clean_term.rsplit(' ', 1)
                    core_part = parts[0].strip()
                    cand_art = parts[1].strip()
                else:
                    core_part = clean_term
                    cand_art = art_hint

                # 贯彻三级梯队：传递 album、title 与 artist
                target_alb = alb_hint if (alb_hint and alb_hint.lower() != cand_art.lower()) else (core_part if alb_hint else '')
                target_title = title_hint or (core_part if not target_alb else '')

                def _on_progress_cb(evt: dict):
                    try:
                        if self.top.winfo_exists() and my_tid == self._search_thread_id:
                            self.top.after(0, lambda e=evt: self._handle_progress_evt(e, my_tid))
                    except Exception:
                        pass

                cands = search_candidate_album_covers(
                    target_title or core_part,
                    cand_art,
                    album=target_alb,
                    limit=14,
                    prefer_sources=self.prefer_sources,
                    on_progress=_on_progress_cb,
                )
            except Exception:
                cands = []

            def _done():
                try:
                    if not self.top.winfo_exists() or my_tid != self._search_thread_id:
                        return
                    self._stop_timer()
                    self.prog_bar.stop()
                    self.prog_bar.configure(mode='determinate', value=100)
                    total_time = max(0.01, time.time() - self._search_start_time)
                    self.lbl_dash_time.configure(text=f'⏱️ 耗时: {total_time:.2f}s')

                    self.btn_search.configure(state='normal')
                    self._candidates = cands
                    if not cands:
                        self.lbl_dash_headline.configure(text='⚠️ 未检索到候选大碟封面', foreground='#dc2626')
                        self.lbl_dash_log.configure(text='建议修改或简化上方检索关键词后点击【重新检索】。')
                        self.lbl_preview.configure(text='未检索到候选专辑封面', image='')
                        self.lbl_preview_info.configure(text='')
                        self.btn_switch_preview.configure(state='disabled')
                        return

                    self.lbl_dash_headline.configure(text=f'✅ 检索完成 (共优选 {len(cands)} 个版本)', foreground='#059669')
                    self.lbl_dash_log.configure(text=f'已按首版发行年份智能排序，首推: 《{cands[0]["album_name"]}》')

                    for idx, c in enumerate(cands):
                        badge = '★ 首版' if idx == 0 and c['score'] >= 80 else ('推荐' if c['score'] >= 50 else '')
                        self.tree.insert('', 'end', iid=str(idx), values=(
                            badge,
                            c['album_name'],
                            c['release_date'][:4] if c['release_date'] else '未知',
                            c['source'],
                        ))
                    self.btn_switch_preview.configure(state='normal')
                    # 默认选中第一个候选（得分最高项）并自动加载封面大图
                    self.tree.selection_set('0')
                    self.tree.focus('0')
                except Exception:
                    pass

            try:
                if self.top.winfo_exists():
                    self.top.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, daemon=True).start()

    def _on_select_cand(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            self.btn_confirm.configure(state='disabled')
            return
        idx = int(sel[0])
        cand = self._candidates[idx]
        self.btn_confirm.configure(state='normal')

        # 选中任一候选大碟时，平滑切至大图预览界面
        self._show_preview_view()

        cov_url = cand.get('cover_url')
        if not cov_url:
            self.lbl_preview.configure(text='该版本暂无封面预览', image='')
            self.lbl_preview_info.configure(text='')
            return

        if cov_url in self._img_cache:
            photo = self._img_cache[cov_url]
            self.lbl_preview.configure(image=photo, text='')
            self.lbl_preview_info.configure(text=f"《{cand['album_name']}》\n来源: {cand['source']} ({cand['release_date'] or '未知'})")
            return

        self.lbl_preview.configure(text='正在下载高清原图预览...', image='')
        self.lbl_preview_info.configure(text=f"《{cand['album_name']}》\n来源: {cand['source']}")

        def _fetch_img():
            try:
                data = fetch_cover_bytes(cov_url)
                if not data:
                    return
                im = Image.open(io.BytesIO(data))
                im.thumbnail((240, 240), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(im)
            except Exception:
                photo = None

            def _show():
                if not self.top.winfo_exists() or not photo:
                    return
                self._img_cache[cov_url] = photo
                cur_sel = self.tree.selection()
                if cur_sel and int(cur_sel[0]) == idx:
                    self.lbl_preview.configure(image=photo, text='')
                    self.lbl_preview_info.configure(text=f"《{cand['album_name']}》\n来源: {cand['source']} ({cand['release_date'] or '未知'})")

            self.top.after(0, _show)

        threading.Thread(target=_fetch_img, daemon=True).start()

    # -------------------------------------------------------------------------
    # 应用与保存逻辑
    # -------------------------------------------------------------------------

    def _on_double_click_confirm(self):
        self._apply_current_group(advance=self.is_multi)

    def _apply_current_group(self, advance: bool = True):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        cand = self._candidates[idx]
        cur_grp = self.album_groups[self._current_index]

        result = {
            'cover_url': cand['cover_url'],
            'album_name': cand['album_name'],
            'release_date': cand['release_date'],
            'artist': cand['artist'],
            'source': cand['source'],
            'export_cover': self.var_export_cover.get(),
            'update_meta': self.var_update_meta.get(),
            'target_tracks': cur_grp['tracks'],
        }

        # 调用业务回调处理写盘与刷新
        if self.on_apply_group:
            self.on_apply_group(cur_grp, result)
        elif self.on_apply:
            self.on_apply(cur_grp['rep_track'], result)

        # 会话记忆池自动渗透写入外置 artist.jpg (若同一艺人在本次向导中已配置写真，且当前目录尚无 artist.jpg)
        grp_art = (cand.get('artist') or cur_grp.get('artist') or '').strip()
        fld = cur_grp.get('folder') or cur_grp['rep_track'].folder or ''
        if fld and os.path.isdir(fld) and not os.path.isfile(os.path.join(fld, 'artist.jpg')):
            cached_pic = self._wizard_session_memory.get(grp_art) or self._wizard_session_memory.get(norm_core(grp_art))
            if cached_pic:
                distinct_a = {t.artist for t in cur_grp.get('tracks') or [] if t.artist}
                if len(distinct_a) <= 1:
                    try:
                        with open(os.path.join(fld, 'artist.jpg'), 'wb') as fp:
                            fp.write(cached_pic)
                    except OSError:
                        pass

        self._saved_flags[self._current_index] = True

        if self.is_multi:
            self._refresh_combobox_items()
            if advance:
                if self._current_index < len(self.album_groups) - 1:
                    # 流水线推进到下一个专辑
                    self._load_group(self._current_index + 1)
                else:
                    # 所有专辑处理完毕：执行结算盘点与链式补漏
                    missing_groups = self._collect_missing_artist_groups()
                    if missing_groups:
                        art_list_str = '\n  • '.join(list(missing_groups.keys())[:5])
                        if len(missing_groups) > 5:
                            art_list_str += f'\n  • ...等共 {len(missing_groups)} 位艺人'
                        prompt = (
                            f'🎉 已完成所有 {len(self.album_groups)} 个专辑的封面挑选与更新！\n\n'
                            f'💡 媒体库视觉资产分析报告：\n'
                            f'本次批处理涉及的艺术家中，有 {len(missing_groups)} 位尚未配置专属写真 (artist.jpg)：\n'
                            f'  • {art_list_str}\n\n'
                            f'是否立即启动「艺术家写真补漏向导」，顺次为这些艺人挑选写真？'
                        )
                        if _msgbox().askyesno('向导结算 - 顺次挑选写真', prompt, parent=self.top):
                            from ui.dialogs.artist_dlg import ArtistPickerDialog
                            top_parent = self.top.master
                            artist_list = list(missing_groups.values())
                            self.top.destroy()
                            ArtistPickerDialog(
                                top_parent,
                                artist_list,
                                musicbee_artist_dir=self.musicbee_artist_dir,
                                prefer_sources=self.prefer_sources,
                            )
                            return
                    _msgbox().showinfo('完成', f'已完成所有 {len(self.album_groups)} 个专辑的封面挑选与更新！', parent=self.top)
                    self.top.destroy()
        else:
            self.top.destroy()
