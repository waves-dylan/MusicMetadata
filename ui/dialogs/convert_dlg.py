"""音频格式批量转换设置弹窗。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.config import ui_font
from core.constants import CONVERT_TARGET_FORMATS
from core.models import LibraryTrack
from pipelines.executor import resolve_hook
from pipelines.options import ConvertOptions
from tag_io import find_ffmpeg
from ui.dialogs.base import _ModalDialog, _msgbox


class ConvertFormatDialog(_ModalDialog):
    """音频格式批量转换设置弹窗（依赖 ffmpeg）。"""

    def __init__(self, parent: tk.Misc, targets: list[LibraryTrack], default_sync: bool = True, *, has_foobar: bool = True):
        self.targets = targets
        super().__init__(parent, '转换音频格式')

        ffmpeg_path = resolve_hook('find_ffmpeg', find_ffmpeg)()
        ff_note = f'已检测到 ffmpeg: {ffmpeg_path}' if ffmpeg_path else '未检测到 ffmpeg（仅预演可用，实际转换需安装 ffmpeg 并加入 PATH）'
        ttk.Label(self.outer, text=f'已选择 {len(targets)} 首曲目，转换将自动保留元数据、封面与外置 .lrc：', font=ui_font(9, bold=True)).pack(anchor='w', pady=(0, 4))
        ttk.Label(self.outer, text=ff_note, foreground='#15803d' if ffmpeg_path else '#dc2626').pack(anchor='w', pady=(0, 8))

        fmt_frm = ttk.Frame(self.outer)
        fmt_frm.pack(fill='x', pady=(0, 8))
        ttk.Label(fmt_frm, text='目标格式:').pack(side='left')
        self.var_format = tk.StringVar(value='flac')
        self.cmb_format = ttk.Combobox(fmt_frm, textvariable=self.var_format, values=list(CONVERT_TARGET_FORMATS), state='readonly', width=12)
        self.cmb_format.pack(side='left', padx=(8, 0))

        opt_frm = ttk.LabelFrame(self.outer, text='转换选项', padding=(10, 6))
        opt_frm.pack(fill='x', pady=(0, 10))
        self.var_overwrite = tk.BooleanVar(value=False)
        self.var_delete_source = tk.BooleanVar(value=False)
        self.var_sync = tk.BooleanVar(value=default_sync)
        self.var_dry_run = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frm, text='覆盖已存在的目标文件', variable=self.var_overwrite).pack(anchor='w', pady=2)
        ttk.Checkbutton(opt_frm, text='转换并校验成功后删除源文件（谨慎勾选）', variable=self.var_delete_source).pack(anchor='w', pady=2)
        self.chk_sync = ttk.Checkbutton(opt_frm, text='转换后刷新 foobar2000 缓存', variable=self.var_sync)
        if has_foobar:
            self.chk_sync.pack(anchor='w', pady=2)
        ttk.Checkbutton(opt_frm, text='仅预演（预览转换结果，不执行转码）', variable=self.var_dry_run).pack(anchor='w', pady=2)

        self._add_button_bar('开始转换')

    def _on_confirm(self):
        mb = _msgbox()
        dry = self.var_dry_run.get()
        if not dry and not resolve_hook('find_ffmpeg', find_ffmpeg)():
            mb.showerror('缺少 ffmpeg', '未在系统 PATH 中检测到 ffmpeg，无法执行实际转码。', parent=self.top)
            return
        if not dry and self.var_delete_source.get():
            if not mb.askyesno(
                '确认删除源文件',
                f'已勾选“转换后删除源文件”，确认在 {len(self.targets)} 首曲目转换成功后删除原文件吗？',
                icon='warning',
                parent=self.top,
            ):
                return
        self.result = ConvertOptions(
            target_format=self.var_format.get(),
            overwrite=self.var_overwrite.get(),
            delete_source=self.var_delete_source.get(),
            enable_sync=self.var_sync.get(),
            dry_run=dry,
        )
        self.top.destroy()
