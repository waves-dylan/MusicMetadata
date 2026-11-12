"""MusicMetadata 命令行参数解析与批处理入口（供 CLI 模式与无界面自动化调用）。"""

from __future__ import annotations

import argparse
import os
import sys

from art_repair import DELETABLE_FIELDS, LYRICS_MODES, parse_set_tags
from foobar_library import FIELD_LABELS, STATUS_ERROR, detect_foobar_env, scan_library
from providers import ONLINE_PROVIDERS
from runner import (
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SetOptions,
    default_checked,
    execute_convert,
    execute_deletions,
    execute_repairs,
    execute_set_tags,
    parse_delete_fields,
)
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg
from track_lookup import parse_source_list

_ORIG_CLI_HOOKS = {
    'detect_foobar_env': detect_foobar_env,
    'scan_library': scan_library,
    'find_ffmpeg': find_ffmpeg,
    'execute_repairs': execute_repairs,
    'execute_deletions': execute_deletions,
    'execute_set_tags': execute_set_tags,
    'execute_convert': execute_convert,
}


def _cli_hook(name: str, current):
    app_mod = sys.modules.get('app')
    if app_mod is not None and name in _ORIG_CLI_HOOKS:
        val = getattr(app_mod, name, current)
        if val is not _ORIG_CLI_HOOKS[name]:
            return val
    return current


def _prepare_cli_scan(args, *, include_art_scan: bool, show_unavailable: bool = False):
    env = _cli_hook('detect_foobar_env', detect_foobar_env)()
    custom = [os.path.abspath(args.folder)] if args.folder else None
    print('foobar 目录:', env.fb_dir or '未检测到')
    print('媒体库目录:', custom or env.library_folders)
    if show_unavailable and not custom and env.unavailable_folders:
        print('不可访问的媒体库目录:', env.unavailable_folders)
    tracks = _cli_hook('scan_library', scan_library)(env, folders=custom, include_art_scan=include_art_scan)
    skip_full = not args.include_full_album
    return env, tracks, skip_full


def _active_targets(tracks, skip_full: bool) -> list:
    return [
        t for t in tracks
        if t.status != STATUS_ERROR and not t.is_temp_duplicate and not (skip_full and t.is_full_album)
    ]


def _run_set_cli(args) -> int:
    tag_values, err = parse_set_tags(args.set_tags)
    if err or not tag_values:
        print(err or '请通过 --set 指定有效的 key=value 标签', file=sys.stderr)
        return 2
    env, tracks, skip_full = _prepare_cli_scan(args, include_art_scan=False)
    targets = _active_targets(tracks, skip_full)
    set_opts = SetOptions(
        tag_values=tag_values,
        protect_163_key=not getattr(args, 'no_protect_163', False),
        enable_sync=not args.no_sync,
        dry_run=args.dry_run,
    )
    print(f"共扫描 {len(tracks)} 首，将对 {len(targets)} 首曲目设置标签：{', '.join(f'{k}={v}' for k, v in tag_values.items())}。")
    if not targets:
        print('没有可处理的曲目。')
        return 0
    s = _cli_hook('execute_set_tags', execute_set_tags)(env, targets, set_opts, log_cb=print)
    prefix = '[预演] 完成：预计修改' if args.dry_run else '完成：已修改'
    print(f"{prefix} {s['set_ok']} 首，跳过 {s['set_skipped']} 首，失败 {s['set_fail']} 项。")
    return 0 if s['set_fail'] == 0 else 1


