"""音频格式批量转换流水线（通过 ffmpeg 并发转码并保留元数据）。"""

from __future__ import annotations

import os

from services.art import convert_track_format
from core.constants import CONVERT_TARGET_FORMATS, STATUS_ERROR
from core.models import FoobarEnv, LibraryTrack
from pipelines.executor import _run_pool_cancellable, _sync_foobar_targets, resolve_hook
from pipelines.options import ConvertOptions
from tag_io import find_ffmpeg


def execute_convert(
    env: FoobarEnv, targets: list[LibraryTrack], options: ConvertOptions,
    progress_cb=None, row_cb=None, log_cb=None, is_cancelled_cb=None,
) -> dict:
    """批量转换音频格式（通过 ffmpeg 保留元数据与封面/歌词并原子替换）。"""
    fmt = str(options.target_format or 'flac').strip().lower().lstrip('.')
    total = len(targets)
    cancelled = False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    conv_ok = conv_skipped = conv_fail = 0
    output_paths: list[str] = []

    if fmt not in CONVERT_TARGET_FORMATS:
        return {
            'cancelled': False, 'dry_run': options.dry_run,
            'conv_ok': 0, 'conv_skipped': 0, 'conv_fail': total,
            'output_paths': (), 'touched': 0, 'reloaded': False,
            'error': f'不支持的目标格式: {fmt}',
        }

    ffmpeg_bin = resolve_hook('find_ffmpeg', find_ffmpeg)()
    if not options.dry_run and not ffmpeg_bin:
        msg = '未找到 ffmpeg，请先安装 ffmpeg 并加入系统 PATH 后再试。'
        emit_log(f'[转换失败] {msg}')
        return {
            'cancelled': False, 'dry_run': False,
            'conv_ok': 0, 'conv_skipped': 0, 'conv_fail': total,
            'output_paths': (), 'touched': 0, 'reloaded': False, 'error': msg,
        }

    emit_log(f"{'[预演]' if options.dry_run else '开始'}批量转换格式 -> .{fmt}（共 {total} 首）...")
    work_items: list[LibraryTrack] = []
    for idx, tr in enumerate(targets, 1):
        if is_cancel():
            cancelled = True
            break
        if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
            conv_skipped += 1
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            continue
        cur_ext = os.path.splitext(tr.path)[1].lower().lstrip('.')
        out_path = os.path.splitext(tr.path)[0] + '.' + fmt
        if cur_ext == fmt:
            conv_skipped += 1
            emit_row(tr.path, {'status_text': '已跳过', 'source_note': f'已是 .{fmt} 格式', 'color': 'muted'})
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            continue
        if options.dry_run:
            if os.path.exists(out_path) and not options.overwrite:
                conv_skipped += 1
                emit_row(tr.path, {'status_text': '跳过转换', 'source_note': f'目标文件已存在: {os.path.basename(out_path)}', 'color': 'warn'})
                emit_log(f'[预演][跳过] {tr.filename} -> 目标已存在')
            else:
                conv_ok += 1
                note = f'将转为 {os.path.basename(out_path)}' + ('（并删除源文件）' if options.delete_source else '')
                emit_row(tr.path, {'status_text': '预演转换', 'source_note': note, 'color': 'info'})
                emit_log(f'[预演转换] {tr.filename} -> {note}')
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            continue
        work_items.append(tr)

    if work_items and not options.dry_run and not cancelled:
        done_cnt = 0
        work_total = len(work_items)

        def _submit_conv(tr: LibraryTrack):
            return convert_track_format(tr.path, fmt, overwrite=options.overwrite, delete_source=options.delete_source)

        def _on_conv_done(tr: LibraryTrack, fut):
            nonlocal done_cnt, conv_ok, conv_fail
            done_cnt += 1
            try:
                ok, msg, out_p = fut.result()
            except Exception as exc:
                ok, msg, out_p = False, f'转换异常: {exc}', ''
            if ok:
                conv_ok += 1
                if out_p:
                    output_paths.append(out_p)
                emit_row(tr.path, {'status_text': '已转换', 'source_note': msg, 'color': 'ok', 'output_path': out_p})
                emit_log(f'[已转换] {tr.filename} -> {msg}')
            else:
                conv_fail += 1
                emit_row(tr.path, {'status_text': '转换失败', 'source_note': msg, 'color': 'err'})
                emit_log(f'[转换失败] {tr.filename} -> {msg}')
            if progress_cb:
                progress_cb(done_cnt, work_total, tr.filename)

        cancelled = _run_pool_cancellable(work_items, _submit_conv, _on_conv_done, max_workers=2, is_cancel=is_cancel)

    touched, reloaded = (0, False)
    if not options.dry_run and options.enable_sync and output_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, output_paths, None, emit_log, log_noun='转换后文件')

    return {
        'cancelled': cancelled, 'dry_run': options.dry_run,
        'conv_ok': conv_ok, 'conv_skipped': conv_skipped, 'conv_fail': conv_fail,
        'output_paths': tuple(output_paths), 'touched': touched, 'reloaded': reloaded,
    }
