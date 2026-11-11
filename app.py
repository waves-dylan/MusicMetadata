"""SongMetadata — foobar2000 媒体库曲目元数据修复工具（元数据检查刷新 + 封面修复 + 音轨号补全，tkinter GUI + CLI）。"""

from __future__ import annotations

import argparse
import ctypes
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk

from art_repair import MAX_SIDE, FolderArtPlan, apply_folder_cover_file, apply_track_repairs, normalize_jpeg, pick_canonical_album_date
from foobar_library import (
    FIELD_LABELS,
    STATUS_ERROR,
    STATUS_OK,
    FoobarEnv,
    LibraryTrack,
    detect_foobar_env,
    get_running_foobar_exes,
    scan_library,
    sync_files_to_foobar,
    track_has_pending_work,
)
from track_lookup import TrackMatch, clear_caches, fetch_cover_bytes, resolve_track_metadata

FOLDER_COVER_LABELS = {
    'create': '目录缺cover.jpg',
    'rewrite': '目录cover.jpg需重写',
    'remove': '多专辑需撤回cover.jpg',
}


@dataclass
class FixOptions:
    enable_track: bool = True
    enable_art: bool = True
    enable_tags: bool = True
    enable_extra_tags: bool = True
    enable_date: bool = True
    enable_albumartist: bool = True
    enable_artist: bool = True
    enable_composer: bool = True
    enable_performer: bool = True
    enable_genre: bool = True
    enable_disc: bool = True
    enable_sync: bool = True
    sync_only: bool = False
    dry_run: bool = False

    def active_tag_fields(self) -> frozenset[str]:
        if not self.enable_tags:
            return frozenset()
        fields: set[str] = set()
        if self.enable_date and self.enable_albumartist and self.enable_artist:
            fields.update(('title', 'album'))
        if self.enable_date:
            fields.add('date')
        if self.enable_albumartist:
            fields.add('albumartist')
        if self.enable_artist:
            fields.add('artist')
        if self.enable_extra_tags:
            if self.enable_composer:
                fields.add('composer')
            if self.enable_performer:
                fields.add('performer')
            if self.enable_genre:
                fields.add('genre')
            if self.enable_disc:
                fields.add('discnumber')
        return frozenset(fields)


def default_checked(track: LibraryTrack, skip_full_album: bool = True, options: FixOptions | None = None) -> bool:
    opts = options or FixOptions()
    return track_has_pending_work(
        track, enable_track=opts.enable_track, enable_art=opts.enable_art,
        enable_tags=opts.enable_tags, enable_sync=opts.enable_sync, skip_full_album=skip_full_album,
        enable_extra_tags=(opts.enable_tags and opts.enable_extra_tags),
        enabled_tag_fields=opts.active_tag_fields(),
    )


def _format_status_badge(tr: LibraryTrack) -> tuple[str, str]:
    if tr.status == STATUS_ERROR:
        return '读取异常', 'err'
    if tr.is_temp_duplicate:
        return '临时副本', 'warn'
    has_online_tags = bool(tr.needs_tracknumber or tr.needs_date or tr.missing_core_fields or tr.needs_extra_tags)
    if tr.is_full_album and has_online_tags and not (tr.needs_cover_fix or tr.needs_cover_online or tr.needs_tag_fix or tr.needs_foobar_sync):
        return '全长跳过', 'muted'
    tags = []
    if tr.needs_tracknumber:
        tags.append('缺音轨')
    if tr.needs_cover_fix or tr.needs_cover_online or tr.folder_cover_action in ('create', 'rewrite', 'remove'):
        tags.append('封面')
    if tr.needs_tag_fix or any(f != 'date' for f in tr.missing_core_fields):
        tags.append('缺标签')
    elif tr.needs_date and not tr.is_full_album:
        tags.append('缺日期')
    if tr.needs_extra_tags and not tr.is_full_album:
        tags.append('缺扩展')
    if tr.needs_foobar_sync:
        tags.append('缓存过期')
    if not tags:
        return ('缺封面无源', 'warn') if tr.cover_status.startswith('缺封面') else ('正常', 'ok')
    return ' / '.join(tags), ('err' if '缺音轨' in tags else ('warn' if any(t in tags for t in ('封面', '缺标签', '缺日期', '缺扩展')) else 'info'))


def _format_cover_cell(tr: LibraryTrack) -> str:
    parts = [f'待修:{",".join(tr.cover_reasons)}' if tr.needs_cover_fix else (tr.cover_status if tr.cover_status != '正常' else '')]
    if tr.folder_cover_action in FOLDER_COVER_LABELS:
        parts.append(FOLDER_COVER_LABELS[tr.folder_cover_action])
    parts = [p for p in parts if p]
    return '；'.join(parts) if parts else '正常'


def _format_tag_sync_cell(tr: LibraryTrack) -> str:
    parts = []
    if tr.needs_foobar_sync:
        parts.append('fb:' + ','.join(tr.foobar_stale_reasons) if tr.foobar_stale_reasons else 'fb缓存过期')
    fix_items = [f'{k}={v}' for k, v in tr.tag_updates.items()] if tr.tag_updates else []
    if tr.target_disc:
        fix_items.append(f'discnumber={tr.target_disc}')
    if tr.needs_tag_fix and fix_items:
        parts.append('补:' + ','.join(fix_items))
    if not tr.is_full_album:
        pending = [f for f in (tr.missing_core_fields + tr.missing_extra_fields) if f not in tr.tag_updates]
        if pending:
            labels = [FIELD_LABELS.get(f, f) for f in pending[:4]]
            more = f'等{len(pending)}项' if len(pending) > 4 else ''
            parts.append('待联网:' + ','.join(labels) + more)
    return '；'.join(parts) if parts else '正常'


def _album_date_group_key(tr: LibraryTrack, match: TrackMatch | None) -> tuple[str, str] | None:
    if tr.folder_plan and tr.folder_plan.album_count == 1:
        return (tr.folder, '__single__')
    alb_key = (tr.album or (match.album_name if match else '') or '').strip()
    return (tr.folder, alb_key.lower()) if alb_key else None


def _run_pool_cancellable(items, submit_fn, on_result_fn, *, max_workers: int, is_cancel) -> bool:
    """并发执行任务，支持快速取消与即时逐项进度回调；返回是否被取消。"""
    if not items:
        return False
    workers = max(1, min(max_workers, len(items)))
    pool = ThreadPoolExecutor(max_workers=workers)
    cancelled = False
    it = iter(items)
    pending: dict = {}
    max_in_flight = workers * 3

    def _fill():
        while len(pending) < max_in_flight and not is_cancel():
            try:
                item = next(it)
            except StopIteration:
                break
            pending[pool.submit(submit_fn, item)] = item

    try:
        _fill()
        while pending:
            if is_cancel():
                cancelled = True
                break
            done, _ = wait(pending, timeout=0.25, return_when=FIRST_COMPLETED)
            for fut in done:
                on_result_fn(pending.pop(fut), fut)
            if is_cancel():
                cancelled = True
                break
            _fill()
        if not cancelled and is_cancel():
            cancelled = True
    finally:
        for fut in pending:
            fut.cancel()
        pool.shutdown(wait=not cancelled, cancel_futures=cancelled)
    return cancelled