def _run_convert_cli(args) -> int:
    fmt = str(args.convert or '').strip().lower().lstrip('.')
    if fmt not in CONVERT_TARGET_FORMATS:
        print(f"不支持的目标格式: {fmt}；可选: {', '.join(CONVERT_TARGET_FORMATS)}", file=sys.stderr)
        return 2
    if not args.dry_run and not _cli_hook('find_ffmpeg', find_ffmpeg)():
        print('未检测到 ffmpeg，请先安装 ffmpeg 并加入系统 PATH。', file=sys.stderr)
        return 2
    env, tracks, skip_full = _prepare_cli_scan(args, include_art_scan=False)
    targets = _active_targets(tracks, skip_full)
    conv_opts = ConvertOptions(
        target_format=fmt,
        overwrite=bool(getattr(args, 'force', False)),
        delete_source=bool(getattr(args, 'delete_source', False)),
        enable_sync=not args.no_sync,
        dry_run=args.dry_run,
    )
    print(f'共扫描 {len(tracks)} 首，将检查 {len(targets)} 首曲目转换为 .{fmt}。')
    if not targets:
        print('没有可处理的曲目。')
        return 0
    s = _cli_hook('execute_convert', execute_convert)(env, targets, conv_opts, log_cb=print)
    if s.get('error'):
        print(s['error'], file=sys.stderr)
        return 1
    prefix = '[预演] 完成：预计转换' if args.dry_run else '完成：已转换'
    print(f"{prefix} {s['conv_ok']} 首，跳过 {s['conv_skipped']} 首，失败 {s['conv_fail']} 项。")
    return 0 if s['conv_fail'] == 0 else 1


def _run_delete_cli(args) -> int:
    del_fields, invalid = parse_delete_fields(args.delete)
    if invalid:
        print(f"未知的待删除元数据类别: {', '.join(invalid)}；可选: {', '.join(DELETABLE_FIELDS)}", file=sys.stderr)
        return 2
    if not del_fields:
        print(f"请通过 --delete 指定至少一个元数据类别；可选: {', '.join(DELETABLE_FIELDS)}", file=sys.stderr)
        return 2
    if getattr(args, 'delete_folder_cover', False) and 'cover' not in del_fields:
        print('--delete-folder-cover 需与 --delete cover 配合使用。', file=sys.stderr)
        return 2

    env, tracks, skip_full = _prepare_cli_scan(args, include_art_scan=('cover' in del_fields), show_unavailable=True)
    targets = _active_targets(tracks, skip_full)
    del_opts = DeleteOptions(
        fields=frozenset(del_fields),
        delete_folder_cover=bool(getattr(args, 'delete_folder_cover', False)),
        delete_lrc_file=not getattr(args, 'no_delete_lrc', False),
        protect_163_key=not getattr(args, 'no_protect_163', False),
        enable_sync=not args.no_sync,
        dry_run=args.dry_run,
    )
    lbls = '、'.join(FIELD_LABELS.get(f, f) for f in del_fields)
    print(f'共扫描 {len(tracks)} 首，将检查 {len(targets)} 首曲目的待删除类别：[{lbls}]。')
    if not targets:
        print('没有可处理的曲目。')
        return 0
    s = _cli_hook('execute_deletions', execute_deletions)(env, targets, del_opts, log_cb=print)
    prefix = '[预演] 完成：预计修改' if args.dry_run else '完成：已修改'
    fc_part = f"，目录cover.jpg {s['folder_cover_deleted']} 个" if s.get('folder_cover_deleted') else ''
    print(f"{prefix} {s['deleted_ok']} 首{fc_part}，跳过 {s['deleted_skipped']} 首，失败 {s['deleted_fail'] + s['folder_cover_fail']} 项。")
    return 0 if (s['deleted_fail'] + s['folder_cover_fail']) == 0 else 1


