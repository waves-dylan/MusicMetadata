"""删除部分元数据配置弹窗。"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from core.constants import DELETABLE_FIELDS, FIELD_LABELS
from core.models import LibraryTrack
from pipelines.delete import _count_deletable_targets
from pipelines.options import DeleteOptions
from ui.dialogs.base import _ModalDialog, _msgbox


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
