"""在线数据源与 API 凭据配置弹窗。"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from providers import ONLINE_PROVIDERS, PROVIDER_LABELS
from track_lookup import parse_source_list
from ui.dialogs.base import _ModalDialog, _msgbox


class SourceConfigDialog(_ModalDialog):
    """配置在线数据源优先级、跳过平台、自定义网易云 API 与可选 Spotify 凭据的弹窗。"""

    def __init__(
        self, parent: tk.Misc, *,
        prefer_sources: tuple[str, ...], only_sources: tuple[str, ...],
        skip_sources: tuple[str, ...], netease_api: str,
        spotify_client_id: str = '', spotify_client_secret: str = '',
        tadb_api_key: str = '', fanart_api_key: str = '', fanart_client_key: str = '',
    ):
        super().__init__(parent, '在线数据源设置')

        avail_desc = '、'.join(f'{pid}({PROVIDER_LABELS.get(pid, pid)})' for pid in ONLINE_PROVIDERS)
        ttk.Label(self.outer, text=f'可用音源: {avail_desc}', wraplength=540, foreground='#475569').pack(anchor='w', pady=(0, 8))

        frm = ttk.Frame(self.outer)
        frm.pack(fill='x', pady=(0, 8))
        self.var_prefer = tk.StringVar(value=','.join(prefer_sources))
        self.var_only = tk.StringVar(value=','.join(only_sources))
        self.var_skip = tk.StringVar(value=','.join(skip_sources))
        self.var_ne_api = tk.StringVar(value=netease_api)
        self.var_sp_id = tk.StringVar(value=spotify_client_id)
        self.var_sp_secret = tk.StringVar(value=spotify_client_secret)
        self.var_tadb_key = tk.StringVar(value=tadb_api_key)
        self.var_fa_key = tk.StringVar(value=fanart_api_key)
        self.var_fa_client = tk.StringVar(value=fanart_client_key)
        rows = (
            ('优先顺序 (逗号分隔，留空默认):', self.var_prefer, ''),
            ('仅使用源 (逗号分隔，留空不限):', self.var_only, ''),
            ('跳过数据源 (逗号分隔):', self.var_skip, ''),
            ('自定义网易云 API 地址 (留空直连官方):', self.var_ne_api, ''),
            ('Spotify Client ID (可选，留空仅读缓存):', self.var_sp_id, ''),
            ('Spotify Client Secret (可选):', self.var_sp_secret, '*'),
            ('TheAudioDB Key (可选，留空默认公共Key):', self.var_tadb_key, ''),
            ('Fanart.tv Project Key (可选，留空默认):', self.var_fa_key, ''),
            ('Fanart.tv Client Key (可选免缓存):', self.var_fa_client, ''),
        )
        for row_idx, (lbl_txt, var, mask) in enumerate(rows):
            ttk.Label(frm, text=lbl_txt).grid(row=row_idx, column=0, sticky='w', pady=3)
            ent = ttk.Entry(frm, textvariable=var, width=36, show=mask) if mask else ttk.Entry(frm, textvariable=var, width=36)
            ent.grid(row=row_idx, column=1, sticky='ew', padx=(8, 0), pady=3)

        btn_bar = self._add_button_bar('保存设置', pady=(6, 0))
        ttk.Button(btn_bar, text='恢复默认', command=self._on_reset).pack(side='left')

    def _on_reset(self):
        self.var_prefer.set('')
        self.var_only.set('')
        self.var_skip.set('')
        self.var_ne_api.set('')
        self.var_sp_id.set('')
        self.var_sp_secret.set('')
        self.var_tadb_key.set('')
        self.var_fa_key.set('')
        self.var_fa_client.set('')

    def _on_confirm(self):
        prefer, inv_p = parse_source_list(self.var_prefer.get())
        only, inv_o = parse_source_list(self.var_only.get())
        skip, inv_s = parse_source_list(self.var_skip.get())
        invalid = inv_p + inv_o + inv_s
        if invalid:
            _msgbox().showinfo('提示', f"未知的数据源名称: {', '.join(invalid)}", parent=self.top)
            return
        self.result = {
            'prefer_sources': prefer,
            'only_sources': only,
            'skip_sources': skip,
            'netease_api': self.var_ne_api.get().strip(),
            'spotify_client_id': self.var_sp_id.get().strip(),
            'spotify_client_secret': self.var_sp_secret.get().strip(),
            'tadb_api_key': self.var_tadb_key.get().strip(),
            'fanart_api_key': self.var_fa_key.get().strip(),
            'fanart_client_key': self.var_fa_client.get().strip(),
        }
        self.top.destroy()
