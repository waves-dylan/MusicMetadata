"""挑选与应用艺术家写真模态对话框（支持单艺人挑选与多艺人向导模式）。"""

from __future__ import annotations

import io
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from PIL import Image, ImageTk

from core.config import ui_font
from core.constants import ARTIST_MAX_SIDE
from core.models import LibraryTrack
from providers.base import http_get_bytes, norm_core
from pipelines.repair import sync_artist_pictures_to_musicbee
from services.art.image_ops import normalize_artist_jpeg, sniff_image
from services.art.planner import _pick_folder_artist_image
from services.art.writer import apply_artist_picture
from services.lookup.resolver import (
    ARTIST_BYTES_CACHE,
    first_artist,
    search_candidate_artist_pics,
)
from tag_io import parse_filename_clues
from ui.dialogs.base import _ModalDialog, _msgbox


def _extract_track_artist(track: LibraryTrack) -> str:
    """提取曲目的首要规范艺术家名。"""
    if track.artist:
        return first_artist(track.artist) or track.artist.strip()
    if track.tag_updates and track.tag_updates.get('artist'):
        raw = track.tag_updates['artist']
        return first_artist(raw) or raw.strip()
    if track.albumartist:
        return first_artist(track.albumartist) or track.albumartist.strip()
    clues = parse_filename_clues(track.filename)
    if clues.get('inferred_artist'):
        return clues['inferred_artist'].strip()
    return ''