def run_cli(args) -> int:
    if getattr(args, 'set_tags', None):
        return _run_set_cli(args)
    if getattr(args, 'convert', ''):
        return _run_convert_cli(args)
    if getattr(args, 'delete', None):
        return _run_delete_cli(args)

    prefer_sources, inv_pref = parse_source_list(getattr(args, 'prefer', None))
    only_sources, inv_only = parse_source_list(getattr(args, 'source', None))
    skip_sources, inv_skip = parse_source_list(getattr(args, 'skip_source', None))
    invalid_srcs = inv_pref + inv_only + inv_skip
    if invalid_srcs:
        print(f"未知的音源名称: {', '.join(invalid_srcs)}；可选: {', '.join(ONLINE_PROVIDERS)}", file=sys.stderr)
        return 2

    lyrics_mode = 'lrc' if getattr(args, 'external_lrc', False) else (getattr(args, 'lyrics_mode', 'auto') or 'auto')
    enable_lyrics = bool((getattr(args, 'lyrics', False) or getattr(args, 'external_lrc', False)) and not getattr(args, 'no_lyrics', False))

    env, tracks, skip_full = _prepare_cli_scan(args, include_art_scan=(not args.no_art or not args.no_tags), show_unavailable=True)
    opts = FixOptions(
        enable_track=not args.no_track, enable_art=not args.no_art,
        enable_tags=not args.no_tags, enable_extra_tags=not args.no_extra_tags,
        enable_date=not args.no_date, enable_albumartist=not args.no_albumartist,
        enable_artist=not args.no_artist, enable_composer=not args.no_composer,
        enable_genre=not args.no_genre, enable_disc=not args.no_disc,
        enable_lyrics=enable_lyrics, lyrics_mode=lyrics_mode,
        prefer_sources=prefer_sources, only_sources=only_sources, skip_sources=skip_sources,
        netease_api=(getattr(args, 'netease_api', '') or '').strip() or None,
        spotify_client_id=(getattr(args, 'spotify_client_id', None) or None),
        spotify_client_secret=(getattr(args, 'spotify_client_secret', None) or None),
        enable_sync=not args.no_sync, sync_only=args.sync_only, dry_run=args.dry_run,
    )
    if opts.enable_tags and not any((opts.enable_date, opts.enable_albumartist, opts.enable_artist, opts.enable_composer, opts.enable_genre, opts.enable_disc)):
        opts.enable_tags = False

    targets = [t for t in tracks if default_checked(t, skip_full_album=skip_full, options=opts)]
    allowed = opts.active_tag_fields()
    datec = sum(1 for t in targets if 'date' in allowed and t.needs_date and not (skip_full and t.is_full_album) and not t.is_temp_duplicate)
    tagc = sum(
        1 for t in targets
        if any(k in allowed for k in t.tag_updates) or (t.target_disc and 'discnumber' in allowed)
        or ((any(f in allowed for f in t.missing_core_fields) or ('date' in allowed and t.needs_date) or any(f in allowed for f in t.missing_extra_fields))
            and not (skip_full and t.is_full_album) and not t.is_temp_duplicate)
    )
    lrc_part = f'，缺歌词 {sum(1 for t in targets if t.needs_lyrics)}' if enable_lyrics else ''
    print(
        f'共扫描 {len(tracks)} 首，待处理 {len(targets)} 首（缺音轨 {sum(1 for t in targets if t.needs_tracknumber)}，'
        f'封面待修 {sum(1 for t in targets if t.needs_cover_fix)} / 目录cover {len({t.folder for t in targets if t.folder_cover_action in ("create", "rewrite", "remove")})}，'
        f'标签待补 {tagc} [缺日期 {datec}]{lrc_part}，foobar缓存待刷新 {sum(1 for t in targets if t.needs_foobar_sync)}）。'
    )
    if not targets:
        print('媒体库曲目元数据、封面与 foobar 缓存均正常。')
        return 0
    s = _cli_hook('execute_repairs', execute_repairs)(env, targets, opts, log_cb=print)
    prefix = '[预演] 完成：预计写入' if args.dry_run else '完成：文件写入'
    lrc_done = f" / 歌词 {s['lyrics_fixed']}" if s.get('lyrics_fixed') else ''
    lrc_miss = f"，未匹配歌词 {s['lyrics_unmatched']}" if s.get('lyrics_unmatched') else ''
    print(f"{prefix} {s['written_ok']}（标签 {s['tags_fixed']} / 日期 {s.get('date_fixed', 0)}{lrc_done}），目录cover {s['folder_cover_ok']}，纯缓存同步 {s['synced_stale']}，未匹配音轨 {s['unmatched']}，未匹配日期 {s.get('date_unmatched', 0)}{lrc_miss}，失败 {s['written_fail'] + s['folder_cover_fail']}。")
    return 0 if (s['written_fail'] + s['folder_cover_fail']) == 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='MusicMetadata', description='MusicMetadata — foobar2000 媒体库曲目元数据修复工具')
    parser.add_argument('--auto', action='store_true', help='无界面自动扫描并执行修复')
    parser.add_argument('--dry-run', action='store_true', help='只扫描和预览待修复项目，不修改文件')
    parser.add_argument('--sync-only', action='store_true', help='仅检查并刷新 foobar 元数据缓存')
    parser.add_argument('--no-track', action='store_true', help='跳过音轨号补全')
    parser.add_argument('--no-art', action='store_true', help='跳过封面与目录 cover.jpg 修复')
    parser.add_argument('--no-tags', action='store_true', help='跳过全部文本标签与碟片编号补全')
    parser.add_argument('--no-extra-tags', action='store_true', help='跳过扩展标签（作曲/流派/碟片编号）补全')
    parser.add_argument('--no-date', action='store_true', help='跳过日期(date)补全')
    parser.add_argument('--no-albumartist', action='store_true', help='跳过专辑艺术家(albumartist)补全')
    parser.add_argument('--no-artist', action='store_true', help='跳过艺术家(artist)补全')
    parser.add_argument('--no-composer', action='store_true', help='跳过作曲(composer)补全')
    parser.add_argument('--no-genre', action='store_true', help='跳过流派(genre)补全')
    parser.add_argument('--no-disc', action='store_true', help='跳过碟片编号(discnumber)补全')
    parser.add_argument('--lyrics', action='store_true', help='联网获取并补全歌词（支持双语合并）')
    parser.add_argument('--no-lyrics', action='store_true', help='跳过歌词补全')
    parser.add_argument('--lyrics-mode', choices=LYRICS_MODES, default='auto', help='歌词写入模式：auto(WAV/APE外挂其余内嵌), embed, lrc, both')
    parser.add_argument('--external-lrc', action='store_true', help='歌词仅保存为同名外置 .lrc 文件（等价于 --lyrics --lyrics-mode lrc）')
    parser.add_argument('--prefer', default='', help='优先数据源顺序，逗号分隔（可选: ' + ','.join(ONLINE_PROVIDERS) + '）')
    parser.add_argument('--source', '--provider', dest='source', default='', help='仅使用指定在线数据源，逗号分隔')
    parser.add_argument('--skip-source', default='', help='跳过的在线数据源，逗号分隔')
    parser.add_argument('--netease-api', default='', help='自定义网易云音乐 API 地址（留空使用官方接口）')
    parser.add_argument('--spotify-client-id', default='', help='可选 Spotify Web API Client ID（也可通过环境变量 SPOTIFY_CLIENT_ID 设置）')
    parser.add_argument('--spotify-client-secret', default='', help='可选 Spotify Web API Client Secret（也可通过环境变量 SPOTIFY_CLIENT_SECRET 设置）')
    parser.add_argument('--no-sync', action='store_true', help='跳过 foobar 缓存刷新')
    parser.add_argument(
        '--set', dest='set_tags', action='append', default=[], metavar='KEY=VALUE',
        help='手工设置指定或自定义元数据标签（可多次传入，如 --set genre=Pop --set date=2024）',
    )
    parser.add_argument(
        '--convert', default='', metavar='FORMAT',
        help='转换音频格式并保留元数据（可选: ' + ','.join(CONVERT_TARGET_FORMATS) + '）',
    )
    parser.add_argument('--force', '--convert-overwrite', dest='force', action='store_true', help='格式转换时强制覆盖已存在的目标文件')
    parser.add_argument('--delete-source', action='store_true', help='格式转换校验成功后删除源文件')
    parser.add_argument(
        '--delete', action='append', default=[], metavar='FIELDS',
        help='删除指定元数据类别（逗号分隔或多次传入，如 --delete composer,performer,lyrics）；可选: ' + ','.join(DELETABLE_FIELDS),
    )
    parser.add_argument('--delete-folder-cover', action='store_true', help='删除封面时同时删除同目录外置 cover.jpg')
    parser.add_argument('--no-delete-lrc', action='store_true', help='删除歌词时不删除同名外置 .lrc 文件')
    parser.add_argument('--no-protect-163', action='store_true', help='删除注释时一并删除网易云 163 key（默认保留）')
    parser.add_argument('--folder', default='', help='指定扫描目录')
    parser.add_argument('--include-full-album', action='store_true', help='也尝试处理整轨全长文件')
    return parser
