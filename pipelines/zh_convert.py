"""华语简繁文本批量转换流水线（支持核心标签、扩展元数据、歌词、预演与播放器缓存联动）。"""

from __future__ import annotations

import os
from typing import Callable

from core.constants import STATUS_ERROR
from core.models import LibraryTrack, PlayerEnv
from core.zh_norm import convert_lrc, diff_tags
from pipelines.executor import _sync_player_targets
from pipelines.options import ZhConvertOptions
from services.art import apply_track_set_tags
from tag_io import get_lrc_path, read_audio_info, read_lrc_file, write_lrc_file


def execute_zh_convert(
    env: PlayerEnv,
    targets: list[LibraryTrack],
    options: ZhConvertOptions,
    progress_cb: Callable[[int, int, str], None] | None = None,
    row_cb: Callable[[str, dict], None] | None = None,
    log_cb: Callable[[str], None] | None = None,
    is_cancelled_cb: Callable[[], bool] | None = None,
) -> dict:
    """批量执行音轨元数据简繁转换。"""
    total = len(targets)
    cancelled = False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    zh_ok = zh_skipped = zh_fail = 0
    total_diff_count = 0
    modified_paths: list[str] = []
    synced_paths: list[str] = []
    foobar_mtimes: dict[str, float] = {}

    dir_name = '繁体 -> 简体' if options.direction == 't2s' else '简体 -> 繁体'
    fld_desc = ', '.join(options.fields)
    emit_log(f"{'[预演]' if options.dry_run else '开始'}华语简繁文本转换（{dir_name}，共 {total} 首，目标字段：{fld_desc}）...")

    for idx, tr in enumerate(targets, 1):
        if cancelled or is_cancel():
            cancelled = True
            break
        if progress_cb:
            progress_cb(idx, total, tr.filename)

        if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
            zh_skipped += 1
            continue

        info = read_audio_info(tr.path)
        if info.get('error'):
            zh_fail += 1
            emit_row(tr.path, {'status_text': '读取失败', 'source_note': str(info['error']), 'color': 'err'})
            emit_log(f'[读取失败] {tr.filename} -> {info["error"]}')
            continue

        # 检查常规标签字段差异
        diffs = diff_tags(
            info,
            options.fields,
            direction=options.direction,
            preserve_jpop=options.preserve_jpop,
        )

        tag_updates: dict[str, str] = {fld: new_v for fld, _, new_v in diffs}

        # 检查歌词
        new_emb_lrc: str | None = None
        new_ext_lrc: str | None = None
        ext_lrc_file = get_lrc_path(tr.path)

        if options.convert_lyrics:
            emb_lrc = info.get('lyrics') or ''
            if emb_lrc.strip():
                conv_emb = convert_lrc(emb_lrc, direction=options.direction, preserve_jpop=options.preserve_jpop)
                if conv_emb != emb_lrc:
                    new_emb_lrc = conv_emb
                    tag_updates['lyrics'] = conv_emb
                    diffs.append(('lyrics', '[内嵌歌词]', '[内嵌歌词已转换]'))

            if os.path.isfile(ext_lrc_file):
                ext_lrc = read_lrc_file(tr.path)
                if ext_lrc.strip():
                    conv_ext = convert_lrc(ext_lrc, direction=options.direction, preserve_jpop=options.preserve_jpop)
                    if conv_ext != ext_lrc:
                        new_ext_lrc = conv_ext
                        diffs.append(('lrc_file', '[外置LRC]', '[外置LRC已转换]'))

        if not diffs:
            zh_skipped += 1
            emit_row(tr.path, {'status_text': '无需转换', 'source_note': '无文字差异', 'color': 'gray'})
            continue

        total_diff_count += len(diffs)
        diff_summary = '; '.join(f'{f}: {old}->{new}' for f, old, new in diffs[:3])
        if len(diffs) > 3:
            diff_summary += f' (等共 {len(diffs)} 项)'

        if options.dry_run:
            zh_ok += 1
            emit_row(tr.path, {'status_text': '预演转换', 'source_note': f'将转换: {diff_summary}', 'color': 'info'})
            emit_log(f'[预演转换] {tr.filename} -> {diff_summary}')
            continue

        # 写入真实文件
        write_ok = True
        err_msg = ''

        if tag_updates:
            ok_tags, msg_tags = apply_track_set_tags(tr.path, tag_updates)
            if not ok_tags:
                write_ok = False
                err_msg = msg_tags

        if write_ok and new_ext_lrc is not None:
            try:
                write_lrc_file(tr.path, new_ext_lrc)
            except Exception as e:
                write_ok = False
                err_msg = f'外置LRC写入失败: {e}'

        if write_ok:
            zh_ok += 1
            modified_paths.append(tr.path)
            if options.enable_sync:
                synced_paths.append(tr.path)
                foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.cached_mtime, tr.disk_mtime)

            # 更新内存模型
            for fld, new_v in tag_updates.items():
                if hasattr(tr, fld) and fld != 'lyrics':
                    setattr(tr, fld, new_v)

            emit_row(tr.path, {
                'status_text': '已转换',
                'source_note': f'已转换: {len(diffs)} 项',
                'color': 'ok',
                'diffs': diffs,
                'synced': options.enable_sync,
            })
            emit_log(f'[已转换] {tr.filename} -> {diff_summary}')
        else:
            zh_fail += 1
            emit_row(tr.path, {'status_text': '转换失败', 'source_note': err_msg, 'color': 'err'})
            emit_log(f'[转换失败] {tr.filename} -> {err_msg}')

    # 播放器联动同步
    touched = 0
    reloaded = False
    if synced_paths and options.enable_sync and not options.dry_run:
        touched, reloaded = _sync_player_targets(
            env, synced_paths, foobar_mtimes, emit_log, log_noun='转换文件'
        )

    summary = {
        'cancelled': cancelled,
        'dry_run': options.dry_run,
        'zh_ok': zh_ok,
        'zh_skipped': zh_skipped,
        'zh_fail': zh_fail,
        'modified_paths': tuple(modified_paths),
        'diff_count': total_diff_count,
        'touched': touched,
        'reloaded': reloaded,
    }
    prefix = '[预演] ' if options.dry_run else ''
    emit_log(
        f"{prefix}华语简繁转换完成：成功处理 {zh_ok} 首，无需转换 {zh_skipped} 首，失败 {zh_fail} 项（共变动 {total_diff_count} 处字段）。"
    )
    return summary
