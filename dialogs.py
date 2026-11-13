"""MusicMetadata 模态设置弹窗组件（删除元数据、手工设置标签、格式转换、在线音源配置）。"""

from __future__ import annotations

import os
import sys
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from art_repair import DELETABLE_FIELDS, SETTABLE_FIELDS, parse_set_tags
from library_scan import FIELD_LABELS, LibraryTrack
from providers import ONLINE_PROVIDERS, PROVIDER_LABELS
from runner import ConvertOptions, DeleteOptions, SetOptions, _count_deletable_targets, resolve_hook
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg
from track_lookup import parse_source_list
from user_config import get_default_music_folders, normalize_folder_list, ui_font

_ORIG_MSGBOX = messagebox


def _msgbox():
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


class DeleteMetadataDialog(_ModalDialog):
    """选择要删除的部分元数据类别与安全选项的模态弹窗。"""

    _GRID_ORDER = (
        'title', 'artist', 'albumartist', 'album',
        'date', 'tracknumber', 'discnumber', 'genre',
        'composer', 'performer', 'comment', 'lyrics',
        'cover', 'artist_cover',
    )

    def __init__(self, parent: tk.Misc, targets: list[LibraryTrack], default_sync: bool = True, *, has_foobar: bool = True):
        self.targets = targets
        super().__init__(
            parent, '删除部分元数据',
            f'已选择 {len(targets)} 首曲目。请勾选要删除的元数据类别条目（未勾选类别保持不变）：',
        )

        counts, count_163, folder_cover_count = _count_deletable_targets(targets)
        seen_folders = {t.folder for t in targets if t.folder}
        folder_artist_count = sum(1 for f in seen_folders if os.path.isfile(os.path.join(f, 'artist.jpg')))
        lrc_file_count = sum(1 for t in targets if t.has_lrc_file)

        cat_frame = ttk.LabelFrame(self.outer, text='元数据类别', padding=(10, 8))
        cat_frame.pack(fill='x', pady=(0, 8))

        self.field_vars: dict[str, tk.BooleanVar] = {}
        self.field_checks: dict[str, ttk.Checkbutton] = {}
        grid_frm = ttk.Frame(cat_frame)
        grid_frm.pack(fill='x')
        for idx, key in enumerate(self._GRID_ORDER):
            var = tk.BooleanVar(value=False)
            self.field_vars[key] = var
            label = FIELD_LABELS.get(key, key)
            cmd = (
                self._on_cover_toggled if key == 'cover'
                else (self._on_artist_cover_toggled if key == 'artist_cover'
                      else (self._on_lyrics_toggled if key == 'lyrics' else None))
            )
            chk = ttk.Checkbutton(grid_frm, text=f'{label} ({counts.get(key, 0)})', variable=var, command=cmd)
            chk.grid(row=idx // 4, column=idx % 4, sticky='w', padx=(0, 14), pady=3)
            self.field_checks[key] = chk

        qbar = ttk.Frame(cat_frame)
        qbar.pack(fill='x', pady=(6, 0))
        self.btn_select_all = ttk.Button(qbar, text='全选', width=7, command=lambda: self._set_all(True))
        self.btn_select_all.pack(side='left', padx=(0, 6))
        self.btn_select_none = ttk.Button(qbar, text='全不选', width=7, command=lambda: self._set_all(False))
        self.btn_select_none.pack(side='left')

        opt_frame = ttk.LabelFrame(self.outer, text='安全与同步选项', padding=(10, 8))
        opt_frame.pack(fill='x', pady=(0, 10))

        self.var_protect_163 = tk.BooleanVar(value=True)
        self.var_delete_folder_cover = tk.BooleanVar(value=False)
        self.var_delete_folder_artist = tk.BooleanVar(value=False)
        self.var_delete_lrc = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=default_sync)
        self.var_dry_run = tk.BooleanVar(value=False)

        p163_txt = "保护网易云 163 key（删除注释时保留 163 key(Don't modify):...）"
        if count_163:
            p163_txt += f'（当前含 {count_163} 首）'
        self.chk_protect_163 = ttk.Checkbutton(opt_frame, text=p163_txt, variable=self.var_protect_163)
        self.chk_protect_163.pack(anchor='w', pady=2)

        fcov_txt = '删除封面时同时删除同目录外置 cover.jpg'
        if folder_cover_count:
            fcov_txt += f'（涉及 {folder_cover_count} 个）'
        self.chk_delete_folder_cover = ttk.Checkbutton(
            opt_frame, text=fcov_txt, variable=self.var_delete_folder_cover, state='disabled',
        )
        self.chk_delete_folder_cover.pack(anchor='w', pady=2)

        fart_txt = '删除封面-艺术家时同时删除同目录外置 artist.jpg'
        if folder_artist_count:
            fart_txt += f'（涉及 {folder_artist_count} 个）'
        self.chk_delete_folder_artist = ttk.Checkbutton(
            opt_frame, text=fart_txt, variable=self.var_delete_folder_artist, state='disabled',
        )
        self.chk_delete_folder_artist.pack(anchor='w', pady=2)

        lrc_txt = '删除歌词时同时删除同名外置 .lrc 文件'
        if lrc_file_count:
            lrc_txt += f'（涉及 {lrc_file_count} 个）'
        self.chk_delete_lrc = ttk.Checkbutton(
            opt_frame, text=lrc_txt, variable=self.var_delete_lrc, state='disabled',
        )
        self.chk_delete_lrc.pack(anchor='w', pady=2)

        self.chk_sync = ttk.Checkbutton(opt_frame, text='删除后刷新 foobar2000 缓存', variable=self.var_sync)
        if has_foobar:
            self.chk_sync.pack(anchor='w', pady=2)

        self.chk_dry_run = ttk.Checkbutton(opt_frame, text='仅预演（预览待删除项目，不实际修改文件）', variable=self.var_dry_run)
        self.chk_dry_run.pack(anchor='w', pady=2)

        btn_bar = self._add_button_bar('确认删除', confirm_bg='#dc2626', confirm_active_bg='#b91c1c')
        ttk.Label(btn_bar, text='注意：实际删除会直接改写音频文件标签。', foreground='#b45309').pack(side='left')

    def _set_all(self, checked: bool):
        for var in self.field_vars.values():
            var.set(checked)
        self._on_cover_toggled()
        self._on_artist_cover_toggled()
        self._on_lyrics_toggled()

    def _on_cover_toggled(self):
        if self.field_vars['cover'].get():
            self.chk_delete_folder_cover.configure(state='normal')
        else:
            self.var_delete_folder_cover.set(False)
            self.chk_delete_folder_cover.configure(state='disabled')

    def _on_artist_cover_toggled(self):
        if self.field_vars.get('artist_cover') and self.field_vars['artist_cover'].get():
            self.chk_delete_folder_artist.configure(state='normal')
        else:
            self.var_delete_folder_artist.set(False)
            self.chk_delete_folder_artist.configure(state='disabled')

    def _on_lyrics_toggled(self):
        self.chk_delete_lrc.configure(state='normal' if self.field_vars['lyrics'].get() else 'disabled')

    def _on_confirm(self):
        mb = _msgbox()
        selected = frozenset(k for k, v in self.field_vars.items() if v.get())
        if not selected:
            mb.showinfo('提示', '请至少勾选一项要删除的元数据类别。', parent=self.top)
            return
        del_folder_cov = bool('cover' in selected and self.var_delete_folder_cover.get())
        del_folder_artist = bool('artist_cover' in selected and self.var_delete_folder_artist.get())
        del_lrc = bool('lyrics' in selected and self.var_delete_lrc.get())
        dry_run = self.var_dry_run.get()
        if not dry_run:
            names = '、'.join(FIELD_LABELS.get(f, f) for f in DELETABLE_FIELDS if f in selected)
            if del_folder_cov:
                names += '、同目录 cover.jpg'
            if del_folder_artist:
                names += '、同目录 artist.jpg'
            if del_lrc:
                names += '、外置 .lrc'
            if not mb.askyesno(
                '确认删除元数据',
                f'即将从已勾选的 {len(self.targets)} 首曲目中删除：\n{names}\n\n此操作将直接修改磁盘上的音频文件，确定继续吗？',
                icon='warning',
                parent=self.top,
            ):
                return
        self.result = DeleteOptions(
            fields=selected,
            delete_folder_cover=del_folder_cov,
            delete_folder_artist=del_folder_artist,
            delete_lrc_file=del_lrc,
            protect_163_key=self.var_protect_163.get(),
            enable_sync=self.var_sync.get(),
            dry_run=dry_run,
        )
        self.top.destroy()


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