class ArtistPickerDialog(_ModalDialog):
    """可视化挑选与应用艺术家写真模态弹窗（支持单艺人模式与多艺人批量向导模式）。"""

    def __init__(
        self,
        parent: tk.Misc,
        artist_groups: list[dict] | LibraryTrack | str,
        *,
        target_tracks: list[LibraryTrack] | None = None,
        folder: str = '',
        musicbee_artist_dir: str = '',
        on_apply: Callable[[dict, dict], None] | None = None,
        prefer_sources: tuple[str, ...] | list[str] | None = None,
        auto_search: bool = True,
    ):
        # 兼容三种入参形式：str (单一艺人名)、LibraryTrack (单曲推断)、list[dict] (多艺人组)
        if isinstance(artist_groups, str):
            art_name = artist_groups.strip()
            self.artist_groups: list[dict] = [{
                'artist': art_name,
                'folder': folder or (target_tracks[0].folder if target_tracks else ''),
                'tracks': list(target_tracks) if target_tracks else [],
                'hint': target_tracks[0].title if target_tracks else '',
                'saved': False,
            }]
        elif isinstance(artist_groups, LibraryTrack):
            t = artist_groups
            art_name = _extract_track_artist(t)
            self.artist_groups = [{
                'artist': art_name,
                'folder': t.folder,
                'tracks': list(target_tracks) if target_tracks else [t],
                'hint': t.title or '',
                'saved': False,
            }]
        else:
            self.artist_groups = list(artist_groups) if artist_groups else []

        if not self.artist_groups:
            raise ValueError("artist_groups 不能为空")

        self.musicbee_artist_dir = str(musicbee_artist_dir or '').strip()
        self.on_apply = on_apply
        self.prefer_sources = prefer_sources
        self.auto_search = auto_search
        self._current_index = 0
        self._saved_flags: dict[int, bool] = {i: False for i in range(len(self.artist_groups))}
        self._candidates: list[dict] = []
        self._img_cache: dict[str, ImageTk.PhotoImage] = {}
        self._search_thread_id = 0
        self._is_searching = False
        self._search_start_time = 0.0
        self._timer_after_id = None
        self._plat_status_labels: dict[str, ttk.Label] = {}
        self._view_mode = 'dash'

        self.is_multi = len(self.artist_groups) > 1
        first_grp = self.artist_groups[0]
        init_desc = self._compute_group_desc(first_grp)

        super().__init__(
            parent,
            '选择艺术家写真 (多艺人向导)' if self.is_multi else '选择艺术家写真 (artist.jpg)',
            None,
            resizable=True,
        )
        self.top.geometry('860x640')
        self.top.minsize(720, 520)

        self.lbl_intro = ttk.Label(self.outer, text=init_desc, wraplength=800)
        self.lbl_intro.pack(anchor='w', pady=(0, 6))

        def _on_outer_configure(event):
            if self.lbl_intro.winfo_exists():
                self.lbl_intro.configure(wraplength=max(500, event.width - 24))
        self.outer.bind('<Configure>', _on_outer_configure)

        # 1. 顶部向导导航栏 (多艺人向导模式呈现)
        if self.is_multi:
            nav_bar = ttk.Frame(self.outer)
            nav_bar.pack(fill='x', pady=(0, 6))
            ttk.Label(nav_bar, text='艺人切换:', font=ui_font(9, bold=True)).pack(side='left', padx=(0, 6))

            self.var_combo_text = tk.StringVar(master=self.top)
            self.cmb_artist = ttk.Combobox(
                nav_bar, textvariable=self.var_combo_text, state='readonly', width=48,
            )
            self.cmb_artist.pack(side='left', padx=(0, 8))
            self.cmb_artist.bind('<<ComboboxSelected>>', self._on_combo_select)

            self.btn_nav_prev = ttk.Button(nav_bar, text='◀ 上一个', width=8, command=self._on_prev_artist)
            self.btn_nav_prev.pack(side='left', padx=(0, 4))
            self.btn_nav_next = ttk.Button(nav_bar, text='下一个 ▶', width=8, command=self._on_next_artist)
            self.btn_nav_next.pack(side='left')

            self._refresh_combobox_items()

        # 2. 检索词设置与音源过滤栏
        top_bar = ttk.Frame(self.outer)
        top_bar.pack(fill='x', pady=(0, 6))
        ttk.Label(top_bar, text='检索关键词:').pack(side='left', padx=(0, 6))
        self.var_query = tk.StringVar(master=self.top)
        self.ent_query = ttk.Entry(top_bar, textvariable=self.var_query, width=28)
        self.ent_query.pack(side='left', fill='x', expand=True, padx=(0, 8))
        self.ent_query.bind('<Return>', lambda _e: self._trigger_search())

        self.btn_search = ttk.Button(top_bar, text='重新检索', command=self._trigger_search)
        self.btn_search.pack(side='left', padx=(0, 8))

        ttk.Label(top_bar, text='音源:').pack(side='left', padx=(0, 4))
        self.var_source_filter = tk.StringVar(master=self.top, value='全部多源 (推荐)')
        self.cmb_source = ttk.Combobox(
            top_bar,
            textvariable=self.var_source_filter,
            values=['全部多源 (推荐)', '国内高清头像 (QQ/网易/酷狗)', '海外无损壁纸 (Fanart/TADB)'],
            state='readonly',
            width=22,
        )
        self.cmb_source.pack(side='left')
        self.cmb_source.bind('<<ComboboxSelected>>', lambda _e: self._trigger_search())

        # 5. 底部按钮动作栏 (严格优先 pack 到 bottom，防遮挡与防挤压)
        self.btn_bar = ttk.Frame(self.outer)
        self.btn_bar.pack(fill='x', side='bottom', pady=(8, 0))

        if self.is_multi:
            btn_close = ttk.Button(self.btn_bar, text='完成/退出', command=self.top.destroy)
            btn_close.pack(side='left')

            self.btn_skip = ttk.Button(self.btn_bar, text='跳过此艺人 ⏩', command=self._on_skip_artist)
            self.btn_skip.pack(side='right', padx=(6, 0))

            self.btn_apply_only = ttk.Button(self.btn_bar, text='仅应用本艺人', command=lambda: self._apply_current_group(advance=False))
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
            btn_cancel = ttk.Button(self.btn_bar, text='取消', width=8, command=self.top.destroy)
            btn_cancel.pack(side='right')

            self.btn_confirm = tk.Button(
                self.btn_bar,
                text='应用为此艺术家写真',
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

        self.var_save_dir = tk.BooleanVar(master=self.top, value=True)
        self.chk_dir = ttk.Checkbutton(
            opts_bar,
            text='保存至当前专辑目录为外置 artist.jpg (仅在单艺人独立目录生效)',
            variable=self.var_save_dir,
        )
        self.chk_dir.pack(anchor='w', pady=(0, 2))

        opts_row2 = ttk.Frame(opts_bar)
        opts_row2.pack(fill='x')

        self.var_embed_tag = tk.BooleanVar(master=self.top, value=True)
        self.chk_embed = ttk.Checkbutton(
            opts_row2,
            text='同步内嵌至音频文件的 Picture Type = 8 (Lead Artist) 标签',
            variable=self.var_embed_tag,
        )
        self.chk_embed.pack(side='left')

        self.var_sync_musicbee = tk.BooleanVar(master=self.top, value=bool(self.musicbee_artist_dir))
        mb_label = f'同步推送至 MusicBee 艺术家中央写真库 ({self.musicbee_artist_dir})' if self.musicbee_artist_dir else '同步推送至 MusicBee 艺术家中央写真库'
        self.chk_mb = ttk.Checkbutton(
            opts_row2,
            text=mb_label,
            variable=self.var_sync_musicbee,
        )
        self.chk_mb.pack(side='left', padx=(16, 0))

        # 3. 中间主体区 (左侧候选列表 + 右侧写真大图与实时看板，pack expand=True)
        body = ttk.Frame(self.outer)
        body.pack(fill='both', expand=True, pady=(0, 6))

        # 3.1 左侧列表
        left_frame = ttk.LabelFrame(body, text='候选写真版本', padding=6)
        left_frame.pack(side='left', fill='both', expand=True, padx=(0, 8))

        cols = ('badge', 'aspect', 'clues', 'source')
        self.tree = ttk.Treeview(left_frame, columns=cols, show='headings', selectmode='browse')
        self.tree.heading('badge', text='推荐')
        self.tree.heading('aspect', text='构图/分辨率')
        self.tree.heading('clues', text='风格/代表作辨别标签')
        self.tree.heading('source', text='来源平台')

        self.tree.column('badge', width=65, minwidth=50, stretch=False, anchor='center')
        self.tree.column('aspect', width=110, minwidth=90, anchor='center')
        self.tree.column('clues', width=210, minwidth=140)
        self.tree.column('source', width=90, minwidth=70, anchor='center')

        scroll = ttk.Scrollbar(left_frame, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')

        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Double-1>', lambda _e: self._apply_current_group(advance=self.is_multi))

        # 3.2 右侧写真大图与检索看板区
        self.right_frame = ttk.LabelFrame(body, text='写真预览与状态看板', padding=6)
        self.right_frame.pack(side='right', fill='both', expand=False, padx=(0, 0))
        self.right_frame.configure(width=340)
        self.right_frame.pack_propagate(False)

        # 模式切换按钮栏（等宽分栏无空隙设计）
        self.mode_switcher_frame = ttk.Frame(self.right_frame)
        self.mode_switcher_frame.pack(fill='x', pady=(0, 6))

        self.btn_tab_preview = tk.Button(
            self.mode_switcher_frame,
            text='● 写真大图',
            relief='solid',
            bd=1,
            padx=10,
            pady=3,
            font=ui_font(9, bold=True),
            bg='#2563eb',
            fg='#ffffff',
            activebackground='#1d4ed8',
            activeforeground='#ffffff',
            cursor='hand2',
            command=lambda: self._switch_view_mode('preview'),
        )
        self.btn_tab_preview.pack(side='left', fill='x', expand=True, padx=(0, 2))

        self.btn_tab_dash = tk.Button(
            self.mode_switcher_frame,
            text='检索看板',
            relief='solid',
            bd=1,
            padx=10,
            pady=3,
            font=ui_font(9),
            bg='#f3f4f6',
            fg='#374151',
            activebackground='#e5e7eb',
            activeforeground='#111827',
            cursor='hand2',
            command=lambda: self._switch_view_mode('dash'),
        )
        self.btn_tab_dash.pack(side='right', fill='x', expand=True, padx=(2, 0))

        # 容器：容纳预览面板与看板面板
        self.panel_container = ttk.Frame(self.right_frame)
        self.panel_container.pack(fill='both', expand=True)

        # 预览面板
        self.preview_panel = ttk.Frame(self.panel_container)
        self.lbl_preview_img = ttk.Label(self.preview_panel, anchor='center')
        self.lbl_preview_img.pack(fill='both', expand=True)
        self.lbl_preview_info = ttk.Label(
            self.preview_panel, text='暂未选择写真', anchor='center', foreground='#6b7280', font=ui_font(8),
            wraplength=320,
        )
        self.lbl_preview_info.pack(fill='x', pady=(4, 0))

        # 看板面板
        self.dash_panel = ttk.Frame(self.panel_container)
        self._build_dashboard_ui(self.dash_panel)

        self._switch_view_mode('dash')

        # 载入当前首项并启动检索
        self._load_current_group(auto_search=self.auto_search)

    def _cancel_timer(self):
        if self._timer_after_id:
            try:
                self.top.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None

    def _on_cancel(self):
        self._cancel_timer()
        super()._on_cancel()

    def _on_confirm(self):
        self._cancel_timer()
        super()._on_confirm()

    def _compute_group_desc(self, grp: dict) -> str:
        art = grp.get('artist') or '未知艺术家'
        hint = grp.get('hint') or ''
        track_cnt = len(grp.get('tracks') or [])
        desc = f'正在为艺术家「{art}」检索全网高清肖像与舞台写真壁纸'
        if hint:
            desc += f' (关联作品: 《{hint}》)'
        if track_cnt > 1:
            desc += f' [共管理 {track_cnt} 首本地曲目]'
        desc += '：'
        return desc

    def _build_dashboard_ui(self, parent: ttk.Frame):
        """构建检索状态实时动态看板。"""
        card_box = ttk.LabelFrame(parent, text='音源矩阵并发探测', padding=6)
        card_box.pack(fill='x', pady=(0, 6))

        plats = [
            ('qq', 'QQ 音乐'),
            ('netease', '网易云音乐'),
            ('fanart', 'Fanart.tv (壁纸)'),
            ('tadb', 'TheAudioDB (壁纸)'),
            ('kugou', '酷狗音乐'),
        ]
        self._plat_status_labels.clear()
        for plat_key, plat_name in plats:
            row = ttk.Frame(card_box)
            row.pack(fill='x', pady=1)
            ttk.Label(row, text=f'• {plat_name}:', font=ui_font(8), width=16).pack(side='left')
            lbl_st = ttk.Label(row, text='等待中...', font=ui_font(8), foreground='#6b7280')
            lbl_st.pack(side='left', fill='x', expand=True)
            self._plat_status_labels[plat_key] = lbl_st

        prog_box = ttk.Frame(parent)
        prog_box.pack(fill='x', pady=(0, 6))
        self.progress_bar = ttk.Progressbar(prog_box, mode='determinate', maximum=100)
        self.progress_bar.pack(fill='x', pady=(0, 2))

        self.lbl_timer = ttk.Label(prog_box, text='检索就绪', font=ui_font(8), foreground='#4b5563')
        self.lbl_timer.pack(anchor='w')

        log_box = ttk.LabelFrame(parent, text='实时流转日志', padding=4)
        log_box.pack(fill='both', expand=True)
        self.txt_log = tk.Text(
            log_box, height=6, wrap='word', font=ui_font(8), bg='#f9fafb', fg='#374151', relief='flat',
        )
        self.txt_log.pack(fill='both', expand=True)
        self.txt_log.configure(state='disabled')

    def _log_activity(self, text: str):
        if not self.top.winfo_exists() or not hasattr(self, 'txt_log'):
            return
        self.txt_log.configure(state='normal')
        self.txt_log.insert('end', f'{text}\n')
        self.txt_log.see('end')
        self.txt_log.configure(state='disabled')

    def _switch_view_mode(self, mode: str):
        self._view_mode = mode
        if mode == 'dash':
            self.preview_panel.pack_forget()
            self.dash_panel.pack(fill='both', expand=True)
            self.btn_tab_dash.configure(bg='#2563eb', fg='#ffffff', font=ui_font(9, bold=True))
            self.btn_tab_preview.configure(bg='#f3f4f6', fg='#374151', font=ui_font(9))
        else:
            self.dash_panel.pack_forget()
            self.preview_panel.pack(fill='both', expand=True)
            self.btn_tab_preview.configure(bg='#2563eb', fg='#ffffff', font=ui_font(9, bold=True))
            self.btn_tab_dash.configure(bg='#f3f4f6', fg='#374151', font=ui_font(9))

    def _refresh_combobox_items(self):
        if not self.is_multi or not hasattr(self, 'cmb_artist'):
            return
        items = []
        for i, grp in enumerate(self.artist_groups):
            art = grp.get('artist') or '未知艺术家'
            cnt = len(grp.get('tracks') or [])
            st = ' [已配置 ✓]' if self._saved_flags.get(i) else ' [待挑选]'
            items.append(f'{i+1}/{len(self.artist_groups)}: {art} ({cnt}首){st}')
        self.cmb_artist['values'] = items
        if 0 <= self._current_index < len(items):
            self.cmb_artist.current(self._current_index)

    def _load_current_group(self, auto_search: bool = True):
        grp = self.artist_groups[self._current_index]
        art = grp.get('artist') or ''
        self.lbl_intro.configure(text=self._compute_group_desc(grp))
        self.var_query.set(art)

        if self.is_multi:
            self._refresh_combobox_items()
            if self._current_index == len(self.artist_groups) - 1:
                self.btn_confirm.configure(text='★ 应用并完成退出')
            else:
                self.btn_confirm.configure(text='应用并下一个 ▶')

        if auto_search:
            self._trigger_search()

    def _trigger_search(self):
        query = self.var_query.get().strip()
        if not query:
            return

        for item in self.tree.get_children():
            self.tree.delete(item)
        self._candidates.clear()
        self.lbl_preview_img.configure(image='', text='正在检索写真...')
        self.lbl_preview_info.configure(text='')

        self._switch_view_mode('dash')
        for lbl in self._plat_status_labels.values():
            lbl.configure(text='等待中...', foreground='#6b7280')
        self.progress_bar.configure(value=10)

        self._search_thread_id += 1
        cur_tid = self._search_thread_id
        self._is_searching = True
        self._search_start_time = time.time()
        self._start_timer()

        # 解析音源过滤
        sf = self.var_source_filter.get()
        prefer_sources = None
        if '国内' in sf:
            prefer_sources = ['qq', 'netease', 'kugou']
        elif '海外' in sf:
            prefer_sources = ['fanart', 'tadb']

        cur_grp = self.artist_groups[self._current_index]
        hint = cur_grp.get('hint') or ''

        def _worker():
            try:
                def _on_prog(evt: dict):
                    if cur_tid == self._search_thread_id and self.top.winfo_exists():
                        self.top.after(0, lambda e=evt: self._handle_progress_event(e, cur_tid))

                cands = search_candidate_artist_pics(
                    query,
                    track_hint=hint,
                    limit=14,
                    prefer_sources=prefer_sources,
                    on_progress=_on_prog,
                )
            except Exception as err:
                cands = []
                if cur_tid == self._search_thread_id and self.top.winfo_exists():
                    self.top.after(0, lambda: self._log_activity(f'检索异常: {err}'))

            if cur_tid == self._search_thread_id and self.top.winfo_exists():
                self.top.after(0, lambda: self._on_search_finished(cands, cur_tid))

        t = threading.Thread(target=_worker, daemon=True)
        t.start()

    def _start_timer(self):
        if self._timer_after_id:
            try:
                self.top.after_cancel(self._timer_after_id)
            except Exception:
                pass
            self._timer_after_id = None
        self._update_timer_tick()

    def _update_timer_tick(self):
        if not self._is_searching or not self.top.winfo_exists():
            return
        elapsed = time.time() - self._search_start_time
        cnt = len(self._candidates)
        self.lbl_timer.configure(text=f'检索耗时: {elapsed:.1f}s | 已捕获候选: {cnt} 个')
        self._timer_after_id = self.top.after(100, self._update_timer_tick)

    def _handle_progress_event(self, evt: dict, tid: int):
        if tid != self._search_thread_id or not self.top.winfo_exists():
            return
        etype = evt.get('type')
        if etype == 'start':
            art = evt.get('artist') or ''
            self._log_activity(f'开始为「{art}」发起多源写真并发探测...')
            self.progress_bar.configure(value=20)
        elif etype == 'platform_start':
            plat = evt.get('platform')
            plabel = evt.get('platform_label') or plat
            if plat in self._plat_status_labels:
                self._plat_status_labels[plat].configure(text='正在检索...', foreground='#2563eb')
            self._log_activity(f'探测 [{plabel}] 写真库...')
        elif etype == 'platform_done':
            plat = evt.get('platform')
            added = evt.get('added', 0)
            if plat in self._plat_status_labels:
                if added > 0:
                    self._plat_status_labels[plat].configure(text=f'命中 {added} 个写真', foreground='#059669')
                else:
                    self._plat_status_labels[plat].configure(text='未收录', foreground='#9ca3af')
            cur_val = self.progress_bar['value']
            self.progress_bar.configure(value=min(90, cur_val + 15))
        elif etype == 'scoring':
            msg = evt.get('message') or '正在进行加权排序...'
            self._log_activity(msg)
            self.progress_bar.configure(value=95)

    def _on_search_finished(self, candidates: list[dict], tid: int):
        if tid != self._search_thread_id or not self.top.winfo_exists():
            return
        self._is_searching = False
        elapsed = time.time() - self._search_start_time
        self.progress_bar.configure(value=100)
        self.lbl_timer.configure(text=f'检索完成，耗时 {elapsed:.1f}s | 共捕获 {len(candidates)} 个写真')
        self._log_activity(f'检索完成，共命中 {len(candidates)} 个候选版本。')

        self._candidates = candidates
        for item in self.tree.get_children():
            self.tree.delete(item)

        if not candidates:
            self.lbl_preview_img.configure(image='', text='未检索到该艺术家的写真\n可尝试调整关键词重新检索')
            return

        for idx, c in enumerate(candidates):
            badge = c.get('badge') or ''
            res_label = c.get('resolution') or (f'{c.get("width")}×{c.get("height")}' if c.get('width') else '高清')
            if c.get('aspect_type') == 'landscape' and '横版' not in res_label:
                res_label += ' 横版'
            clues = c.get('clues') or c.get('artist_name') or ''
            source = c.get('source') or c.get('platform') or ''
            iid = f'cand_{idx}'
            self.tree.insert('', 'end', iid=iid, values=(badge, res_label, clues, source))

        # 默认选中第一首选条目并平滑切回写真预览
        first_id = 'cand_0'
        self.tree.selection_set(first_id)
        self.tree.focus(first_id)
        self._load_candidate_preview(0)
        self._switch_view_mode('preview')

    def _on_tree_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        item_id = sel[0]
        try:
            idx = int(item_id.split('_')[1])
            self._load_candidate_preview(idx)
        except Exception:
            pass

    def _load_candidate_preview(self, idx: int):
        if not (0 <= idx < len(self._candidates)):
            return
        c = self._candidates[idx]
        url = c.get('pic_url')
        if not url:
            return

        res_label = c.get('resolution') or '高清'
        src_label = c.get('source') or ''
        aspect_label = '宽幅壁纸' if c.get('aspect_type') == 'landscape' else '方形肖像'
        self.lbl_preview_info.configure(text=f'来源: {src_label} ({res_label}) · {aspect_label}')

        if url in self._img_cache:
            self.lbl_preview_img.configure(image=self._img_cache[url], text='')
            return

        self.lbl_preview_img.configure(image='', text='正在载入写真原图...')

        def _fetch_img():
            try:
                raw = http_get_bytes(url, timeout=10)
                if not raw or not sniff_image(raw):
                    return
                # PIL 打开并在工作线程完成等比缩放计算
                im = Image.open(io.BytesIO(raw))
                if im.mode != 'RGB':
                    im = im.convert('RGB')
                im.thumbnail((320, 230), Image.Resampling.LANCZOS)

                def _update_ui():
                    try:
                        if not self.top.winfo_exists():
                            return
                        tk_img = ImageTk.PhotoImage(im)
                        self._img_cache[url] = tk_img
                        cur_sel = self.tree.selection()
                        if cur_sel and cur_sel[0] == f'cand_{idx}':
                            self.lbl_preview_img.configure(image=tk_img, text='')
                    except Exception:
                        pass

                try:
                    self.top.after(0, _update_ui)
                except Exception:
                    pass
            except Exception:
                try:
                    def _show_fail():
                        if self.top.winfo_exists():
                            self.lbl_preview_img.configure(image='', text='写真加载失败')
                    self.top.after(0, _show_fail)
                except Exception:
                    pass

        threading.Thread(target=_fetch_img, daemon=True).start()

    def _on_combo_select(self, _event=None):
        idx = self.cmb_artist.current()
        if 0 <= idx < len(self.artist_groups) and idx != self._current_index:
            self._current_index = idx
            self._load_current_group(auto_search=self.auto_search)

    def _on_prev_artist(self):
        if self._current_index > 0:
            self._current_index -= 1
            self._load_current_group(auto_search=self.auto_search)

    def _on_next_artist(self):
        if self._current_index < len(self.artist_groups) - 1:
            self._current_index += 1
            self._load_current_group(auto_search=self.auto_search)

    def _on_skip_artist(self):
        if self.is_multi:
            if self._current_index < len(self.artist_groups) - 1:
                self._current_index += 1
                self._load_current_group(auto_search=self.auto_search)
            else:
                _msgbox().showinfo('向导完成', '已到达最后一位艺人，向导结束。', parent=self.top)
                self.top.destroy()
        else:
            self.top.destroy()

    def _apply_current_group(self, advance: bool = False):
        sel = self.tree.selection()
        if not sel:
            _msgbox().showwarning('请选择写真', '请先在左侧列表中点击选择一个候选写真版本。', parent=self.top)
            return

        idx = int(sel[0].split('_')[1])
        cand = self._candidates[idx]
        url = cand.get('pic_url')
        if not url:
            _msgbox().showerror('错误', '所选写真条目无有效图片直链。', parent=self.top)
            return

        grp = self.artist_groups[self._current_index]
        art_name = grp.get('artist') or ''
        tracks: list[LibraryTrack] = grp.get('tracks') or []
        folder = grp.get('folder') or (tracks[0].folder if tracks else '')

        # 1. 抓取原图并规范化为高保真 JPEG
        raw_bytes = http_get_bytes(url, timeout=15)
        if not raw_bytes or not sniff_image(raw_bytes):
            _msgbox().showerror('下载失败', '无法下载所选的高清写真图片，请检查网络或更换其他条目。', parent=self.top)
            return

        norm_jpg = normalize_artist_jpeg(raw_bytes, ARTIST_MAX_SIDE) or raw_bytes

        # 内存双键缓存固化
        if art_name:
            ARTIST_BYTES_CACHE[art_name] = norm_jpg
            ARTIST_BYTES_CACHE[norm_core(art_name)] = norm_jpg
        ARTIST_BYTES_CACHE[url] = norm_jpg

        write_logs: list[str] = []

        # 2. 落盘目标一：当前目录外置 artist.jpg (具备严格单艺人安全判定)
        if self.var_save_dir.get() and folder and os.path.isdir(folder):
            # 检查目录是否包含多位不同艺人
            distinct_artists = {t.artist for t in tracks if t.artist}
            if len(distinct_artists) > 1:
                write_logs.append('[目录跳过] 当前目录包含多位不同艺术家，为防串图已跳过外置 artist.jpg 生成')
            else:
                target_art_path = os.path.join(folder, 'artist.jpg')
                try:
                    with open(target_art_path, 'wb') as fp:
                        fp.write(norm_jpg)
                    write_logs.append(f'[外置保存] {target_art_path}')
                except OSError as err:
                    write_logs.append(f'[外置保存失败] {err}')

        # 3. 落盘目标二：音频文件内嵌 Picture Type = 8 (Lead Artist)
        if self.var_embed_tag.get() and tracks:
            embed_cnt = 0
            for t in tracks:
                track_file = getattr(t, 'path', '') or getattr(t, 'filepath', '')
                if not track_file or not os.path.isfile(track_file):
                    continue
                try:
                    from mutagen import File as MutagenFile
                    audio = MutagenFile(track_file)
                    if audio is not None:
                        container = t.container or os.path.splitext(track_file)[1].lstrip('.').lower()
                        if apply_artist_picture(audio, container, norm_jpg):
                            audio.save()
                            embed_cnt += 1
                except Exception:
                    pass
            if embed_cnt > 0:
                write_logs.append(f'[标签内嵌] 已为 {embed_cnt} 首音频文件注入 Type 8 写真帧')

        # 4. 落盘目标三：MusicBee 中央写真库
        if self.var_sync_musicbee.get() and self.musicbee_artist_dir and art_name:
            saved = sync_artist_pictures_to_musicbee(self.musicbee_artist_dir, art_name, norm_jpg)
            if saved:
                write_logs.append(f'[MusicBee] 已同步至中央写真库: {os.path.basename(saved[0])}')

        self._saved_flags[self._current_index] = True
        grp['saved'] = True

        res_info = {
            'candidate': cand,
            'jpeg_bytes': norm_jpg,
            'logs': write_logs,
            'artist': art_name,
            'folder': folder,
        }

        if self.on_apply:
            try:
                self.on_apply(grp, res_info)
            except Exception:
                pass

        if advance and self.is_multi:
            if self._current_index < len(self.artist_groups) - 1:
                self._current_index += 1
                self._load_current_group(auto_search=self.auto_search)
            else:
                _msgbox().showinfo('完成', f'已完成全部 {len(self.artist_groups)} 位艺术家的写真挑选与配置！', parent=self.top)
                self.top.destroy()
        else:
            self._refresh_combobox_items()
            _msgbox().showinfo(
                '写真应用成功',
                f'已成功为艺术家「{art_name}」配置写真！\n\n' + ('\n'.join(write_logs) if write_logs else '已缓存写真数据'),
                parent=self.top,
            )
            if not self.is_multi:
                self.top.destroy()
