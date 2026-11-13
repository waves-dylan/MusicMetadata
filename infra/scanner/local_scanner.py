"""通用本地音乐库目录扫描、全专一致性校对与文件时间戳推进。"""

from __future__ import annotations

import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from core.constants import (
    AUDIO_EXTS,
    CORE_TEXT_FIELDS,
    EXTRA_TEXT_FIELDS,
    FIELD_LABELS,
    STATUS_ERROR,
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
    SUPPORTS_ARTIST_WRITE,
    SUPPORTS_COVER_WRITE,
)
from core.models import FolderArtPlan, LibraryTrack
from tag_io import bump_mtime, has_valid_tracknumber, read_audio_info

_TEMP_COPY_RE = re.compile(r'^(.+)\.[a-z0-9_]{6,10}$', re.IGNORECASE)
_FULL_ALBUM_RE = re.compile(r'(专辑全长|full\s*album|整轨)', re.IGNORECASE)


def _is_full_album_file(path: str, info: dict) -> bool:
    if float(info.get('duration') or 0.0) < 1200.0:
        return False
    return bool(_FULL_ALBUM_RE.search(info.get('title') or '') or _FULL_ALBUM_RE.search(os.path.splitext(os.path.basename(path))[0]))


def _is_temp_duplicate(path: str) -> bool:
    stem, ext = os.path.splitext(os.path.basename(path))
    m = _TEMP_COPY_RE.match(stem)
    return bool(m and os.path.isfile(os.path.join(os.path.dirname(path), m.group(1) + ext)))


def _norm_cmp(value: str) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def is_under_folders(path: str, folders: list[str]) -> bool:
    if not path or not folders:
        return False
    norm_p = os.path.normcase(os.path.abspath(path))
    return any(
        norm_p == (nr := os.path.normcase(os.path.abspath(r)).rstrip('\\/')) or norm_p.startswith(nr + os.sep)
        for r in folders if r
    )


def track_has_pending_work(
    track: LibraryTrack,
    enable_track: bool = True, enable_art: bool = True, enable_tags: bool = True,
    enable_sync: bool = True, skip_full_album: bool = True, enable_extra_tags: bool = False,
    enabled_tag_fields: set[str] | frozenset[str] | None = None,
    enable_lyrics: bool = False,
    enable_artist_art: bool = False,
) -> bool:
    if track.status == STATUS_ERROR or track.is_temp_duplicate:
        return False
    if enable_track and track.needs_tracknumber and not (skip_full_album and track.is_full_album):
        return True
    if enable_art and (track.needs_cover_fix or track.needs_cover_online or track.folder_cover_action in ('create', 'rewrite', 'remove')):
        return True
    if enable_artist_art and (track.needs_artist_art or track.folder_artist_action in ('create', 'rewrite', 'remove')):
        return True
    if enable_lyrics and track.needs_lyrics and not (skip_full_album and track.is_full_album):
        return True
    if enable_tags:
        if enabled_tag_fields is None:
            has_planned = track.needs_tag_fix
            has_online = (
                bool(track.missing_core_fields)
                or track.needs_date
                or (enable_extra_tags and track.needs_extra_tags)
            )
        else:
            has_planned = (
                any(k in enabled_tag_fields for k in track.tag_updates)
                or (bool(track.target_disc) and 'discnumber' in enabled_tag_fields)
            )
            has_online = (
                any(f in enabled_tag_fields for f in track.missing_core_fields)
                or (track.needs_date and 'date' in enabled_tag_fields)
                or any(f in enabled_tag_fields for f in track.missing_extra_fields)
            )
        if has_planned or (has_online and not (skip_full_album and track.is_full_album)):
            return True
    if enable_sync and track.needs_foobar_sync:
        return True
    return False


def _append_note(track: LibraryTrack, item: str) -> None:
    if not item:
        return
    if not track.note:
        track.note = item
    elif item not in track.note:
        track.note = f'{track.note}；{item}'


