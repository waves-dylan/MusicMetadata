"""元数据自动修复、封面/艺术家写真补全、歌词下载与同步执行管线。"""

from __future__ import annotations

import os
import sys
import time
from collections import Counter

from core.constants import STATUS_ERROR
from core.models import FoobarEnv, FolderArtPlan, LibraryTrack, TrackMatch
from pipelines.executor import _run_pool_cancellable, _sync_foobar_targets
from pipelines.options import FixOptions
from providers.base import _VA_ARTISTS, first_artist, norm_core
from services.art import (
    ARTIST_MAX_SIDE,
    MAX_SIDE,
    SUPPORTS_ARTIST_WRITE,
    apply_folder_artist_file,
    apply_folder_cover_file,
    apply_track_repairs,
    normalize_artist_jpeg,
    normalize_jpeg,
    pick_canonical_album_date,
)
from services.lookup import (
    clear_caches,
    fetch_artist_bytes,
    fetch_cover_bytes,
    get_fanart_keys,
    get_spotify_credentials,
    get_tadb_api_key,
    resolve_track_metadata,
    set_fanart_keys,
    set_netease_api_base,
    set_spotify_credentials,
    set_tadb_api_key,
)


def resolve_hook(name: str, current):
    from pipelines.executor import resolve_hook as _exec_resolve_hook
    return _exec_resolve_hook(name, current)


FOLDER_COVER_LABELS = {
    'create': '目录缺cover.jpg',
    'rewrite': '目录cover.jpg需重写',
    'remove': '多专辑需撤回cover.jpg',
}


def _album_date_group_key(tr: LibraryTrack, match: TrackMatch | None) -> tuple[str, str] | None:
    if tr.folder_plan and tr.folder_plan.album_count == 1:
        return (tr.folder, '__single__')
    alb_key = (tr.album or (match.album_name if match else '') or '').strip()
    return (tr.folder, alb_key.lower()) if alb_key else None


def _propagate_album_group_metadata(
    targets: list[LibraryTrack],
    lookup_targets: list[LibraryTrack],
    matches: dict[str, TrackMatch],
    allowed_tags: frozenset[str],
    enable_art: bool,
) -> None:
    """同一专辑或单专辑目录内传播日期、专辑艺术家、流派、封面 URL 与单碟碟号。"""
    grp_buckets: dict[tuple[str, str], dict[str, list]] = {}
    for tr in targets:
        m = matches.get(tr.path)
        grp_key = _album_date_group_key(tr, m)
        if grp_key is None:
            continue
        bucket = grp_buckets.setdefault(
            grp_key,
            {
                'release_date': [],
                'albumartist': [],
                'genre': [],
                'cover_url': [],
                'artist_pic_url': [],
                'discnumber': [],
            },
        )
        for mk, tk in (('release_date', 'date'), ('albumartist', 'albumartist'), ('genre', 'genre')):
            val = (getattr(m, mk, '') if m else '') or (getattr(tr, tk, '') or '').strip() or (tr.tag_updates.get(tk) or '').strip()
            if val:
                bucket[mk].append(val)
        if m and m.cover_url:
            bucket['cover_url'].append(m.cover_url)
        if m and m.artist_pic_url:
            bucket['artist_pic_url'].append(m.artist_pic_url)
        dn = (m.discnumber if (m and m.discnumber and m.discnumber > 0) else None) or tr.disk_disc_int or tr.target_disc
        if dn:
            bucket['discnumber'].append(int(dn))

    grp_maj: dict[tuple[str, str], dict] = {}
    for gk, b in grp_buckets.items():
        maj_entry: dict = {}
        for fk in ('release_date', 'albumartist', 'genre', 'cover_url', 'artist_pic_url'):
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
            ('cover_url', enable_art),
            ('artist_pic_url', True),
        ):
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

    cancelled = _run_pool_cancellable(list(need_urls), fetch_fn, _on_cov_done, max_workers=12, is_cancel=is_cancel)
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