def execute_repairs(
    env: FoobarEnv, targets: list[LibraryTrack], options: FixOptions,
    progress_cb=None, row_cb=None, log_cb=None, is_cancelled_cb=None,
) -> dict:
    """统一执行元数据检查刷新、封面修复、标签补全与音轨号补全（GUI 与 CLI 共用）。"""
    clear_caches()
    total, cancelled = len(targets), False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    written_ok = written_fail = unmatched = date_unmatched_cnt = cover_fixed = tags_fixed = date_fixed = folder_cover_ok = folder_cover_fail = synced_stale = 0
    synced_paths: list[str] = []
    foobar_mtimes: dict[str, float] = {}

    if options.sync_only:
        for idx, tr in enumerate(targets, 1):
            if is_cancel():
                cancelled = True
                break
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
                continue
            synced_stale += 1
            synced_paths.append(tr.path)
            foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            emit_row(tr.path, {'status_text': '待刷新' if options.dry_run else '已刷新',
                               'source_note': '推进时间戳并通知 foobar 重新载入元数据',
                               'color': 'info' if options.dry_run else 'ok', 'fixed_sync': not options.dry_run})
            emit_log(f"[{'预演' if options.dry_run else '刷新'}] {tr.filename} -> 刷新 foobar 元数据缓存")
        touched, reloaded = (0, False)
        if not options.dry_run and synced_paths and not cancelled:
            emit_log(f'正在同步 {len(synced_paths)} 个文件到 foobar2000...')
            touched, reloaded = sync_files_to_foobar(env, synced_paths, foobar_mtimes)
            emit_log(f'已推进 {touched} 个文件时间戳' + ('并触发运行中的 foobar2000 载入。' if reloaded else '；foobar 下次启动会自动载入。'))
        return {'cancelled': cancelled, 'dry_run': options.dry_run, 'sync_only': True,
                'written_ok': 0, 'written_fail': 0, 'unmatched': 0, 'date_unmatched': 0,
                'cover_fixed': 0, 'tags_fixed': 0, 'date_fixed': 0,
                'folder_cover_ok': 0, 'folder_cover_fail': 0, 'synced_stale': synced_stale, 'touched': touched, 'reloaded': reloaded}

    handled_folders: set[str] = set()
    if options.enable_art:
        seen_folders: dict[str, FolderArtPlan] = {}
        for tr in targets:
            if tr.folder_plan and tr.folder_plan.has_folder_cover_work:
                seen_folders.setdefault(tr.folder, tr.folder_plan)
        for folder, f_plan in seen_folders.items():
            if is_cancel():
                cancelled = True
                break
            if options.dry_run:
                folder_cover_ok += 1
                handled_folders.add(folder)
                emit_log(f'[预演][目录封面] {folder} -> {f_plan.cover_action} ({f_plan.detail})')
            else:
                ok_d, msg_d = apply_folder_cover_file(f_plan)
                if ok_d:
                    folder_cover_ok += 1
                    handled_folders.add(folder)
                    f_plan.cover_action = 'mixed-skip' if f_plan.cover_action == 'remove' else 'ok'
                    emit_log(f'[目录封面] {folder} -> {msg_d}')
                else:
                    folder_cover_fail += 1
                    emit_log(f'[目录封面失败] {folder} -> {msg_d}')

    matches: dict[str, TrackMatch] = {}
    allowed_tags = options.active_tag_fields()

    def _lookup_flags(tr: LibraryTrack) -> tuple[bool, bool, bool, bool, bool, frozenset[str]]:
        need_tn = bool(options.enable_track and tr.needs_tracknumber)
        need_cov = bool(options.enable_art and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata')))
        need_dt = bool('date' in allowed_tags and tr.needs_date and 'date' not in tr.tag_updates)
        need_core = bool(allowed_tags and any(f != 'date' and f in allowed_tags and f not in tr.tag_updates for f in tr.missing_core_fields))
        need_ext = bool(allowed_tags and any(f in allowed_tags for f in tr.missing_extra_fields))
        need_flds: set[str] = set()
        if need_dt:
            need_flds.add('date')
        if need_cov:
            need_flds.add('cover')
        for f in tr.missing_core_fields:
            if f in allowed_tags and f not in tr.tag_updates:
                need_flds.add(f)
        for f in tr.missing_extra_fields:
            if f in allowed_tags and f not in tr.tag_updates:
                need_flds.add(f)
        return need_tn, need_dt, need_cov, need_core, need_ext, frozenset(need_flds)

    lookup_targets = [
        t for t in targets
        if not t.is_temp_duplicate and t.status != STATUS_ERROR and any(_lookup_flags(t)[:5])
    ]
    if lookup_targets and not cancelled:
        lookup_total = len(lookup_targets)
        emit_log(f'开始联网查询并合并 {lookup_total} 首曲目元数据...')
        if progress_cb:
            progress_cb(0, lookup_total, '正在联网查询元数据...')
        done_cnt = hit_cnt = 0
        lookup_t0 = last_log_t = time.monotonic()

        def _submit_lookup(tr: LibraryTrack):
            need_tn, need_dt, need_cov, need_core, need_ext, need_flds = _lookup_flags(tr)
            return resolve_track_metadata(
                tr,
                need_track=need_tn,
                need_date=need_dt,
                need_cover=need_cov,
                need_core=need_core,
                need_extra=need_ext,
                need_fields=need_flds,
            )

        def _on_lookup_done(tr: LibraryTrack, fut):
            nonlocal done_cnt, hit_cnt, last_log_t
            done_cnt += 1
            try:
                m = fut.result()
            except Exception as exc:
                m = TrackMatch(None, reason=f'查询异常: {exc}')
            matches[tr.path] = m
            if m.source or m.tracknumber or m.release_date or m.cover_url or m.genre or m.composer or m.performer:
                hit_cnt += 1
            if progress_cb:
                progress_cb(done_cnt, lookup_total, f'联网查询: {tr.filename}')
            now = time.monotonic()
            if done_cnt == 1 or done_cnt % 25 == 0 or done_cnt == lookup_total or (now - last_log_t) >= 3.0:
                last_log_t = now
                emit_log(f'联网查询进度：{done_cnt}/{lookup_total}（已命中 {hit_cnt} 首）...')

        cancelled = _run_pool_cancellable(lookup_targets, _submit_lookup, _on_lookup_done, max_workers=8, is_cancel=is_cancel)
        if not cancelled:
            emit_log(f'联网查询完成：共 {lookup_total} 首，命中 {hit_cnt} 首，耗时 {time.monotonic() - lookup_t0:.1f}s。')

    if matches and not cancelled:
        # 同一专辑/单专辑目录内传播专辑级字段（日期、专辑艺术家、流派、封面 URL、单碟碟号）
        grp_buckets: dict[tuple[str, str], dict[str, list]] = {}
        for tr in targets:
            m = matches.get(tr.path)
            grp_key = _album_date_group_key(tr, m)
            if grp_key is None:
                continue
            bucket = grp_buckets.setdefault(grp_key, {'release_date': [], 'albumartist': [], 'genre': [], 'cover_url': [], 'discnumber': []})
            for mk, tk in (('release_date', 'date'), ('albumartist', 'albumartist'), ('genre', 'genre')):
                val = (getattr(m, mk, '') if m else '') or (getattr(tr, tk, '') or '').strip() or (tr.tag_updates.get(tk) or '').strip()
                if val:
                    bucket[mk].append(val)
            if m and m.cover_url:
                bucket['cover_url'].append(m.cover_url)
            dn = (m.discnumber if (m and m.discnumber and m.discnumber > 0) else None) or tr.disk_disc_int or tr.target_disc
            if dn:
                bucket['discnumber'].append(int(dn))
        grp_maj: dict[tuple[str, str], dict] = {}
        for gk, b in grp_buckets.items():
            maj_entry: dict = {}
            for fk in ('release_date', 'albumartist', 'genre', 'cover_url'):
                vals = b[fk]
                if vals:
                    maj_entry[fk] = pick_canonical_album_date(vals) if fk == 'release_date' else Counter(vals).most_common(1)[0][0]
            if b['discnumber'] and set(b['discnumber']) == {1}:
                maj_entry['discnumber'] = 1
            grp_maj[gk] = maj_entry
        for tr in lookup_targets:
            m = matches.get(tr.path)
            grp_key = _album_date_group_key(tr, m)
            maj = grp_maj.get(grp_key) if grp_key else None
            if not maj:
                continue
            if m is None:
                m = matches[tr.path] = TrackMatch(None, source='同专辑元数据补全', album_name=(tr.album or '').strip())
            if 'date' in allowed_tags and maj.get('release_date') and not (tr.date or '').strip() and 'date' not in tr.tag_updates:
                if m.release_date != maj['release_date']:
                    m.release_date = maj['release_date']
                    if not m.source:
                        m.source = '同专辑元数据补全'
            if 'discnumber' in allowed_tags and maj.get('discnumber') == 1 and not tr.disk_disc_int and not m.discnumber and 'discnumber' in tr.missing_extra_fields:
                m.discnumber = 1
                if not m.source:
                    m.source = '同专辑元数据补全'
            for fk, enabled in (
                ('albumartist', 'albumartist' in allowed_tags),
                ('genre', 'genre' in allowed_tags),
                ('cover_url', options.enable_art),
            ):
                if enabled and not getattr(m, fk) and maj.get(fk):
                    setattr(m, fk, maj[fk])
                    if not m.source:
                        m.source = '同专辑元数据补全'

    online_covers: dict[str, bytes] = {}
    if options.enable_art and not options.dry_run and not cancelled:
        need_urls = {
            matches[tr.path].cover_url
            for tr in lookup_targets
            if tr.path in matches and matches[tr.path].cover_url
            and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata'))
        }
        if need_urls:
            cov_total, cov_done = len(need_urls), 0
            emit_log(f'正在下载 {cov_total} 张在线专辑封面...')
            if progress_cb:
                progress_cb(0, cov_total, '正在下载在线专辑封面...')

            def _on_cov_done(u: str, fut):
                nonlocal cov_done
                cov_done += 1
                try:
                    data = fut.result()
                    if data:
                        online_covers[u] = data
                except Exception:
                    pass
                if progress_cb:
                    progress_cb(cov_done, cov_total, f'下载封面 ({cov_done}/{cov_total})')

            cancelled = _run_pool_cancellable(list(need_urls), fetch_cover_bytes, _on_cov_done, max_workers=6, is_cancel=is_cancel)
            if not cancelled:
                # 单专辑目录若原本无外置 cover.jpg，顺带生成一份
                seen_nodata: dict[str, tuple[FolderArtPlan, bytes]] = {}
                for tr in targets:
                    fp = tr.folder_plan
                    if fp and fp.cover_action == 'nodata' and tr.folder not in seen_nodata:
                        m = matches.get(tr.path)
                        cov = online_covers.get(m.cover_url) if (m and m.cover_url) else None
                        if cov:
                            seen_nodata[tr.folder] = (fp, cov)
                for folder, (fp, cov) in seen_nodata.items():
                    dir_jpg = normalize_jpeg(cov, MAX_SIDE)
                    if dir_jpg:
                        try:
                            with open(fp.cover_path, 'wb') as out_fp:
                                out_fp.write(dir_jpg)
                            fp.cover_action = 'ok'
                            folder_cover_ok += 1
                            handled_folders.add(folder)
                            emit_log(f'[目录封面] {folder} -> 已联网生成 cover.jpg')
                        except OSError as err:
                            folder_cover_fail += 1
                            emit_log(f'[目录封面失败] {folder} -> {err}')

    for idx, tr in enumerate(targets, 1):
        if cancelled or is_cancel():
            cancelled = True
            break
        if progress_cb:
            progress_cb(idx, total, tr.filename)
        if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
            continue

        want_track = options.enable_track and tr.needs_tracknumber
        want_date_online = bool('date' in allowed_tags and tr.needs_date and 'date' not in tr.tag_updates)
        want_cover_online = options.enable_art and tr.needs_cover_online
        match = matches.get(tr.path)
        matched_tn = match.tracknumber if (want_track and match and match.tracknumber) else None
        matched_date = match.release_date if (want_date_online and match and match.release_date) else ''
        matched_cov_url = match.cover_url if (want_cover_online and match and match.cover_url) else ''
        online_cov_bytes = online_covers.get(matched_cov_url) if matched_cov_url else None
        track_unmatched = bool(want_track and not matched_tn)
        date_unmatched = bool(want_date_online and not matched_date)
        if track_unmatched:
            unmatched += 1
        if date_unmatched:
            date_unmatched_cnt += 1

        want_cover = options.enable_art and (
            (tr.needs_cover_fix and tr.target_pic_bytes is not None)
            or (want_cover_online and (online_cov_bytes is not None or (options.dry_run and bool(matched_cov_url))))
        )
        cov_bytes_to_write = tr.target_pic_bytes if (options.enable_art and tr.needs_cover_fix) else online_cov_bytes
        cov_mime_to_write = tr.target_pic_mime if (options.enable_art and tr.needs_cover_fix) else 'image/jpeg'

        effective_tags = {k: v for k, v in tr.tag_updates.items() if k in allowed_tags} if allowed_tags else {}
        if allowed_tags and match:
            core_map = {
                'title': match.title,
                'artist': match.artist,
                'albumartist': match.albumartist,
                'album': match.album_name,
                'date': match.release_date,
            }
            for fk, fv in core_map.items():
                if fv and fk in allowed_tags and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv
            extra_map = {
                'composer': match.composer,
                'performer': match.performer,
                'genre': match.genre,
            }
            for fk, fv in extra_map.items():
                if fv and fk in allowed_tags and fk in tr.missing_extra_fields and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv

        allow_disc = 'discnumber' in allowed_tags
        planned_dn = tr.target_disc if (allow_disc and tr.target_disc and not tr.disk_disc_int) else None
        matched_dn = planned_dn
        if matched_dn is None and allow_disc and match and match.discnumber and match.discnumber > 0 and not tr.disk_disc_int:
            if 'discnumber' in tr.missing_extra_fields or (options.enable_track and matched_tn and match.discnumber > 1):
                matched_dn = match.discnumber

        want_tags = bool(allowed_tags and effective_tags)
        want_sync = options.enable_sync and tr.needs_foobar_sync
        folder_done = tr.folder in handled_folders

        if options.dry_run:
            pre = []
            if matched_tn:
                pre.append(f'音轨号->{matched_tn}({match.source})')
            elif track_unmatched:
                pre.append(f'未匹配音轨({match.reason if match else "未知"})')
            if matched_dn:
                pre.append(f'碟号->{matched_dn}')
            if want_cover:
                pre.append('联网补封面' if want_cover_online else '修复封面')
            if want_tags:
                pre.append('补标签:' + ','.join(f'{k}={v}' for k, v in effective_tags.items()))
            if date_unmatched:
                pre.append(f'未匹配日期({match.reason if match else "未知"})')
            if want_sync:
                pre.append('刷新foobar缓存')
            if folder_done:
                pre.append('已含目录cover.jpg')
            if matched_tn or matched_dn or want_cover or want_tags:
                written_ok += 1
                cover_fixed += int(want_cover)
                tags_fixed += int(want_tags or bool(planned_dn))
                date_fixed += int(bool(want_tags and 'date' in effective_tags))
            if want_sync and not (matched_tn or matched_dn or want_cover or want_tags):
                synced_stale += 1
            emit_row(tr.path, {'status_text': '预演' if pre else '无需改动', 'matched_track': matched_tn,
                               'source_note': '；'.join(pre) or '无待修项目',
                               'color': 'warn' if ((track_unmatched or date_unmatched) and not (matched_dn or want_cover or want_tags or want_sync)) else 'info'})
            emit_log(f"[预演] {tr.filename} -> {'；'.join(pre) or '无需改动'}")
            continue

        if matched_tn or matched_dn or want_cover or want_tags:
            ok, msg = apply_track_repairs(
                tr.path, tag_updates=effective_tags if want_tags else None,
                cover_bytes=cov_bytes_to_write if want_cover else None,
                cover_mime=cov_mime_to_write,
                tracknumber=matched_tn, discnumber=matched_dn,
                overwrite_track=(tr.disk_track_int == 0),
            )
            if ok:
                written_ok += 1
                cover_fixed += int(want_cover)
                tags_fixed += int(want_tags or bool(planned_dn))
                date_fixed += int(bool(want_tags and 'date' in effective_tags))
                if options.enable_sync:
                    synced_paths.append(tr.path)
                    foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
                extra = []
                if match and match.source and (matched_tn or matched_dn or matched_date or want_cover_online or want_tags):
                    extra.append(f'{match.source}' + (f'·《{match.album_name}》' if match.album_name else ''))
                if track_unmatched and match:
                    extra.append(f'音轨未匹配:{match.reason}')
                if date_unmatched and match and not track_unmatched:
                    extra.append(f'日期未匹配:{match.reason}')
                note_str = msg + (f"（{'；'.join(extra)}）" if extra else '')
                emit_row(tr.path, {'status_text': '已修复', 'matched_track': matched_tn,
                                   'current_track': str(matched_tn) if matched_tn else None,
                                   'current_disc': str(matched_dn) if matched_dn else None,
                                   'source_note': note_str,
                                   'color': 'ok', 'fixed_track': bool(matched_tn), 'fixed_disc': bool(matched_dn),
                                   'fixed_cover': want_cover, 'fixed_cover_online': bool(want_cover and want_cover_online),
                                   'fixed_tags': want_tags, 'written_tags': effective_tags if want_tags else {},
                                   'fixed_sync': options.enable_sync, 'fixed_folder_cover': folder_done})
                emit_log(f'[已修复] {tr.filename} -> {note_str}')
            else:
                written_fail += 1
                emit_row(tr.path, {'status_text': '写入失败', 'matched_track': matched_tn, 'source_note': msg, 'color': 'err'})
                emit_log(f'[失败] {tr.filename} -> {msg}')
        elif want_sync:
            synced_stale += 1
            synced_paths.append(tr.path)
            foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            desc = '、'.join(tr.foobar_stale_reasons) if tr.foobar_stale_reasons else '缓存过期'
            emit_row(tr.path, {'status_text': '已同步', 'source_note': f'已触发 foobar 刷新（{desc}）',
                               'color': 'ok', 'fixed_sync': True, 'fixed_folder_cover': folder_done})
            emit_log(f'[同步] {tr.filename} -> 触发 foobar 刷新（{desc}）')
        elif track_unmatched or date_unmatched:
            reason = match.reason if match else ('未找到匹配音轨号' if track_unmatched else '未找到匹配发布日期')
            emit_row(tr.path, {'status_text': '未匹配', 'source_note': reason, 'color': 'warn', 'fixed_folder_cover': folder_done})
            emit_log(f'[未匹配] {tr.filename}（{reason}）')
        elif folder_done:
            emit_row(tr.path, {'status_text': '目录已修', 'source_note': '目录外置 cover.jpg 已处理', 'color': 'ok', 'fixed_folder_cover': True})

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        unique = list(dict.fromkeys(synced_paths))
        emit_log(f'正在同步 {len(unique)} 个变更文件到 foobar2000...')
        touched, reloaded = sync_files_to_foobar(env, unique, foobar_mtimes)
        emit_log(f'已更新时间戳 ({touched} 个)' + ('并触发运行中的 foobar2000 重载。' if reloaded else '；foobar 下次启动会自动读取。'))

    return {'cancelled': cancelled, 'dry_run': options.dry_run, 'sync_only': False,
            'written_ok': written_ok, 'written_fail': written_fail, 'unmatched': unmatched,
            'date_unmatched': date_unmatched_cnt, 'cover_fixed': cover_fixed,
            'tags_fixed': tags_fixed, 'date_fixed': date_fixed, 'folder_cover_ok': folder_cover_ok,
            'folder_cover_fail': folder_cover_fail, 'synced_stale': synced_stale, 'touched': touched, 'reloaded': reloaded}


# ==================== 轻量原生界面 (tkinter) ====================
def _resource_path(rel_path: str) -> str:
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, rel_path)