def reconcile_album_consistency(tracks: list[LibraryTrack]) -> None:
    """跨目录按同专辑校对并统一不一致的 DATE 与混用的空/非空 DISCNUMBER。"""
    from art_repair import pick_canonical_album_date

    groups: dict[tuple[str, str], list[LibraryTrack]] = {}
    for tr in tracks:
        if tr.error or tr.is_temp_duplicate or tr.is_full_album:
            continue
        eff_alb = (tr.tag_updates.get('album') or tr.album or '').strip()
        if not eff_alb:
            continue
        eff_aa = (tr.tag_updates.get('albumartist') or tr.albumartist or tr.artist or '').strip()
        groups.setdefault((eff_aa.lower(), eff_alb.lower()), []).append(tr)

    for grp in groups.values():
        if len(grp) < 2:
            continue
        raw_dates = [(tr.date or '').strip() for tr in grp if (tr.date or '').strip()]
        if raw_dates:
            canon_date = pick_canonical_album_date(raw_dates)
            for tr in grp:
                cur_date = (tr.date or '').strip()
                if cur_date != canon_date:
                    tr.tag_updates['date'] = canon_date
                    tr.needs_tag_fix = True
                    tr.needs_date = False
                    if 'date' in tr.missing_core_fields:
                        tr.missing_core_fields = [f for f in tr.missing_core_fields if f != 'date']
                    _append_note(tr, f'统一专辑日期:{cur_date}->{canon_date}' if cur_date else f'补标签:date={canon_date}')
                elif 'date' in tr.tag_updates:
                    tr.tag_updates.pop('date', None)
                    tr.needs_tag_fix = bool(tr.tag_updates or tr.target_disc)

        disc_ints = [tr.disk_disc_int for tr in grp if tr.disk_disc_int and tr.disk_disc_int > 0]
        if disc_ints and set(disc_ints) == {1}:
            for tr in grp:
                if not (tr.disk_disc_int or (tr.disk_discnumber or '').strip()):
                    tr.target_disc = 1
                    tr.needs_tag_fix = True
                    if 'discnumber' in tr.missing_extra_fields:
                        tr.missing_extra_fields = [f for f in tr.missing_extra_fields if f != 'discnumber']
                        tr.needs_extra_tags = bool(tr.missing_extra_fields)
                    _append_note(tr, '统一专辑碟号:1')


def touch_files_mtime(paths: list[str], min_mtimes: dict[str, float] | None = None) -> int:
    """推进文件修改时间，帮助各类本地播放器目录监听感知文件变更。"""
    touched = 0
    for p in paths:
        if not p or not os.path.isfile(p):
            continue
        prev = (min_mtimes or {}).get(os.path.normcase(os.path.abspath(p)), 0.0)
        try:
            bump_mtime(p, min_prev_mtime=prev)
            touched += 1
        except OSError:
            pass
    return touched


