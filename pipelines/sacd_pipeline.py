"""SACD ISO 自动化无损提取与智能标签修复流水线。

集成步骤：
1. 预检 SACD 镜像与 sacd_extract 绿色工具环境；
2. 读取并解析光盘 TOC 目录结构；
3. 防御性检查目标磁盘剩余空间（防写入溢出）；
4. 调用 sacd_extract 高速解压 DST 并输出 Sony DSF 音轨；
5. 自动写入 TOC 原生规范标签（ID3v2.3）；
6. （可选）触发联网搜索补全（QQ音乐/网易云）、注入 1000px 双封面及下载双语 LRC 歌词。
"""

from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable

from core.models import LibraryTrack, PlayerEnv
from infra.integrations.base import sanitize_filename
from infra.scanner.local_scanner import scan_library
from infra.tools.sacd import (
    SacdIsoToc,
    check_disk_space,
    extract_sacd_iso,
    find_sacd_extract,
    read_sacd_iso_toc,
)
from pipelines.executor import resolve_hook
from pipelines.options import FixOptions, SacdExtractOptions
from pipelines.repair import execute_repairs
from services.art import apply_track_set_tags


def execute_sacd_pipeline(
    env: PlayerEnv,
    iso_path: str | Path,
    options: SacdExtractOptions,
    *,
    progress_cb: Callable[[int, int, str], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> dict:
    """执行 SACD ISO 完整提取与后处理流水线。"""
    emit_log = log_cb or (lambda _m: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    iso = Path(iso_path).resolve()
    if not iso.is_file():
        msg = f"未找到指定的 SACD ISO 镜像文件: {iso}"
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": "", "generated_files": ()}

    exe = resolve_hook("find_sacd_extract", find_sacd_extract)()
    if not exe:
        msg = "未检测到 sacd_extract.exe，请确保工具已收纳进 tools/ 目录。"
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": "", "generated_files": ()}

    emit_log(f"开始分析 SACD ISO 镜像结构: {iso.name}")
    try:
        toc = read_sacd_iso_toc(iso, exe_path=exe)
    except Exception as exc:
        msg = f"读取 SACD ISO 目录 (TOC) 失败: {exc}"
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": "", "generated_files": ()}

    # 确定目标输出目录
    album_name = toc.album_title or iso.stem
    clean_folder_name = sanitize_filename(f"{album_name} (DSF)")
    if options.output_dir:
        out_dir = Path(options.output_dir).resolve()
    else:
        out_dir = iso.parent / clean_folder_name

    target_tracks = toc.get_tracks(options.channel_area)
    if not target_tracks:
        area_desc = "立体声 (2ch)" if options.channel_area.lower() in ("2ch", "stereo") else "多声道 (mch)"
        msg = f"该 SACD ISO 不包含所选的 {area_desc} 声道区域曲目。"
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": str(out_dir), "generated_files": ()}

    # 预估所需空间并检查磁盘容量
    est_mb = toc.estimate_dsf_size_mb(options.channel_area)
    has_space, free_bytes, req_bytes = check_disk_space(out_dir, est_mb * 1024 * 1024)
    if not has_space:
        msg = (
            f"目标磁盘剩余空间不足！预估需要约 {est_mb} MB "
            f"({req_bytes / (1024**3):.2f} GB)，实际剩余 {free_bytes / (1024**3):.2f} GB。"
        )
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": str(out_dir), "generated_files": ()}

    emit_log(
        f"光盘信息解析成功: 《{toc.album_title}》 | 艺术家: {toc.album_artist or '未知'} | "
        f"年份: {toc.year or '未知'} | 曲目: {len(target_tracks)} 首 | 预估空间: ~{est_mb} MB"
    )

    if options.dry_run:
        emit_log(f"[预演模式] 将解压至目录: {out_dir}，共 {len(target_tracks)} 首曲目，不执行实际提取。")
        return {
            "success": True,
            "dry_run": True,
            "output_dir": str(out_dir),
            "generated_files": (),
            "track_count": len(target_tracks),
            "toc": toc,
            "error": None,
        }

    # 执行解压分轨
    emit_log(f"开始执行 DST ➜ DSF 无损解压分轨 -> {out_dir}")
    selected_list = list(options.selected_tracks) if options.selected_tracks else None

    def _on_extract_progress(pct: float, desc: str):
        emit_log(f"[提取进度] {desc}")
        if progress_cb:
            progress_cb(int(pct * 100), 100, desc)

    ok, msg, gen_files = extract_sacd_iso(
        iso,
        out_dir,
        channel_area=options.channel_area,
        output_format=options.output_format,
        selected_tracks=selected_list,
        progress_callback=_on_extract_progress,
        is_cancelled_callback=is_cancel,
        exe_path=exe,
    )

    if not ok:
        emit_log(f"[SACD 失败] {msg}")
        return {"success": False, "error": msg, "output_dir": str(out_dir), "generated_files": ()}

    emit_log(f"分轨成功完成，共生成 {len(gen_files)} 首 DSF 文件。")

    # 阶段 3: 写入原生 TOC 标签 (ID3v2.3)
    if options.apply_toc_tags and gen_files:
        emit_log("正在向 DSF 音轨注入原生 TOC 元数据 (ID3v2.3)...")
        # 建立序号或名称映射
        track_map = {t.index: t for t in target_tracks}
        for f in gen_files:
            if is_cancel():
                break
            # 从文件名中猜测曲目序号，如 "01 - Title.dsf" 或 "01. Title.dsf"
            m = re.match(r"^(\d+)", f.name)
            t_idx = int(m.group(1)) if m else None
            t_info = track_map.get(t_idx) if t_idx is not None else None

            tag_dict: dict[str, str] = {
                "album": toc.album_title,
                "albumartist": toc.album_artist,
            }
            if toc.year:
                tag_dict["date"] = toc.year
            if toc.genre:
                tag_dict["genre"] = toc.genre

            if t_info:
                if t_info.title:
                    tag_dict["title"] = t_info.title
                if t_info.performer:
                    tag_dict["artist"] = t_info.performer
                tag_dict["tracknumber"] = str(t_info.index)

            try:
                apply_track_set_tags(str(f), tag_dict)
            except Exception as exc:
                emit_log(f"[标签警告] 写入 {f.name} 标签失败: {exc}")

    # 阶段 4: 联网自动修复 (标签/封面/歌词)
    if (options.auto_repair or options.fetch_cover or options.fetch_lyrics) and gen_files:
        emit_log("正在执行联网元数据检索、双封面与歌词下载流水线...")
        try:
            tracks = scan_library(env, folders=[str(out_dir)], include_art_scan=True)
            if tracks:
                fix_opts = FixOptions(
                    enable_track=options.auto_repair,
                    enable_tags=options.auto_repair,
                    enable_extra_tags=options.auto_repair,
                    enable_art=options.fetch_cover,
                    enable_artist_art=options.fetch_cover,
                    enable_lyrics=options.fetch_lyrics,
                    lyrics_mode=getattr(options, 'lyrics_mode', 'both'),
                    enable_sync=False,
                    dry_run=False,
                )
                repair_res = execute_repairs(
                    env,
                    tracks,
                    fix_opts,
                    progress_cb=(lambda c, t, m: progress_cb(c, t, m) if progress_cb else None),
                    log_cb=emit_log,
                    is_cancelled_cb=is_cancel,
                )
                emit_log(f"联网智能修复完成: 封面成功 {repair_res.get('art_ok', 0)}, 标签成功 {repair_res.get('tag_ok', 0)}")
        except Exception as exc:
            emit_log(f"[联网修复警告] 联网补充信息失败: {exc}")

    emit_log(f"🎉 SACD ISO 处理全部完成！输出目录: {out_dir}")
    return {
        "success": True,
        "dry_run": False,
        "output_dir": str(out_dir),
        "generated_files": tuple(str(f) for f in gen_files),
        "track_count": len(gen_files),
        "toc": toc,
        "error": None,
    }
