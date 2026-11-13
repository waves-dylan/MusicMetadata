"""批量删除元数据流水线（支持多类别局部清理、外置封面清理与时间戳同步）。"""

from __future__ import annotations

import os

from art_repair import (
    DELETABLE_FIELDS,
    apply_track_deletions,
    delete_folder_artist_file,
    delete_folder_cover_file,
    inspect_deletable_fields,
)
from core.constants import FIELD_LABELS, STATUS_ERROR
from core.models import FoobarEnv, LibraryTrack
from pipelines.executor import _run_pool_cancellable, _sync_foobar_targets
from pipelines.options import DeleteOptions
from tag_io import read_audio_info


def _count_deletable_targets(targets: list[LibraryTrack]) -> tuple[dict[str, int], int, int]:
    """统计当前勾选曲目中各可删除元数据类别的有效覆盖数量。"""
    counts: dict[str, int] = {f: 0 for f in DELETABLE_FIELDS}
    count_163 = 0
    seen_folders: set[str] = set()
    folder_cover_count = 0
    for tr in targets:
        if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
            continue
        if tr.folder and tr.folder not in seen_folders:
            seen_folders.add(tr.folder)
            if os.path.isfile(os.path.join(tr.folder, 'cover.jpg')):
                folder_cover_count += 1
        if tr.has_163_key:
            count_163 += 1
        if not tr.cover_status.startswith('缺封面') and 'no-cover' not in tr.cover_reasons and 'no-cover-source' not in tr.cover_reasons:
            counts['cover'] += 1
        if tr.has_artist_art:
            counts['artist_cover'] += 1
        for f in ('title', 'artist', 'albumartist', 'album', 'date', 'composer', 'performer', 'genre', 'comment'):
            if (getattr(tr, f, '') or '').strip():
                counts[f] += 1
        if (tr.disk_tracknumber or '').strip() or tr.disk_track_int is not None:
            counts['tracknumber'] += 1
        if (tr.disk_discnumber or '').strip() or tr.disk_disc_int is not None:
            counts['discnumber'] += 1
        if tr.has_lyrics:
            counts['lyrics'] += 1
    return counts, count_163, folder_cover_count