def scan_library(
    env=None,
    folders: list[str] | None = None,
    progress_cb=None,
    include_art_scan: bool = True,
    *,
    metadb_loader: Callable | None = None,
    diff_finder: Callable | None = None,
) -> list[LibraryTrack]:
    """扫描本地音乐目录并结合可选播放器缓存、封面与标签健康度判定待修状态。"""
    from art_repair import analyze_folder_art_and_tags

    env_folders = list(getattr(env, 'library_folders', ()) or ()) if env is not None else []
    target_folders = folders if folders is not None else env_folders
    metadb = metadb_loader(env) if (metadb_loader is not None and env is not None) else {}
    metadb_loaded = bool(metadb)

    audio_paths, seen = [], set()
    last_walk_t = 0.0
    for root in target_folders:
        if not root or not os.path.isdir(root):
            continue
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith('.')]
            if progress_cb:
                now = time.monotonic()
                if now - last_walk_t >= 0.08:
                    last_walk_t = now
                    progress_cb(len(audio_paths), 0, dp)
            for fn in sorted(fns):
                if os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
                    continue
                fp = os.path.abspath(os.path.join(dp, fn))
                norm = os.path.normcase(fp)
                if norm not in seen:
                    seen.add(norm)
                    audio_paths.append(fp)

    total = len(audio_paths)
    total_steps = total * (2 if include_art_scan else 1)
    infos_by_path, temp_dups, folder_clean = {}, {}, {}
    last_prog_t = 0.0

    max_scan_workers = min(16, max(2, (os.cpu_count() or 4) * 2))
    if total <= 8 or max_scan_workers <= 1:
        for idx, fp in enumerate(audio_paths, 1):
            if progress_cb:
                now = time.monotonic()
                if idx == 1 or idx == total or idx % 5 == 0 or (now - last_prog_t) >= 0.08:
                    last_prog_t = now
                    progress_cb(idx, total_steps, fp)
            infos_by_path[fp] = read_audio_info(fp)
            temp_dup = _is_temp_duplicate(fp)
            temp_dups[fp] = temp_dup
            if not temp_dup:
                folder_clean.setdefault(os.path.dirname(fp), []).append(fp)
    else:
        def _read_worker(fp):
            return fp, read_audio_info(fp), _is_temp_duplicate(fp)

        with ThreadPoolExecutor(max_workers=max_scan_workers) as pool:
            chunk = max(1, total // (max_scan_workers * 4))
            done_cnt = 0
            for fp, info, temp_dup in pool.map(_read_worker, audio_paths, chunksize=chunk):
                done_cnt += 1
                infos_by_path[fp] = info
                temp_dups[fp] = temp_dup
                if not temp_dup:
                    folder_clean.setdefault(os.path.dirname(fp), []).append(fp)
                if progress_cb:
                    now = time.monotonic()
                    if done_cnt == 1 or done_cnt == total or done_cnt % 10 == 0 or (now - last_prog_t) >= 0.08:
                        last_prog_t = now
                        progress_cb(done_cnt, total_steps, fp)

    folder_plans: dict[str, FolderArtPlan] = {}
    if include_art_scan:
        folder_items = list(folder_clean.items())
        done_art = 0
        last_art_t = 0.0
        if len(folder_items) > 4 and max_scan_workers > 1:
            def _folder_art_worker(item):
                f, c_paths = item
                return f, analyze_folder_art_and_tags(f, c_paths, pre_read_infos=infos_by_path)

            with ThreadPoolExecutor(max_workers=min(max_scan_workers, len(folder_items))) as f_pool:
                for f, f_plan in f_pool.map(_folder_art_worker, folder_items):
                    folder_plans[f] = f_plan
                    done_art += len(folder_clean[f])
                    if progress_cb and total > 0:
                        now = time.monotonic()
                        if done_art == total or (now - last_art_t) >= 0.08:
                            last_art_t = now
                            progress_cb(min(total + done_art, total_steps), total_steps, folder_clean[f][-1])
        else:
            for folder, clean_paths in folder_items:
                folder_plans[folder] = analyze_folder_art_and_tags(folder, clean_paths, pre_read_infos=infos_by_path)
                done_art += len(clean_paths)
                if progress_cb and total > 0:
                    now = time.monotonic()
                    if done_art == total or (now - last_art_t) >= 0.08:
                        last_art_t = now
                        progress_cb(min(total + done_art, total_steps), total_steps, clean_paths[-1])

    tracks: list[LibraryTrack] = []
    for fp in audio_paths:
        info = infos_by_path[fp]
        try:
            st = os.stat(fp)
            disk_size, disk_mtime = st.st_size, st.st_mtime
        except OSError:
            disk_size, disk_mtime = 0, 0.0

        norm = os.path.normcase(fp)
        fb = metadb.get(norm)
        full_album, temp_dup = _is_full_album_file(fp, info), temp_dups[fp]
        stale_reasons = (
            diff_finder(
                info, fb, disk_size, disk_mtime,
                in_monitored_library=is_under_folders(fp, env_folders),
                metadb_loaded=metadb_loaded, temp_dup=temp_dup,
            )
            if diff_finder is not None
            else []
        )

        f_plan = folder_plans.get(os.path.dirname(fp))
        t_plan = f_plan.track_plans.get(norm) if f_plan else None
        folder_cov_action = f_plan.cover_action if f_plan else 'ok'

        notes: list[str] = []
        if info.get('extension_mismatch'):
            notes.append(f"扩展名与真实格式({info.get('container')})不符")
        if full_album:
            notes.append('整轨全长文件(默认跳过)')
        if temp_dup:
            notes.append('疑似下载临时副本(正式文件已存在)')

        needs_track, needs_sync = False, False
        if info.get('error'):
            status = STATUS_ERROR
            notes.append(str(info['error']))
        elif not has_valid_tracknumber(info):
            status, needs_track = STATUS_MISSING_DISK, True
            if info.get('track_int') == 0:
                notes.append('音轨号为 0')
            if stale_reasons:
                needs_sync = True
                notes.append('foobar缓存:' + '、'.join(stale_reasons))
        elif stale_reasons:
            status, needs_sync = STATUS_STALE_FOOBAR, True
            notes.append('foobar缓存未刷新:' + '、'.join(stale_reasons))
        else:
            status = STATUS_OK

        cov_status = t_plan.cover_status if t_plan else '正常'
        cov_reasons = list(t_plan.cover_reasons) if t_plan else []
        needs_cov = bool(t_plan and t_plan.needs_cover_fix)
        needs_cov_online = bool(
            'no-cover-source' in cov_reasons
            and not info.get('error')
            and not temp_dup
            and info.get('container') in SUPPORTS_COVER_WRITE
        )
        if needs_cov_online:
            cov_status = '缺封面(待联网)'
        needs_tags = bool(t_plan and t_plan.needs_tag_fix)
        tag_updates = dict(t_plan.tag_updates) if t_plan else {}

        folder_art_action = f_plan.artist_action if f_plan else 'ok'
        artist_stat = t_plan.artist_status if t_plan else '正常'
        artist_reas = list(t_plan.artist_reasons) if t_plan else []
        has_art_art = bool(t_plan and t_plan.has_artist_art)
        needs_art_art = bool(
            not info.get('error')
            and not temp_dup
            and (
                (t_plan and t_plan.needs_artist_fix)
                or (not has_art_art and (info.get('container') in SUPPORTS_ARTIST_WRITE or folder_art_action in ('create', 'rewrite')))
            )
        )
        if not has_art_art and artist_stat == '正常':
            artist_stat = '缺艺术家图'

        missing_core: list[str] = []
        missing_extra: list[str] = []
        if not info.get('error') and not temp_dup:
            for f in CORE_TEXT_FIELDS:
                if not (info.get(f) or '').strip() and f not in tag_updates:
                    missing_core.append(f)
            for f in EXTRA_TEXT_FIELDS:
                if f == 'discnumber':
                    if not (info.get('disc_int') or (info.get('discnumber') or '').strip()):
                        missing_extra.append(f)
                elif not (info.get(f) or '').strip() and f not in tag_updates:
                    missing_extra.append(f)
        needs_date = 'date' in missing_core
        needs_extra = bool(missing_extra)

        if needs_cov:
            notes.append('封面:' + ','.join(cov_reasons))
        elif cov_status != '正常':
            notes.append(cov_status)
        if folder_cov_action in ('create', 'rewrite', 'remove') and f_plan and f_plan.detail:
            notes.append(f_plan.detail)
        if needs_art_art:
            if not has_art_art:
                notes.append('缺艺术家图')
            elif artist_reas:
                notes.append('艺术家图:' + ','.join(artist_reas))
        elif artist_stat != '正常':
            notes.append(artist_stat)
        if folder_art_action in ('create', 'rewrite', 'remove') and f_plan and f_plan.artist_detail:
            notes.append(f_plan.artist_detail)
        if needs_tags:
            notes.append('补标签:' + ','.join(f'{k}={v}' for k, v in tag_updates.items()))
        if missing_core:
            notes.append('缺核心:' + ','.join(FIELD_LABELS[f] for f in missing_core))
        if missing_extra:
            notes.append('缺扩展:' + ','.join(FIELD_LABELS[f] for f in missing_extra))

        tracks.append(LibraryTrack(
            path=fp, filename=os.path.basename(fp), folder=os.path.dirname(fp),
            title=info.get('title') or '', artist=info.get('artist') or '',
            albumartist=info.get('albumartist') or '', album=info.get('album') or '',
            date=info.get('date') or '', composer=info.get('composer') or '',
            performer=info.get('performer') or '', genre=info.get('genre') or '',
            duration=float(info.get('duration') or 0.0),
            disk_tracknumber=info.get('tracknumber') or '', disk_track_int=info.get('track_int'),
            disk_discnumber=info.get('discnumber') or '', disk_disc_int=info.get('disc_int'),
            comment=info.get('comment') or '', has_163_key=bool(info.get('has_163_key')),
            has_lyrics=bool(info.get('has_lyrics')),
            has_embedded_lyrics=bool(info.get('has_embedded_lyrics')),
            has_lrc_file=bool(info.get('has_lrc_file')),
            needs_lyrics=bool(not info.get('error') and not temp_dup and not info.get('has_lyrics')),
            netease_meta=info.get('netease_meta'), container=info.get('container'),
            extension_mismatch=bool(info.get('extension_mismatch')), error=info.get('error'),
            foobar_in_db=fb is not None, foobar_tracknumber=fb['tracknumber'] if fb else '',
            foobar_title=fb.get('title', '') if fb else '', foobar_artist=fb.get('artist', '') if fb else '',
            foobar_albumartist=fb.get('albumartist', '') if fb else '', foobar_album=fb.get('album', '') if fb else '',
            foobar_date=fb.get('date', '') if fb else '', foobar_composer=fb.get('composer', '') if fb else '',
            foobar_performer=fb.get('performer', '') if fb else '', foobar_genre=fb.get('genre', '') if fb else '',
            foobar_discnumber=fb.get('discnumber', '') if fb else '', foobar_comment=fb.get('comment', '') if fb else '',
            foobar_size=fb['size'] if fb else None,
            foobar_mtime=fb['mtime'] if fb else 0.0, foobar_stale_reasons=stale_reasons,
            disk_size=disk_size, disk_mtime=disk_mtime, status=status,
            is_full_album=full_album, is_temp_duplicate=temp_dup, note='；'.join(notes),
            needs_tracknumber=needs_track, needs_foobar_sync=needs_sync,
            needs_cover_fix=needs_cov, needs_cover_online=needs_cov_online,
            cover_status=cov_status, cover_reasons=cov_reasons,
            target_pic_bytes=t_plan.target_pic_bytes if t_plan else None,
            target_pic_mime=t_plan.target_pic_mime if t_plan else 'image/jpeg',
            has_artist_art=has_art_art,
            needs_artist_art=needs_art_art,
            artist_status=artist_stat,
            artist_reasons=artist_reas,
            target_artist_bytes=t_plan.target_artist_bytes if t_plan else None,
            target_artist_mime=t_plan.target_artist_mime if t_plan else 'image/jpeg',
            needs_tag_fix=needs_tags, tag_updates=tag_updates, needs_date=needs_date,
            missing_core_fields=missing_core, missing_extra_fields=missing_extra,
            needs_extra_tags=needs_extra,
            folder_cover_action=folder_cov_action, folder_artist_action=folder_art_action,
            folder_plan=f_plan,
        ))
    if include_art_scan:
        reconcile_album_consistency(tracks)
    return tracks
