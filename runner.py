"""批量修复、删除、手工设置标签与格式转换的核心执行管线（GUI 与 CLI 共用）。"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass

from art_repair import (
    DELETABLE_FIELDS,
    MAX_SIDE,
    FolderArtPlan,
    apply_folder_cover_file,
    apply_track_deletions,
    apply_track_repairs,
    apply_track_set_tags,
    convert_track_format,
    delete_folder_cover_file,
    inspect_deletable_fields,
    normalize_jpeg,
)
from foobar_library import (
    FIELD_LABELS,
    STATUS_ERROR,
    FoobarEnv,
    LibraryTrack,
    sync_files_to_foobar,
    track_has_pending_work,
)
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg, read_audio_info
from track_lookup import (
    TrackMatch,
    clear_caches,
    fetch_cover_bytes,
    resolve_track_metadata,
    set_netease_api_base,
)

FOLDER_COVER_LABELS = {
    'create': '目录缺cover.jpg',
    'rewrite': '目录cover.jpg需重写',
    'remove': '多专辑需撤回cover.jpg',
}

_ORIG_HOOKS = {
    'resolve_track_metadata': resolve_track_metadata,
    'fetch_cover_bytes': fetch_cover_bytes,
    'sync_files_to_foobar': sync_files_to_foobar,
    'find_ffmpeg': find_ffmpeg,
}


def resolve_hook(name: str, current):
    """兼容 tests 直接 patch app.<symbol> 或模块本地符号的解析辅助。"""
    app_mod = sys.modules.get('app')
    if app_mod is not None and name in _ORIG_HOOKS:
        val = getattr(app_mod, name, current)
        if val is not _ORIG_HOOKS[name]:
            return val
    return current


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
    enable_genre: bool = True
    enable_disc: bool = True
    enable_lyrics: bool = False
    lyrics_mode: str = 'auto'
    prefer_sources: tuple[str, ...] = ()
    only_sources: tuple[str, ...] = ()
    skip_sources: tuple[str, ...] = ()
    netease_api: str | None = None
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
        enable_lyrics=opts.enable_lyrics,
    )


@dataclass
class DeleteOptions:
    fields: frozenset[str] = frozenset()
    delete_folder_cover: bool = False
    delete_lrc_file: bool = True
    protect_163_key: bool = True
    enable_sync: bool = True
    dry_run: bool = False


@dataclass
class SetOptions:
    tag_values: dict[str, str]
    protect_163_key: bool = True
    enable_sync: bool = True
    dry_run: bool = False


@dataclass
class ConvertOptions:
    target_format: str = 'flac'
    overwrite: bool = False
    delete_source: bool = False
    enable_sync: bool = True
    dry_run: bool = False


_DELETE_FIELD_ALIASES = {
    'art': 'cover',
    'album_artist': 'albumartist',
    'track': 'tracknumber',
    'disc': 'discnumber',
    'year': 'date',
    'comments': 'comment',
    'lrc': 'lyrics',
    'lyric': 'lyrics',
    **{v: k for k, v in FIELD_LABELS.items()},
}


def parse_delete_fields(raw_items: list[str] | tuple[str, ...] | str) -> tuple[list[str], list[str]]:
    """解析命令行/外部传入的待删除元数据类别名称，返回 (有效字段列表, 无效标记列表)。"""
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items or ())
    requested: set[str] = set()
    invalid: list[str] = []
    valid_set = set(DELETABLE_FIELDS)
    for item in items:
        for raw_tok in str(item or '').replace('，', ',').replace(';', ',').split(','):
            tok = raw_tok.strip()
            if not tok:
                continue
            low = tok.lower()
            mapped = low if low in valid_set else _DELETE_FIELD_ALIASES.get(low) or _DELETE_FIELD_ALIASES.get(tok)
            if mapped in valid_set:
                requested.add(mapped)
            elif tok not in invalid:
                invalid.append(tok)
    ordered = [f for f in DELETABLE_FIELDS if f in requested]
    return ordered, invalid


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


def _sync_foobar_targets(
    env: FoobarEnv,
    synced_paths: list[str],
    foobar_mtimes: dict[str, float] | None,
    emit_log,
    *,
    log_noun: str = '变更文件',
) -> tuple[int, bool]:
    unique = list(dict.fromkeys(synced_paths))
    emit_log(f'正在同步 {len(unique)} 个{log_noun}到 foobar2000...')
    sync_fn = resolve_hook('sync_files_to_foobar', sync_files_to_foobar)
    touched, reloaded = sync_fn(env, unique, foobar_mtimes) if foobar_mtimes is not None else sync_fn(env, unique)
    emit_log(f'已更新时间戳 ({touched} 个)' + ('并触发运行中的 foobar2000 重载。' if reloaded else '；foobar 下次启动会自动读取。'))
    return touched, reloaded


def _propagate_album_group_metadata(
    targets: list[LibraryTrack],
    lookup_targets: list[LibraryTrack],
    matches: dict[str, TrackMatch],
    allowed_tags: frozenset[str],
    enable_art: bool,
) -> None:
    """同一专辑或单专辑目录内传播日期、专辑艺术家、流派、封面 URL 与单碟碟号。"""
    from art_repair import pick_canonical_album_date

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
        for fk, enabled in (('albumartist', 'albumartist' in allowed_tags), ('genre', 'genre' in allowed_tags), ('cover_url', enable_art)):
            if enabled and not getattr(m, fk) and maj.get(fk):
                setattr(m, fk, maj[fk])
                if not m.source:
                    m.source = '同专辑元数据补全'


def _download_and_apply_online_covers(
    targets: list[LibraryTrack],
    lookup_targets: list[LibraryTrack],
    matches: dict[str, TrackMatch],
    handled_folders: set[str],
    progress_cb,
    emit_log,
    is_cancel,
) -> tuple[bool, dict[str, bytes], int, int]:
    """批量下载联网封面并为缺少外置 cover.jpg 的单专辑目录写入封面。"""
    online_covers: dict[str, bytes] = {}
    folder_cover_ok = folder_cover_fail = 0
    need_urls = {
        matches[tr.path].cover_url
        for tr in lookup_targets
        if tr.path in matches and matches[tr.path].cover_url
        and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata'))
    }
    if not need_urls:
        return False, online_covers, 0, 0

    cov_total, cov_done = len(need_urls), 0
    emit_log(f'正在下载 {cov_total} 张在线专辑封面...')
    if progress_cb:
        progress_cb(0, cov_total, '正在下载在线专辑封面...')

    fetch_fn = resolve_hook('fetch_cover_bytes', fetch_cover_bytes)

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

    cancelled = _run_pool_cancellable(list(need_urls), fetch_fn, _on_cov_done, max_workers=6, is_cancel=is_cancel)
    if cancelled:
        return True, online_covers, 0, 0

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
        if not dir_jpg:
            continue
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
    return False, online_covers, folder_cover_ok, folder_cover_fail


def execute_repairs(
    env: FoobarEnv, targets: list[LibraryTrack], options: FixOptions,
    progress_cb=None, row_cb=None, log_cb=None, is_cancelled_cb=None,
) -> dict:
    """统一执行元数据检查刷新、封面修复、标签补全、歌词抓取与音轨号补全（GUI 与 CLI 共用）。"""
    if options.netease_api is not None:
        set_netease_api_base(options.netease_api)
    clear_caches()
    total, cancelled = len(targets), False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    written_ok = written_fail = unmatched = date_unmatched_cnt = cover_fixed = tags_fixed = date_fixed = lyrics_fixed = lyrics_unmatched_cnt = folder_cover_ok = folder_cover_fail = synced_stale = 0
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
            sync_fn = resolve_hook('sync_files_to_foobar', sync_files_to_foobar)
            touched, reloaded = sync_fn(env, synced_paths, foobar_mtimes)
            emit_log(f'已推进 {touched} 个文件时间戳' + ('并触发运行中的 foobar2000 载入。' if reloaded else '；foobar 下次启动会自动载入。'))
        return {'cancelled': cancelled, 'dry_run': options.dry_run, 'sync_only': True,
                'written_ok': 0, 'written_fail': 0, 'unmatched': 0, 'date_unmatched': 0,
                'cover_fixed': 0, 'tags_fixed': 0, 'date_fixed': 0, 'lyrics_fixed': 0, 'lyrics_unmatched': 0,
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

    def _lookup_flags(tr: LibraryTrack) -> tuple[bool, bool, bool, bool, bool, bool, frozenset[str]]:
        need_tn = bool(options.enable_track and tr.needs_tracknumber)
        need_cov = bool(options.enable_art and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata')))
        need_dt = bool('date' in allowed_tags and tr.needs_date and 'date' not in tr.tag_updates)
        need_core = bool(allowed_tags and any(f != 'date' and f in allowed_tags and f not in tr.tag_updates for f in tr.missing_core_fields))
        need_ext = bool(allowed_tags and any(f in allowed_tags for f in tr.missing_extra_fields))
        need_lrc = bool(options.enable_lyrics and tr.needs_lyrics)
        need_flds: set[str] = set()
        if need_dt:
            need_flds.add('date')
        if need_cov:
            need_flds.add('cover')
        if need_lrc:
            need_flds.add('lyrics')
        for f in tr.missing_core_fields + tr.missing_extra_fields:
            if f in allowed_tags and f not in tr.tag_updates:
                need_flds.add(f)
        return need_tn, need_dt, need_cov, need_core, need_ext, need_lrc, frozenset(need_flds)

    lookup_targets = [
        t for t in targets
        if not t.is_temp_duplicate and t.status != STATUS_ERROR and any(_lookup_flags(t)[:6])
    ]
    if lookup_targets and not cancelled:
        lookup_total = len(lookup_targets)
        emit_log(f'开始联网查询并合并 {lookup_total} 首曲目元数据...')
        if progress_cb:
            progress_cb(0, lookup_total, '正在联网查询元数据...')
        done_cnt = hit_cnt = 0
        lookup_t0 = last_log_t = time.monotonic()
        resolve_fn = resolve_hook('resolve_track_metadata', resolve_track_metadata)

        def _submit_lookup(tr: LibraryTrack):
            need_tn, need_dt, need_cov, need_core, need_ext, need_lrc, need_flds = _lookup_flags(tr)
            kwargs: dict = {
                'need_track': need_tn,
                'need_date': need_dt,
                'need_cover': need_cov,
                'need_core': need_core,
                'need_extra': need_ext,
                'need_fields': need_flds,
            }
            if need_lrc:
                kwargs['need_lyrics'] = True
            if options.prefer_sources:
                kwargs['prefer_sources'] = options.prefer_sources
            if options.only_sources:
                kwargs['only_sources'] = options.only_sources
            if options.skip_sources:
                kwargs['skip_sources'] = options.skip_sources
            return resolve_fn(tr, **kwargs)

        def _on_lookup_done(tr: LibraryTrack, fut):
            nonlocal done_cnt, hit_cnt, last_log_t
            done_cnt += 1
            try:
                m = fut.result()
            except Exception as exc:
                m = TrackMatch(None, reason=f'查询异常: {exc}')
            matches[tr.path] = m
            if m.source or m.tracknumber or m.release_date or m.cover_url or m.genre or m.composer or m.lyrics:
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
        _propagate_album_group_metadata(targets, lookup_targets, matches, allowed_tags, options.enable_art)

    online_covers: dict[str, bytes] = {}
    if options.enable_art and not options.dry_run and not cancelled:
        cov_cancel, online_covers, cov_ok_cnt, cov_fail_cnt = _download_and_apply_online_covers(
            targets, lookup_targets, matches, handled_folders, progress_cb, emit_log, is_cancel,
        )
        cancelled = cancelled or cov_cancel
        folder_cover_ok += cov_ok_cnt
        folder_cover_fail += cov_fail_cnt

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
        want_lyrics = bool(options.enable_lyrics and tr.needs_lyrics)
        match = matches.get(tr.path)
        matched_tn = match.tracknumber if (want_track and match and match.tracknumber) else None
        matched_date = match.release_date if (want_date_online and match and match.release_date) else ''
        matched_cov_url = match.cover_url if (want_cover_online and match and match.cover_url) else ''
        matched_lyrics = match.lyrics.strip() if (want_lyrics and match and match.lyrics) else ''
        online_cov_bytes = online_covers.get(matched_cov_url) if matched_cov_url else None
        track_unmatched = bool(want_track and not matched_tn)
        date_unmatched = bool(want_date_online and not matched_date)
        lyrics_unmatched = bool(want_lyrics and not matched_lyrics)
        unmatched += int(track_unmatched)
        date_unmatched_cnt += int(date_unmatched)
        lyrics_unmatched_cnt += int(lyrics_unmatched)

        want_cover = options.enable_art and (
            (tr.needs_cover_fix and tr.target_pic_bytes is not None)
            or (want_cover_online and (online_cov_bytes is not None or (options.dry_run and bool(matched_cov_url))))
        )
        cov_bytes_to_write = tr.target_pic_bytes if (options.enable_art and tr.needs_cover_fix) else online_cov_bytes
        cov_mime_to_write = tr.target_pic_mime if (options.enable_art and tr.needs_cover_fix) else 'image/jpeg'

        effective_tags = {k: v for k, v in tr.tag_updates.items() if k in allowed_tags} if allowed_tags else {}
        if allowed_tags and match:
            for fk, fv in {'title': match.title, 'artist': match.artist, 'albumartist': match.albumartist, 'album': match.album_name, 'date': match.release_date}.items():
                if fv and fk in allowed_tags and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv
            for fk, fv in {'composer': match.composer, 'genre': match.genre}.items():
                if fv and fk in allowed_tags and fk in tr.missing_extra_fields and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv

        allow_disc = 'discnumber' in allowed_tags
        planned_dn = tr.target_disc if (allow_disc and tr.target_disc and not tr.disk_disc_int) else None
        matched_dn = planned_dn
        if matched_dn is None and allow_disc and match and match.discnumber and match.discnumber > 0 and not tr.disk_disc_int:
            if 'discnumber' in tr.missing_extra_fields or (options.enable_track and matched_tn and match.discnumber > 1):
                matched_dn = match.discnumber

        want_tags = bool(allowed_tags and effective_tags)
        want_write_lyrics = bool(matched_lyrics)
        want_sync = options.enable_sync and tr.needs_foobar_sync
        folder_done = tr.folder in handled_folders
        has_file_write = bool(matched_tn or matched_dn or want_cover or want_tags or want_write_lyrics)

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
            if want_write_lyrics:
                pre.append(f'补歌词({options.lyrics_mode})')
            elif lyrics_unmatched:
                pre.append('未匹配歌词')
            if date_unmatched:
                pre.append(f'未匹配日期({match.reason if match else "未知"})')
            if want_sync:
                pre.append('刷新foobar缓存')
            if folder_done:
                pre.append('已含目录cover.jpg')
            if has_file_write:
                written_ok += 1
                cover_fixed += int(want_cover)
                tags_fixed += int(want_tags or bool(planned_dn))
                date_fixed += int(bool(want_tags and 'date' in effective_tags))
                lyrics_fixed += int(want_write_lyrics)
            if want_sync and not has_file_write:
                synced_stale += 1
            emit_row(tr.path, {'status_text': '预演' if pre else '无需改动', 'matched_track': matched_tn,
                               'source_note': '；'.join(pre) or '无待修项目',
                               'color': 'warn' if ((track_unmatched or date_unmatched or lyrics_unmatched) and not (matched_dn or want_cover or want_tags or want_write_lyrics or want_sync)) else 'info'})
            emit_log(f"[预演] {tr.filename} -> {'；'.join(pre) or '无需改动'}")
            continue

        if has_file_write:
            repair_kwargs = {
                'tag_updates': effective_tags if want_tags else None,
                'cover_bytes': cov_bytes_to_write if want_cover else None,
                'cover_mime': cov_mime_to_write,
                'tracknumber': matched_tn,
                'discnumber': matched_dn,
                'overwrite_track': (tr.disk_track_int == 0),
            }
            if want_write_lyrics:
                repair_kwargs['lyrics'] = matched_lyrics
                repair_kwargs['lyrics_mode'] = options.lyrics_mode
            ok, msg = apply_track_repairs(tr.path, **repair_kwargs)
            if ok:
                written_ok += 1
                cover_fixed += int(want_cover)
                tags_fixed += int(want_tags or bool(planned_dn))
                date_fixed += int(bool(want_tags and 'date' in effective_tags))
                lyrics_fixed += int(want_write_lyrics)
                if options.enable_sync:
                    synced_paths.append(tr.path)
                    foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
                extra = []
                if match and match.source and (matched_tn or matched_dn or matched_date or want_cover_online or want_tags or want_write_lyrics):
                    extra.append(f'{match.source}' + (f'·《{match.album_name}》' if match.album_name else ''))
                if track_unmatched and match:
                    extra.append(f'音轨未匹配:{match.reason}')
                if date_unmatched and match and not track_unmatched:
                    extra.append(f'日期未匹配:{match.reason}')
                if lyrics_unmatched and match:
                    extra.append('歌词未匹配')
                note_str = msg + (f"（{'；'.join(extra)}）" if extra else '')
                emit_row(tr.path, {'status_text': '已修复', 'matched_track': matched_tn,
                                   'current_track': str(matched_tn) if matched_tn else None,
                                   'current_disc': str(matched_dn) if matched_dn else None,
                                   'source_note': note_str,
                                   'color': 'ok', 'fixed_track': bool(matched_tn), 'fixed_disc': bool(matched_dn),
                                   'fixed_cover': want_cover, 'fixed_cover_online': bool(want_cover and want_cover_online),
                                   'fixed_tags': want_tags, 'written_tags': effective_tags if want_tags else {},
                                   'fixed_lyrics': want_write_lyrics, 'lyrics_mode': options.lyrics_mode,
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
        elif track_unmatched or date_unmatched or lyrics_unmatched:
            reason = match.reason if match else ('未找到匹配音轨号' if track_unmatched else ('未找到匹配发布日期' if date_unmatched else '未找到匹配歌词'))
            emit_row(tr.path, {'status_text': '未匹配', 'source_note': reason, 'color': 'warn', 'fixed_folder_cover': folder_done})
            emit_log(f'[未匹配] {tr.filename}（{reason}）')
        elif folder_done:
            emit_row(tr.path, {'status_text': '目录已修', 'source_note': '目录外置 cover.jpg 已处理', 'color': 'ok', 'fixed_folder_cover': True})

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {'cancelled': cancelled, 'dry_run': options.dry_run, 'sync_only': False,
            'written_ok': written_ok, 'written_fail': written_fail, 'unmatched': unmatched,
            'date_unmatched': date_unmatched_cnt, 'lyrics_unmatched': lyrics_unmatched_cnt,
            'cover_fixed': cover_fixed, 'tags_fixed': tags_fixed, 'date_fixed': date_fixed,
            'lyrics_fixed': lyrics_fixed, 'folder_cover_ok': folder_cover_ok,
            'folder_cover_fail': folder_cover_fail, 'synced_stale': synced_stale, 'touched': touched, 'reloaded': reloaded}


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

        actually_deleted: list[str] = []
        ok, msg = apply_track_deletions(
            tr.path, fields, protect_163_key=options.protect_163_key,
            delete_lrc=options.delete_lrc_file, out_deleted=actually_deleted,
        )
        if ok and actually_deleted:
            deleted_ok += 1
            modified_paths.append(tr.path)
            for f in actually_deleted:
                field_counts[f] = field_counts.get(f, 0) + 1
            if options.enable_sync:
                synced_paths.append(tr.path)
                foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            new_info = read_audio_info(tr.path)
            emit_row(tr.path, {
                'status_text': '已删除', 'source_note': msg, 'color': 'ok',
                'deleted_fields': list(actually_deleted), 'new_info': new_info, 'synced': options.enable_sync,
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

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {
        'cancelled': cancelled, 'dry_run': options.dry_run,
        'deleted_ok': deleted_ok, 'deleted_skipped': deleted_skipped, 'deleted_fail': deleted_fail,
        'folder_cover_deleted': folder_cover_deleted, 'folder_cover_fail': folder_cover_fail,
        'field_counts': field_counts, 'modified_paths': tuple(modified_paths),
        'touched': touched, 'reloaded': reloaded,
    }


def _count_deletable_targets(targets: list[LibraryTrack]) -> tuple[dict[str, int], int, int]:
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

        ok, msg = apply_track_set_tags(tr.path, tag_values, protect_163_key=options.protect_163_key)
        if ok:
            set_ok += 1
            modified_paths.append(tr.path)
            if options.enable_sync:
                synced_paths.append(tr.path)
                foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            new_info = read_audio_info(tr.path)
            emit_row(tr.path, {
                'status_text': '已设置', 'source_note': msg, 'color': 'ok',
                'set_tags': dict(tag_values), 'new_info': new_info, 'synced': options.enable_sync,
            })
            emit_log(f'[已设置] {tr.filename} -> {msg}')
        else:
            set_fail += 1
            emit_row(tr.path, {'status_text': '设置失败', 'source_note': msg, 'color': 'err'})
            emit_log(f'[设置失败] {tr.filename} -> {msg}')

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {
        'cancelled': cancelled, 'dry_run': options.dry_run,
        'set_ok': set_ok, 'set_skipped': set_skipped, 'set_fail': set_fail,
        'modified_paths': tuple(modified_paths), 'touched': touched, 'reloaded': reloaded,
    }


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