def _apply_window_icon(root: tk.Tk) -> tk.PhotoImage | None:
    """加载应用图标，优先使用多尺寸 .ico，缺失时回退 .png。"""
    ico_path = _resource_path(os.path.join('assets', 'app_icon.ico'))
    if os.path.isfile(ico_path):
        try:
            root.iconbitmap(default=ico_path)
            return None
        except Exception:
            pass
    png_path = _resource_path(os.path.join('assets', 'app_icon.png'))
    if os.path.isfile(png_path):
        try:
            photo = tk.PhotoImage(file=png_path)
            root.iconphoto(True, photo)
            return photo
        except Exception:
            pass
    return None


class MainWindow:
    _COLS = ('chk', 'status', 'title', 'artist', 'album', 'track', 'cover', 'tagsync', 'note')
    _HEADS = ('选择', '综合状态', '歌曲名', '艺术家', '专辑', '音轨号(磁盘/fb)', '封面状态', '缓存/标签检查', '说明 / 结果')
    _WIDTHS = (44, 108, 175, 115, 145, 105, 145, 165, 240)

    def __init__(self, root: tk.Tk, auto_scan: bool = False):
        self.root = root
        self.root.title('SongMetadata')
        self._icon_photo = _apply_window_icon(self.root)
        self.root.geometry('1220x760')
        self.root.minsize(920, 560)

        self.env = FoobarEnv()
        self.tracks: list[LibraryTrack] = []
        self.visible_tracks: list[LibraryTrack] = []
        self.custom_folders: list[str] | None = None
        self.checked_paths: set[str] = set()
        self._busy = False
        self._has_scanned = False
        self._prog_indet = False
        self._cancel_flag = False
        self._ev_q: queue.Queue = queue.Queue()

        self.var_track = tk.BooleanVar(value=True)
        self.var_art = tk.BooleanVar(value=True)
        self.var_cover = self.var_art
        self.var_date = tk.BooleanVar(value=True)
        self.var_albumartist = tk.BooleanVar(value=True)
        self.var_artist = tk.BooleanVar(value=True)
        self.var_composer = tk.BooleanVar(value=True)
        self.var_performer = tk.BooleanVar(value=True)
        self.var_genre = tk.BooleanVar(value=True)
        self.var_disc = tk.BooleanVar(value=True)
        self.var_sync = tk.BooleanVar(value=True)
        self.var_only_pending = tk.BooleanVar(value=True)
        self.var_skip_full = tk.BooleanVar(value=True)
        self.var_env = tk.StringVar(value='未自动扫描媒体库；请点击下方「扫描媒体库」或右上角「选择目录...」开始扫描。')
        self.var_summary = tk.StringVar(value='尚未扫描，请点击「扫描媒体库」开始检查。')
        self.var_status = tk.StringVar(value='就绪：点击「扫描媒体库」或「选择目录...」开始扫描')

        self._build_ui()
        self.root.after(40, self._poll_queue)
        if auto_scan:
            self.start_scan()

    def _build_ui(self):
        style = ttk.Style()
        if 'vista' in style.theme_names():
            style.theme_use('vista')
        style.configure('.', font=('Microsoft YaHei UI', 9))
        style.configure('Treeview', rowheight=24)
        style.configure('Treeview.Heading', font=('Microsoft YaHei UI', 9, 'bold'))
        self.root.configure(bg='#f5f7fa')

        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill='both', expand=True)

        # 顶部环境与统计卡
        top = ttk.LabelFrame(outer, text='媒体库状态', padding=(10, 6))
        top.pack(fill='x', pady=(0, 8))
        r1 = ttk.Frame(top)
        r1.pack(fill='x')
        ttk.Label(r1, textvariable=self.var_env, anchor='w').pack(side='left', fill='x', expand=True)
        self.btn_reset_lib = ttk.Button(r1, text='使用媒体库', command=self._reset_to_library)
        self.btn_pick_dir = ttk.Button(r1, text='选择目录...', command=self._pick_custom_folder)
        self.btn_pick_dir.pack(side='right', padx=(6, 0))
        ttk.Label(top, textvariable=self.var_summary, foreground='#334155', font=('Microsoft YaHei UI', 9, 'bold')).pack(fill='x', pady=(4, 0))

        # 操作按钮栏
        acts = ttk.Frame(outer)
        acts.pack(fill='x', pady=(0, 6))
        self.btn_one_click = tk.Button(
            acts, text='一键修复已选项目', command=self._on_one_click,
            bg='#2563eb', fg='#ffffff', activebackground='#1d4ed8', activeforeground='#ffffff',
            relief='flat', padx=14, pady=5, font=('Microsoft YaHei UI', 10, 'bold'), cursor='hand2',
        )
        self.btn_one_click.pack(side='left', padx=(0, 8))
        self.btn_sync_only = ttk.Button(acts, text='仅刷新 foobar 缓存', command=self._on_sync_only)
        self.btn_sync_only.pack(side='left', padx=(0, 6))
        self.btn_rescan = ttk.Button(acts, text='扫描媒体库', command=self.start_scan)
        self.btn_rescan.pack(side='left', padx=(0, 6))
        self.btn_toggle_all = ttk.Button(acts, text='全选 / 取消', command=self._toggle_select_all)
        self.btn_toggle_all.pack(side='left', padx=(0, 6))
        self.btn_stop = ttk.Button(acts, text='停止', command=self._on_stop)

        # 修复项选择（9 个独立元数据修复按钮 + 快捷全选/清空 + 辅助选项）
        opts_card = ttk.LabelFrame(outer, text='修复项选择', padding=(8, 4))
        opts_card.pack(fill='x', pady=(0, 6))

        opts_fields = ttk.Frame(opts_card)
        opts_fields.pack(fill='x')
        self.field_vars: dict[str, tk.BooleanVar] = {
            'cover': self.var_art,
            'date': self.var_date,
            'albumartist': self.var_albumartist,
            'artist': self.var_artist,
            'composer': self.var_composer,
            'performer': self.var_performer,
            'genre': self.var_genre,
            'tracknumber': self.var_track,
            'discnumber': self.var_disc,
        }
        self.field_buttons: dict[str, ttk.Checkbutton] = {}
        for key, label in (
            ('cover', '封面'),
            ('date', '日期'),
            ('albumartist', '专辑艺术家'),
            ('artist', '艺术家'),
            ('composer', '作曲'),
            ('performer', '表演者'),
            ('genre', '流派'),
            ('tracknumber', '音轨号'),
            ('discnumber', '碟片编号'),
        ):
            btn = ttk.Checkbutton(opts_fields, text=label, variable=self.field_vars[key], command=self._on_option_changed)
            btn.pack(side='left', padx=(0, 10))
            self.field_buttons[key] = btn

        self.btn_fields_none = ttk.Button(opts_fields, text='全不选', width=7, command=lambda: self._set_all_fields(False))
        self.btn_fields_none.pack(side='right')
        self.btn_fields_all = ttk.Button(opts_fields, text='全选', width=6, command=lambda: self._set_all_fields(True))
        self.btn_fields_all.pack(side='right', padx=(0, 4))

        opts_misc = ttk.Frame(opts_card)
        opts_misc.pack(fill='x', pady=(4, 0))
        self.chk_sync = ttk.Checkbutton(opts_misc, text='刷新 foobar 缓存', variable=self.var_sync, command=self._on_option_changed)
        self.chk_sync.pack(side='left', padx=(0, 12))
        ttk.Checkbutton(opts_misc, text='默认跳过整轨全长', variable=self.var_skip_full, command=self._on_option_changed).pack(side='right')
        ttk.Checkbutton(opts_misc, text='只显示待处理曲目', variable=self.var_only_pending, command=self._refresh_table).pack(side='right', padx=(0, 12))

        # 底部固定进度与状态栏（先按 side='bottom' 布局，确保窗口较矮时不被分割区挤压隐藏）
        bottom_bar = ttk.Frame(outer)
        bottom_bar.pack(side='bottom', fill='x', pady=(6, 0))
        self.lbl_status = ttk.Label(bottom_bar, textvariable=self.var_status, anchor='w', foreground='#475569')
        self.lbl_status.pack(side='bottom', fill='x')
        self.progress = ttk.Progressbar(bottom_bar, mode='determinate', maximum=100)

        # 主分割区：表格 + 日志
        paned = ttk.PanedWindow(outer, orient='vertical')
        paned.pack(fill='both', expand=True)

        tbl_frame = ttk.Frame(paned)
        self.tree = ttk.Treeview(tbl_frame, columns=self._COLS, show='headings', selectmode='browse')
        for col, head, width in zip(self._COLS, self._HEADS, self._WIDTHS):
            self.tree.heading(col, text=head)
            self.tree.column(col, width=width, minwidth=36, anchor='center' if col in ('chk', 'status', 'track') else 'w', stretch=(col == 'note'))
        for tag, fg in (('err', '#dc2626'), ('warn', '#b45309'), ('info', '#2563eb'), ('ok', '#15803d'), ('muted', '#64748b')):
            self.tree.tag_configure(tag, foreground=fg)

        vsb = ttk.Scrollbar(tbl_frame, orient='vertical', command=self.tree.yview)
        hsb = ttk.Scrollbar(tbl_frame, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky='nsew')
        vsb.grid(row=0, column=1, sticky='ns')
        hsb.grid(row=1, column=0, sticky='ew')
        tbl_frame.rowconfigure(0, weight=1)
        tbl_frame.columnconfigure(0, weight=1)
        self.tree.bind('<Button-1>', self._on_tree_click)
        self.tree.bind('<Double-1>', self._on_tree_double_click)
        self.tree.bind('<space>', self._on_tree_space)
        paned.add(tbl_frame, weight=3)

        log_frame = ttk.Frame(paned)
        self.log_box = tk.Text(
            log_frame, height=8, bg='#0f172a', fg='#e2e8f0', relief='flat',
            font=('Consolas', 9), state='disabled', wrap='word',
        )
        l_sb = ttk.Scrollbar(log_frame, orient='vertical', command=self.log_box.yview)
        self.log_box.configure(yscrollcommand=l_sb.set)
        self.log_box.pack(side='left', fill='both', expand=True)
        l_sb.pack(side='right', fill='y')
        paned.add(log_frame, weight=1)

    def _set_all_fields(self, checked: bool):
        for var in self.field_vars.values():
            var.set(checked)
        self._on_option_changed()

    def _current_options(self, sync_only: bool = False) -> FixOptions:
        any_tag = any(
            v.get() for v in (
                self.var_date, self.var_albumartist, self.var_artist,
                self.var_composer, self.var_performer, self.var_genre, self.var_disc,
            )
        )
        any_extra = any(v.get() for v in (self.var_composer, self.var_performer, self.var_genre, self.var_disc))
        return FixOptions(
            enable_track=self.var_track.get(),
            enable_art=self.var_art.get(),
            enable_tags=any_tag,
            enable_extra_tags=any_extra,
            enable_date=self.var_date.get(),
            enable_albumartist=self.var_albumartist.get(),
            enable_artist=self.var_artist.get(),
            enable_composer=self.var_composer.get(),
            enable_performer=self.var_performer.get(),
            enable_genre=self.var_genre.get(),
            enable_disc=self.var_disc.get(),
            enable_sync=self.var_sync.get(),
            sync_only=sync_only,
        )

    def _log(self, text: str):
        self.log_box.configure(state='normal')
        self.log_box.insert('end', f"[{time.strftime('%H:%M:%S')}] {text}\n")
        if int(self.log_box.index('end-1c').split('.')[0]) > 1500:
            self.log_box.delete('1.0', '200.0')
        self.log_box.see('end')
        self.log_box.configure(state='disabled')

    def _set_busy(self, busy: bool, text: str = ''):
        self._busy = busy
        state = 'disabled' if busy else 'normal'
        self.btn_one_click.configure(state=state, bg='#93c5fd' if busy else '#2563eb')
        for b in (
            self.btn_sync_only, self.btn_rescan, self.btn_pick_dir, self.btn_reset_lib,
            self.btn_toggle_all, self.btn_fields_all, self.btn_fields_none, self.chk_sync,
            *self.field_buttons.values(),
        ):
            b.configure(state=state)
        if text:
            self.var_status.set(text)

    def _show_progress(self, show: bool, maximum: int = 100, value: int = 0):
        if show:
            if self.progress.winfo_manager() != 'pack':
                self.progress.pack(side='top', fill='x', pady=(0, 4), before=self.lbl_status)
            if maximum <= 0:
                if not self._prog_indet:
                    self.progress.configure(mode='indeterminate', maximum=100, value=0)
                    self.progress.start(14)
                    self._prog_indet = True
            else:
                if self._prog_indet:
                    self.progress.stop()
                    self._prog_indet = False
                self.progress.configure(mode='determinate', maximum=maximum, value=value)
        else:
            if self._prog_indet:
                self.progress.stop()
                self._prog_indet = False
            if self.progress.winfo_manager() == 'pack':
                self.progress.pack_forget()

    def _poll_queue(self):
        try:
            while True:
                kind, payload = self._ev_q.get_nowait()
                if kind == 'scan_prog':
                    idx, tot, fp = payload
                    if tot <= 0:
                        self._show_progress(True, 0, 0)
                        name = os.path.basename(fp) or fp
                        self.var_status.set(f'扫描目录 ({idx} 个音频): {name}' if idx else f'扫描目录: {name}')
                    else:
                        self._show_progress(True, tot, idx)
                        self.var_status.set(f'扫描检查 ({idx}/{tot}): {os.path.basename(fp)}')
                elif kind == 'scan_done':
                    self._on_scan_finished(*payload)
                elif kind == 'fix_prog':
                    idx, tot, name = payload
                    self._show_progress(True, tot, idx)
                    self.var_status.set(f'处理中 ({idx}/{tot}): {name}')
                elif kind == 'fix_row':
                    self._on_fix_row_updated(*payload)
                elif kind == 'log':
                    self._log(payload)
                elif kind == 'fix_done':
                    self._on_fix_finished(payload)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(40, self._poll_queue)

    def _pick_custom_folder(self):
        folder = filedialog.askdirectory(title='选择要扫描的音乐文件夹')
        if folder:
            self.custom_folders = [os.path.abspath(folder)]
            self.btn_reset_lib.pack(side='right', padx=(6, 0))
            self.start_scan()

    def _reset_to_library(self):
        self.custom_folders = None
        self.btn_reset_lib.pack_forget()
        self.start_scan()

    def start_scan(self):
        if self._busy:
            return
        self._set_busy(True, '正在扫描媒体库元数据、封面与 foobar 缓存...')
        self._show_progress(True, 0, 0)
        custom = self.custom_folders

        def _worker():
            env = detect_foobar_env()
            tracks = scan_library(env, folders=custom, progress_cb=lambda i, t, f: self._ev_q.put(('scan_prog', (i, t, f))), include_art_scan=True)
            self._ev_q.put(('scan_done', (env, tracks)))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_scan_finished(self, env: FoobarEnv, tracks: list[LibraryTrack]):
        self.env, self.tracks = env, tracks
        self._has_scanned = True
        self.btn_rescan.configure(text='重新扫描')
        self._show_progress(False)
        self._set_busy(False)
        if self.custom_folders:
            self.var_env.set('当前扫描自定义目录：' + '；'.join(self.custom_folders))
        elif env.library_folders or env.unavailable_folders:
            fb_state = '运行中' if get_running_foobar_exes() else '未运行'
            fdesc = '；'.join(env.library_folders) if env.library_folders else '无'
            wdesc = f'（不可访问：{"；".join(env.unavailable_folders)}）' if env.unavailable_folders else ''
            self.var_env.set(f'已识别 foobar2000 媒体库（{len(env.library_folders)} 个目录，foobar {fb_state}）：{fdesc}{wdesc}')
        else:
            self.var_env.set('未自动检测到 foobar2000 媒体库目录，请点击右侧“选择目录...”手动指定。')

        opts = self._current_options()
        self.checked_paths = {tr.path for tr in tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._update_summary()
        self._refresh_table()

        miss = sum(1 for t in tracks if t.needs_tracknumber and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        cov = sum(1 for t in tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in tracks if t.needs_date and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate)
        tagc = sum(1 for t in tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not (self.var_skip_full.get() and t.is_full_album) and not t.is_temp_duplicate))
        stale = sum(1 for t in tracks if t.needs_foobar_sync)
        self._log(f'扫描完成：共 {len(tracks)} 首 | 缺音轨 {miss} 首 | 封面待修 {cov} 首 (目录 cover.jpg {dcov} 个) | 标签待补 {tagc} 首 (缺日期 {datec} 首) | foobar 缓存待刷新 {stale} 首。')
        self.var_status.set('扫描完成，可按需勾选模块后点击「一键修复已选项目」或「仅刷新 foobar 缓存」')

    def _update_summary(self):
        miss = sum(1 for t in self.tracks if t.needs_tracknumber and not t.is_full_album and not t.is_temp_duplicate)
        cov = sum(1 for t in self.tracks if t.needs_cover_fix or t.needs_cover_online)
        dcov = len({t.folder for t in self.tracks if t.folder_cover_action in ('create', 'rewrite', 'remove')})
        datec = sum(1 for t in self.tracks if t.needs_date and not t.is_full_album and not t.is_temp_duplicate)
        tagc = sum(1 for t in self.tracks if t.needs_tag_fix or ((bool(t.missing_core_fields) or t.needs_date or t.needs_extra_tags) and not t.is_full_album and not t.is_temp_duplicate))
        stale = sum(1 for t in self.tracks if t.needs_foobar_sync)
        ok_cnt = sum(1 for t in self.tracks if not track_has_pending_work(t, True, True, True, True, True, enable_extra_tags=True))
        self.var_summary.set('  |  '.join([
            f'共 {len(self.tracks)} 首', f'缺音轨号 {miss}',
            f'封面待修 {cov}' + (f' (目录cover {dcov})' if dcov else ''),
            f'标签待补 {tagc}' + (f' (缺日期 {datec})' if datec else ''),
            f'foobar待刷新 {stale}', f'正常 {ok_cnt}',
        ]))

    def _on_option_changed(self):
        opts = self._current_options()
        self.checked_paths = {tr.path for tr in self.tracks if default_checked(tr, self.var_skip_full.get(), opts)}
        self._refresh_table()

    def _row_values(self, tr: LibraryTrack) -> tuple[tuple[str, ...], str]:
        st_text, st_tag = _format_status_badge(tr)
        chk = '☑' if tr.path in self.checked_paths else '☐'
        title = tr.title or os.path.splitext(tr.filename)[0]
        if tr.needs_foobar_sync and tr.disk_tracknumber:
            tn_disp = f"{tr.disk_tracknumber} / fb:{tr.foobar_tracknumber or '空'}"
        elif tr.disk_track_int == 0:
            tn_disp = '0 (无效)'
        else:
            tn_disp = tr.disk_tracknumber or '无'
        note = tr.match_note or tr.note or tr.path
        return (chk, st_text, title, tr.artist, tr.album, tn_disp, _format_cover_cell(tr), _format_tag_sync_cell(tr), note), st_tag

    def _refresh_table(self):
        opts = self._current_options()
        skip_full = self.var_skip_full.get()
        if self.var_only_pending.get():
            allowed = opts.active_tag_fields()
            self.visible_tracks = [
                t for t in self.tracks
                if t.status == STATUS_ERROR or t.is_temp_duplicate or (opts.enable_track and t.is_full_album and t.needs_tracknumber)
                or track_has_pending_work(
                    t, opts.enable_track, opts.enable_art, opts.enable_tags, opts.enable_sync, skip_full,
                    enable_extra_tags=(opts.enable_tags and opts.enable_extra_tags),
                    enabled_tag_fields=allowed,
                )
            ]
        else:
            self.visible_tracks = list(self.tracks)

        self.tree.delete(*self.tree.get_children())
        for tr in self.visible_tracks:
            vals, tag = self._row_values(tr)
            self.tree.insert('', 'end', iid=tr.path, values=vals, tags=(tag,))

    def _toggle_row(self, iid: str):
        if not iid or not self.tree.exists(iid):
            return
        if iid in self.checked_paths:
            self.checked_paths.discard(iid)
        else:
            self.checked_paths.add(iid)
        vals = list(self.tree.item(iid, 'values'))
        vals[0] = '☑' if iid in self.checked_paths else '☐'
        self.tree.item(iid, values=vals)

    def _on_tree_click(self, event):
        if self.tree.identify_region(event.x, event.y) == 'cell' and self.tree.identify_column(event.x) == '#1':
            iid = self.tree.identify_row(event.y)
            if iid:
                self._toggle_row(iid)

    def _on_tree_space(self, _event):
        for iid in self.tree.selection():
            self._toggle_row(iid)

    def _on_tree_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and os.path.exists(iid):
            subprocess.Popen(['explorer', '/select,', os.path.normpath(iid)])

    def _toggle_select_all(self):
        if not self.visible_tracks:
            return
        all_checked = all(tr.path in self.checked_paths for tr in self.visible_tracks)
        for tr in self.visible_tracks:
            (self.checked_paths.discard if all_checked else self.checked_paths.add)(tr.path)
        self._refresh_table()

    def _start_fix(self, targets: list[LibraryTrack], opts: FixOptions, busy_label: str):
        if self._busy:
            return
        self._cancel_flag = False
        self._set_busy(True, busy_label)
        self.btn_stop.configure(state='normal')
        self.btn_stop.pack(side='left')
        self._show_progress(True, len(targets), 0)

        def _worker():
            summary = execute_repairs(
                self.env, targets, opts,
                progress_cb=lambda i, t, n: self._ev_q.put(('fix_prog', (i, t, n))),
                row_cb=lambda p, d: self._ev_q.put(('fix_row', (p, d))),
                log_cb=lambda m: self._ev_q.put(('log', m)),
                is_cancelled_cb=lambda: self._cancel_flag,
            )
            self._ev_q.put(('fix_done', summary))

        threading.Thread(target=_worker, daemon=True).start()

    def _on_one_click(self):
        if not self._has_scanned:
            messagebox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        opts = self._current_options(sync_only=False)
        if not (opts.enable_track or opts.enable_art or opts.enable_tags or opts.enable_sync):
            messagebox.showinfo('提示', '请至少勾选一项要执行的修复功能。')
            return
        targets = [t for t in self.tracks if t.path in self.checked_paths]
        if not targets:
            messagebox.showinfo('提示', '当前没有已勾选的待处理曲目。\n如果刚添加新歌，请点击“重新扫描”。')
            return
        self._start_fix(targets, opts, f'正在修复已选曲目元数据 (共 {len(targets)} 首)...')

    def _on_sync_only(self):
        if not self._has_scanned:
            messagebox.showinfo('提示', '尚未扫描媒体库，请先点击“扫描媒体库”或“选择目录...”。')
            return
        targets = [t for t in self.tracks if t.path in self.checked_paths] or [t for t in self.tracks if t.needs_foobar_sync]
        if not targets:
            messagebox.showinfo('提示', '当前没有检测到需要刷新 foobar 缓存的曲目；如需强制刷新，请取消“只显示待处理曲目”并勾选歌曲。')
            return
        self._start_fix(targets, FixOptions(enable_track=False, enable_art=False, enable_tags=False, enable_sync=True, sync_only=True), f'正在刷新 foobar 缓存 (共 {len(targets)} 首)...')

    def _on_stop(self):
        self._cancel_flag = True
        self.btn_stop.configure(state='disabled')
        self.var_status.set('正在停止...')

    def _on_fix_row_updated(self, path: str, payload: dict):
        tr = next((t for t in self.tracks if t.path == path), None)
        is_ok = payload.get('color') == 'ok'
        if tr is not None:
            if payload.get('matched_track'):
                tr.matched_track = payload['matched_track']
            if payload.get('source_note'):
                tr.match_note = payload['source_note']
            if is_ok:
                if payload.get('fixed_track'):
                    tr.needs_tracknumber = False
                if payload.get('fixed_cover'):
                    tr.needs_cover_fix, tr.needs_cover_online, tr.cover_status, tr.cover_reasons = False, False, '正常', []
                if payload.get('fixed_tags'):
                    applied_tags = payload.get('written_tags') or tr.tag_updates
                    for k, v in applied_tags.items():
                        attr = 'album' if k == 'album' else k
                        if hasattr(tr, attr):
                            setattr(tr, attr, v)
                        tr.tag_updates.pop(k, None)
                    tr.missing_core_fields = [f for f in tr.missing_core_fields if f not in applied_tags]
                    tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f not in applied_tags]
                    if tr.date:
                        tr.needs_date = False
                if payload.get('current_disc'):
                    tr.disk_discnumber = str(payload['current_disc'])
                    tr.disk_disc_int = int(payload['current_disc'])
                    tr.target_disc = None
                    tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f != 'discnumber']
                tr.needs_tag_fix = bool(tr.tag_updates or tr.target_disc)
                tr.needs_extra_tags = bool(tr.missing_extra_fields)
                if payload.get('fixed_sync') or not tr.needs_foobar_sync:
                    tr.needs_foobar_sync, tr.foobar_stale_reasons = False, []
                if payload.get('fixed_folder_cover'):
                    tr.folder_cover_action = 'ok'
                if payload.get('current_track'):
                    tr.disk_tracknumber = str(payload['current_track'])
                    tr.disk_track_int = int(payload['current_track'])
                if not any(k in payload for k in ('fixed_track', 'fixed_disc', 'fixed_cover', 'fixed_tags', 'fixed_sync', 'fixed_folder_cover')):
                    tr.needs_tracknumber = tr.needs_foobar_sync = False
                if not tr.needs_tracknumber and not tr.needs_foobar_sync:
                    tr.status = STATUS_OK
                self.checked_paths.discard(path)

        if self.tree.exists(path):
            vals = list(self.tree.item(path, 'values'))
            vals[0] = '☑' if path in self.checked_paths else '☐'
            if payload.get('status_text'):
                vals[1] = payload['status_text']
            if tr is not None:
                vals[2] = tr.title or os.path.splitext(tr.filename)[0]
                vals[3] = tr.artist
                vals[4] = tr.album
                vals[5] = tr.disk_tracknumber or '-'
                vals[6] = _format_cover_cell(tr)
                vals[7] = _format_tag_sync_cell(tr)
            if payload.get('source_note'):
                vals[8] = payload['source_note']
            self.tree.item(path, values=vals, tags=(payload.get('color', 'info'),))

    def _on_fix_finished(self, summary: dict):
        opts = self._current_options()
        for tr in self.tracks:
            if tr.folder_plan is not None:
                tr.folder_cover_action = tr.folder_plan.cover_action
                if not track_has_pending_work(
                    tr, opts.enable_track, opts.enable_art, opts.enable_tags, opts.enable_sync,
                    self.var_skip_full.get(), enable_extra_tags=opts.enable_extra_tags,
                    enabled_tag_fields=opts.active_tag_fields(),
                ):
                    self.checked_paths.discard(tr.path)
        self.btn_stop.pack_forget()
        self._show_progress(False)
        self._set_busy(False)
        self._update_summary()

        if summary.get('sync_only'):
            msg = f"缓存刷新完成：共处理 {summary['synced_stale']} 首，推进时间戳 {summary['touched']} 个。"
        else:
            date_part = f"（含日期 {summary['date_fixed']}）" if summary.get('date_fixed') else ''
            date_miss = f"、未匹配日期 {summary['date_unmatched']} 首" if summary.get('date_unmatched') else ''
            msg = (
                f"修复完成：文件写入 {summary['written_ok']} 首（封面 {summary['cover_fixed']} / 标签 {summary['tags_fixed']}{date_part}），"
                f"目录 cover.jpg {summary['folder_cover_ok']} 个，纯缓存同步 {summary['synced_stale']} 首，"
                f"未匹配音轨 {summary['unmatched']} 首{date_miss}，失败 {summary['written_fail'] + summary['folder_cover_fail']} 项。"
            )
        if summary.get('cancelled'):
            msg = '已手动停止。' + msg
        self._log(msg)
        self.var_status.set(msg)
        if summary['written_ok'] > 0 or summary['synced_stale'] > 0 or summary['folder_cover_ok'] > 0:
            messagebox.showinfo('完成', msg)


