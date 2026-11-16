"""
ui/track_detail.py - 曲目详细元数据与封面预览展示组件
支持底部横向面板 (默认) 与右侧纵向面板两种布局形态，
支持单选精细化展示、多选智能差异对比 (Smart Diff) 以及封面缩略图实时渲染。
"""

import io
import os
import tkinter as tk
from tkinter import ttk
from typing import Callable
from PIL import Image, ImageTk, ImageDraw

from core.models import LibraryTrack
from tag_io import sniff_container, LOADERS, read_pictures

def _format_duration_str(secs: float) -> str:
    if not secs or secs <= 0:
        return '--:--'
    m, s = divmod(int(round(secs)), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

class TrackDetailWidget(ttk.Frame):
    """
    曲目元数据详情面板。
    可作为底部卡片横向展开，或作为右侧边栏纵向展开。
    """
    def __init__(
        self,
        parent: tk.Widget,
        orient: str = 'horizontal',
        on_pick_cover: Callable[[], None] | None = None,
        on_reveal_file: Callable[[str], None] | None = None,
    ):
        super().__init__(parent)
        self.orient = orient
        self.on_pick_cover = on_pick_cover
        self.on_reveal_file = on_reveal_file
        self.current_tracks: list[LibraryTrack] = []

        # 封面缩略图缓存 (path/folder -> PhotoImage)
        self._cover_cache: dict[str, ImageTk.PhotoImage] = {}
        self._placeholder_photo: ImageTk.PhotoImage | None = None

        self._build_ui()
        self.set_empty()

    def set_orientation(self, orient: str):
        if orient == self.orient:
            return
        self.orient = orient
        for child in self.winfo_children():
            child.destroy()
        self._build_ui()
        if self.current_tracks:
            self.update_selection(self.current_tracks)
        else:
            self.set_empty()

    def _get_placeholder_image(self, size: tuple[int, int]) -> ImageTk.PhotoImage:
        w, h = size
        img = Image.new('RGB', (w, h), color='#f1f5f9')
        draw = ImageDraw.Draw(img)
        # 绘制黑胶唱片外圈
        margin = max(4, int(min(w, h) * 0.08))
        cx, cy = w // 2, h // 2
        r = min(w, h) // 2 - margin
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill='#cbd5e1', outline='#94a3b8', width=2)
        # 唱片同心内圈
        r_inner = int(r * 0.45)
        draw.ellipse((cx - r_inner, cy - r_inner, cx + r_inner, cy + r_inner), fill='#e2e8f0', outline='#94a3b8')
        r_hole = max(3, int(r * 0.12))
        draw.ellipse((cx - r_hole, cy - r_hole, cx + r_hole, cy + r_hole), fill='#f1f5f9', outline='#64748b')
        return ImageTk.PhotoImage(img)

    def _build_ui(self):
        if self.orient == 'horizontal':
            self._build_horizontal_ui()
        else:
            self._build_vertical_ui()

    def _build_horizontal_ui(self):
        card = ttk.LabelFrame(self, text='曲目详细元数据', padding=(8, 4))
        card.pack(fill='both', expand=True)

        # 1. 左侧封面区
        cov_frame = ttk.Frame(card, width=105)
        cov_frame.pack(side='left', fill='y', padx=(0, 10))
        cov_frame.pack_propagate(False)

        self.lbl_cover = tk.Label(cov_frame, bg='#f8fafc', relief='solid', borderwidth=1, cursor='hand2')
        self.lbl_cover.pack(side='top', pady=(2, 2))
        self.lbl_cover.bind('<Button-1>', lambda _e: self.on_pick_cover() if self.on_pick_cover else None)

        self.lbl_cover_info = ttk.Label(cov_frame, text='未选择曲目', font=('', 8), foreground='#64748b', anchor='center')
        self.lbl_cover_info.pack(side='top', fill='x')

        # 分隔线
        ttk.Separator(card, orient='vertical').pack(side='left', fill='y', padx=(0, 8))

        # 2. 中间三列信息网格
        info_grid = ttk.Frame(card)
        info_grid.pack(side='left', fill='both', expand=True)

        info_grid.columnconfigure(0, weight=3) # 核心基础
        info_grid.columnconfigure(1, weight=3) # 创作者与流派
        info_grid.columnconfigure(2, weight=3) # 音频规格与歌词
        info_grid.rowconfigure(0, weight=1)

        # 列 1: 核心基础信息
        c1 = ttk.Frame(info_grid)
        c1.grid(row=0, column=0, sticky='nsew', padx=4)
        self.lbl_title = ttk.Label(c1, text='标题: --', font=('', 9, 'bold'), foreground='#0f172a', anchor='w')
        self.lbl_title.pack(fill='x', pady=(0, 2))
        self.lbl_artist = ttk.Label(c1, text='艺术家: --', foreground='#334155', anchor='w')
        self.lbl_artist.pack(fill='x', pady=1)
        self.lbl_album = ttk.Label(c1, text='专辑: --', foreground='#334155', anchor='w')
        self.lbl_album.pack(fill='x', pady=1)
        self.lbl_albumartist = ttk.Label(c1, text='专辑艺术家: --', foreground='#64748b', anchor='w')
        self.lbl_albumartist.pack(fill='x', pady=1)

        # 列 2: 创作团队与流派
        c2 = ttk.Frame(info_grid)
        c2.grid(row=0, column=1, sticky='nsew', padx=4)
        self.lbl_composer = ttk.Label(c2, text='作曲: --', foreground='#334155', anchor='w')
        self.lbl_composer.pack(fill='x', pady=1)
        self.lbl_lyricist = ttk.Label(c2, text='作词: --', foreground='#334155', anchor='w')
        self.lbl_lyricist.pack(fill='x', pady=1)
        self.lbl_arranger = ttk.Label(c2, text='编曲: --', foreground='#64748b', anchor='w')
        self.lbl_arranger.pack(fill='x', pady=1)
        self.lbl_genre_date = ttk.Label(c2, text='年份: --  流派: --', foreground='#64748b', anchor='w')
        self.lbl_genre_date.pack(fill='x', pady=1)

        # 列 3: 音频规格与物理信息
        c3 = ttk.Frame(info_grid)
        c3.grid(row=0, column=2, sticky='nsew', padx=4)
        self.lbl_track_disc = ttk.Label(c3, text='音轨: --  碟号: --', foreground='#334155', anchor='w')
        self.lbl_track_disc.pack(fill='x', pady=1)
        self.lbl_tech = ttk.Label(c3, text='规格: --', foreground='#334155', anchor='w')
        self.lbl_tech.pack(fill='x', pady=1)
        self.lbl_lyrics_status = ttk.Label(c3, text='歌词: --', foreground='#64748b', anchor='w')
        self.lbl_lyrics_status.pack(fill='x', pady=1)
        self.lbl_file_info = ttk.Label(c3, text='文件: --', foreground='#64748b', anchor='w', cursor='hand2')
        self.lbl_file_info.pack(fill='x', pady=1)
        self.lbl_file_info.bind('<Double-1>', self._on_file_double_click)

    def _build_vertical_ui(self):
        card = ttk.LabelFrame(self, text='曲目详细元数据', padding=(4, 4))
        card.pack(fill='both', expand=True)

        # 滚动区域 (支持滚轮上下滑动与小窗口自适应)
        canvas = tk.Canvas(card, borderwidth=0, highlightthickness=0)
        vsb = ttk.Scrollbar(card, orient='vertical', command=canvas.yview)
        scroll_content = ttk.Frame(canvas)

        scroll_content.bind(
            '<Configure>',
            lambda _e: canvas.configure(scrollregion=canvas.bbox('all'))
        )
        win_id = canvas.create_window((0, 0), window=scroll_content, anchor='nw')

        def _on_canvas_configure(e):
            canvas.itemconfig(win_id, width=e.width)
            w_wrap = max(140, e.width - 24)
            for w in (self.lbl_title, self.lbl_artist, self.lbl_album, self.lbl_albumartist,
                      self.lbl_composer, self.lbl_lyricist, self.lbl_arranger, self.lbl_file_info):
                if hasattr(w, 'configure'):
                    try:
                        w.configure(wraplength=w_wrap)
                    except Exception:
                        pass

        canvas.bind('<Configure>', _on_canvas_configure)
        canvas.configure(yscrollcommand=vsb.set)

        def _on_mousewheel(event):
            if canvas.winfo_exists():
                delta = event.delta
                if delta:
                    canvas.yview_scroll(int(-1 * (delta / 120)), 'units')

        canvas.bind('<MouseWheel>', _on_mousewheel)
        scroll_content.bind('<MouseWheel>', _on_mousewheel)
        card.bind('<MouseWheel>', _on_mousewheel)

        vsb.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        self._vert_canvas = canvas

        # 封面图区 (紧凑 96x96 居中)
        cov_frame = ttk.Frame(scroll_content)
        cov_frame.pack(fill='x', pady=(0, 4))
        cov_frame.bind('<MouseWheel>', _on_mousewheel)
        self.lbl_cover = tk.Label(cov_frame, bg='#f8fafc', relief='solid', borderwidth=1, cursor='hand2')
        self.lbl_cover.pack(side='top', pady=(2, 2))
        self.lbl_cover.bind('<Button-1>', lambda _e: self.on_pick_cover() if self.on_pick_cover else None)
        self.lbl_cover.bind('<MouseWheel>', _on_mousewheel)
        self.lbl_cover_info = ttk.Label(cov_frame, text='未选择曲目', font=('', 8), foreground='#64748b', anchor='center')
        self.lbl_cover_info.pack(side='top', fill='x')
        self.lbl_cover_info.bind('<MouseWheel>', _on_mousewheel)

        ttk.Separator(scroll_content, orient='horizontal').pack(fill='x', pady=3)

        # 纵向文本流 (紧凑规整)
        content = ttk.Frame(scroll_content)
        content.pack(fill='both', expand=True, padx=2)
        content.bind('<MouseWheel>', _on_mousewheel)

        self.lbl_title = ttk.Label(content, text='标题: --', font=('', 9, 'bold'), foreground='#0f172a', anchor='w', wraplength=220)
        self.lbl_title.pack(fill='x', pady=(1, 1))
        self.lbl_artist = ttk.Label(content, text='艺术家: --', foreground='#1e293b', anchor='w', wraplength=220)
        self.lbl_artist.pack(fill='x', pady=1)
        self.lbl_album = ttk.Label(content, text='专辑: --', foreground='#1e293b', anchor='w', wraplength=220)
        self.lbl_album.pack(fill='x', pady=1)
        self.lbl_albumartist = ttk.Label(content, text='专辑艺术家: --', foreground='#64748b', anchor='w', wraplength=220)
        self.lbl_albumartist.pack(fill='x', pady=1)

        ttk.Separator(content, orient='horizontal').pack(fill='x', pady=3)

        self.lbl_genre_date = ttk.Label(content, text='年份: --  流派: --', foreground='#475569', anchor='w')
        self.lbl_genre_date.pack(fill='x', pady=1)
        self.lbl_track_disc = ttk.Label(content, text='音轨: --  碟号: --', foreground='#475569', anchor='w')
        self.lbl_track_disc.pack(fill='x', pady=1)

        ttk.Separator(content, orient='horizontal').pack(fill='x', pady=3)

        self.lbl_composer = ttk.Label(content, text='作曲: --', foreground='#475569', anchor='w', wraplength=220)
        self.lbl_composer.pack(fill='x', pady=1)
        self.lbl_lyricist = ttk.Label(content, text='作词: --', foreground='#475569', anchor='w', wraplength=220)
        self.lbl_lyricist.pack(fill='x', pady=1)
        self.lbl_arranger = ttk.Label(content, text='编曲: --', foreground='#64748b', anchor='w', wraplength=220)
        self.lbl_arranger.pack(fill='x', pady=1)

        ttk.Separator(content, orient='horizontal').pack(fill='x', pady=3)

        self.lbl_tech = ttk.Label(content, text='规格: --', foreground='#475569', anchor='w')
        self.lbl_tech.pack(fill='x', pady=1)
        self.lbl_lyrics_status = ttk.Label(content, text='歌词: --', foreground='#64748b', anchor='w')
        self.lbl_lyrics_status.pack(fill='x', pady=1)
        self.lbl_file_info = ttk.Label(content, text='文件: --', foreground='#64748b', anchor='w', cursor='hand2', wraplength=220)
        self.lbl_file_info.pack(fill='x', pady=1)
        self.lbl_file_info.bind('<Double-1>', self._on_file_double_click)

        for child in content.winfo_children():
            child.bind('<MouseWheel>', _on_mousewheel)

    def _on_file_double_click(self, _event=None):
        if self.current_tracks and self.on_reveal_file:
            self.on_reveal_file(self.current_tracks[0].path)

    def set_empty(self):
        self.current_tracks = []
        thumb_size = (84, 84) if self.orient == 'horizontal' else (96, 96)
        placeholder = self._get_placeholder_image(thumb_size)
        self._placeholder_photo = placeholder
        self.lbl_cover.configure(image=placeholder)
        self.lbl_cover_info.configure(text='未选择曲目')

        self.lbl_title.configure(text='未选中曲目')
        self.lbl_artist.configure(text='请在列表中点击曲目以查看完整元数据')
        self.lbl_album.configure(text='专辑: --')
        self.lbl_albumartist.configure(text='专辑艺术家: --')
        self.lbl_composer.configure(text='作曲: --')
        self.lbl_lyricist.configure(text='作词: --')
        self.lbl_arranger.configure(text='编曲: --')
        self.lbl_genre_date.configure(text='年份: --  流派: --')
        self.lbl_track_disc.configure(text='音轨: --  碟号: --')
        self.lbl_tech.configure(text='格式与时长: --')
        self.lbl_lyrics_status.configure(text='歌词: --')
        self.lbl_file_info.configure(text='文件路径: --')

    def update_selection(self, tracks: list[LibraryTrack]):
        self.current_tracks = list(tracks)
        if not tracks:
            self.set_empty()
            return

        if len(tracks) == 1:
            self._display_single_track(tracks[0])
        else:
            self._display_multi_tracks(tracks)

    def _display_single_track(self, tr: LibraryTrack):
        # 1. 标题与核心信息
        t_name = tr.title or os.path.splitext(tr.filename)[0] or '(无标题)'
        self.lbl_title.configure(text=f"🎵 {t_name}")
        self.lbl_artist.configure(text=f"艺术家: {tr.artist or '未知'}")
        self.lbl_album.configure(text=f"专辑: {tr.album or '未知'}")
        self.lbl_albumartist.configure(text=f"专辑艺术家: {tr.albumartist or '(同艺术家)' if tr.albumartist else '未指定'}")

        # 2. 创作团队与流派
        self.lbl_composer.configure(text=f"作曲: {tr.composer or '未填'}")
        self.lbl_lyricist.configure(text=f"作词: {tr.lyricist or '未填'}")
        self.lbl_arranger.configure(text=f"编曲: {tr.arranger or '未填'}")

        date_str = tr.date or '未知年份'
        genre_str = tr.genre or '未分类'
        self.lbl_genre_date.configure(text=f"年份: {date_str}   流派: {genre_str}")

        # 3. 音轨与规格
        tn = tr.disk_tracknumber or (str(tr.disk_track_int) if tr.disk_track_int else '--')
        dn = tr.disk_discnumber or (str(tr.disk_disc_int) if tr.disk_disc_int else '--')
        self.lbl_track_disc.configure(text=f"音轨: {tn}   碟号: {dn}")

        ext = (tr.container or os.path.splitext(tr.filename)[1].lstrip('.')).upper()
        dur_str = _format_duration_str(tr.duration)
        self.lbl_tech.configure(text=f"格式: {ext}   时长: {dur_str}")

        # 歌词
        lrc_parts = []
        if tr.has_embedded_lyrics:
            lrc_parts.append('已内嵌歌词')
        if tr.has_lrc_file:
            lrc_parts.append('外部.lrc')
        if not lrc_parts:
            lrc_str = '缺少歌词'
        else:
            lrc_str = ' + '.join(lrc_parts)
        self.lbl_lyrics_status.configure(text=f"歌词: {lrc_str}")

        # 文件路径
        self.lbl_file_info.configure(text=f"文件: {tr.filename} (双击打开)")

        # 4. 封面图片加载
        self._load_cover_thumbnail(tr)

    def _display_multi_tracks(self, tracks: list[LibraryTrack]):
        count = len(tracks)
        self.lbl_title.configure(text=f"已选中 {count} 首曲目 (多选合并)")

        # 共同属性聚合检查
        artists = {t.artist for t in tracks if t.artist}
        self.lbl_artist.configure(text=f"艺术家: {next(iter(artists)) if len(artists) == 1 else f'<{len(artists)} 位不同艺术家>'}")

        albums = {t.album for t in tracks if t.album}
        self.lbl_album.configure(text=f"专辑: {next(iter(albums)) if len(albums) == 1 else f'<{len(albums)} 个不同专辑>'}")

        albumartists = {t.albumartist for t in tracks if t.albumartist}
        self.lbl_albumartist.configure(text=f"专辑艺术家: {next(iter(albumartists)) if len(albumartists) == 1 else '<多种或空>'}")

        composers = {t.composer for t in tracks if t.composer}
        self.lbl_composer.configure(text=f"作曲: {next(iter(composers)) if len(composers) == 1 else '<多种作曲人>'}")

        lyricists = {t.lyricist for t in tracks if t.lyricist}
        self.lbl_lyricist.configure(text=f"作词: {next(iter(lyricists)) if len(lyricists) == 1 else '<多种作词人>'}")

        arrangers = {t.arranger for t in tracks if t.arranger}
        self.lbl_arranger.configure(text=f"编曲: {next(iter(arrangers)) if len(arrangers) == 1 else '<多种编曲人>'}")

        dates = {t.date for t in tracks if t.date}
        d_str = next(iter(dates)) if len(dates) == 1 else '<多种年份>'
        genres = {t.genre for t in tracks if t.genre}
        g_str = next(iter(genres)) if len(genres) == 1 else '<多种流派>'
        self.lbl_genre_date.configure(text=f"年份: {d_str}   流派: {g_str}")

        self.lbl_track_disc.configure(text=f"曲目范围: 共 {count} 首")

        total_dur = sum(t.duration for t in tracks)
        dur_str = _format_duration_str(total_dur)
        self.lbl_tech.configure(text=f"总时长: {dur_str}")

        has_lrc_count = sum(1 for t in tracks if t.has_lyrics)
        self.lbl_lyrics_status.configure(text=f"歌词覆盖率: {has_lrc_count} / {count} 首")
        self.lbl_file_info.configure(text=f"共跨越 {len({t.folder for t in tracks})} 个文件夹")

        # 封面聚合展示 (若属于同一目录则展示第一首封面)
        folders = {t.folder for t in tracks}
        if len(folders) == 1:
            self._load_cover_thumbnail(tracks[0])
            self.lbl_cover_info.configure(text='专辑统一封面')
        else:
            thumb_size = (84, 84) if self.orient == 'horizontal' else (96, 96)
            placeholder = self._get_placeholder_image(thumb_size)
            self._placeholder_photo = placeholder
            self.lbl_cover.configure(image=placeholder)
            self.lbl_cover_info.configure(text=f'跨 {len(folders)} 个专辑')

    def _load_cover_thumbnail(self, tr: LibraryTrack):
        thumb_size = (84, 84) if self.orient == 'horizontal' else (96, 96)
        cache_key = f"{tr.path}_{thumb_size[0]}"
        if cache_key in self._cover_cache:
            self.lbl_cover.configure(image=self._cover_cache[cache_key])
            self.lbl_cover_info.configure(text='已嵌入 / 缓存')
            return

        # 尝试从磁盘加载
        photo = None
        desc = '无封面'

        # 1. 优先检查文件夹下已有的 cover.jpg / cover.png
        for c_name in ('cover.jpg', 'cover.png', 'folder.jpg', 'front.jpg'):
            c_path = os.path.join(tr.folder, c_name)
            if os.path.exists(c_path):
                try:
                    with Image.open(c_path) as im:
                        im.thumbnail(thumb_size, Image.Resampling.LANCZOS)
                        photo = ImageTk.PhotoImage(im)
                        desc = f"目录 {c_name}"
                        break
                except Exception:
                    pass

        # 2. 检查内嵌封面
        if photo is None and os.path.exists(tr.path):
            try:
                cnt = sniff_container(tr.path)
                loader = LOADERS.get(cnt) if cnt else None
                if loader:
                    audio = loader(tr.path)
                    pics = read_pictures(audio, cnt)
                    if pics and pics[0].get('data'):
                        with Image.open(io.BytesIO(pics[0]['data'])) as im:
                            orig_w, orig_h = im.size
                            im.thumbnail(thumb_size, Image.Resampling.LANCZOS)
                            photo = ImageTk.PhotoImage(im)
                            desc = f"内嵌 ({orig_w}x{orig_h})"
            except Exception:
                pass

        if photo is not None:
            if len(self._cover_cache) > 60:
                self._cover_cache.clear()
            self._cover_cache[cache_key] = photo
            self.lbl_cover.configure(image=photo)
            self.lbl_cover_info.configure(text=desc)
        else:
            placeholder = self._get_placeholder_image(thumb_size)
            self._placeholder_photo = placeholder
            self.lbl_cover.configure(image=placeholder)
            self.lbl_cover_info.configure(text='无封面 (点击挑选)')
