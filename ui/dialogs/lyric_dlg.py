"""挑选与切换歌词模态对话框（支持多平台并发检索、双语实时预览、时间轴偏置与批量流水线模式）。"""

from __future__ import annotations

import io
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Callable

from PIL import Image, ImageTk

from core.config import ui_font
from core.constants import MAX_SIDE, SUPPORTS_COVER_WRITE
from core.models import LibraryTrack
from services.art.image_ops import normalize_jpeg
from services.art.writer import LYRICS_MODES, apply_track_repairs
from services.lookup import fetch_cover_bytes
from services.lookup.lyric_search import (
    LyricCandidate,
    apply_lrc_offset,
    detect_has_translation,
    search_candidate_lyrics,
)
from services.lookup.scoring import (
    compute_album_affinity_score,
    clean_album_noise,
    clean_title_noise,
)
from tag_io import parse_filename_clues, sniff_container
from ui.dialogs.base import _ModalDialog, _msgbox
from ui.dialogs.cover_dlg import _extract_group_clues


class LyricPickerDialog(_ModalDialog):
    """挑选与应用歌曲歌词的可视化弹窗（支持单曲模式与多曲目流水线向导模式）。"""

    def __init__(
        self,
        parent: tk.Misc,
        tracks: list[LibraryTrack] | LibraryTrack,
        *,
        on_apply: Callable[[LibraryTrack, dict], None] | None = None,
        prefer_sources: tuple[str, ...] | list[str] | None = None,
        auto_search: bool = True,
    ):
        if isinstance(tracks, LibraryTrack):
            self.track_list: list[LibraryTrack] = [tracks]
        else:
            self.track_list = list(tracks) if tracks else []

        if not self.track_list:
            raise ValueError("tracks 列表不能为空")

        self.on_apply = on_apply
        self.prefer_sources = prefer_sources
        self.auto_search = auto_search
        self._current_index = 0
        self._saved_flags: dict[int, bool] = {i: False for i in range(len(self.track_list))}
        self._candidates: list[LyricCandidate] = []
        self._is_searching = False
        self._search_thread_id = 0
        self._event_queue: queue.Queue = queue.Queue()
        self._current_offset_ms = 0
        self._search_start_time = 0.0
        self._cover_img_cache: dict[str, ImageTk.PhotoImage] = {}
        self._cover_raw_cache: dict[str, bytes] = {}
        self._active_cov_url: str = ''

        self.is_multi = len(self.track_list) > 1
        super().__init__(
            parent,
            f'挑选与切换歌词 (共 {len(self.track_list)} 首流水线向导)' if self.is_multi else '挑选与切换歌词',
            None,
            resizable=True,
        )
        self.top.geometry('980x680')
        self.top.minsize(820, 560)

        # 1. 顶部向导导航栏 (仅在多曲目时呈现)
        if self.is_multi:
            nav_bar = ttk.Frame(self.outer)
            nav_bar.pack(fill='x', pady=(0, 6))
            ttk.Label(nav_bar, text='曲目切换:', font=ui_font(9, bold=True)).pack(side='left', padx=(0, 6))

            self.var_combo_text = tk.StringVar()
            self.cmb_track = ttk.Combobox(
                nav_bar, textvariable=self.var_combo_text, state='readonly', width=50,
            )
            self.cmb_track.pack(side='left', padx=(0, 8))
            self.cmb_track.bind('<<ComboboxSelected>>', self._on_combo_select)

            self.btn_nav_prev = ttk.Button(nav_bar, text='◀ 上一首', width=8, command=self._on_prev_track)
            self.btn_nav_prev.pack(side='left', padx=(0, 4))
            self.btn_nav_next = ttk.Button(nav_bar, text='下一首 ▶', width=8, command=self._on_next_track)
            self.btn_nav_next.pack(side='left')

            self.lbl_progress = ttk.Label(nav_bar, text='', foreground='#2563eb', font=ui_font(9, bold=True))
            self.lbl_progress.pack(side='right')

        # 2. 检索词与数据源设置卡片
        search_card = ttk.LabelFrame(self.outer, text='检索条件与音源', padding=(8, 6))
        search_card.pack(fill='x', pady=(0, 6))

        row1 = ttk.Frame(search_card)
        row1.pack(fill='x')

        ttk.Label(row1, text='歌曲名:').pack(side='left')
        self.var_title = tk.StringVar()
        self.ent_title = ttk.Entry(row1, textvariable=self.var_title, width=18)
        self.ent_title.pack(side='left', padx=(4, 8))

        ttk.Label(row1, text='艺术家:').pack(side='left')
        self.var_artist = tk.StringVar()
        self.ent_artist = ttk.Entry(row1, textvariable=self.var_artist, width=14)
        self.ent_artist.pack(side='left', padx=(4, 8))

        ttk.Label(row1, text='专辑名:').pack(side='left')
        self.var_album = tk.StringVar()
        self.ent_album = ttk.Entry(row1, textvariable=self.var_album, width=18)
        self.ent_album.pack(side='left', padx=(4, 8))

        self.btn_search = ttk.Button(row1, text='重新搜索', width=9, command=self._on_search_clicked)
        self.btn_search.pack(side='left', padx=(0, 8))

        self.lbl_status = ttk.Label(row1, text='就绪', foreground='#64748b')
        self.lbl_status.pack(side='left')

        row2 = ttk.Frame(search_card)
        row2.pack(fill='x', pady=(4, 0))

        ttk.Label(row2, text='音源筛选:').pack(side='left', padx=(0, 4))
        self.src_vars: dict[str, tk.BooleanVar] = {
            'netease': tk.BooleanVar(value=True),
            'qq': tk.BooleanVar(value=True),
            'kugou': tk.BooleanVar(value=True),
            'lrclib': tk.BooleanVar(value=True),
        }
        for p, label in (('netease', '网易云'), ('qq', 'QQ音乐'), ('kugou', '酷狗'), ('lrclib', 'LRCLIB(国际)')):
            cb = ttk.Checkbutton(row2, text=label, variable=self.src_vars[p], command=self._on_search_clicked)
            cb.pack(side='left', padx=(0, 8))

        # 偏好勾选项 1：优先选择含双语翻译/词曲齐全版本
        self.var_prefer_rich = tk.BooleanVar(value=False)
        self.chk_prefer_rich = ttk.Checkbutton(
            row2, text='⭐ 优先选择含双语翻译/词曲齐全版本', variable=self.var_prefer_rich,
            command=self._on_prefer_rich_toggled,
        )
        self.chk_prefer_rich.pack(side='right')

        # 3. 核心分屏展示区：左侧候选版本列表，右侧歌词实时预览
        main_paned = ttk.PanedWindow(self.outer, orient='horizontal')
        main_paned.pack(fill='both', expand=True, pady=(0, 6))

        # --- 左侧：候选列表 Frame ---
        left_frame = ttk.LabelFrame(main_paned, text='候选版本列表 (单击预览 / 双击应用)', padding=(6, 4))
        main_paned.add(left_frame, weight=3)

        cols = ('source', 'album', 'year', 'diff', 'trans', 'credits', 'score')
        self.tree_cand = ttk.Treeview(left_frame, columns=cols, show='headings', selectmode='browse')
        self.tree_cand.heading('source', text='音源')
        self.tree_cand.heading('album', text='所属大碟 / 版本')
        self.tree_cand.heading('year', text='年份')
        self.tree_cand.heading('diff', text='时差')
        self.tree_cand.heading('trans', text='双语')
        self.tree_cand.heading('credits', text='词曲')
        self.tree_cand.heading('score', text='亲和分')

        self.tree_cand.column('source', width=68, anchor='center')
        self.tree_cand.column('album', width=170, anchor='w')
        self.tree_cand.column('year', width=55, anchor='center')
        self.tree_cand.column('diff', width=55, anchor='center')
        self.tree_cand.column('trans', width=52, anchor='center')
        self.tree_cand.column('credits', width=52, anchor='center')
        self.tree_cand.column('score', width=60, anchor='center')

        tree_scroll_y = ttk.Scrollbar(left_frame, orient='vertical', command=self.tree_cand.yview)
        self.tree_cand.configure(yscrollcommand=tree_scroll_y.set)
        self.tree_cand.pack(side='left', fill='both', expand=True)
        tree_scroll_y.pack(side='right', fill='y')

        self.tree_cand.bind('<<TreeviewSelect>>', self._on_candidate_selected)
        self.tree_cand.bind('<Double-1>', lambda _e: self._on_apply_clicked())

        # --- 右侧：歌词实时预览与微调 Frame ---
        right_frame = ttk.LabelFrame(main_paned, text='歌词实时预览与微调', padding=(8, 4))
        main_paned.add(right_frame, weight=4)

        # 预览顶部卡片：母盘大碟封面缩略图 + 详细元数据 + 时间轴偏置 Offset 控件
        header_card = ttk.Frame(right_frame)
        header_card.pack(fill='x', pady=(0, 6))

        # 封面缩略图 Label (70x70)
        self.lbl_cover_preview = tk.Label(
            header_card,
            width=9,
            height=4,
            bg='#1e293b',
            fg='#94a3b8',
            text='[大碟封面]',
            font=ui_font(8),
            relief='solid',
            bd=1,
        )
        self.lbl_cover_preview.pack(side='left', padx=(0, 10))

        # 右侧信息与微调容器
        info_col = ttk.Frame(header_card)
        info_col.pack(side='left', fill='both', expand=True)

        row_meta = ttk.Frame(info_col)
        row_meta.pack(fill='x')
        self.lbl_cand_meta = ttk.Label(row_meta, text='未选择候选', font=ui_font(9, bold=True), foreground='#0f172a')
        self.lbl_cand_meta.pack(side='left')

        row_sub = ttk.Frame(info_col)
        row_sub.pack(fill='x', pady=(1, 1))
        self.lbl_cand_sub = ttk.Label(row_sub, text='', font=ui_font(8), foreground='#64748b')
        self.lbl_cand_sub.pack(side='left')

        row_cred = ttk.Frame(info_col)
        row_cred.pack(fill='x', pady=(1, 1))
        self.lbl_credits = ttk.Label(row_cred, text='演职人员信息: 未检测到', font=ui_font(8), foreground='#475569')
        self.lbl_credits.pack(side='left')

        # 时间轴偏置微调行
        offset_frame = ttk.Frame(info_col)
        offset_frame.pack(fill='x', pady=(2, 0))

        ttk.Label(offset_frame, text='时间轴偏置:', font=ui_font(8)).pack(side='left', padx=(0, 2))
        ttk.Button(offset_frame, text='-200ms', width=7, command=lambda: self._adjust_offset(-200)).pack(side='left', padx=1)
        self.var_offset = tk.StringVar(value='0')
        self.ent_offset = ttk.Entry(offset_frame, textvariable=self.var_offset, width=6, justify='center')
        self.ent_offset.pack(side='left', padx=2)
        self.ent_offset.bind('<Return>', lambda _e: self._on_offset_entry_changed())
        self.ent_offset.bind('<FocusOut>', lambda _e: self._on_offset_entry_changed())
        ttk.Button(offset_frame, text='+200ms', width=7, command=lambda: self._adjust_offset(200)).pack(side='left', padx=1)
        ttk.Button(offset_frame, text='归零', width=5, command=lambda: self._reset_offset()).pack(side='left', padx=(2, 0))

        # 歌词文本展示框
        txt_frame = ttk.Frame(right_frame)
        txt_frame.pack(fill='both', expand=True)

        self.txt_lyrics = tk.Text(
            txt_frame,
            wrap='none',
            bg='#0f172a',
            fg='#f8fafc',
            insertbackground='#38bdf8',
            selectbackground='#334155',
            selectforeground='#ffffff',
            relief='flat',
            padx=8,
            pady=8,
            font=ui_font(10, mono=True),
        )
        txt_scroll_y = ttk.Scrollbar(txt_frame, orient='vertical', command=self.txt_lyrics.yview)
        txt_scroll_x = ttk.Scrollbar(txt_frame, orient='horizontal', command=self.txt_lyrics.xview)
        self.txt_lyrics.configure(yscrollcommand=txt_scroll_y.set, xscrollcommand=txt_scroll_x.set)

        self.txt_lyrics.grid(row=0, column=0, sticky='nsew')
        txt_scroll_y.grid(row=0, column=1, sticky='ns')
        txt_scroll_x.grid(row=1, column=0, sticky='ew')
        txt_frame.rowconfigure(0, weight=1)
        txt_frame.columnconfigure(0, weight=1)

        # 4. 底部选项与操作按钮栏
        bot_bar = ttk.Frame(self.outer)
        bot_bar.pack(fill='x', pady=(6, 0))

        opts_sub = ttk.Frame(bot_bar)
        opts_sub.pack(side='left')

        ttk.Label(opts_sub, text='写入模式:').pack(side='left')
        self.var_lmode = tk.StringVar(value='auto')
        self.cmb_lmode = ttk.Combobox(
            opts_sub, textvariable=self.var_lmode, values=['auto', 'both', 'embed', 'lrc'],
            state='readonly', width=7,
        )
        self.cmb_lmode.pack(side='left', padx=(4, 10))

        self.var_sync_credits = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_sub, text='提取词/曲/编作者', variable=self.var_sync_credits).pack(side='left', padx=(0, 8))

        # 联动更新勾选项 2：同步更新该曲目的专辑名与年份标签 (用户决断 2)
        self.var_sync_album_year = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_sub, text='同步更新曲目的专辑名与年份', variable=self.var_sync_album_year).pack(side='left', padx=(0, 8))

        # 联动更新勾选项 3：同步更新该大碟的专辑封面 (一石二鸟)
        self.var_sync_cover = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_sub, text='同步更新封面 (cover.jpg/内嵌)', variable=self.var_sync_cover).pack(side='left', padx=(0, 8))

        acts_sub = ttk.Frame(bot_bar)
        acts_sub.pack(side='right')

        ttk.Button(acts_sub, text='📋 复制LRC', width=10, command=self._on_copy_lrc).pack(side='left', padx=(0, 6))
        if self.is_multi:
            ttk.Button(acts_sub, text='跳过当前', width=9, command=self._on_next_track).pack(side='left', padx=(0, 6))
        ttk.Button(acts_sub, text='取消', width=8, command=self._on_cancel).pack(side='left', padx=(0, 6))

        self.btn_apply = ttk.Button(
            acts_sub,
            text='写入并下一首 (Enter)' if self.is_multi else '写入并应用 (Enter)',
            command=self._on_apply_clicked,
        )
        self.btn_apply.pack(side='left')

        self.top.bind('<Return>', lambda _e: self._on_apply_clicked())

        # 启动当前曲目载入
        self._load_current_track_view()
        if self.auto_search:
            self._start_search()

        self._poll_event_queue()

    # --- 界面状态与曲目切换 ---

    def _get_current_track(self) -> LibraryTrack:
        return self.track_list[self._current_index]

    def _load_current_track_view(self):
        tr = self._get_current_track()
        t, a, alb = _extract_group_clues(tr)
        self.var_title.set(t)
        self.var_artist.set(a)
        self.var_album.set(alb)

        self._current_offset_ms = 0
        self.var_offset.set('0')
        self._clear_preview()

        if self.is_multi:
            self._refresh_combobox_items()

    def _refresh_combobox_items(self):
        items = []
        for i, tr in enumerate(self.track_list):
            mark = ' [已写入]' if self._saved_flags.get(i) else ''
            items.append(f'{i + 1}. {tr.filename}{mark}')
        self.cmb_track['values'] = items
        self.cmb_track.current(self._current_index)
        self.lbl_progress.configure(text=f'进度: {self._current_index + 1} / {len(self.track_list)}')

    def _on_combo_select(self, _event=None):
        idx = self.cmb_track.current()
        if 0 <= idx < len(self.track_list) and idx != self._current_index:
            self._current_index = idx
            self._load_current_track_view()
            self._start_search()

    def _on_prev_track(self):
        if self._current_index > 0:
            self._current_index -= 1
            self._load_current_track_view()
            self._start_search()

    def _on_next_track(self):
        if self._current_index < len(self.track_list) - 1:
            self._current_index += 1
            self._load_current_track_view()
            self._start_search()
        elif self.is_multi:
            _msgbox().showinfo('完成', '已到达最后一首曲目。')

    # --- 搜索与异步拉取 ---

    def _on_search_clicked(self):
        self._start_search()

    def _on_prefer_rich_toggled(self):
        """当用户切换'优先双语/词曲齐全'时，就地对已有候选重新打分排序，无感刷新。"""
        if not self._candidates:
            return
        t_alb = self.var_album.get().strip()
        t_tit = self.var_title.get().strip()
        t_art = self.var_artist.get().strip()
        pref = self.var_prefer_rich.get()

        for c in self._candidates:
            c.score = compute_album_affinity_score(
                cand_album=c.album_name,
                target_album=t_alb,
                target_title=t_tit,
                target_artist=t_art,
                cand_artist=c.artist,
                cand_title=c.title,
                release_date=c.release_date,
                duration_diff=c.duration_diff,
                has_translation=c.has_translation,
                has_credits=bool(c.composer or c.lyricist),
                prefer_rich_lyrics=pref,
            )
        self._candidates.sort(key=lambda x: x.score, reverse=True)
        self._populate_candidates_tree(select_first=True)

    def _start_search(self):
        self._search_thread_id += 1
        cur_tid = self._search_thread_id
        self._is_searching = True
        self._candidates.clear()
        self.tree_cand.delete(*self.tree_cand.get_children())
        self._clear_preview()

        self.lbl_status.configure(text='正在并发检索多平台歌词...', foreground='#2563eb')
        self.btn_search.configure(state='disabled')
        self._search_start_time = time.monotonic()

        title = self.var_title.get().strip()
        artist = self.var_artist.get().strip()
        album = self.var_album.get().strip()
        tr = self._get_current_track()
        duration = float(tr.duration or 0.0)

        selected_sources = [p for p, v in self.src_vars.items() if v.get()]
        prefer_rich = self.var_prefer_rich.get()

        def _worker():
            def _on_progress(evt):
                if cur_tid == self._search_thread_id:
                    self._event_queue.put((cur_tid, evt))

            search_candidate_lyrics(
                title=title,
                artist=artist,
                album=album,
                duration=duration,
                prefer_sources=selected_sources,
                prefer_rich_lyrics=prefer_rich,
                on_progress=_on_progress,
            )

        threading.Thread(target=_worker, daemon=True).start()

    def _poll_event_queue(self):
        try:
            while True:
                tid, evt = self._event_queue.get_nowait()
                if tid == self._search_thread_id:
                    self._handle_search_event(evt)
        except queue.Empty:
            pass
        if self.top.winfo_exists():
            self.top.after(40, self._poll_event_queue)

    def _handle_search_event(self, evt: dict):
        etype = evt.get('type')
        if etype == 'candidate_found':
            cand: LyricCandidate = evt['candidate']
            self._candidates.append(cand)
            self._insert_candidate_item(cand)
        elif etype == 'done':
            self._is_searching = False
            self.btn_search.configure(state='normal')
            dur = time.monotonic() - self._search_start_time
            self.lbl_status.configure(
                text=f'完成: 找到 {len(self._candidates)} 个版本 (耗时 {dur:.1f}s)',
                foreground='#16a34a' if self._candidates else '#dc2626',
            )
            # 排序后重排并自动高亮 Rank 1 首选
            self._candidates.sort(key=lambda x: x.score, reverse=True)
            self._populate_candidates_tree(select_first=True)

    def _insert_candidate_item(self, c: LyricCandidate):
        diff_str = f"{c.duration_diff:+.1f}s" if c.duration_diff > 0 else '±0.0s'
        trans_str = '[双语]' if c.has_translation else '[纯原]'
        cred_str = '✓' if (c.composer or c.lyricist) else '-'
        yr = c.release_date[:4] if c.release_date else '-'

        self.tree_cand.insert('', 'end', iid=c.song_id, values=(
            c.platform_label,
            c.album_name or c.title,
            yr,
            diff_str,
            trans_str,
            cred_str,
            f"{c.score:.1f}",
        ))

    def _populate_candidates_tree(self, select_first: bool = False):
        self.tree_cand.delete(*self.tree_cand.get_children())
        for c in self._candidates:
            diff_str = f"{c.duration_diff:+.1f}s" if c.duration_diff > 0 else '±0.0s'
            trans_str = '[双语]' if c.has_translation else '[纯原]'
            cred_str = '✓' if (c.composer or c.lyricist) else '-'
            yr = c.release_date[:4] if c.release_date else '-'

            self.tree_cand.insert('', 'end', iid=c.song_id, values=(
                c.platform_label,
                c.album_name or c.title,
                yr,
                diff_str,
                trans_str,
                cred_str,
                f"{c.score:.1f}",
            ))

        if select_first and self._candidates:
            first_id = self._candidates[0].song_id
            self.tree_cand.selection_set(first_id)
            self.tree_cand.focus(first_id)
            self._render_candidate(self._candidates[0])

    # --- 歌词预览与微调渲染 ---

    def _on_candidate_selected(self, _event=None):
        cand = self._get_selected_candidate()
        if cand:
            self._render_candidate(cand)

    def _get_selected_candidate(self) -> LyricCandidate | None:
        sel = self.tree_cand.selection()
        if not sel:
            return None
        sid = sel[0]
        return next((c for c in self._candidates if c.song_id == sid), None)

    def _render_candidate(self, cand: LyricCandidate):
        diff_str = f"{cand.duration_diff:+.1f}s" if cand.duration_diff > 0 else '±0.0s'
        yr = f" ({cand.release_date[:4]})" if cand.release_date else ""
        self.lbl_cand_meta.configure(text=f"《{cand.album_name or cand.title}》{yr}")
        self.lbl_cand_sub.configure(text=f"音源: {cand.platform_label} | 时差: {diff_str} | 亲和度: {cand.score:.1f}分")

        # 演职人员
        parts = []
        if cand.composer: parts.append(f"作曲: {cand.composer}")
        if cand.lyricist: parts.append(f"作词: {cand.lyricist}")
        if cand.arranger: parts.append(f"编曲: {cand.arranger}")
        cred_text = "演职人员: " + (" | ".join(parts) if parts else "未检测到独立词曲署名")
        self.lbl_credits.configure(text=cred_text)

        # 异步加载并展示大碟封面缩略图
        self._render_cover_thumbnail(cand)

        # 渲染歌词文本 (附带当前偏置)
        lrc_text = cand.lyrics
        if self._current_offset_ms != 0:
            lrc_text = apply_lrc_offset(lrc_text, self._current_offset_ms)

        self.txt_lyrics.configure(state='normal')
        self.txt_lyrics.delete('1.0', 'end')
        self.txt_lyrics.insert('1.0', lrc_text)
        self.txt_lyrics.configure(state='disabled')

    def _render_cover_thumbnail(self, cand: LyricCandidate):
        cov_url = cand.cover_url
        self._active_cov_url = cov_url
        if not cov_url:
            self.lbl_cover_preview.configure(image='', text='无封面')
            return

        if cov_url in self._cover_img_cache:
            photo = self._cover_img_cache[cov_url]
            self.lbl_cover_preview.configure(image=photo, text='')
            return

        self.lbl_cover_preview.configure(image='', text='加载中...')

        def _fetch_worker(target_url: str, sid: str):
            try:
                data = fetch_cover_bytes(target_url)
                if not data:
                    return
                im = Image.open(io.BytesIO(data))
                im = im.convert('RGB')
                im.thumbnail((70, 70), Image.Resampling.LANCZOS)
                photo = ImageTk.PhotoImage(im)
            except Exception:
                data = None
                photo = None

            def _update_ui():
                if not self.top.winfo_exists():
                    return
                if data and photo:
                    self._cover_img_cache[target_url] = photo
                    self._cover_raw_cache[target_url] = data
                cur = self._get_selected_candidate()
                if cur and cur.song_id == sid:
                    if photo:
                        self.lbl_cover_preview.configure(image=photo, text='')
                    else:
                        self.lbl_cover_preview.configure(image='', text='图片失败')

            try:
                if self.top.winfo_exists():
                    self.top.after(0, _update_ui)
            except Exception:
                pass

        threading.Thread(target=_fetch_worker, args=(cov_url, cand.song_id), daemon=True).start()

    def _clear_preview(self):
        self.lbl_cand_meta.configure(text='未选择候选')
        self.lbl_cand_sub.configure(text='')
        self.lbl_credits.configure(text='演职人员信息: 未检测到')
        self.lbl_cover_preview.configure(image='', text='[大碟封面]')
        self.txt_lyrics.configure(state='normal')
        self.txt_lyrics.delete('1.0', 'end')
        self.txt_lyrics.configure(state='disabled')

    def _adjust_offset(self, delta_ms: int):
        self._current_offset_ms += delta_ms
        self.var_offset.set(str(self._current_offset_ms))
        cand = self._get_selected_candidate()
        if cand:
            self._render_candidate(cand)

    def _reset_offset(self):
        self._current_offset_ms = 0
        self.var_offset.set('0')
        cand = self._get_selected_candidate()
        if cand:
            self._render_candidate(cand)

    def _on_offset_entry_changed(self):
        try:
            val = int(self.var_offset.get().strip())
            self._current_offset_ms = val
        except ValueError:
            self.var_offset.set(str(self._current_offset_ms))
        cand = self._get_selected_candidate()
        if cand:
            self._render_candidate(cand)

    def _on_copy_lrc(self):
        txt = self.txt_lyrics.get('1.0', 'end').strip()
        if not txt:
            _msgbox().showwarning('提示', '当前无歌词可供复制。')
            return
        self.top.clipboard_clear()
        self.top.clipboard_append(txt)
        _msgbox().showinfo('提示', '已复制当前预览的 LRC 歌词文本到剪贴板。')

    # --- 写入落盘与流水线推进 ---

    def _on_apply_clicked(self):
        cand = self._get_selected_candidate()
        if not cand:
            _msgbox().showwarning('提示', '请先在左侧列表中选择一个歌词候选版本。')
            return

        target_lrc = cand.lyrics
        if self._current_offset_ms != 0:
            target_lrc = apply_lrc_offset(target_lrc, self._current_offset_ms)

        tag_updates: dict[str, str] = {}
        if self.var_sync_credits.get():
            if cand.composer: tag_updates['composer'] = cand.composer
            if cand.lyricist: tag_updates['lyricist'] = cand.lyricist
            if cand.arranger: tag_updates['arranger'] = cand.arranger

        # 联动更新勾选项 2：同步更新该曲目的专辑名与年份标签
        if self.var_sync_album_year.get():
            if cand.album_name:
                tag_updates['album'] = cand.album_name
            if cand.release_date:
                tag_updates['date'] = cand.release_date

        cur_tr = self._get_current_track()
        lmode = self.var_lmode.get()

        # 联动更新勾选项 3：同步更新该大碟的专辑封面 (一石二鸟)
        cover_saved = False
        norm_cov = None
        if self.var_sync_cover.get() and cand.cover_url:
            raw_cov = self._cover_raw_cache.get(cand.cover_url)
            if not raw_cov:
                try:
                    raw_cov = fetch_cover_bytes(cand.cover_url)
                except Exception:
                    raw_cov = None
            if raw_cov:
                norm_cov = normalize_jpeg(raw_cov, MAX_SIDE)
                if norm_cov:
                    folder = os.path.dirname(cur_tr.path)
                    try:
                        cov_path = os.path.join(folder, 'cover.jpg')
                        with open(cov_path, 'wb') as f_out:
                            f_out.write(norm_cov)
                        cover_saved = True
                    except Exception:
                        pass

        # 内嵌写标准备：检查容器是否支持写入封面（严格约束：.dff 绝对不内嵌封面，发烧隔离）
        container = sniff_container(cur_tr.path)
        embed_cov = norm_cov if (container in SUPPORTS_COVER_WRITE) else None

        ok, msg = apply_track_repairs(
            cur_tr.path,
            tag_updates=tag_updates,
            cover_bytes=embed_cov,
            lyrics=target_lrc,
            lyrics_mode=lmode,
            lrc_title=cand.title or cur_tr.title,
            lrc_artist=cand.artist or cur_tr.artist,
            lrc_album=cand.album_name or cur_tr.album,
        )

        if not ok:
            _msgbox().showerror('写入失败', f'写入歌词与元数据失败: {msg}')
            return

        # 更新内存曲目实体状态
        cur_tr.lyrics = target_lrc
        cur_tr.has_lyrics = True
        if cover_saved or embed_cov:
            cur_tr.cover_status = '正常'
            cur_tr.needs_cover_online = False
            cur_tr.needs_cover_fix = False
            cur_tr.folder_cover_action = 'ok'
            if cur_tr.folder_plan:
                cur_tr.folder_plan.cover_action = 'ok'
        for k, v in tag_updates.items():
            if hasattr(cur_tr, k):
                setattr(cur_tr, k, v)

        self._saved_flags[self._current_index] = True
        if self.on_apply:
            try:
                self.on_apply(cur_tr, {
                    'lyrics': target_lrc,
                    'candidate': cand,
                    'updates': tag_updates,
                    'cover_saved': cover_saved or bool(embed_cov),
                })
            except Exception:
                pass

        if self.is_multi:
            if self._current_index < len(self.track_list) - 1:
                self._current_index += 1
                self._load_current_track_view()
                self._start_search()
            else:
                _msgbox().showinfo('完成', '已完成所有选中曲目的歌词与大碟封面写入。')
                self.result = True
                self.top.destroy()
        else:
            _msgbox().showinfo('成功', '歌词与选定大碟封面/元数据已成功写入！')
            self.result = True
            self.top.destroy()

    def _on_cancel(self):
        self._is_searching = False
        self._search_thread_id += 1
        self.top.destroy()


__all__ = ['LyricPickerDialog']