def _download_and_apply_online_artist_pics(
    targets: list[LibraryTrack],
    lookup_targets: list[LibraryTrack],
    matches: dict[str, TrackMatch],
    handled_folders: set[str],
    progress_cb,
    emit_log,
    is_cancel,
    options: FixOptions,
) -> tuple[bool, dict[str, bytes], int, int]:
    """批量下载联网艺术家图片并为缺少外置 artist.jpg 的单专辑目录写入图片。"""
    online_artists: dict[str, bytes] = {}
    folder_artist_ok = folder_artist_fail = 0

    need_items: set[str] = set()
    for tr in targets:
        if not options.enable_artist_art or not tr.needs_artist_art:
            continue
        if tr.target_artist_bytes is not None:
            continue
        m = matches.get(tr.path)
        if m and m.artist_pic_url:
            need_items.add(m.artist_pic_url)
        else:
            name = tr.albumartist or tr.artist or (m.albumartist if m else '') or (m.artist if m else '')
            name = first_artist(name) or str(name or '').strip()
            if name and norm_core(name) not in _VA_ARTISTS:
                need_items.add(name)

    for tr in targets:
        fp = tr.folder_plan
        if fp and fp.artist_action == 'nodata':
            m = matches.get(tr.path)
            if m and m.artist_pic_url:
                need_items.add(m.artist_pic_url)
            else:
                name = first_artist(tr.albumartist or tr.artist)
                if name and norm_core(name) not in _VA_ARTISTS:
                    need_items.add(name)

    if not need_items:
        return False, online_artists, 0, 0

    art_total, art_done = len(need_items), 0
    emit_log(f'正在下载 {art_total} 张在线艺术家图片...')
    if progress_cb:
        progress_cb(0, art_total, '正在下载在线艺术家图片...')

    fetch_fn = resolve_hook('fetch_artist_bytes', fetch_artist_bytes)

    def _submit_fetch(item: str):
        if item.startswith('http'):
            return fetch_fn(artist_url=item)
        return fetch_fn(
            artist_name=item,
            prefer_sources=options.prefer_sources,
            only_sources=options.only_sources,
            skip_sources=options.skip_sources,
        )

    def _on_art_done(item: str, fut):
        nonlocal art_done
        art_done += 1
        try:
            data = fut.result()
            if data:
                online_artists[item] = data
        except Exception:
            pass
        if progress_cb:
            progress_cb(art_done, art_total, f'下载艺术家图片 ({art_done}/{art_total})')

    cancelled = _run_pool_cancellable(list(need_items), _submit_fetch, _on_art_done, max_workers=12, is_cancel=is_cancel)
    if cancelled:
        return True, online_artists, 0, 0

    seen_nodata: dict[str, tuple[FolderArtPlan, bytes]] = {}
    for tr in targets:
        fp = tr.folder_plan
        if fp and fp.artist_action == 'nodata' and tr.folder not in seen_nodata:
            m = matches.get(tr.path)
            pic = None
            if m and m.artist_pic_url:
                pic = online_artists.get(m.artist_pic_url)
            if not pic:
                name = first_artist(tr.albumartist or tr.artist)
                pic = online_artists.get(name)
            if pic:
                seen_nodata[tr.folder] = (fp, pic)
    for folder, (fp, pic) in seen_nodata.items():
        dir_jpg = normalize_artist_jpeg(pic, ARTIST_MAX_SIDE)
        if not dir_jpg:
            continue
        try:
            art_out_path = getattr(fp, 'artist_cover_path', '') or getattr(fp, 'artist_path', '') or os.path.join(folder, 'artist.jpg')
            with open(art_out_path, 'wb') as out_fp:
                out_fp.write(dir_jpg)
            fp.artist_action = 'ok'
            folder_artist_ok += 1
            handled_folders.add(f'{folder}:artist')
            emit_log(f'[目录艺术家图] {folder} -> 已联网生成 artist.jpg')
        except Exception as err:
            folder_artist_fail += 1
            emit_log(f'[目录艺术家图失败] {folder} -> {err}')
    return False, online_artists, folder_artist_ok, folder_artist_fail


