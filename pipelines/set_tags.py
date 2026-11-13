"""批量手工设置标签流水线（支持预演、163 key 保护与并发安全写入）。"""

from __future__ import annotations

import os

from art_repair import apply_track_set_tags
from core.constants import STATUS_ERROR
from core.models import FoobarEnv, LibraryTrack
from pipelines.executor import _run_pool_cancellable, _sync_foobar_targets
from pipelines.options import SetOptions
from tag_io import read_audio_info


def execute_set_tags(
    env: FoobarEnv, targets: list[LibraryTrack], options: SetOptions,
    progress_cb=None, row_cb=None, log_cb=None, is_cancelled_cb=None,
) -> dict:
    """批量手工覆写选定曲目的标准字段或自定义标签（支持预演、163 key 保护与 foobar 刷新）。"""
    tag_values = dict(options.tag_values or {})
    total = len(targets)
    cancelled = False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    set_ok = set_skipped = set_fail = 0
    modified_paths: list[str] = []
    synced_paths: list[str] = []
    foobar_mtimes: dict[str, float] = {}

    if not tag_values:
        return {
            'cancelled': False, 'dry_run': options.dry_run,
            'set_ok': 0, 'set_skipped': total, 'set_fail': 0,
            'modified_paths': (), 'touched': 0, 'reloaded': False,
        }

    desc = ', '.join(f'{k}={v}' for k, v in tag_values.items())
    emit_log(f"{'[预演]' if options.dry_run else '开始'}手工设置元数据（共 {total} 首，标签：{desc}）...")

    def _execute_set_worker(tr: LibraryTrack):
        ok, msg = apply_track_set_tags(tr.path, tag_values, protect_163_key=options.protect_163_key)
        new_info = read_audio_info(tr.path) if ok else None
        return ok, msg, new_info

    def _apply_set_result(tr: LibraryTrack, ok: bool, msg: str, new_info: dict | None):
        nonlocal set_ok, set_fail
        if ok:
            set_ok += 1
            modified_paths.append(tr.path)
            if options.enable_sync:
                synced_paths.append(tr.path)
                foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            emit_row(tr.path, {
                'status_text': '已设置', 'source_note': msg, 'color': 'ok',
                'set_tags': dict(tag_values), 'new_info': new_info or read_audio_info(tr.path), 'synced': options.enable_sync,
            })
            emit_log(f'[已设置] {tr.filename} -> {msg}')
        else:
            set_fail += 1
            emit_row(tr.path, {'status_text': '设置失败', 'source_note': msg, 'color': 'err'})
            emit_log(f'[设置失败] {tr.filename} -> {msg}')

    if options.dry_run or total <= 1:
        for idx, tr in enumerate(targets, 1):
            if cancelled or is_cancel():
                cancelled = True
                break
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
                set_skipped += 1
                continue

            if options.dry_run:
                set_ok += 1
                emit_row(tr.path, {'status_text': '预演设置', 'source_note': f'将设置: {desc}', 'color': 'info'})
                emit_log(f'[预演设置] {tr.filename} -> {desc}')
                continue

            ok, msg, new_info = _execute_set_worker(tr)
            _apply_set_result(tr, ok, msg, new_info)
    else:
        valid_targets = [tr for tr in targets if tr.status != STATUS_ERROR and not tr.is_temp_duplicate]
        set_skipped += total - len(valid_targets)
        write_workers = min(8, max(2, (os.cpu_count() or 4)))
        done_set_cnt = 0

        def _on_set_single_done(tr: LibraryTrack, fut):
            nonlocal done_set_cnt
            done_set_cnt += 1
            if progress_cb:
                progress_cb(done_set_cnt, total, tr.filename)
            try:
                ok, msg, new_info = fut.result()
            except Exception as err:
                ok, msg, new_info = False, str(err), None
            _apply_set_result(tr, ok, msg, new_info)

        pool_cancelled = _run_pool_cancellable(
            valid_targets, _execute_set_worker, _on_set_single_done,
            max_workers=write_workers, is_cancel=is_cancel,
        )
        cancelled = cancelled or pool_cancelled

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {
        'cancelled': cancelled, 'dry_run': options.dry_run,
        'set_ok': set_ok, 'set_skipped': set_skipped, 'set_fail': set_fail,
        'modified_paths': tuple(modified_paths), 'touched': touched, 'reloaded': reloaded,
    }
