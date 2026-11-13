"""模态弹窗公共基类与系统消息框辅助。"""

from __future__ import annotations

import sys
import tkinter as tk
from tkinter import messagebox, ttk

from core.config import ui_font

_ORIG_MSGBOX = messagebox


def _msgbox():
    """获取弹窗消息框（兼容测试对 app.messagebox 的打桩）。"""
    app_mod = sys.modules.get('app')
    if app_mod is not None:
        mb = getattr(app_mod, 'messagebox', _ORIG_MSGBOX)
        if mb is not _ORIG_MSGBOX:
            return mb
    return messagebox


class _ModalDialog:
    """模态弹窗公共基类：统一窗口初始化、Esc 关闭与底部确认/取消按钮栏。"""

    def __init__(self, parent: tk.Misc, title: str, intro: str = '', *, wraplength: int = 0, bold_intro: bool = True):
        self.result = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.transient(parent)
        self.top.grab_set()
        self.top.resizable(False, False)
        self.top.protocol('WM_DELETE_WINDOW', self._on_cancel)
        self.top.bind('<Escape>', lambda _e: self._on_cancel())

        self.outer = ttk.Frame(self.top, padding=14)
        self.outer.pack(fill='both', expand=True)

        if intro:
            kw: dict = {}
            if bold_intro:
                kw['font'] = ui_font(9, bold=True)
            if wraplength > 0:
                kw['wraplength'] = wraplength
            ttk.Label(self.outer, text=intro, **kw).pack(anchor='w', pady=(0, 8))

    def _add_button_bar(
        self, confirm_text: str, *, confirm_bg: str = '#2563eb', confirm_active_bg: str = '#1d4ed8', pady=(0, 0),
    ) -> ttk.Frame:
        btn_bar = ttk.Frame(self.outer)
        btn_bar.pack(fill='x', pady=pady)
        self.btn_cancel = ttk.Button(btn_bar, text='取消', width=8, command=self._on_cancel)
        self.btn_cancel.pack(side='right')
        self.btn_confirm = tk.Button(
            btn_bar, text=confirm_text, command=self._on_confirm,
            bg=confirm_bg, fg='#ffffff', activebackground=confirm_active_bg, activeforeground='#ffffff',
            relief='flat', padx=12, pady=4, font=ui_font(9, bold=True), cursor='hand2',
        )
        self.btn_confirm.pack(side='right', padx=(0, 8))
        return btn_bar

    def _on_cancel(self):
        self.result = None
        self.top.destroy()

    def _on_confirm(self):
        self.top.destroy()