def execute_repairs(
    env: FoobarEnv,
    targets: list[LibraryTrack],
    options: FixOptions,
    progress_cb=None,
    row_cb=None,
    log_cb=None,
    is_cancelled_cb=None,
) -> dict:
    """统一执行元数据检查刷新、封面修复、标签补全、歌词抓取与音轨号补全（GUI 与 CLI 共用）。"""
    if options.netease_api is not None:
        set_netease_api_base(options.netease_api)
    if options.spotify_client_id is not None or options.spotify_client_secret is not None:
        cur_sp_id, cur_sp_sec = get_spotify_credentials()
        set_spotify_credentials(
            cur_sp_id if options.spotify_client_id is None else options.spotify_client_id,
            cur_sp_sec if options.spotify_client_secret is None else options.spotify_client_secret,
        )
    if options.tadb_api_key is not None:
        set_tadb_api_key(options.tadb_api_key)
    if options.fanart_api_key is not None or options.fanart_client_key is not None:
        cur_fa_ak, cur_fa_ck = get_fanart_keys()
        set_fanart_keys(
            cur_fa_ak if options.fanart_api_key is None else options.fanart_api_key,
            cur_fa_ck if options.fanart_client_key is None else options.fanart_client_key,
        )
    clear_caches()
    total, cancelled = len(targets), False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    written_ok = written_fail = unmatched = date_unmatched_cnt = cover_fixed = tags_fixed = date_fixed = lyrics_fixed = lyrics_unmatched_cnt = folder_cover_ok = folder_cover_fail = synced_stale = 0
    artist_fixed = folder_artist_ok = folder_artist_fail = 0
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
            emit_row(
                tr.path,
                {
                    'status_text': '待刷新' if options.dry_run else '已刷新',
                    'source_note': '推进时间戳并通知 foobar 重新载入元数据',
                    'color': 'info' if options.dry_run else 'ok',
                    'fixed_sync': not options.dry_run,
                },
            )
            emit_log(f"[{'预演' if options.dry_run else '刷新'}] {tr.filename} -> 刷新 foobar 元数据缓存")
        touched, reloaded = (0, False)
        if not options.dry_run and synced_paths and not cancelled:
            touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log, log_noun='文件')
        return {
            'cancelled': cancelled,
            'dry_run': options.dry_run,
            'sync_only': True,
            'written_ok': 0,
            'written_fail': 0,
            'unmatched': 0,
            'date_unmatched': 0,
            'cover_fixed': 0,
            'tags_fixed': 0,
            'date_fixed': 0,
            'lyrics_fixed': 0,
            'lyrics_unmatched': 0,
            'folder_cover_ok': 0,
            'folder_cover_fail': 0,
            'synced_stale': synced_stale,
            'touched': touched,
            'reloaded': reloaded,
        }

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

    if options.enable_artist_art:
        seen_artist_folders: dict[str, FolderArtPlan] = {}
        for tr in targets:
            if tr.folder_plan and tr.folder_plan.has_folder_artist_work:
                seen_artist_folders.setdefault(tr.folder, tr.folder_plan)
        for folder, f_plan in seen_artist_folders.items():
            if is_cancel():
                cancelled = True
                break
            if options.dry_run:
                folder_artist_ok += 1
                handled_folders.add(f'{folder}:artist')
                emit_log(f'[预演][目录艺术家图] {folder} -> {f_plan.artist_action} ({f_plan.artist_detail})')
            else:
                ok_f, msg_f = apply_folder_artist_file(f_plan)
                if ok_f:
                    folder_artist_ok += 1
                    handled_folders.add(f'{folder}:artist')
                    f_plan.artist_action = 'ok'
                    emit_log(f'[目录艺术家图] {folder} -> {msg_f}')
                else:
                    folder_artist_fail += 1
                    emit_log(f'[目录艺术家图失败] {folder} -> {msg_f}')

    matches: dict[str, TrackMatch] = {}
    allowed_tags = options.active_tag_fields()

    def _lookup_flags(tr: LibraryTrack) -> tuple[bool, bool, bool, bool, bool, bool, bool, frozenset[str]]:
        need_tn = bool(options.enable_track and tr.needs_tracknumber)
        need_cov = bool(options.enable_art and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata')))
        need_dt = bool('date' in allowed_tags and tr.needs_date and 'date' not in tr.tag_updates)
        need_core = bool(allowed_tags and any(f != 'date' and f in allowed_tags and f not in tr.tag_updates for f in tr.missing_core_fields))
        need_ext = bool(allowed_tags and any(f in allowed_tags for f in tr.missing_extra_fields))
        need_lrc = bool(options.enable_lyrics and tr.needs_lyrics)
        need_artist = bool(options.enable_artist_art and (tr.needs_artist_art or (tr.folder_plan and tr.folder_plan.artist_action == 'nodata')))
        need_flds: set[str] = set()
        if need_dt:
            need_flds.add('date')
        if need_cov:
            need_flds.add('cover')
        if need_lrc:
            need_flds.add('lyrics')
        if need_artist:
            need_flds.add('artist_cover')
        for f in tr.missing_core_fields + tr.missing_extra_fields:
            if f in allowed_tags and f not in tr.tag_updates:
                need_flds.add(f)
        return need_tn, need_dt, need_cov, need_core, need_ext, need_lrc, need_artist, frozenset(need_flds)

    lookup_targets = [
        t for t in targets
        if not t.is_temp_duplicate and t.status != STATUS_ERROR and any(_lookup_flags(t)[:7])
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
            need_tn, need_dt, need_cov, need_core, need_ext, need_lrc, need_artist, need_flds = _lookup_flags(tr)
            kwargs: dict = {
                'need_track': need_tn,
                'need_date': need_dt,
                'need_cover': need_cov,
                'need_artist_art': need_artist,
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
            if m.source or m.tracknumber or m.release_date or m.cover_url or m.artist_pic_url or m.genre or m.composer or m.lyrics:
                hit_cnt += 1
            if progress_cb:
                progress_cb(done_cnt, lookup_total, f'联网查询: {tr.filename}')
            now = time.monotonic()
            if done_cnt == 1 or done_cnt % 25 == 0 or done_cnt == lookup_total or (now - last_log_t) >= 3.0:
                last_log_t = now
                emit_log(f'联网查询进度：{done_cnt}/{lookup_total}（已命中 {hit_cnt} 首）...')

        cancelled = _run_pool_cancellable(lookup_targets, _submit_lookup, _on_lookup_done, max_workers=16, is_cancel=is_cancel)
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

    online_artists: dict[str, bytes] = {}
    if options.enable_artist_art and not options.dry_run and not cancelled:
        art_cancel, online_artists, art_ok_cnt, art_fail_cnt = _download_and_apply_online_artist_pics(
            targets, lookup_targets, matches, handled_folders, progress_cb, emit_log, is_cancel, options,
        )
        cancelled = cancelled or art_cancel
        folder_artist_ok += art_ok_cnt
        folder_artist_fail += art_fail_cnt

    def _execute_track_action(tr: LibraryTrack) -> dict:
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

        want_artist = bool(options.enable_artist_art and tr.needs_artist_art)
        artist_bytes_to_write = None
        artist_mime_to_write = 'image/jpeg'
        if want_artist:
            if tr.target_artist_bytes is not None:
                artist_bytes_to_write = tr.target_artist_bytes
                artist_mime_to_write = tr.target_artist_mime
            else:
                m = matches.get(tr.path)
                if m and m.artist_pic_url and m.artist_pic_url in online_artists:
                    artist_bytes_to_write = online_artists[m.artist_pic_url]
                else:
                    target_name = first_artist(tr.albumartist or tr.artist or (m.albumartist if m else '') or (m.artist if m else ''))
                    artist_bytes_to_write = online_artists.get(target_name)

        can_write_artist = (tr.container in SUPPORTS_ARTIST_WRITE)
        want_artist_write = bool(
            want_artist
            and can_write_artist
            and (
                artist_bytes_to_write is not None
                or (options.dry_run and (tr.target_artist_bytes or (match and match.artist_pic_url) or bool(tr.albumartist or tr.artist)))
            )
        )

        want_tags = bool(allowed_tags and effective_tags)
        want_write_lyrics = bool(matched_lyrics)
        want_sync = options.enable_sync and tr.needs_foobar_sync
        folder_done = tr.folder in handled_folders
        has_file_write = bool(matched_tn or matched_dn or want_cover or want_artist_write or want_tags or want_write_lyrics)

        ok, msg = False, ''
        if has_file_write and not options.dry_run:
            repair_kwargs = {
                'tag_updates': effective_tags if want_tags else None,
                'cover_bytes': cov_bytes_to_write if want_cover else None,
                'cover_mime': cov_mime_to_write,
                'artist_bytes': artist_bytes_to_write if want_artist_write else None,
                'artist_mime': artist_mime_to_write,
                'tracknumber': matched_tn,
                'discnumber': matched_dn,
                'overwrite_track': (tr.disk_track_int == 0),
            }
            if want_write_lyrics:
                repair_kwargs['lyrics'] = matched_lyrics
                repair_kwargs['lyrics_mode'] = options.lyrics_mode
            ok, msg = apply_track_repairs(tr.path, **repair_kwargs)

        return {
            'want_track': want_track,
            'want_date_online': want_date_online,
            'want_cover_online': want_cover_online,
            'want_lyrics': want_lyrics,
            'matched_tn': matched_tn,
            'matched_date': matched_date,
            'matched_cov_url': matched_cov_url,
            'matched_lyrics': matched_lyrics,
            'track_unmatched': track_unmatched,
            'date_unmatched': date_unmatched,
            'lyrics_unmatched': lyrics_unmatched,
            'want_cover': want_cover,
            'effective_tags': effective_tags,
            'planned_dn': planned_dn,
            'matched_dn': matched_dn,
            'want_artist_write': want_artist_write,
            'want_tags': want_tags,
            'want_write_lyrics': want_write_lyrics,
            'want_sync': want_sync,
            'folder_done': folder_done,
            'has_file_write': has_file_write,
            'ok': ok,
            'msg': msg,
        }

    def _apply_track_result(tr: LibraryTrack, res: dict):
        nonlocal written_ok, written_fail, cover_fixed, artist_fixed, tags_fixed, date_fixed
        nonlocal lyrics_fixed, synced_stale, unmatched, date_unmatched_cnt, lyrics_unmatched_cnt
        unmatched += int(res['track_unmatched'])
        date_unmatched_cnt += int(res['date_unmatched'])
        lyrics_unmatched_cnt += int(res['lyrics_unmatched'])
        match = matches.get(tr.path)
        matched_tn = res['matched_tn']
        matched_dn = res['matched_dn']
        folder_done = res['folder_done']

        if options.dry_run:
            pre = []
            if matched_tn:
                pre.append(f'音轨号->{matched_tn}({match.source})')
            elif res['track_unmatched']:
                pre.append(f'未匹配音轨({match.reason if match else "未知"})')
            if matched_dn:
                pre.append(f'碟号->{matched_dn}')
            if res['want_cover']:
                pre.append('联网补封面' if res['want_cover_online'] else '修复封面')
            if res['want_artist_write']:
                pre.append('修复艺术家图' if tr.has_artist_art else '补艺术家图')
            if res['want_tags']:
                pre.append('补标签:' + ','.join(f'{k}={v}' for k, v in res['effective_tags'].items()))
            if res['want_write_lyrics']:
                pre.append(f'补歌词({options.lyrics_mode})')
            elif res['lyrics_unmatched']:
                pre.append('未匹配歌词')
            if res['date_unmatched']:
                pre.append(f'未匹配日期({match.reason if match else "未知"})')
            if res['want_sync']:
                pre.append('刷新foobar缓存')
            if folder_done:
                pre.append('已含目录cover.jpg')
            if res['has_file_write']:
                written_ok += 1
                cover_fixed += int(res['want_cover'])
                artist_fixed += int(res['want_artist_write'])
                tags_fixed += int(res['want_tags'] or bool(res['planned_dn']))
                date_fixed += int(bool(res['want_tags'] and 'date' in res['effective_tags']))
                lyrics_fixed += int(res['want_write_lyrics'])
            if res['want_sync'] and not res['has_file_write']:
                synced_stale += 1
            emit_row(
                tr.path,
                {
                    'status_text': '预演' if pre else '无需改动',
                    'matched_track': matched_tn,
                    'source_note': '；'.join(pre) or '无待修项目',
                    'color': 'warn' if ((res['track_unmatched'] or res['date_unmatched'] or res['lyrics_unmatched']) and not (matched_dn or res['want_cover'] or res['want_artist_write'] or res['want_tags'] or res['want_write_lyrics'] or res['want_sync'])) else 'info',
                },
            )
            emit_log(f"[预演] {tr.filename} -> {'；'.join(pre) or '无需改动'}")
            return

        if res['has_file_write']:
            if res['ok']:
                written_ok += 1
                cover_fixed += int(res['want_cover'])
                artist_fixed += int(res['want_artist_write'])
                tags_fixed += int(res['want_tags'] or bool(res['planned_dn']))
                date_fixed += int(bool(res['want_tags'] and 'date' in res['effective_tags']))
                lyrics_fixed += int(res['want_write_lyrics'])
                if options.enable_sync:
                    synced_paths.append(tr.path)
                    foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
                extra = []
                if match and match.source and (matched_tn or matched_dn or res['matched_date'] or res['want_cover_online'] or res['want_artist_write'] or res['want_tags'] or res['want_write_lyrics']):
                    extra.append(f'{match.source}' + (f'·《{match.album_name}》' if match.album_name else ''))
                if res['track_unmatched'] and match:
                    extra.append(f'音轨未匹配:{match.reason}')
                if res['date_unmatched'] and match and not res['track_unmatched']:
                    extra.append(f'日期未匹配:{match.reason}')
                if res['lyrics_unmatched'] and match:
                    extra.append('歌词未匹配')
                note_str = res['msg'] + (f"（{'；'.join(extra)}）" if extra else '')
                emit_row(
                    tr.path,
                    {
                        'status_text': '已修复',
                        'matched_track': matched_tn,
                        'current_track': str(matched_tn) if matched_tn else None,
                        'current_disc': str(matched_dn) if matched_dn else None,
                        'source_note': note_str,
                        'color': 'ok',
                        'fixed_track': bool(matched_tn),
                        'fixed_disc': bool(matched_dn),
                        'fixed_cover': res['want_cover'],
                        'fixed_cover_online': bool(res['want_cover'] and res['want_cover_online']),
                        'fixed_artist_art': res['want_artist_write'],
                        'fixed_tags': res['want_tags'],
                        'written_tags': res['effective_tags'] if res['want_tags'] else {},
                        'fixed_lyrics': res['want_write_lyrics'],
                        'lyrics_mode': options.lyrics_mode,
                        'fixed_sync': options.enable_sync,
                        'fixed_folder_cover': folder_done,
                    },
                )
                emit_log(f'[已修复] {tr.filename} -> {note_str}')
            else:
                written_fail += 1
                emit_row(tr.path, {'status_text': '写入失败', 'matched_track': matched_tn, 'source_note': res['msg'], 'color': 'err'})
                emit_log(f'[失败] {tr.filename} -> {res["msg"]}')
        elif res['want_sync']:
            synced_stale += 1
            synced_paths.append(tr.path)
            foobar_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.foobar_mtime, tr.disk_mtime)
            desc = '、'.join(tr.foobar_stale_reasons) if tr.foobar_stale_reasons else '缓存过期'
            emit_row(
                tr.path,
                {
                    'status_text': '已同步',
                    'source_note': f'已触发 foobar 刷新（{desc}）',
                    'color': 'ok',
                    'fixed_sync': True,
                    'fixed_folder_cover': folder_done,
                },
            )
            emit_log(f'[同步] {tr.filename} -> 触发 foobar 刷新（{desc}）')
        elif res['track_unmatched'] or res['date_unmatched'] or res['lyrics_unmatched']:
            reason = match.reason if match else ('未找到匹配音轨号' if res['track_unmatched'] else ('未找到匹配发布日期' if res['date_unmatched'] else '未找到匹配歌词'))
            emit_row(tr.path, {'status_text': '未匹配', 'source_note': reason, 'color': 'warn', 'fixed_folder_cover': folder_done})
            emit_log(f'[未匹配] {tr.filename}（{reason}）')
        elif folder_done:
            emit_row(tr.path, {'status_text': '目录已修', 'source_note': '目录外置 cover.jpg 已处理', 'color': 'ok', 'fixed_folder_cover': True})

    if options.dry_run or total <= 1:
        for idx, tr in enumerate(targets, 1):
            if cancelled or is_cancel():
                cancelled = True
                break
            if progress_cb:
                progress_cb(idx, total, tr.filename)
            if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
                continue
            res = _execute_track_action(tr)
            _apply_track_result(tr, res)
    else:
        valid_targets = [tr for tr in targets if tr.status != STATUS_ERROR and not tr.is_temp_duplicate]
        write_workers = min(8, max(2, (os.cpu_count() or 4)))
        done_write_cnt = 0

        def _on_single_done(tr: LibraryTrack, fut):
            nonlocal done_write_cnt
            done_write_cnt += 1
            if progress_cb:
                progress_cb(done_write_cnt, total, tr.filename)
            try:
                res = fut.result()
            except Exception as err:
                res = {
                    'has_file_write': True,
                    'ok': False,
                    'msg': str(err),
                    'matched_tn': None,
                    'matched_dn': None,
                    'matched_date': '',
                    'want_cover_online': False,
                    'folder_done': False,
                    'want_cover': False,
                    'want_artist_write': False,
                    'want_tags': False,
                    'want_write_lyrics': False,
                    'track_unmatched': False,
                    'date_unmatched': False,
                    'lyrics_unmatched': False,
                    'want_sync': False,
                    'planned_dn': None,
                    'effective_tags': {},
                }
            _apply_track_result(tr, res)

        pool_cancelled = _run_pool_cancellable(
            valid_targets,
            _execute_track_action,
            _on_single_done,
            max_workers=write_workers,
            is_cancel=is_cancel,
        )
        cancelled = cancelled or pool_cancelled

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_foobar_targets(env, synced_paths, foobar_mtimes, emit_log)

    return {
        'cancelled': cancelled,
        'dry_run': options.dry_run,
        'sync_only': False,
        'written_ok': written_ok,
        'written_fail': written_fail,
        'unmatched': unmatched,
        'date_unmatched': date_unmatched_cnt,
        'lyrics_unmatched': lyrics_unmatched_cnt,
        'cover_fixed': cover_fixed,
        'artist_fixed': artist_fixed,
        'tags_fixed': tags_fixed,
        'date_fixed': date_fixed,
        'lyrics_fixed': lyrics_fixed,
        'folder_cover_ok': folder_cover_ok,
        'folder_cover_fail': folder_cover_fail,
        'folder_artist_ok': folder_artist_ok,
        'folder_artist_fail': folder_artist_fail,
        'synced_stale': synced_stale,
        'touched': touched,
        'reloaded': reloaded,
    }
