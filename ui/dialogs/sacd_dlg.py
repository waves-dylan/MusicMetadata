"""SACD ISO 提取与智能分轨设置弹窗。"""

from __future__ import annotations

import os
from pathlib import Path
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from core.config import ui_font
from infra.integrations.base import sanitize_filename
from infra.tools.sacd import (
    SacdIsoToc,
    check_disk_space,
    find_sacd_extract,
    read_sacd_iso_toc,
)
from pipelines.executor import resolve_hook
from pipelines.options import SacdExtractOptions
from ui.dialogs.base import _ModalDialog, _msgbox


class SacdExtractDialog(_ModalDialog):
    """SACD ISO 无损提取与智能修复弹窗。"""

    def __init__(self, parent: tk.Misc, default_iso: str | None = None, default_lyrics_mode: str = 'both'):
        self.current_toc: SacdIsoToc | None = None
        self._sacd_exe = resolve_hook("find_sacd_extract", find_sacd_extract)()
        self._lyrics_mode = default_lyrics_mode or 'both'

        super().__init__(
            parent,
            "⚡ 提取 SACD ISO 镜像并智能修复",
            "选择 SACD ISO 镜像文件，无损解压为 Sony DSF 音轨并自动补全元数据与双封面：",
        )
        self.top.geometry("680x620")
        self.top.minsize(620, 560)
        self.top.resizable(True, True)

        # 1. 顶部：ISO 文件选择与工具状态
        top_frame = ttk.LabelFrame(self.outer, text="SACD ISO 镜像来源", padding=(10, 6))
        top_frame.pack(fill="x", pady=(0, 6))

        file_row = ttk.Frame(top_frame)
        file_row.pack(fill="x")
        self.var_iso_path = tk.StringVar(value=default_iso or "")
        self.ent_iso = ttk.Entry(file_row, textvariable=self.var_iso_path)
        self.ent_iso.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.btn_browse = ttk.Button(file_row, text="浏览 ISO...", width=12, command=self._on_browse_iso)
        self.btn_browse.pack(side="right")

        tool_note = f"已就绪内置工具: {self._sacd_exe}" if self._sacd_exe else "未检测到 sacd_extract.exe（需将工具收纳至 tools/ 目录）"
        ttk.Label(
            top_frame,
            text=tool_note,
            foreground="#15803d" if self._sacd_exe else "#dc2626",
            font=ui_font(8),
        ).pack(anchor="w", pady=(4, 0))

        # 2. 光盘信息卡片
        disc_frame = ttk.LabelFrame(self.outer, text="光盘目录与元数据信息 (TOC)", padding=(10, 6))
        disc_frame.pack(fill="x", pady=(0, 6))

        self.lbl_disc_title = ttk.Label(disc_frame, text="专辑: 请先选择或解析 ISO 镜像", font=ui_font(9, bold=True))
        self.lbl_disc_title.pack(anchor="w")

        info_grid = ttk.Frame(disc_frame)
        info_grid.pack(fill="x", pady=(2, 0))
        self.lbl_artist = ttk.Label(info_grid, text="艺术家: -", foreground="#475569")
        self.lbl_artist.pack(side="left", padx=(0, 16))
        self.lbl_year = ttk.Label(info_grid, text="年份: -", foreground="#475569")
        self.lbl_year.pack(side="left", padx=(0, 16))
        self.lbl_cat = ttk.Label(info_grid, text="唱片号: -", foreground="#475569")
        self.lbl_cat.pack(side="left")

        # 声道选择与容量预估
        area_row = ttk.Frame(disc_frame)
        area_row.pack(fill="x", pady=(4, 0))
        ttk.Label(area_row, text="提取声道:").pack(side="left", padx=(0, 8))
        self.var_channel = tk.StringVar(value="2ch")
        self.rad_stereo = ttk.Radiobutton(
            area_row, text="立体声 (Stereo 2.0)", value="2ch", variable=self.var_channel, command=self._on_area_changed
        )
        self.rad_stereo.pack(side="left", padx=(0, 12))
        self.rad_mch = ttk.Radiobutton(
            area_row, text="多声道 (Surround 5.1/6ch)", value="mch", variable=self.var_channel, command=self._on_area_changed
        )
        self.rad_mch.pack(side="left", padx=(0, 16))

        self.lbl_est_size = ttk.Label(area_row, text="预估解压空间: -", foreground="#2563eb", font=ui_font(8, bold=True))
        self.lbl_est_size.pack(side="right")

        # 3. 曲目列表树状图
        tree_frame = ttk.Frame(self.outer)
        tree_frame.pack(fill="both", expand=True, pady=(0, 6))

        tree_scroll = ttk.Scrollbar(tree_frame, orient="vertical")
        cols = ("idx", "title", "performer", "duration")
        self.tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", yscrollcommand=tree_scroll.set, height=6
        )
        tree_scroll.config(command=self.tree.yview)

        self.tree.heading("idx", text="#", anchor="center")
        self.tree.heading("title", text="曲目标题", anchor="w")
        self.tree.heading("performer", text="表演者", anchor="w")
        self.tree.heading("duration", text="时长", anchor="center")

        self.tree.column("idx", width=40, anchor="center")
        self.tree.column("title", width=260, anchor="w")
        self.tree.column("performer", width=140, anchor="w")
        self.tree.column("duration", width=80, anchor="center")

        self.tree.pack(side="left", fill="both", expand=True)
        tree_scroll.pack(side="right", fill="y")

        # 4. 输出目录与自动化流水线选项
        opt_frame = ttk.LabelFrame(self.outer, text="输出与智能修复选项", padding=(10, 6))
        opt_frame.pack(fill="x", pady=(0, 8))

        out_row = ttk.Frame(opt_frame)
        out_row.pack(fill="x", pady=(0, 6))
        ttk.Label(out_row, text="输出目录:").pack(side="left", padx=(0, 6))
        self.var_output_dir = tk.StringVar(value="")
        self.ent_out = ttk.Entry(out_row, textvariable=self.var_output_dir)
        self.ent_out.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_browse_out = ttk.Button(out_row, text="选择目录...", width=10, command=self._on_browse_output)
        self.btn_browse_out.pack(side="right")

        self.lbl_free_space = ttk.Label(opt_frame, text="", foreground="#475569", font=ui_font(8))
        self.lbl_free_space.pack(anchor="w", pady=(0, 4))

        chk_grid = ttk.Frame(opt_frame)
        chk_grid.pack(fill="x")

        self.var_apply_toc = tk.BooleanVar(value=True)
        self.var_auto_repair = tk.BooleanVar(value=True)
        self.var_fetch_cover = tk.BooleanVar(value=True)
        self.var_fetch_lyrics = tk.BooleanVar(value=True)
        self.var_import_lib = tk.BooleanVar(value=True)

        col1 = ttk.Frame(chk_grid)
        col1.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(col1, text="写入原生光盘 TOC 规范标签 (ID3v2.3)", variable=self.var_apply_toc).pack(anchor="w", pady=1)
        ttk.Checkbutton(col1, text="自动联网匹配并校准精细元数据 (QQ/网易云)", variable=self.var_auto_repair).pack(anchor="w", pady=1)
        ttk.Checkbutton(col1, text="提取完成后自动将音轨导入主曲目库", variable=self.var_import_lib).pack(anchor="w", pady=1)

        col2 = ttk.Frame(chk_grid)
        col2.pack(side="left", fill="x", expand=True)
        ttk.Checkbutton(col2, text="注入 1000px 高清双封面 (封面+写真) 并导出 .jpg", variable=self.var_fetch_cover).pack(anchor="w", pady=1)
        ttk.Checkbutton(col2, text="下载并导出同名双语同步 LRC 歌词", variable=self.var_fetch_lyrics).pack(anchor="w", pady=1)

        self._add_button_bar("开始无损分轨与修复")

        if default_iso:
            self.top.after(100, lambda: self._load_iso_toc(default_iso))

    def _on_browse_iso(self):
        chosen = filedialog.askopenfilename(
            parent=self.top,
            title="选择 SACD ISO 镜像文件",
            filetypes=[("SACD ISO 镜像", "*.iso"), ("所有文件", "*.*")],
        )
        if chosen:
            self.var_iso_path.set(chosen)
            self._load_iso_toc(chosen)

    def _on_browse_output(self):
        chosen = filedialog.askdirectory(parent=self.top, title="选择音轨输出目录")
        if chosen:
            self.var_output_dir.set(chosen)
            self._check_space()

    def _load_iso_toc(self, iso_path: str):
        if not os.path.isfile(iso_path):
            return
        mb = _msgbox()
        if not self._sacd_exe:
            mb.showerror("缺少工具", "未检测到 sacd_extract.exe，请确保工具已收纳进 tools/ 目录。", parent=self.top)
            return

        self.lbl_disc_title.config(text="正在分析光盘目录结构 (TOC)...")
        self.top.update_idletasks()

        def _worker():
            try:
                toc = read_sacd_iso_toc(iso_path, exe_path=self._sacd_exe)
                self.top.after(0, lambda: self._apply_toc(toc))
            except Exception as exc:
                self.top.after(0, lambda: self._on_load_error(str(exc)))

        threading.Thread(target=_worker, daemon=True).start()

    def _apply_toc(self, toc: SacdIsoToc):
        self.current_toc = toc
        self.lbl_disc_title.config(text=f"💿 专辑: {toc.album_title}")
        self.lbl_artist.config(text=f"艺术家: {toc.album_artist or '未知'}")
        self.lbl_year.config(text=f"年份: {toc.year or '未知'}")
        self.lbl_cat.config(text=f"唱片号: {toc.catalog_number or '-'}")

        # 设置默认输出目录
        if not self.var_output_dir.get():
            clean_name = sanitize_filename(f"{toc.album_title or Path(toc.iso_path).stem} (DSF)")
            default_out = str(Path(toc.iso_path).parent / clean_name)
            self.var_output_dir.set(default_out)

        # 声道可用性
        self.rad_stereo.config(state="normal" if toc.has_stereo else "disabled")
        self.rad_mch.config(state="normal" if toc.has_multichannel else "disabled")
        if not toc.has_stereo and toc.has_multichannel:
            self.var_channel.set("mch")
        else:
            self.var_channel.set("2ch")

        self._refresh_track_tree()
        self._check_space()

    def _on_load_error(self, err: str):
        self.lbl_disc_title.config(text="读取光盘目录失败")
        _msgbox().showerror("TOC 解析失败", f"无法读取 SACD ISO 光盘信息: {err}", parent=self.top)

    def _on_area_changed(self):
        self._refresh_track_tree()
        self._check_space()

    def _refresh_track_tree(self):
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.current_toc:
            return

        channel = self.var_channel.get()
        tracks = self.current_toc.get_tracks(channel)
        for t in tracks:
            self.tree.insert("", "end", values=(f"{t.index:02d}", t.title, t.performer, t.duration_str))

    def _check_space(self):
        if not self.current_toc:
            return
        channel = self.var_channel.get()
        est_mb = self.current_toc.estimate_dsf_size_mb(channel)
        self.lbl_est_size.config(text=f"预估解压体积: ~{est_mb} MB")

        out_path = self.var_output_dir.get()
        if out_path:
            has_space, free_bytes, _ = check_disk_space(out_path, est_mb * 1024 * 1024)
            free_gb = free_bytes / (1024**3)
            if has_space:
                self.lbl_free_space.config(
                    text=f"目标磁盘剩余空间: {free_gb:.1f} GB (空间充足)", foreground="#15803d"
                )
            else:
                self.lbl_free_space.config(
                    text=f"警告: 目标磁盘剩余空间仅 {free_gb:.1f} GB，可能不足以存放解压后的音频！", foreground="#dc2626"
                )

    def _on_confirm(self):
        mb = _msgbox()
        iso = self.var_iso_path.get().strip()
        if not iso or not os.path.isfile(iso):
            mb.showerror("镜像路径无效", "请先选择有效的 SACD ISO 镜像文件。", parent=self.top)
            return

        if not self._sacd_exe:
            mb.showerror("缺少工具", "未检测到 sacd_extract.exe，无法开始提取。", parent=self.top)
            return

        out_dir = self.var_output_dir.get().strip()
        if not out_dir:
            mb.showerror("缺少输出目录", "请指定音轨输出目录。", parent=self.top)
            return

        if self.current_toc:
            channel = self.var_channel.get()
            est_mb = self.current_toc.estimate_dsf_size_mb(channel)
            has_space, free_bytes, _ = check_disk_space(out_dir, est_mb * 1024 * 1024)
            if not has_space:
                if not mb.askyesno(
                    "磁盘空间警告",
                    f"目标磁盘剩余容量仅 {free_bytes / (1024**3):.1f} GB，预估需要约 {est_mb} MB，是否仍要继续？",
                    parent=self.top,
                ):
                    return

        opts = SacdExtractOptions(
            channel_area=self.var_channel.get(),
            output_format="dsf",
            output_dir=out_dir,
            apply_toc_tags=self.var_apply_toc.get(),
            auto_repair=self.var_auto_repair.get(),
            fetch_cover=self.var_fetch_cover.get(),
            fetch_lyrics=self.var_fetch_lyrics.get(),
            lyrics_mode=self._lyrics_mode,
            import_to_library=self.var_import_lib.get(),
            dry_run=False,
        )

        self.result = (iso, opts)
        self.top.destroy()
