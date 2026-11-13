"""手工设置元数据标签配置弹窗。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from art_repair import parse_set_tags
from core.constants import FIELD_LABELS, SETTABLE_FIELDS
from core.models import LibraryTrack
from pipelines.options import SetOptions
from ui.dialogs.base import _ModalDialog, _msgbox


class SetMetadataDialog(_ModalDialog):
    """批量手工设置标准字段或自定义元数据标签的模态弹窗。"""

    def __init__(self, parent: tk.Misc, targets: list[LibraryTrack], default_sync: bool = True, *, has_foobar: bool = True):
        self.targets = targets
        self._items: dict[str, str] = {}
        super().__init__(
            parent, '手工设置元数据',
            f'已选择 {len(targets)} 首曲目。输入要批量设置的标签名（支持标准字段或自定义标签名）与值：',
        )

        inp = ttk.LabelFrame(self.outer, text='添加标签条目', padding=(10, 8))
        inp.pack(fill='x', pady=(0, 8))

        row1 = ttk.Frame(inp)
        row1.pack(fill='x')
        ttk.Label(row1, text='标签名:').pack(side='left')
        preset_keys = [f'{k} ({FIELD_LABELS.get(k, k)})' for k in SETTABLE_FIELDS]
        self.var_key = tk.StringVar(value='genre (流派)')
        self.cmb_key = ttk.Combobox(row1, textvariable=self.var_key, values=preset_keys, width=22)
        self.cmb_key.pack(side='left', padx=(6, 10))
        ttk.Label(row1, text='标签值:').pack(side='left')
        self.var_val = tk.StringVar()
        self.ent_val = ttk.Entry(row1, textvariable=self.var_val, width=28)
        self.ent_val.pack(side='left', padx=(6, 8), fill='x', expand=True)
        self.btn_add = ttk.Button(row1, text='加入列表', command=self._on_add)
        self.btn_add.pack(side='left')

        list_frm = ttk.LabelFrame(self.outer, text='待写入标签列表', padding=(10, 6))
        list_frm.pack(fill='x', pady=(0, 8))
        self.listbox = tk.Listbox(list_frm, height=5, activestyle='none')
        self.listbox.pack(side='left', fill='both', expand=True)
        l_acts = ttk.Frame(list_frm)
        l_acts.pack(side='right', padx=(8, 0), fill='y')
        self.btn_remove_item = ttk.Button(l_acts, text='移除选中', command=self._on_remove_selected)
        self.btn_remove_item.pack(anchor='n', pady=(0, 4))
        self.btn_clear_items = ttk.Button(l_acts, text='清空列表', command=self._on_clear_items)
        self.btn_clear_items.pack(anchor='n')

        opt_frm = ttk.LabelFrame(self.outer, text='安全与同步选项', padding=(10, 6))
        opt_frm.pack(fill='x', pady=(0, 10))
        self.var_protect_163 = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=default_sync)
        self.var_dry_run = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt_frm, text="保护网易云 163 key（不覆盖内嵌 163 key 注释）", variable=self.var_protect_163).pack(anchor='w', pady=2)
        self.chk_sync = ttk.Checkbutton(opt_frm, text='写入后刷新 foobar2000 缓存', variable=self.var_sync)
        if has_foobar:
            self.chk_sync.pack(anchor='w', pady=2)
        ttk.Checkbutton(opt_frm, text='仅预演（不修改文件）', variable=self.var_dry_run).pack(anchor='w', pady=2)

        self._add_button_bar('应用设置')

    def _extract_raw_key(self) -> str:
        raw = self.var_key.get().strip()
        if ' (' in raw and raw.endswith(')'):
            return raw.split(' (', 1)[0].strip()
        return raw

    def _refresh_list(self):
        self.listbox.delete(0, 'end')
        for k, v in self._items.items():
            self.listbox.insert('end', f'{k} = {v}')

    def _on_add(self):
        k = self._extract_raw_key()
        v = self.var_val.get().strip()
        parsed, err = parse_set_tags([f'{k}={v}'])
        if err:
            _msgbox().showinfo('提示', err, parent=self.top)
            return
        self._items.update(parsed)
        self.var_val.set('')
        self._refresh_list()

    def _on_remove_selected(self):
        sel = list(self.listbox.curselection())
        if not sel:
            return
        keys = list(self._items.keys())
        for idx in reversed(sel):
            if 0 <= idx < len(keys):
                self._items.pop(keys[idx], None)
        self._refresh_list()

    def _on_clear_items(self):
        self._items.clear()
        self._refresh_list()

    def _on_confirm(self):
        mb = _msgbox()
        if self.var_val.get().strip():
            k = self._extract_raw_key()
            parsed, err = parse_set_tags([f'{k}={self.var_val.get().strip()}'])
            if err:
                mb.showinfo('提示', err, parent=self.top)
                return
            self._items.update(parsed)
            self.var_val.set('')
            self._refresh_list()
        if not self._items:
            mb.showinfo('提示', '请至少添加一个有效的标签设置条目。', parent=self.top)
            return
        self.result = SetOptions(
            tag_values=dict(self._items),
            protect_163_key=self.var_protect_163.get(),
            enable_sync=self.var_sync.get(),
            dry_run=self.var_dry_run.get(),
        )
        self.top.destroy()
