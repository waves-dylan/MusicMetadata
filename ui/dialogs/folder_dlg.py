"""扫描目录管理弹窗。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import filedialog, ttk

from core.config import get_default_music_folders, normalize_folder_list
from ui.dialogs.base import _ModalDialog, _msgbox


class FolderManagerDialog(_ModalDialog):
    """管理多个自定义音乐扫描目录并支持持久化保存的模态弹窗。"""

    def __init__(self, parent: tk.Misc, folders: list[str] | None = None):
        self._folders = normalize_folder_list(folders)
        super().__init__(
            parent,
            '管理扫描目录',
            '添加或移除要扫描的本地音乐目录（列表会自动保存，清空后将恢复自动探测）：',
        )

        list_frm = ttk.Frame(self.outer)
        list_frm.pack(fill='both', expand=True, pady=(0, 8))
        self.listbox = tk.Listbox(list_frm, width=64, height=7, activestyle='none', selectmode='extended')
        self.listbox.pack(side='left', fill='both', expand=True)

        acts = ttk.Frame(list_frm)
        acts.pack(side='right', padx=(8, 0), fill='y')
        self.btn_add = ttk.Button(acts, text='添加目录...', width=14, command=self._on_add)
        self.btn_add.pack(anchor='n', pady=(0, 4))
        self.btn_add_music = ttk.Button(acts, text='加入系统音乐', width=14, command=self._on_add_default_music)
        self.btn_add_music.pack(anchor='n', pady=(0, 4))
        self.btn_remove = ttk.Button(acts, text='移除选中', width=14, command=self._on_remove)
        self.btn_remove.pack(anchor='n', pady=(0, 4))
        self.btn_clear = ttk.Button(acts, text='清空(自动)', width=14, command=self._on_clear)
        self.btn_clear.pack(anchor='n')

        self._refresh_list()
        self._add_button_bar('保存并扫描')

    def _refresh_list(self):
        self.listbox.delete(0, 'end')
        for folder in self._folders:
            suffix = '' if os.path.isdir(folder) else ' (目录不存在)'
            self.listbox.insert('end', f'{folder}{suffix}')

    def _on_add(self):
        folder = filedialog.askdirectory(title='添加要扫描的音乐目录', parent=self.top)
        if folder:
            self._folders = normalize_folder_list([*self._folders, folder])
            self._refresh_list()

    def _on_add_default_music(self):
        defaults = get_default_music_folders()
        if not defaults:
            _msgbox().showinfo('提示', '未检测到系统默认音乐目录（~/Music）。', parent=self.top)
            return
        self._folders = normalize_folder_list([*self._folders, *defaults])
        self._refresh_list()

    def _on_remove(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        for idx in reversed(sel):
            if 0 <= idx < len(self._folders):
                self._folders.pop(idx)
        self._refresh_list()

    def _on_clear(self):
        self._folders.clear()
        self._refresh_list()

    def _on_confirm(self):
        self.result = list(self._folders)
        self.top.destroy()
