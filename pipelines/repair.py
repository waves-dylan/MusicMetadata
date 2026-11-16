"""元数据自动修复、封面/艺术家写真补全、歌词下载与同步执行管线。"""

from __future__ import annotations

import os
import queue
import sys
import threading
import time
from collections import Counter

from core.constants import STATUS_ERROR
from core.models import FolderArtPlan, LibraryTrack, PlayerEnv, TrackMatch
from infra.integrations.musicbee import sync_artist_pictures_to_musicbee
from infra.scanner.local_scanner import track_needs_lyrics
from pipelines.executor import _run_pool_cancellable, _sync_player_targets
from pipelines.options import FixOptions
from providers.base import _VA_ARTISTS, first_artist, norm_core
from services.art import (
    ARTIST_MAX_SIDE,
    MAX_SIDE,
    SUPPORTS_ARTIST_WRITE,
    SUPPORTS_COVER_WRITE,
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
    get_discogs_token,
    get_fanart_keys,
    get_spotify_credentials,
    get_tadb_api_key,
    resolve_track_metadata,
    set_discogs_token,
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
    options: FixOptions | None = None,
    online_covers: dict[str, bytes] | None = None,
) -> tuple[bool, dict[str, bytes], int, int]:
    """批量或按组下载联网封面并为缺少外置 cover.jpg 的单专辑目录写入封面。"""
    if online_covers is None:
        online_covers = {}
    folder_cover_ok = folder_cover_fail = 0
    need_urls = {
        matches[tr.path].cover_url
        for tr in lookup_targets
        if tr.path in matches and matches[tr.path].cover_url
        and (tr.needs_cover_online or (tr.folder_plan and tr.folder_plan.cover_action == 'nodata'))
        and matches[tr.path].cover_url not in online_covers
    }
    if need_urls:
        cov_total, cov_done = len(need_urls), 0
        if cov_total > 1:
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
    elif is_cancel():
        return True, online_covers, 0, 0

    seen_nodata: dict[str, tuple[FolderArtPlan, bytes]] = {}
    for tr in targets:
        fp = tr.folder_plan
        if fp and fp.cover_action == 'nodata' and tr.folder not in seen_nodata and tr.folder not in handled_folders:
            m = matches.get(tr.path)
            cov = online_covers.get(m.cover_url) if (m and m.cover_url) else None
            if cov:
                # 场景 B 智能升级：真身大碟封面反哺（Reverse Metadata Cascade）
                # 默认关闭（Default OFF），开启需在主界面设置中明确授权并警告
                # 仅当开启且亲和度得分 >= 180.0 时，触发全自动反向注入生成 cover.jpg！
                has_explicit_album = bool((tr.album or '').strip())
                is_song_guess = bool(m and m.source and any(k in m.source for k in ('单曲反查', '单曲搜索')))
                aff_score = getattr(m, 'affinity_score', 0.0) or 0.0
                allow_cascade = bool(getattr(options, 'enable_reverse_cover_cascade', False)) if options is not None else False
                is_high_confidence = (aff_score >= 180.0) and allow_cascade

                if not has_explicit_album and is_song_guess and not is_high_confidence:
                    if (aff_score >= 180.0) and not allow_cascade:
                        emit_log(f'[目录封面跳过] {tr.folder} -> 智能加权命中母盘大碟《{m.album_name}》(亲和分: {aff_score:.1f})，但场景B全自动反哺默认关闭（需在界面勾选开启，或使用“挑选歌词/封面”手动挑选）')
                    else:
                        emit_log(f'[目录封面跳过] {tr.folder} -> 原始音频无确切专辑名称且单曲命中亲和分不足({aff_score:.1f}<180)，为防误写跳过自动生成（可使用“选择专辑封面...”手动挑选）')
                    continue
                if is_high_confidence and is_song_guess:
                    emit_log(f'[真身大碟封面反哺] {tr.folder} -> 智能加权高置信度命中母盘大碟《{m.album_name}》(亲和分: {aff_score:.1f})，反向注入生成 cover.jpg')
                seen_nodata[tr.folder] = (fp, cov)
    for folder, (fp, cov) in seen_nodata.items():
        if folder in handled_folders:
            continue
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
    artist_pictures_dir: str = '',
    online_artists: dict[str, bytes] | None = None,
) -> tuple[bool, dict[str, bytes], int, int]:
    """批量或按组下载联网艺术家图片并为缺少外置 artist.jpg 的单专辑目录写入图片，可选同步 MusicBee 中央写真库。"""
    if online_artists is None:
        online_artists = {}
    folder_artist_ok = folder_artist_fail = 0

    need_items: set[str] = set()
    for tr in targets:
        if not options.enable_artist_art or not tr.needs_artist_art:
            continue
        if tr.target_artist_bytes is not None:
            continue
        m = matches.get(tr.path)
        if m and m.artist_pic_url:
            if m.artist_pic_url not in online_artists:
                need_items.add(m.artist_pic_url)
        else:
            name = tr.albumartist or tr.artist or (m.albumartist if m else '') or (m.artist if m else '')
            name = first_artist(name) or str(name or '').strip()
            if name and norm_core(name) not in _VA_ARTISTS and name not in online_artists:
                need_items.add(name)

    for tr in targets:
        fp = tr.folder_plan
        if fp and fp.artist_action == 'nodata' and f'{tr.folder}:artist' not in handled_folders:
            m = matches.get(tr.path)
            if m and m.artist_pic_url:
                if m.artist_pic_url not in online_artists:
                    need_items.add(m.artist_pic_url)
            else:
                name = first_artist(tr.albumartist or tr.artist)
                if name and norm_core(name) not in _VA_ARTISTS and name not in online_artists:
                    need_items.add(name)

    if need_items:
        art_total, art_done = len(need_items), 0
        if art_total > 1:
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
    elif is_cancel():
        return True, online_artists, 0, 0

    if options.sync_musicbee_artist and artist_pictures_dir:
        for item, pic_data in online_artists.items():
            if not item.startswith('http') and pic_data:
                dir_jpg = normalize_artist_jpeg(pic_data, ARTIST_MAX_SIDE)
                if dir_jpg:
                    saved = sync_artist_pictures_to_musicbee(artist_pictures_dir, item, dir_jpg)
                    if saved:
                        emit_log(f'[MusicBee艺人图] 已同步中央写真库: {item}')

    seen_nodata: dict[str, tuple[FolderArtPlan, bytes]] = {}
    for tr in targets:
        fp = tr.folder_plan
        f_key = f'{tr.folder}:artist'
        if fp and fp.artist_action == 'nodata' and tr.folder not in seen_nodata and f_key not in handled_folders:
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
        f_key = f'{folder}:artist'
        if f_key in handled_folders:
            continue
        dir_jpg = normalize_artist_jpeg(pic, ARTIST_MAX_SIDE)
        if not dir_jpg:
            continue
        try:
            art_out_path = getattr(fp, 'artist_cover_path', '') or getattr(fp, 'artist_path', '') or os.path.join(folder, 'artist.jpg')
            with open(art_out_path, 'wb') as out_fp:
                out_fp.write(dir_jpg)
            fp.artist_action = 'ok'
            folder_artist_ok += 1
            handled_folders.add(f_key)
            emit_log(f'[目录艺术家图] {folder} -> 已联网生成 artist.jpg')
            if options.sync_musicbee_artist and artist_pictures_dir:
                art_names: list[str] = []
                for tr in targets:
                    if tr.folder == folder:
                        m = matches.get(tr.path)
                        n = tr.albumartist or tr.artist or (m.albumartist if m else '') or (m.artist if m else '')
                        if n and n not in art_names:
                            art_names.append(n)
                if not art_names and os.path.basename(folder):
                    art_names.append(os.path.basename(folder))
                if art_names:
                    sync_artist_pictures_to_musicbee(artist_pictures_dir, art_names, dir_jpg)
        except Exception as err:
            folder_artist_fail += 1
            emit_log(f'[目录艺术家图失败] {folder} -> {err}')

    # 针对已有外置 artist.jpg 的目录，若开启了同步 MusicBee 写真库，确保其也镜像同步进中央写真库
    if options.sync_musicbee_artist and artist_pictures_dir:
        seen_existing_sync: set[str] = set()
        for tr in targets:
            if tr.folder not in seen_existing_sync:
                seen_existing_sync.add(tr.folder)
                local_art = os.path.join(tr.folder, 'artist.jpg')
                if os.path.isfile(local_art):
                    m = matches.get(tr.path)
                    name = tr.albumartist or tr.artist or (m.albumartist if m else '') or (m.artist if m else '') or os.path.basename(tr.folder)
                    if name and norm_core(name) not in _VA_ARTISTS:
                        try:
                            with open(local_art, 'rb') as fp_art:
                                exist_bytes = fp_art.read()
                            if exist_bytes:
                                norm_jpg = normalize_artist_jpeg(exist_bytes, ARTIST_MAX_SIDE) or exist_bytes
                                sync_artist_pictures_to_musicbee(artist_pictures_dir, [name], norm_jpg)
                        except OSError:
                            pass

    return False, online_artists, folder_artist_ok, folder_artist_fail