# ==================== 命令行模式 ====================
def run_cli(args) -> int:
    env = detect_foobar_env()
    custom = [os.path.abspath(args.folder)] if args.folder else None
    print('foobar 目录:', env.fb_dir or '未检测到')
    print('媒体库目录:', custom or env.library_folders)
    if not custom and env.unavailable_folders:
        print('不可访问的媒体库目录:', env.unavailable_folders)

    tracks = scan_library(env, folders=custom, include_art_scan=(not args.no_art or not args.no_tags))
    opts = FixOptions(
        enable_track=not args.no_track, enable_art=not args.no_art,
        enable_tags=not args.no_tags, enable_extra_tags=not args.no_extra_tags,
        enable_date=not args.no_date, enable_albumartist=not args.no_albumartist,
        enable_artist=not args.no_artist, enable_composer=not args.no_composer,
        enable_performer=not args.no_performer, enable_genre=not args.no_genre,
        enable_disc=not args.no_disc,
        enable_sync=not args.no_sync, sync_only=args.sync_only, dry_run=args.dry_run,
    )
    if not opts.active_tag_fields() and not opts.enable_tags:
        pass
    elif not any((
        opts.enable_date, opts.enable_albumartist, opts.enable_artist,
        opts.enable_composer, opts.enable_performer, opts.enable_genre, opts.enable_disc,
    )):
        opts.enable_tags = False
    targets = [t for t in tracks if default_checked(t, skip_full_album=not args.include_full_album, options=opts)]
    skip_full = not args.include_full_album
    allowed = opts.active_tag_fields()
    datec = sum(1 for t in targets if 'date' in allowed and t.needs_date and not (skip_full and t.is_full_album) and not t.is_temp_duplicate)
    tagc = sum(
        1 for t in targets
        if any(k in allowed for k in t.tag_updates) or (t.target_disc and 'discnumber' in allowed)
        or ((any(f in allowed for f in t.missing_core_fields) or ('date' in allowed and t.needs_date) or any(f in allowed for f in t.missing_extra_fields))
            and not (skip_full and t.is_full_album) and not t.is_temp_duplicate)
    )
    print(
        f'共扫描 {len(tracks)} 首，待处理 {len(targets)} 首（缺音轨 {sum(1 for t in targets if t.needs_tracknumber)}，'
        f'封面待修 {sum(1 for t in targets if t.needs_cover_fix)} / 目录cover {len({t.folder for t in targets if t.folder_cover_action in ("create", "rewrite", "remove")})}，'
        f'标签待补 {tagc} [缺日期 {datec}]，foobar缓存待刷新 {sum(1 for t in targets if t.needs_foobar_sync)}）。'
    )
    if not targets:
        print('媒体库曲目元数据、封面与 foobar 缓存均正常。')
        return 0
    s = execute_repairs(env, targets, opts, log_cb=print)
    prefix = '[预演] 完成：预计写入' if args.dry_run else '完成：文件写入'
    print(f"{prefix} {s['written_ok']}（标签 {s['tags_fixed']} / 日期 {s.get('date_fixed', 0)}），目录cover {s['folder_cover_ok']}，纯缓存同步 {s['synced_stale']}，未匹配音轨 {s['unmatched']}，未匹配日期 {s.get('date_unmatched', 0)}，失败 {s['written_fail'] + s['folder_cover_fail']}。")
    return 0 if (s['written_fail'] + s['folder_cover_fail']) == 0 else 1