def execute_deletions(
    env: FoobarEnv, targets: list[LibraryTrack], options: DeleteOptions,
    progress_cb=None, row_cb=None, log_cb=None, is_cancelled_cb=None,
) -> dict:
    """批量删除指定曲目中选定元数据类别的条目（支持预演、163 key 保护、外置封面清理与 foobar 刷新）。"""
    fields = [f for f in DELETABLE_FIELDS if f in options.fields]
    total = len(targets)
    cancelled = False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    deleted_ok = deleted_skipped = deleted_fail = folder_cover_deleted = folder_cover_fail = 0
    folder_artist_deleted = folder_artist_fail = 0
    field_counts: dict[str, int] = {f: 0 for f in fields}
    modified_paths: list[str] = []
    synced_paths: list[str] = []
    foobar_mtimes: dict[str, float] = {}

    if not fields:
        return {
            'cancelled': False, 'dry_run': options.dry_run,
            'deleted_ok': 0, 'deleted_skipped': total, 'deleted_fail': 0,
            'folder_cover_deleted': 0, 'folder_cover_fail': 0,
            'field_counts': field_counts, 'modified_paths': (), 'touched': 0, 'reloaded': False,
        }

    field_desc = '、'.join(FIELD_LABELS.get(f, f) for f in fields)
    emit_log(f"{'[预演]' if options.dry_run else '开始'}批量删除部分元数据（共 {total} 首，类别：{field_desc}）...")

    if options.delete_folder_cover and 'cover' in fields:
        seen_folders = list(dict.fromkeys(
            tr.folder for tr in targets
            if tr.folder and tr.status != STATUS_ERROR and not tr.is_temp_duplicate
        ))
        for folder in seen_folders:
            if is_cancel():
                cancelled = True
                break
            cov_path = os.path.join(folder, 'cover.jpg')
            if not os.path.isfile(cov_path):
                continue
            if options.dry_run:
                folder_cover_deleted += 1
                emit_log(f'[预演][目录封面] {folder} -> 将删除 cover.jpg')
            else:
                ok_d, msg_d = delete_folder_cover_file(folder)
                if ok_d:
                    folder_cover_deleted += 1
                    emit_log(f'[目录封面] {folder} -> {msg_d}')
                else:
                    folder_cover_fail += 1
                    emit_log(f'[目录封面失败] {folder} -> {msg_d}')

    if options.delete_folder_artist and 'artist_cover' in fields:
        seen_art_folders = list(dict.fromkeys(
            tr.folder for tr in targets
            if tr.folder and tr.status != STATUS_ERROR and not tr.is_temp_duplicate
        ))
        for folder in seen_art_folders:
            if is_cancel():
                cancelled = True
                break
            art_path = os.path.join(folder, 'artist.jpg')
            if not os.path.isfile(art_path):
                continue
            if options.dry_run:
                folder_artist_deleted += 1
                emit_log(f'[预演][目录艺术家图] {folder} -> 将删除 artist.jpg')
            else:
                ok_d, msg_d = delete_folder_artist_file(folder)
                if ok_d:
                    folder_artist_deleted += 1
                    emit_log(f'[目录艺术家图] {folder} -> {msg_d}')
                else:
                    folder_artist_fail += 1
                    emit_log(f'[目录艺术家图失败] {folder} -> {msg_d}')

    def _execute_del_worker(tr: LibraryTrack):
        act_del = []
        ok, msg = apply_track_deletions(
            tr.path, fields, protect_163_key=options.protect_163_key,
            delete_lrc=options.delete_lrc_file, out_deleted=act_del,
        )
        new_info = read_audio_info(tr.path) if ok and act_del else None
        return ok, msg, act_del, new_info

    def _apply_del_result(tr: LibraryTrack, ok: bool, msg: str, act_del: list[str], new_info: dict | None):
        nonlocal deleted_ok, deleted_skipped, deleted_fail
        if ok and act_del:
            deleted_ok += 1
            modified_paths.append(tr.path)
            for f in act_del:
                field_counts[f] = field_counts.get(f, 0) + 1
            if options.enable_sync:
                synced_paths.append(tr.path)
                foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            emit_row(tr.path, {
                'status_text': '已删除', 'source_note': msg, 'color': 'ok',
                'deleted_fields': list(act_del), 'new_info': new_info or read_audio_info(tr.path), 'synced': options.enable_sync,
            })
            emit_log(f'[已删除] {tr.filename} -> {msg}')
        elif ok:
            deleted_skipped += 1
            emit_row(tr.path, {'status_text': '跳过删除', 'source_note': msg, 'color': 'muted'})
            emit_log(f'[跳过] {tr.filename} -> {msg}')
        else:
            deleted_fail += 1
            emit_row(tr.path, {'status_text': '删除失败', 'source_note': msg, 'color': 'err'})
            emit_log(f'[删除失败] {tr.filename} -> {msg}')

    if options.dry_run or total <= 1:
        for idx, tr in enumerate(targets, 1):
            if cancelled or is_cancel():
                cancelled = True
                break
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
                deleted_skipped += 1
                continue

            if options.dry_run:
                info, deletable, protected_notes = inspect_deletable_fields(
                    tr.path, fields, protect_163_key=options.protect_163_key, include_lrc_file=options.delete_lrc_file,
                )
                if info.get('error'):
                    deleted_fail += 1
                    emit_row(tr.path, {'status_text': '读取失败', 'source_note': str(info['error']), 'color': 'err'})
                    emit_log(f"[删除失败] {tr.filename} -> {info['error']}")
                    continue
                if not deletable:
                    deleted_skipped += 1
                    note = '；'.join(protected_notes) if protected_notes else '无所选元数据可删'
                    emit_row(tr.path, {'status_text': '跳过删除', 'source_note': note, 'color': 'muted'})
                    emit_log(f'[预演][跳过] {tr.filename} -> {note}')
                    continue
                deleted_ok += 1
                for f in deletable:
                    field_counts[f] = field_counts.get(f, 0) + 1
                lbls = ', '.join(FIELD_LABELS.get(f, f) for f in deletable)
                extra = f"（{'；'.join(protected_notes)}）" if protected_notes else ''
                emit_row(tr.path, {'status_text': '预演删除', 'source_note': f'将删除: {lbls}{extra}', 'color': 'info'})
                emit_log(f'[预演删除] {tr.filename} -> 将删除: {lbls}{extra}')
                continue

            ok, msg, act_del, new_info = _execute_del_worker(tr)
            _apply_del_result(tr, ok, msg, act_del, new_info)
    else:
        valid_targets = [tr for tr in targets if tr.status != STATUS_ERROR and not tr.is_temp_duplicate]
        deleted_skipped += total - len(valid_targets)
        write_workers = min(8, max(2, (os.cpu_count() or 4)))
        done_del_cnt = 0

        def _on_del_single_done(tr: LibraryTrack, fut):
            nonlocal done_del_cnt
            done_del_cnt += 1
            if progress_cb:
                progress_cb(done_del_cnt, total, tr.filename)
            try:
                ok, msg, act_del, new_info = fut.result()
            except Exception as err:
                ok, msg, act_del, new_info = False, str(err), [], None
            _apply_del_result(tr, ok, msg, act_del, new_info)

        pool_cancelled = _run_pool_cancellable(valid_targets, _execute_del_worker, _on_del_single_done, max_workers=write_workers, is_cancel=is_cancel)
        cancelled = cancelled or pool_cancelled

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {
        'cancelled': cancelled, 'dry_run': options.dry_run,
        'deleted_ok': deleted_ok, 'deleted_skipped': deleted_skipped, 'deleted_fail': deleted_fail,
        'folder_cover_deleted': folder_cover_deleted, 'folder_cover_fail': folder_cover_fail,
        'folder_artist_deleted': folder_artist_deleted, 'folder_artist_fail': folder_artist_fail,
        'field_counts': field_counts, 'modified_paths': tuple(modified_paths),
        'touched': touched, 'reloaded': reloaded,
    }