def execute_repairs(
    env: PlayerEnv,
    targets: list[LibraryTrack],
    options: FixOptions,
    progress_cb=None,
    row_cb=None,
    log_cb=None,
    is_cancelled_cb=None,
) -> dict:
    """统一执行元数据检查刷新、封面修复、标签补全、歌词抓取与音轨号补全（GUI 与 CLI 共用）。"""
    p_name = getattr(env, 'player_name', '播放器')
    art_mb_dir = options.musicbee_artist_dir or getattr(env, 'artist_pictures_dir', '')
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
    if options.discogs_token is not None:
        set_discogs_token(options.discogs_token)
    clear_caches()
    total, cancelled = len(targets), False
    emit_log = log_cb or (lambda _m: None)
    emit_row = row_cb or (lambda _p, _d: None)
    is_cancel = is_cancelled_cb or (lambda: False)

    written_ok = written_fail = unmatched = date_unmatched_cnt = cover_fixed = tags_fixed = date_fixed = lyrics_fixed = lyrics_unmatched_cnt = folder_cover_ok = folder_cover_fail = synced_stale = 0
    artist_fixed = folder_artist_ok = folder_artist_fail = 0
    synced_paths: list[str] = []
    player_mtimes: dict[str, float] = {}

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
            player_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.cached_mtime, tr.disk_mtime)
            if options.dry_run:
                emit_row(
                    tr.path,
                    {
                        'status_text': '待刷新',
                        'source_note': f'预演：通知 {p_name} 重新载入元数据',
                        'color': 'info',
                        'fixed_sync': False,
                    },
                )
                emit_log(f"[预演] {tr.filename} -> 刷新 {p_name} 元数据缓存")
        touched, reloaded = (0, False)
        if not options.dry_run and synced_paths and not cancelled:
            touched, reloaded = _sync_player_targets(env, synced_paths, player_mtimes, emit_log, log_noun='文件')
            success = reloaded if (p_name.lower().startswith('foobar') or getattr(env, 'player_type', '') == 'foobar') else bool(touched > 0 or reloaded)
            for tr in targets:
                if tr.path in synced_paths:
                    if success:
                        emit_row(
                            tr.path,
                            {
                                'status_text': '已刷新',
                                'source_note': f'已通知 {p_name} 重新载入元数据',
                                'color': 'ok',
                                'fixed_sync': True,
                            },
                        )
                        emit_log(f"[刷新] {tr.filename} -> 已刷新 {p_name} 元数据缓存")
                    else:
                        emit_row(
                            tr.path,
                            {
                                'status_text': '未刷新',
                                'source_note': f'{p_name} 未在运行，未能重载缓存',
                                'color': 'warn',
                                'fixed_sync': False,
                            },
                        )
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

    def _target_group_key(tr: LibraryTrack) -> tuple:
        folder = tr.folder or os.path.dirname(tr.path)
        if tr.folder_plan and tr.folder_plan.album_count == 1:
            return (folder, '__single__')
        alb = (tr.album or '').strip().lower()
        if alb:
            return (folder, alb)
        return (folder, f'__track__{os.path.normcase(os.path.abspath(tr.path))}')

    class _RepairGroup:
        def __init__(self, key: tuple, folder: str):
            self.key = key
            self.folder = folder
            self.tracks: list[LibraryTrack] = []
            self.lookup_tracks: list[LibraryTrack] = []
            self.pending_lookups: int = 0

    groups_map: dict[tuple, _RepairGroup] = {}
    ordered_groups: list[_RepairGroup] = []
    for tr in targets:
        gk = _target_group_key(tr)
        if gk not in groups_map:
            grp = _RepairGroup(gk, tr.folder)
            groups_map[gk] = grp
            ordered_groups.append(grp)
        groups_map[gk].tracks.append(tr)

    lookup_targets = [
        t for t in targets
        if not t.is_temp_duplicate and t.status != STATUS_ERROR and any(_lookup_flags(t)[:7])
    ]
    lookup_paths = {t.path for t in lookup_targets}
    track_to_group: dict[str, _RepairGroup] = {}
    ready_queue: queue.Queue[_RepairGroup] = queue.Queue()
    grp_lock = threading.Lock()

    for grp in ordered_groups:
        grp.lookup_tracks = [t for t in grp.tracks if t.path in lookup_paths]
        grp.pending_lookups = len(grp.lookup_tracks)
        for t in grp.tracks:
            track_to_group[t.path] = grp
        if grp.pending_lookups == 0:
            ready_queue.put(grp)

    lookup_total = len(lookup_targets)
    done_cnt = hit_cnt = 0
    lookup_t0 = last_log_t = time.monotonic()
    resolve_fn = resolve_hook('resolve_track_metadata', resolve_track_metadata)
    lookup_cancelled = False

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
        if getattr(options, 'prefer_rich_lyrics', False):
            kwargs['prefer_rich_lyrics'] = True
        if options.prefer_sources:
            kwargs['prefer_sources'] = options.prefer_sources
        if options.only_sources:
            kwargs['only_sources'] = options.only_sources
        if options.skip_sources:
            kwargs['skip_sources'] = options.skip_sources
        return resolve_fn(tr, **kwargs)

    def _on_lookup_done(tr: LibraryTrack, fut):
        nonlocal done_cnt, hit_cnt, last_log_t
        with grp_lock:
            done_cnt += 1
            try:
                m = fut.result()
            except Exception as exc:
                m = TrackMatch(None, reason=f'查询异常: {exc}')
            matches[tr.path] = m
            if m.source or m.tracknumber or m.release_date or m.cover_url or m.artist_pic_url or m.genre or m.composer or m.lyricist or m.arranger or m.lyrics:
                hit_cnt += 1
            if progress_cb:
                progress_cb(done_cnt, lookup_total, f'联网查询: {tr.filename}')
            now = time.monotonic()
            if done_cnt == 1 or done_cnt % 25 == 0 or done_cnt == lookup_total or (now - last_log_t) >= 3.0:
                last_log_t = now
                emit_log(f'联网查询进度：{done_cnt}/{lookup_total}（已命中 {hit_cnt} 首）...')

            grp = track_to_group.get(tr.path)
            if grp:
                grp.pending_lookups -= 1
                if grp.pending_lookups == 0:
                    ready_queue.put(grp)

    def _run_lookups():
        nonlocal lookup_cancelled
        if lookup_targets and not is_cancel():
            emit_log(f'开始联网查询并合并 {lookup_total} 首曲目元数据...')
            if progress_cb:
                progress_cb(0, lookup_total, '正在联网查询元数据...')
            lookup_cancelled = _run_pool_cancellable(
                lookup_targets,
                _submit_lookup,
                _on_lookup_done,
                max_workers=16,
                is_cancel=is_cancel,
            )
            if not lookup_cancelled:
                emit_log(f'联网查询完成：共 {lookup_total} 首，命中 {hit_cnt} 首，耗时 {time.monotonic() - lookup_t0:.1f}s。')

    lookup_thread = threading.Thread(target=_run_lookups, daemon=True)
    if lookup_targets and not cancelled:
        lookup_thread.start()

    online_covers: dict[str, bytes] = {}
    online_artists: dict[str, bytes] = {}

    def _execute_track_action(tr: LibraryTrack) -> dict:
        is_dff = (tr.container == 'dff')
        can_write_cover = (tr.container in SUPPORTS_COVER_WRITE)
        want_track = bool(options.enable_track and tr.needs_tracknumber)
        want_date_online = bool('date' in allowed_tags and tr.needs_date and 'date' not in tr.tag_updates)
        want_cover_online = options.enable_art and tr.needs_cover_online
        want_lyrics = bool(options.enable_lyrics and track_needs_lyrics(tr, options.lyrics_mode))
        match = matches.get(tr.path)
        matched_tn = match.tracknumber if (want_track and match and match.tracknumber) else None
        matched_date = match.release_date if (want_date_online and match and match.release_date) else ''
        matched_cov_url = match.cover_url if (want_cover_online and match and match.cover_url) else ''
        matched_lyrics = match.lyrics.strip() if (want_lyrics and match and match.lyrics) else ''
        if want_lyrics and not matched_lyrics:
            # 零网络本地互转快车道：已有内嵌或外置歌词时，直接复用已有歌词完成目标模式补全，无需联网
            if tr.has_embedded_lyrics or tr.has_lrc_file or getattr(tr, 'lyrics', None):
                if getattr(tr, 'lyrics', None):
                    matched_lyrics = str(tr.lyrics or '').strip()
                else:
                    from tag_io import read_audio_info
                    matched_lyrics = str(read_audio_info(tr.path).get('lyrics') or '').strip()
        online_cov_bytes = online_covers.get(matched_cov_url) if matched_cov_url else None
        track_unmatched = bool(want_track and not matched_tn)
        date_unmatched = bool(want_date_online and not matched_date)
        lyrics_unmatched = bool(want_lyrics and not matched_lyrics)

        want_cover = bool(options.enable_art and (
            (tr.needs_cover_fix and tr.target_pic_bytes is not None)
            or (want_cover_online and (online_cov_bytes is not None or (options.dry_run and bool(matched_cov_url))))
        ))
        cov_bytes_to_write = tr.target_pic_bytes if (options.enable_art and tr.needs_cover_fix) else online_cov_bytes
        cov_mime_to_write = tr.target_pic_mime if (options.enable_art and tr.needs_cover_fix) else 'image/jpeg'

        effective_tags = {k: v for k, v in tr.tag_updates.items() if k in allowed_tags} if allowed_tags else {}
        if allowed_tags and match:
            for fk, fv in {'title': match.title, 'artist': match.artist, 'albumartist': match.albumartist, 'album': match.album_name, 'date': match.release_date}.items():
                if fv and fk in allowed_tags and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv
            for fk, fv in {'composer': match.composer, 'lyricist': match.lyricist, 'arranger': match.arranger, 'genre': match.genre}.items():
                if fv and fk in allowed_tags and not (getattr(tr, fk, '') or '').strip() and fk not in effective_tags:
                    effective_tags[fk] = fv

        allow_disc = bool('discnumber' in allowed_tags)
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
        want_sync = options.enable_sync and tr.needs_player_sync
        folder_done = tr.folder in handled_folders
        has_file_write = bool(matched_tn or matched_dn or want_cover or want_artist_write or want_tags or want_write_lyrics)

        ok, msg = False, ''
        if has_file_write and not options.dry_run:
            repair_kwargs = {
                'tag_updates': effective_tags if want_tags else None,
                'cover_bytes': (cov_bytes_to_write if can_write_cover else None) if want_cover else None,
                'cover_mime': cov_mime_to_write,
                'artist_bytes': (artist_bytes_to_write if can_write_artist else None) if want_artist_write else None,
                'artist_mime': artist_mime_to_write,
                'tracknumber': matched_tn,
                'discnumber': matched_dn,
                'overwrite_track': (tr.disk_track_int == 0),
            }
            if want_write_lyrics:
                repair_kwargs['lyrics'] = matched_lyrics
                repair_kwargs['lyrics_mode'] = options.lyrics_mode
                repair_kwargs['lrc_title'] = (match.title if match else '') or tr.title or ''
                repair_kwargs['lrc_artist'] = (match.artist if match else '') or tr.artist or ''
                repair_kwargs['lrc_album'] = (match.album_name if match else '') or tr.album or ''
            ok, msg = apply_track_repairs(tr.path, **repair_kwargs)
            if want_artist_write and artist_bytes_to_write and options.sync_musicbee_artist and art_mb_dir:
                names = [n for n in (tr.albumartist, tr.artist, match.albumartist if match else '', match.artist if match else '') if n]
                if names:
                    sync_artist_pictures_to_musicbee(art_mb_dir, names, artist_bytes_to_write)

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
        is_dff = (tr.container == 'dff')
        unmatched += int(res['track_unmatched'])
        date_unmatched_cnt += int(res['date_unmatched'])
        lyrics_unmatched_cnt += int(res['lyrics_unmatched'])
        match = matches.get(tr.path)
        matched_tn = res['matched_tn']
        matched_dn = res['matched_dn']
        folder_done = res['folder_done']

        if options.dry_run:
            pre = []
            if is_dff and (matched_tn or matched_dn or res['want_tags'] or res['want_cover']):
                tag_hint = f"拟补:{','.join(res['effective_tags'].keys())}" if res['want_tags'] else ''
                pre.append(f"DFF跳过内嵌写标(遵循发烧规范保护，不支持直接内嵌写标，建议使用顶部【格式转换】转封装为.dsf{('；' + tag_hint) if tag_hint else ''})")
            else:
                if matched_tn:
                    pre.append(f'音轨号->{matched_tn}({match.source})')
                elif res['track_unmatched']:
                    pre.append(f'未匹配音轨({match.reason if match else "未知"})')
                if matched_dn:
                    pre.append(f'碟号->{matched_dn}')
                if res['want_cover']:
                    pre.append('联网补封面' if res['want_cover_online'] else '修复封面')
                if res['want_tags']:
                    pre.append('补标签:' + ','.join(f'{k}={v}' for k, v in res['effective_tags'].items()))
            if res['want_artist_write'] and not is_dff:
                pre.append('修复艺术家图' if tr.has_artist_art else '补艺术家图')
            if res['want_write_lyrics']:
                if not tr.has_lrc_file and options.lyrics_mode in ('lrc', 'both'):
                    pre.append('导出外置.lrc')
                elif not tr.has_embedded_lyrics and options.lyrics_mode in ('embed', 'both'):
                    pre.append('注入内嵌歌词')
                else:
                    pre.append(f'补歌词({options.lyrics_mode})')
            elif res['lyrics_unmatched']:
                pre.append('未匹配歌词')
            if res['date_unmatched']:
                pre.append(f'未匹配日期({match.reason if match else "未知"})')
            if res['want_sync']:
                pre.append(f'刷新{p_name}缓存')
            if folder_done:
                pre.append('已含目录cover.jpg')
            if res['has_file_write']:
                written_ok += 1
                if not is_dff:
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
                    'matched_track': matched_tn if not is_dff else None,
                    'source_note': '；'.join(pre) or '无待修项目',
                    'color': 'warn' if (is_dff or ((res['track_unmatched'] or res['date_unmatched'] or res['lyrics_unmatched']) and not (matched_dn or res['want_cover'] or res['want_artist_write'] or res['want_tags'] or res['want_write_lyrics'] or res['want_sync']))) else 'info',
                },
            )
            emit_log(f"[预演] {tr.filename} -> {'；'.join(pre) or '无需改动'}")
            return

        if res['has_file_write']:
            if res['ok']:
                written_ok += 1
                if not is_dff:
                    cover_fixed += int(res['want_cover'])
                    artist_fixed += int(res['want_artist_write'])
                    tags_fixed += int(res['want_tags'] or bool(res['planned_dn']))
                    date_fixed += int(bool(res['want_tags'] and 'date' in res['effective_tags']))
                lyrics_fixed += int(res['want_write_lyrics'])
                if options.enable_sync:
                    synced_paths.append(tr.path)
                    player_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.cached_mtime, tr.disk_mtime)
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
                        'matched_track': matched_tn if not is_dff else None,
                        'current_track': (str(matched_tn) if matched_tn else None) if not is_dff else None,
                        'current_disc': (str(matched_dn) if matched_dn else None) if not is_dff else None,
                        'source_note': note_str,
                        'color': 'ok',
                        'fixed_track': bool(matched_tn and not is_dff),
                        'fixed_disc': bool(matched_dn and not is_dff),
                        'fixed_cover': bool(res['want_cover'] and not is_dff),
                        'fixed_cover_online': bool(res['want_cover'] and res['want_cover_online'] and not is_dff),
                        'fixed_artist_art': bool(res['want_artist_write'] and not is_dff),
                        'fixed_tags': bool(res['want_tags'] and not is_dff),
                        'written_tags': (res['effective_tags'] if res['want_tags'] else {}) if not is_dff else {},
                        'fixed_lyrics': res['want_write_lyrics'],
                        'lyrics_mode': options.lyrics_mode,
                        'fixed_sync': options.enable_sync,
                        'fixed_folder_cover': folder_done,
                    },
                )
                emit_log(f'[已修复] {tr.filename} -> {note_str}')
            else:
                written_fail += 1
                is_dff_protected = bool(is_dff or ('DFF' in str(res['msg'])))
                st_text = '只读保护' if is_dff_protected else '写入失败'
                st_color = 'warn' if is_dff_protected else 'err'
                emit_row(tr.path, {'status_text': st_text, 'matched_track': matched_tn, 'source_note': res['msg'], 'color': st_color})
                emit_log(f"[{st_text if is_dff_protected else '失败'}] {tr.filename} -> {res['msg']}")
        elif res['want_sync']:
            synced_stale += 1
            synced_paths.append(tr.path)
            player_mtimes[os.path.normcase(os.path.abspath(tr.path))] = max(tr.cached_mtime, tr.disk_mtime)
            desc = '、'.join(tr.player_stale_reasons) if tr.player_stale_reasons else '缓存过期'
            emit_row(
                tr.path,
                {
                    'status_text': '已同步',
                    'source_note': f'已触发 {p_name} 刷新（{desc}）',
                    'color': 'ok',
                    'fixed_sync': True,
                    'fixed_folder_cover': folder_done,
                },
            )
            emit_log(f'[同步] {tr.filename} -> 触发 {p_name} 刷新（{desc}）')
        elif res['track_unmatched'] or res['date_unmatched'] or res['lyrics_unmatched']:
            reason = match.reason if match else ('未找到匹配音轨号' if res['track_unmatched'] else ('未找到匹配发布日期' if res['date_unmatched'] else '未找到匹配歌词'))
            emit_row(tr.path, {'status_text': '未匹配', 'source_note': reason, 'color': 'warn', 'fixed_folder_cover': folder_done})
            emit_log(f'[未匹配] {tr.filename}（{reason}）')
        elif folder_done:
            emit_row(tr.path, {'status_text': '目录已修', 'source_note': '目录外置 cover.jpg 已处理', 'color': 'ok', 'fixed_folder_cover': True})

    total_groups = len(ordered_groups)
    processed_groups = 0
    done_write_cnt = 0

    while processed_groups < total_groups:
        if is_cancel() or cancelled:
            cancelled = True
            break
        try:
            grp = ready_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        # 1. 组内元数据传播与多数决推断
        if matches and grp.lookup_tracks and not cancelled:
            _propagate_album_group_metadata(grp.tracks, grp.lookup_tracks, matches, allowed_tags, options.enable_art)

        # 2. 组内封面下载与外置封面处理
        if options.enable_art and not options.dry_run and not cancelled:
            cov_cancel, _, cov_ok_cnt, cov_fail_cnt = _download_and_apply_online_covers(
                grp.tracks, grp.lookup_tracks, matches, handled_folders, None, emit_log, is_cancel,
                options=options,
                online_covers=online_covers,
            )
            cancelled = cancelled or cov_cancel
            folder_cover_ok += cov_ok_cnt
            folder_cover_fail += cov_fail_cnt

        # 3. 组内艺人写真下载与外置写真处理
        if options.enable_artist_art and not options.dry_run and not cancelled:
            art_cancel, _, art_ok_cnt, art_fail_cnt = _download_and_apply_online_artist_pics(
                grp.tracks, grp.lookup_tracks, matches, handled_folders, None, emit_log, is_cancel, options,
                artist_pictures_dir=art_mb_dir,
                online_artists=online_artists,
            )
            cancelled = cancelled or art_cancel
            folder_artist_ok += art_ok_cnt
            folder_artist_fail += art_fail_cnt

        # 4. 组内音频文件写入与行状态更新
        for tr in grp.tracks:
            if is_cancel() or cancelled:
                cancelled = True
                break
            if tr.status == STATUS_ERROR or tr.is_temp_duplicate:
                continue
            try:
                res = _execute_track_action(tr)
            except Exception as err:
                res = {
                    'want_track': False,
                    'want_date_online': False,
                    'want_cover_online': False,
                    'want_lyrics': False,
                    'matched_tn': None,
                    'matched_date': '',
                    'matched_cov_url': '',
                    'matched_lyrics': '',
                    'track_unmatched': False,
                    'date_unmatched': False,
                    'lyrics_unmatched': False,
                    'want_cover': False,
                    'effective_tags': {},
                    'planned_dn': None,
                    'matched_dn': None,
                    'want_artist_write': False,
                    'want_tags': False,
                    'want_write_lyrics': False,
                    'want_sync': False,
                    'folder_done': False,
                    'has_file_write': True,
                    'ok': False,
                    'msg': str(err),
                }
            _apply_track_result(tr, res)
            done_write_cnt += 1
            if progress_cb:
                progress_cb(done_write_cnt, total, tr.filename)

        processed_groups += 1

    if lookup_targets and lookup_thread.is_alive():
        lookup_thread.join()
    cancelled = cancelled or lookup_cancelled or is_cancel()

    touched, reloaded = (0, False)
    if not options.dry_run and synced_paths and not cancelled:
        touched, reloaded = _sync_player_targets(env, synced_paths, player_mtimes, emit_log)

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