def main():
    parser = argparse.ArgumentParser(prog='SongMetadata', description='SongMetadata — foobar2000 媒体库曲目元数据修复工具')
    parser.add_argument('--auto', action='store_true', help='无界面自动扫描并执行修复')
    parser.add_argument('--dry-run', action='store_true', help='只扫描和预览待修复项目，不修改文件')
    parser.add_argument('--sync-only', action='store_true', help='仅检查并刷新 foobar 元数据缓存')
    parser.add_argument('--no-track', action='store_true', help='跳过音轨号补全')
    parser.add_argument('--no-art', action='store_true', help='跳过封面与目录 cover.jpg 修复')
    parser.add_argument('--no-tags', action='store_true', help='跳过全部文本标签与碟片编号补全')
    parser.add_argument('--no-extra-tags', action='store_true', help='跳过扩展标签（作曲/表演者/流派/碟片编号）补全')
    parser.add_argument('--no-date', action='store_true', help='跳过日期(date)补全')
    parser.add_argument('--no-albumartist', action='store_true', help='跳过专辑艺术家(albumartist)补全')
    parser.add_argument('--no-artist', action='store_true', help='跳过艺术家(artist)补全')
    parser.add_argument('--no-composer', action='store_true', help='跳过作曲(composer)补全')
    parser.add_argument('--no-performer', action='store_true', help='跳过表演者(performer)补全')
    parser.add_argument('--no-genre', action='store_true', help='跳过流派(genre)补全')
    parser.add_argument('--no-disc', action='store_true', help='跳过碟片编号(discnumber)补全')
    parser.add_argument('--no-sync', action='store_true', help='跳过 foobar 缓存刷新')
    parser.add_argument('--folder', default='', help='指定扫描目录')
    parser.add_argument('--include-full-album', action='store_true', help='也尝试处理整轨全长文件')
    args = parser.parse_args()

    if args.auto or args.dry_run or args.sync_only:
        sys.exit(run_cli(args))

    if os.name == 'nt':
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('foobar.musicdl.SongMetadata')
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()


if __name__ == '__main__':
    main()
