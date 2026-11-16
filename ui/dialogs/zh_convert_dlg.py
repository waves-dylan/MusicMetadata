"""华语简繁文本转换配置与 Diff 预览弹窗。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from core.config import ui_font
from core.constants import FIELD_LABELS
from core.models import LibraryTrack
from core.zh_norm import convert_lrc, diff_tags
from pipelines.options import ZhConvertOptions
from tag_io import get_lrc_path, read_audio_info, read_lrc_file
from ui.dialogs.base import _ModalDialog, _msgbox


class ZhConvertDialog(_ModalDialog):
    """华语简繁文本批量转换与可视化 Diff 比对模态弹窗。"""

    _CORE_FIELDS = ('title', 'artist', 'albumartist', 'album')
    _EXTRA_FIELDS = ('composer', 'lyricist', 'arranger', 'genre', 'comment')

    def __init__(
        self,
        parent: tk.Misc,
        targets: list[LibraryTrack],
        default_sync: bool = True,
        *,
        has_player: bool = True,
    ):
        self.targets = targets
        super().__init__(
            parent,
            '华语简繁文本批量转换',
            f'已选择 {len(targets)} 首曲目。请选择转换方向、目标字段与安全选项：',
        )

        # 1. 转换方向
        dir_frame = ttk.LabelFrame(self.outer, text='转换方向', padding=(10, 6))
        dir_frame.pack(fill='x', pady=(0, 8))
        self.var_direction = tk.StringVar(value='t2s')
        ttk.Radiobutton(
            dir_frame, text='繁体中文 -> 简体中文（t2s，解决港台原盘与流媒体繁简割裂）',
            variable=self.var_direction, value='t2s', command=self._on_options_changed,
        ).pack(anchor='w', pady=2)
        ttk.Radiobutton(
            dir_frame, text='简体中文 -> 繁体中文（s2t，基于上下文消歧，规范化港台大碟）',
            variable=self.var_direction, value='s2t', command=self._on_options_changed,
        ).pack(anchor='w', pady=2)

        # 2. 待转换字段
        fld_frame = ttk.LabelFrame(self.outer, text='待转换字段', padding=(10, 6))
        fld_frame.pack(fill='x', pady=(0, 8))

        self.field_vars: dict[str, tk.BooleanVar] = {}
        grid_core = ttk.Frame(fld_frame)
        grid_core.pack(fill='x', pady=(0, 4))
        for idx, k in enumerate(self._CORE_FIELDS):
            v = tk.BooleanVar(value=True)
            self.field_vars[k] = v
            lbl = FIELD_LABELS.get(k, k)
            ttk.Checkbutton(grid_core, text=f'{lbl} ({k})', variable=v, command=self._on_options_changed).grid(
                row=0, column=idx, sticky='w', padx=(0, 12), pady=2
            )

        grid_extra = ttk.Frame(fld_frame)
        grid_extra.pack(fill='x', pady=(0, 4))
        for idx, k in enumerate(self._EXTRA_FIELDS):
            v = tk.BooleanVar(value=False)
            self.field_vars[k] = v
            lbl = FIELD_LABELS.get(k, k)
            ttk.Checkbutton(grid_extra, text=f'{lbl} ({k})', variable=v, command=self._on_options_changed).grid(
                row=0, column=idx, sticky='w', padx=(0, 12), pady=2
            )

        lrc_bar = ttk.Frame(fld_frame)
        lrc_bar.pack(fill='x', pady=(2, 0))
        self.var_convert_lyrics = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            lrc_bar,
            text='同步转换歌词（严格保护 [mm:ss.xx] 时间轴，支持内嵌 USLT 与同名外置 .lrc）',
            variable=self.var_convert_lyrics,
            command=self._on_options_changed,
        ).pack(side='left')

        # 3. 安全与同步选项
        opt_frame = ttk.LabelFrame(self.outer, text='安全与同步选项', padding=(10, 6))
        opt_frame.pack(fill='x', pady=(0, 8))

        self.var_preserve_jpop = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=default_sync)
        self.var_dry_run = tk.BooleanVar(value=False)

        ttk.Checkbutton(
            opt_frame,
            text='保护日文汉字（包含平假名/片假名或命中知名日文艺术家时自动跳过）',
            variable=self.var_preserve_jpop,
            command=self._on_options_changed,
        ).pack(anchor='w', pady=2)

        if has_player:
            ttk.Checkbutton(
                opt_frame, text='转换后刷新播放器缓存（同步更新 foobar2000 / MusicBee / Roon）',
                variable=self.var_sync,
            ).pack(anchor='w', pady=2)

        ttk.Checkbutton(opt_frame, text='仅预演（预览转换结果与差异，不修改音频文件）', variable=self.var_dry_run).pack(anchor='w', pady=2)

        # 4. 可视化差异比对 (Diff Preview)
        preview_frame = ttk.LabelFrame(self.outer, text='差异比对预览 (Diff Preview)', padding=(8, 6))
        preview_frame.pack(fill='both', expand=True, pady=(0, 8))

        p_toolbar = ttk.Frame(preview_frame)
        p_toolbar.pack(fill='x', pady=(0, 4))
        self.btn_preview = ttk.Button(p_toolbar, text='🔍 刷新差异比对', command=self._refresh_diff_preview)
        self.btn_preview.pack(side='left', padx=(0, 8))
        self.lbl_preview_stat = ttk.Label(p_toolbar, text='点击“刷新差异比对”查看具体哪些字段将发生改动。')
        self.lbl_preview_stat.pack(side='left')

        tree_scroll = ttk.Scrollbar(preview_frame)
        tree_scroll.pack(side='right', fill='y')
        self.tree = ttk.Treeview(
            preview_frame,
            columns=('file', 'field', 'old', 'new'),
            show='headings',
            height=6,
            yscrollcommand=tree_scroll.set,
        )
        tree_scroll.config(command=self.tree.yview)

        self.tree.heading('file', text='曲目文件名')
        self.tree.heading('field', text='变更字段')
        self.tree.heading('old', text='原始文本')
        self.tree.heading('new', text='转换后文本')

        self.tree.column('file', width=160, anchor='w')
        self.tree.column('field', width=90, anchor='center')
        self.tree.column('old', width=180, anchor='w')
        self.tree.column('new', width=180, anchor='w')
        self.tree.pack(fill='both', expand=True)

        self._add_button_bar('开始转换')

    def _get_active_fields(self) -> list[str]:
        return [k for k, v in self.field_vars.items() if v.get()]

    def _on_options_changed(self):
        # 选项变动时清空预览区提示用户重新比对
        self.lbl_preview_stat.config(text='配置已更改，请点击“刷新差异比对”重新审查。')

    def _refresh_diff_preview(self):
        self.tree.delete(*self.tree.get_children())
        direction = self.var_direction.get()
        fields = self._get_active_fields()
        preserve_jpop = self.var_preserve_jpop.get()
        inc_lrc = self.var_convert_lyrics.get()

        diff_count = 0
        changed_tracks = 0

        for tr in self.targets[:100]:  # 预览最多 100 首以保证瞬间响应
            info = read_audio_info(tr.path)
            diffs = diff_tags(info, fields, direction=direction, preserve_jpop=preserve_jpop)

            if inc_lrc:
                emb = (info.get('lyrics') or '').strip()
                if emb:
                    c_emb = convert_lrc(emb, direction=direction, preserve_jpop=preserve_jpop)
                    if c_emb != emb:
                        diffs.append(('lyrics (内嵌)', '[原始内嵌歌词]', '[内嵌歌词已转换]'))

                lrc_path = get_lrc_path(tr.path)
                if os.path.isfile(lrc_path):
                    ext = read_lrc_file(tr.path).strip()
                    if ext:
                        c_ext = convert_lrc(ext, direction=direction, preserve_jpop=preserve_jpop)
                        if c_ext != ext:
                            diffs.append(('lrc (外置)', '[原始外置LRC]', '[外置LRC已转换]'))

            if diffs:
                changed_tracks += 1
                for fld, old_v, new_v in diffs:
                    diff_count += 1
                    # 避免过长文本挤爆表格
                    old_disp = old_v if len(old_v) <= 30 else old_v[:28] + '...'
                    new_disp = new_v if len(new_v) <= 30 else new_v[:28] + '...'
                    fld_label = FIELD_LABELS.get(fld, fld)
                    self.tree.insert('', 'end', values=(tr.filename, fld_label, old_disp, new_disp))

        suffix = f'（抽样检查前 {min(len(self.targets), 100)} 首）' if len(self.targets) > 100 else ''
        self.lbl_preview_stat.config(
            text=f'预计变动: {changed_tracks} 首曲目，共 {diff_count} 处字段改动{suffix}。'
        )

    def _on_confirm(self):
        fields = self._get_active_fields()
        if not fields and not self.var_convert_lyrics.get():
            _msgbox().showwarning('未选择字段', '请至少勾选一个待转换的标签字段或歌词选项。', parent=self.top)
            return

        self.result = ZhConvertOptions(
            direction=self.var_direction.get(),
            fields=tuple(fields),
            convert_lyrics=self.var_convert_lyrics.get(),
            preserve_jpop=self.var_preserve_jpop.get(),
            enable_sync=self.var_sync.get(),
            dry_run=self.var_dry_run.get(),
        )
        self.top.destroy()
