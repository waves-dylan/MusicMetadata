"""SACD ISO 绿色工具集成与无损分轨引擎。

通过项目内置的 sacd_extract.exe 实现：
1. 秒级只读解析 SACD ISO 光盘目录 (TOC/ScarletBook)；
2. 高速无损解压 DST 并转封装为 Sony DSF 单曲音轨；
3. 提取过程的实时双进度监控、取消中断及磁盘空间防御检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Callable

# Windows 下隐藏子进程控制台黑框
_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class SacdTrackToc:
    """SACD 光盘单首曲目元数据。"""

    index: int
    title: str = ""
    performer: str = ""
    duration_str: str = ""
    duration_seconds: float = 0.0
    start_time_str: str = ""


@dataclass
class SacdAreaInfo:
    """SACD 光盘音频区域信息（立体声或多声道）。"""

    area_index: int
    speaker_config: str  # "2 Channel", "5 Channel", "6 Channel" 等
    track_count: int = 0
    total_playtime_str: str = ""
    total_playtime_seconds: float = 0.0
    tracks: list[SacdTrackToc] = field(default_factory=list)


@dataclass
class SacdIsoToc:
    """SACD ISO 整盘目录元数据结构。"""

    iso_path: Path
    album_title: str = ""
    album_artist: str = ""
    publisher: str = ""
    catalog_number: str = ""
    year: str = ""
    genre: str = ""
    areas: dict[str, SacdAreaInfo] = field(default_factory=dict)

    @property
    def has_stereo(self) -> bool:
        return "stereo" in self.areas and bool(self.areas["stereo"].tracks)

    @property
    def has_multichannel(self) -> bool:
        return "multichannel" in self.areas and bool(self.areas["multichannel"].tracks)

    @property
    def stereo_tracks(self) -> list[SacdTrackToc]:
        area = self.areas.get("stereo")
        return area.tracks if area else []

    @property
    def multichannel_tracks(self) -> list[SacdTrackToc]:
        area = self.areas.get("multichannel")
        return area.tracks if area else []

    def get_tracks(self, channel_area: str = "2ch") -> list[SacdTrackToc]:
        if channel_area.lower() in ("2ch", "stereo"):
            return self.stereo_tracks
        return self.multichannel_tracks

    def estimate_dsf_size_mb(self, channel_area: str = "2ch") -> int:
        """估算解压为未压缩 DSF 的总大小 (MB)。

        DSD64 单声道码率为 2,822,400 bits/sec (约 352.8 KB/s)。
        立体声 2 声道约为 705.6 KB/s (约 41.3 MB/分钟)。
        5.1 环绕 6 声道约为 2.11 MB/s (约 124 MB/分钟)。
        """
        tracks = self.get_tracks(channel_area)
        total_seconds = sum(t.duration_seconds for t in tracks)
        if total_seconds <= 0:
            area = self.areas.get("stereo" if channel_area.lower() in ("2ch", "stereo") else "multichannel")
            if area:
                total_seconds = area.total_playtime_seconds

        channels = 2 if channel_area.lower() in ("2ch", "stereo") else 6
        # DSD64: 2822400 / 8 bytes per second per channel
        bytes_per_sec = (2822400 // 8) * channels
        estimated_bytes = int(total_seconds * bytes_per_sec)
        # 加上 ID3 头与 DSF chunk 开销预留 5%
        return max(50, int((estimated_bytes * 1.05) / (1024 * 1024)))


def find_sacd_extract() -> str | None:
    """按优先级多级探测 sacd_extract.exe 可执行文件路径。

    1. 自定义环境变量 SACD_EXTRACT_PATH
    2. PyInstaller 打包环境 (sys._MEIPASS / tools / sacd_extract.exe)
    3. 项目内嵌绿色目录 (MusicMetadata / tools / sacd_extract.exe)
    4. 系统 PATH 查找
    5. 常见播放器组件目录 (foobar2000 等)
    """
    # 1. 自定义环境变量优先
    custom = os.environ.get("SACD_EXTRACT_PATH")
    if custom and Path(custom).is_file():
        return str(Path(custom).resolve())

    # 2. PyInstaller 运行时目录
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "tools" / "sacd_extract.exe"
        if p.is_file():
            return str(p)

    # 3. 项目内嵌 tools 目录
    # 针对当前文件路径定位项目根/应用根
    candidates = [
        Path(__file__).resolve().parents[2] / "tools" / "sacd_extract.exe",
        Path.cwd() / "MusicMetadata" / "tools" / "sacd_extract.exe",
        Path.cwd() / "tools" / "sacd_extract.exe",
    ]
    for c in candidates:
        if c.is_file():
            return str(c)

    # 4. 系统 PATH
    found = shutil.which("sacd_extract")
    if found:
        return found

    # 5. foobar2000 组件目录回退
    common_fb_paths = [
        Path(r"D:\Program Files\foobar2000\profile\user-components-x64\foo_input_sacd\sacd_extract.exe"),
        Path(r"C:\Program Files\foobar2000\profile\user-components-x64\foo_input_sacd\sacd_extract.exe"),
        Path(r"D:\Program Files\foobar2000\sacd_extract.exe"),
        Path(r"C:\Program Files\foobar2000\sacd_extract.exe"),
    ]
    for fbp in common_fb_paths:
        if fbp.is_file():
            return str(fbp)

    return None


def parse_time_to_seconds(time_str: str) -> float:
    """将 mm:ss:ff 或 hh:mm:ss 格式转为秒数。

    对于 SACD 时间码，frames 通常为 1/75 秒。
    """
    if not time_str:
        return 0.0
    parts = time_str.strip().split(":")
    try:
        if len(parts) == 3:
            p0, p1, p2 = int(parts[0]), int(parts[1]), int(parts[2])
            # 判断第 1 位是否很大（小时）
            return p0 * 60.0 + p1 + (p2 / 75.0)
        elif len(parts) == 2:
            return int(parts[0]) * 60.0 + int(parts[1])
    except Exception:
        pass
    return 0.0


def read_sacd_iso_toc(iso_path: str | Path, *, exe_path: str | None = None) -> SacdIsoToc:
    """秒级读取 SACD ISO 光盘信息与目录 (TOC/ScarletBook)。

    调用 `sacd_extract.exe -P -i "<iso_path>"` 解析标准输出文本。
    """
    iso = Path(iso_path).resolve()
    if not iso.is_file():
        raise FileNotFoundError(f"未找到 SACD ISO 镜像: {iso}")

    exe = exe_path or find_sacd_extract()
    if not exe:
        raise RuntimeError("未检测到 sacd_extract.exe，请确保工具已收纳进 tools/ 目录。")

    cmd = [exe, "-P", "-i", str(iso)]
    # 工具可能会读取同一目录或工作目录下的 sacd_extract.cfg
    cwd = str(Path(exe).parent)
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=_CREATE_NO_WINDOW,
        text=False,
    )

    raw_output = proc.stdout or b""
    # 尝试 utf-8，其次 gbk/latin-1
    for enc in ("utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw_output.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw_output.decode("utf-8", errors="replace")

    return parse_sacd_extract_stdout(text, iso)


def parse_sacd_extract_stdout(text: str, iso_path: Path) -> SacdIsoToc:
    """解析 sacd_extract -P 输出的文本内容为结构化 SacdIsoToc。"""
    toc = SacdIsoToc(iso_path=iso_path)

    # 匹配全局专辑/光盘信息
    album_title_m = re.search(r"^\tTitle:\s*(.+)$", text, re.MULTILINE)
    if album_title_m:
        toc.album_title = album_title_m.group(1).strip()

    artist_m = re.search(r"^\tArtist:\s*(.+)$", text, re.MULTILINE)
    if artist_m:
        toc.album_artist = artist_m.group(1).strip()

    pub_m = re.search(r"^\tPublisher:\s*(.+)$", text, re.MULTILINE)
    if pub_m:
        toc.publisher = pub_m.group(1).strip()

    cat_m = re.search(r"^\tAlbum Catalog Number:\s*(.+)$", text, re.MULTILINE) or re.search(r"^\tDisc Catalog Number:\s*(.+)$", text, re.MULTILINE)
    if cat_m:
        toc.catalog_number = cat_m.group(1).strip()

    date_m = re.search(r"^\tCreation date:\s*(\d{4})-\d{2}-\d{2}", text, re.MULTILINE)
    if date_m:
        toc.year = date_m.group(1).strip()

    genre_m = re.search(r"^\tAlbum Genre:\s*(.+)$", text, re.MULTILINE) or re.search(r"^\tDisc Genre:\s*(.+)$", text, re.MULTILINE)
    if genre_m:
        toc.genre = genre_m.group(1).strip()

    # 解析各音频区域 Area Information 与 Track list
    # 文本中通常包含 "Area Information [0]:" 或 "[1]:"
    # 然后跟着 "Speaker config: 2 Channel" 或 "5 Channel"
    # 之后跟着 "Track list [0]:"
    area_blocks = re.split(r"^\tArea Information\s*\[(\d+)\]:\s*$", text, flags=re.MULTILINE)
    # block 0 是头部，奇数项是 area_idx，偶数项是内容
    if len(area_blocks) > 1:
        for i in range(1, len(area_blocks), 2):
            area_idx = int(area_blocks[i])
            content = area_blocks[i + 1]

            spk_m = re.search(r"^\tSpeaker config:\s*(.+)$", content, re.MULTILINE)
            spk_config = spk_m.group(1).strip() if spk_m else ("2 Channel" if area_idx == 0 else "Multi Channel")

            playtime_m = re.search(r"^\tTotal play time:\s*(\d+:\d+:\d+)", content, re.MULTILINE)
            playtime_str = playtime_m.group(1).strip() if playtime_m else ""

            area_info = SacdAreaInfo(
                area_index=area_idx,
                speaker_config=spk_config,
                total_playtime_str=playtime_str,
                total_playtime_seconds=parse_time_to_seconds(playtime_str),
            )

            # 解析此区域的曲目
            tracks: list[SacdTrackToc] = []
            # 匹配 Title[%d]: ...
            # 每首曲目包含 Title, Performer, Duration, Start_Time
            track_indices = sorted(set(int(m) for m in re.findall(r"^\t\tTitle\[(\d+)\]:", content, re.MULTILINE)))
            for tidx in track_indices:
                t_title_m = re.search(rf"^\t\tTitle\[{tidx}\]:\s*(.*)$", content, re.MULTILINE)
                t_perf_m = re.search(rf"^\t\tPerformer\[{tidx}\]:\s*(.*)$", content, re.MULTILINE)
                t_dur_m = re.search(rf"^\t\tDuration:\s*(\d+:\d+:\d+)", content[content.find(f"Title[{tidx}]"):], re.MULTILINE)
                t_start_m = re.search(rf"^\t\tTrack_Start_Time_Code:\s*(\d+:\d+:\d+)", content[content.find(f"Title[{tidx}]"):], re.MULTILINE)

                title_val = t_title_m.group(1).strip() if t_title_m else f"Track {tidx + 1:02d}"
                perf_val = t_perf_m.group(1).strip() if t_perf_m else toc.album_artist
                dur_val = t_dur_m.group(1).strip() if t_dur_m else ""
                start_val = t_start_m.group(1).strip() if t_start_m else ""

                track = SacdTrackToc(
                    index=tidx + 1,  # 1-based
                    title=title_val,
                    performer=perf_val,
                    duration_str=dur_val,
                    duration_seconds=parse_time_to_seconds(dur_val),
                    start_time_str=start_val,
                )
                tracks.append(track)

            area_info.tracks = tracks
            area_info.track_count = len(tracks)

            area_key = "stereo" if "2" in spk_config else "multichannel"
            toc.areas[area_key] = area_info

    # 兜底：若文件名有专辑名但 TOC 为空
    if not toc.album_title:
        toc.album_title = iso_path.stem

    return toc


def check_disk_space(target_dir: Path | str, required_bytes: int) -> tuple[bool, int, int]:
    """检查目标目录所在磁盘的可用空间。

    返回 (是否有足够空间, 剩余字节数, 所需字节数)。
    """
    p = Path(target_dir).resolve()
    # 向上寻找存在的父目录
    check_p = p
    while not check_p.exists() and check_p.parent != check_p:
        check_p = check_p.parent

    try:
        usage = shutil.disk_usage(check_p)
        free_bytes = usage.free
        return (free_bytes >= required_bytes, free_bytes, required_bytes)
    except Exception:
        # 若无法读取，默认允许
        return (True, 10 * 1024 * 1024 * 1024, required_bytes)


def extract_sacd_iso(
    iso_path: str | Path,
    output_dir: str | Path,
    *,
    channel_area: str = "2ch",
    output_format: str = "dsf",
    selected_tracks: list[int] | None = None,
    progress_callback: Callable[[float, str], None] | None = None,
    is_cancelled_callback: Callable[[], bool] | None = None,
    exe_path: str | None = None,
) -> tuple[bool, str, list[Path]]:
    """调用 sacd_extract 执行 SACD ISO 解压与分轨。

    Args:
        iso_path: ISO 镜像文件路径
        output_dir: 目标输出文件夹
        channel_area: "2ch" (立体声) 或 "mch" (多声道)
        output_format: "dsf" (默认推荐, 带 ID3 标签支持) 或 "dsdiff"
        selected_tracks: 指定曲目序号列表 (1-based), None 为全盘
        progress_callback: 进度回调 (0.0~1.0, 状态描述字符串)
        is_cancelled_callback: 外部取消查询函数，返回 True 时终止执行
        exe_path: 指定的可执行程序路径，None 时自动查找

    Returns:
        (success: bool, message: str, generated_files: list[Path])
    """
    iso = Path(iso_path).resolve()
    if not iso.is_file():
        return False, f"未找到 SACD ISO 文件: {iso}", []

    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    exe = exe_path or find_sacd_extract()
    if not exe:
        return False, "未检测到 sacd_extract.exe，无法执行提取。", []

    # 组装命令行参数
    # -i FILE : 输入文件
    # -2 : 两声道 (或 -m : 多声道)
    # -s : Sony DSF 输出 (或 -p : DSDIFF)
    # -c : 转换 DST 为 DSD
    # -y DIR : 输出文件夹 (DSF/DSDIFF 格式)
    cmd = [exe, "-i", str(iso)]
    if channel_area.lower() in ("mch", "multichannel", "5ch", "6ch"):
        cmd.append("-m")
    else:
        cmd.append("-2")

    if output_format.lower() == "dsdiff":
        cmd.append("-p")
    else:
        cmd.extend(["-s", "-c"])

    cmd.extend(["-y", str(out)])

    if selected_tracks:
        cmd.extend(["-t", ",".join(str(t) for t in selected_tracks)])

    # 启动子进程
    cwd = str(Path(exe).parent)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=_CREATE_NO_WINDOW,
            bufsize=1,
        )
    except Exception as exc:
        return False, f"启动 sacd_extract 进程失败: {exc}", []

    current_track_name = ""
    current_track_no = 0
    total_tracks = len(selected_tracks) if selected_tracks else 0
    last_reported_pct = 0.0

    track_pattern = re.compile(r"Processing\s+\[(.*?)\]\s+\((\d+)/(\d+)\)\.\.")
    # Completed: 12% (file sectors processed: ...), Total: 5% (total sectors...)
    progress_pattern = re.compile(r"Completed:\s*(\d+)%.*?Total:\s*(\d+)%")
    single_pct_pattern = re.compile(r"Completed:\s*(\d+)%")

    cancelled = False
    try:
        assert proc.stdout is not None
        while True:
            # 检查取消标志
            if is_cancelled_callback and is_cancelled_callback():
                cancelled = True
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break

            line_bytes = proc.stdout.readline()
            if not line_bytes:
                break

            try:
                line = line_bytes.decode("utf-8", errors="replace").strip()
            except Exception:
                continue

            if not line:
                continue

            # 匹配当前曲目
            tr_m = track_pattern.search(line)
            if tr_m:
                current_track_name = tr_m.group(1).strip()
                current_track_no = int(tr_m.group(2))
                total_tracks = int(tr_m.group(3))
                msg = f"正在解压第 {current_track_no}/{total_tracks} 首: {current_track_name}"
                if progress_callback:
                    progress_callback(last_reported_pct, msg)
                continue

            # 匹配百分比进度
            pr_m = progress_pattern.search(line)
            if pr_m:
                file_pct = int(pr_m.group(1))
                total_pct = int(pr_m.group(2))
                pct = max(0.0, min(1.0, total_pct / 100.0))
                last_reported_pct = pct
                msg = f"解压进度 {total_pct}% (当前曲目: {file_pct}%)"
                if progress_callback:
                    progress_callback(pct, msg)
                continue

            s_m = single_pct_pattern.search(line)
            if s_m and total_tracks > 0 and current_track_no > 0:
                cur_f_pct = int(s_m.group(1))
                # 估算总体进度: ((cur_no - 1) + cur_f_pct/100) / total_tracks
                pct = max(0.0, min(1.0, ((current_track_no - 1) + cur_f_pct / 100.0) / total_tracks))
                last_reported_pct = pct
                msg = f"解压进度 {int(pct * 100)}% (第 {current_track_no}/{total_tracks} 首)"
                if progress_callback:
                    progress_callback(pct, msg)

        retcode = proc.wait()
    except Exception as exc:
        proc.kill()
        return False, f"提取过程中发生异常: {exc}", []

    if cancelled:
        return False, "用户已取消提取操作。", []

    if retcode != 0:
        return False, f"sacd_extract 退出状态异常 (code={retcode})", []

    # 扫描生成的音频文件
    ext = ".dsf" if output_format.lower() == "dsf" else ".dff"
    # sacd_extract 可能会把文件放在 output_dir 本身或者子文件夹中
    found_files = sorted(out.rglob(f"*{ext}"))
    if not found_files:
        return False, "提取完成，但未在输出目录中发现生成的音频文件。", []

    if progress_callback:
        progress_callback(1.0, f"分轨提取完成，共生成 {len(found_files)} 首曲目！")

    return True, f"成功提取 {len(found_files)} 首曲目", found_files
