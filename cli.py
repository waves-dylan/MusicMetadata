"""MusicMetadata 命令行参数解析与批处理入口（供 CLI 模式与无界面自动化调用）。"""

from __future__ import annotations

import argparse
import os
import sys

from core.config import get_default_music_folders, has_player_env, load_user_config, normalize_folder_list
from core.constants import FIELD_LABELS, PLAYER_TYPE_FOOBAR, PLAYER_TYPE_MUSICBEE, PLAYER_TYPE_ROON, STATUS_ERROR
from infra.integrations import detect_active_player, detect_musicbee_env, detect_roon_env
from infra.integrations.foobar import detect_foobar_env, scan_library
from pipelines import (
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SacdExtractOptions,
    SetOptions,
    ZhConvertOptions,
    default_checked,
    execute_convert,
    execute_deletions,
    execute_repairs,
    execute_sacd_pipeline,
    execute_set_tags,
    execute_zh_convert,
    parse_delete_fields,
)
from providers import ONLINE_PROVIDERS
from services.art import DELETABLE_FIELDS, LYRICS_MODES, parse_set_tags
from services.lookup import parse_source_list
from infra.tools.sacd import find_sacd_extract, read_sacd_iso_toc
from tag_io import CONVERT_TARGET_FORMATS, find_ffmpeg

_ORIG_CLI_HOOKS = {
    'detect_foobar_env': detect_foobar_env,
    'scan_library': scan_library,
    'find_ffmpeg': find_ffmpeg,
    'find_sacd_extract': find_sacd_extract,
    'read_sacd_iso_toc': read_sacd_iso_toc,
    'execute_repairs': execute_repairs,
    'execute_deletions': execute_deletions,
    'execute_set_tags': execute_set_tags,
    'execute_convert': execute_convert,
    'execute_sacd_pipeline': execute_sacd_pipeline,
    'execute_zh_convert': execute_zh_convert,
}


def _cli_hook(name: str, current):
    app_mod = sys.modules.get('app')
    if app_mod is not None and name in _ORIG_CLI_HOOKS:
        val = getattr(app_mod, name, current)
        if val is not _ORIG_CLI_HOOKS[name]:
            return val
    return current


def _resolve_cli_folders(raw_folder) -> list[str] | None:
    if isinstance(raw_folder, (list, tuple)):
        folders = normalize_folder_list(raw_folder)
    elif raw_folder:
        folders = normalize_folder_list([raw_folder])
    else:
        folders = []
    return folders or None


def _prepare_cli_scan(args, *, include_art_scan: bool, show_unavailable: bool = False):
    hooked_fn = _cli_hook('detect_foobar_env', detect_foobar_env)
    is_mocked = hasattr(hooked_fn, 'assert_called') or hasattr(detect_foobar_env, 'assert_called')
    user_player = getattr(args, 'player', 'auto')

    if is_mocked:
        env = hooked_fn()
    elif user_player == 'foobar':
        env = hooked_fn()
    elif user_player == 'musicbee':
        env = detect_musicbee_env()
    elif user_player == 'roon':
        env = detect_roon_env()
    elif user_player == 'generic':
        env, _ = detect_active_player(preferred='generic')
    else:
        # auto 模式：优先探测运行中的播放器，否则探测已安装/配置的播放器
        env, _ = detect_active_player()

    custom = _resolve_cli_folders(getattr(args, 'folder', None))
    is_fb = has_player_env(env)
    p_type = getattr(env, 'player_type', None)
    is_mb = (p_type == PLAYER_TYPE_MUSICBEE) and bool(getattr(env, 'exe_path', '') or getattr(env, 'config_dir', '') or getattr(env, 'library_folders', []))
    is_roon = (p_type == PLAYER_TYPE_ROON) and bool(getattr(env, 'exe_path', '') or getattr(env, 'config_dir', '') or getattr(env, 'library_folders', []))

    if custom is None and not env.library_folders:
        cfg_folders = normalize_folder_list(load_user_config().get('custom_folders'), existing_only=True)
        custom = cfg_folders or get_default_music_folders() or None

    if is_mb:
        print('MusicBee 目录:', env.app_dir or '便携/已安装')
        print('媒体库目录:', custom or env.library_folders)
        if env.artist_pictures_dir:
            print('MusicBee 艺术家图库:', env.artist_pictures_dir)
    elif is_roon:
        print('Roon 目录:', env.app_dir or env.config_dir or '已安装')
        print('媒体库目录:', custom or env.library_folders or '（通过 --folder 指定或依赖 Roon Core 监听）')
    elif is_fb:
        print('foobar 目录:', env.app_dir or '未检测到')
        print('媒体库目录:', custom or env.library_folders)
    else:
        print('扫描模式: 通用本地目录模式（未检测到外部音乐播放器）')
        print('扫描目录:', custom or [])
        if not custom:
            print('提示: 未找到已保存目录或系统音乐目录，请通过 --folder 指定要扫描的文件夹。')
    if show_unavailable and not custom and env.unavailable_folders:
        print('不可访问的媒体库目录:', env.unavailable_folders)
    tracks = _cli_hook('scan_library', scan_library)(env, folders=custom, include_art_scan=include_art_scan)
    skip_full = not getattr(args, 'include_full_album', False)
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


def _run_zh_convert_cli(args) -> int:
    direction = str(getattr(args, 'zh_direction', 't2s') or 't2s').strip().lower()
    if direction not in ('t2s', 's2t'):
        print('未知的转换方向: 请使用 t2s (繁转简) 或 s2t (简转繁)', file=sys.stderr)
        return 2

    raw_fields = getattr(args, 'zh_fields', 'title,artist,albumartist,album') or 'title,artist,albumartist,album'
    fields = [f.strip() for f in raw_fields.split(',') if f.strip()]

    env, tracks, skip_full = _prepare_cli_scan(args, include_art_scan=False)
    targets = _active_targets(tracks, skip_full)
    zh_opts = ZhConvertOptions(
        direction=direction,
        fields=tuple(fields),
        convert_lyrics=bool(getattr(args, 'zh_lyrics', False)),
        preserve_jpop=not bool(getattr(args, 'no_zh_jpop_guard', False)),
        enable_sync=not args.no_sync,
        dry_run=args.dry_run,
    )
    dir_desc = '繁体 -> 简体' if direction == 't2s' else '简体 -> 繁体'
    print(f"共扫描 {len(tracks)} 首，将对 {len(targets)} 首曲目执行简繁文本转换（{dir_desc}，字段: {', '.join(fields)}）。")
    if not targets:
        print('没有可处理的曲目。')
        return 0
    s = _cli_hook('execute_zh_convert', execute_zh_convert)(env, targets, zh_opts, log_cb=print)
    prefix = '[预演] 完成：预计修改' if args.dry_run else '完成：已修改'
    print(f"{prefix} {s['zh_ok']} 首，无需转换 {s['zh_skipped']} 首，失败 {s['zh_fail']} 项（共变更 {s.get('diff_count', 0)} 处字段）。")
    return 0 if s['zh_fail'] == 0 else 1


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
    if getattr(args, 'delete_folder_artist', False) and 'artist_cover' not in del_fields:
        print('--delete-folder-artist 需与 --delete artist_cover 配合使用。', file=sys.stderr)
        return 2

    env, tracks, skip_full = _prepare_cli_scan(
        args, include_art_scan=('cover' in del_fields or 'artist_cover' in del_fields), show_unavailable=True,
    )
    targets = _active_targets(tracks, skip_full)
    del_opts = DeleteOptions(
        fields=frozenset(del_fields),
        delete_folder_cover=bool(getattr(args, 'delete_folder_cover', False)),
        delete_folder_artist=bool(getattr(args, 'delete_folder_artist', False)),
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


def _run_sacd_info_cli(args) -> int:
    iso_path = str(args.sacd_info or '').strip()
    if not os.path.isfile(iso_path):
        print(f"指定的 SACD ISO 文件不存在: {iso_path}", file=sys.stderr)
        return 2
    reader = _cli_hook('read_sacd_iso_toc', read_sacd_iso_toc)
    try:
        toc = reader(iso_path)
    except Exception as exc:
        print(f"读取 SACD ISO 目录失败: {exc}", file=sys.stderr)
        return 1

    print('=' * 60)
    print(f"💿 SACD 光盘信息: {toc.album_title}")
    print(f"👤 艺术家: {toc.album_artist or '未知'}")
    if toc.publisher:
        print(f"🏢 发行机构: {toc.publisher}")
    if toc.catalog_number:
        print(f"🏷️ 唱片编号: {toc.catalog_number}")
    if toc.year:
        print(f"📅 发行年份: {toc.year}")
    if toc.genre:
        print(f"🎵 音乐流派: {toc.genre}")

    st_tracks = toc.stereo_tracks
    mch_tracks = toc.multichannel_tracks
    print(f"🔊 音频区域: 立体声 (2ch) {'[✓]' if st_tracks else '[✗]'} | 多声道 (5.1/6ch) {'[✓]' if mch_tracks else '[✗]'}")
    print(f"💾 预估解压体积: 立体声 ~{toc.estimate_dsf_size_mb('2ch')} MB | 多声道 ~{toc.estimate_dsf_size_mb('mch')} MB")
    print('=' * 60)

    area_choice = getattr(args, 'sacd_channel', '2ch') or '2ch'
    tracks = toc.get_tracks(area_choice)
    area_name = "立体声 (2ch)" if area_choice.lower() in ('2ch', 'stereo') else "多声道 (mch)"
    print(f"📋 曲目清单 [{area_name}] (共 {len(tracks)} 首):")
    for t in tracks:
        perf = f" - {t.performer}" if t.performer and t.performer != toc.album_artist else ""
        dur = f" ({t.duration_str})" if t.duration_str else ""
        print(f"  {t.index:02d}. {t.title}{perf}{dur}")
    print('=' * 60)
    return 0


def _run_sacd_extract_cli(args) -> int:
    iso_path = str(args.sacd_extract or '').strip()
    if not os.path.isfile(iso_path):
        print(f"指定的 SACD ISO 文件不存在: {iso_path}", file=sys.stderr)
        return 2

    finder = _cli_hook('find_sacd_extract', find_sacd_extract)
    if not finder():
        print("未检测到 sacd_extract.exe，请确保工具已收纳进 tools/ 目录。", file=sys.stderr)
        return 2

    tracks_tuple = ()
    if getattr(args, 'sacd_tracks', None):
        try:
            tracks_tuple = tuple(int(x.strip()) for x in str(args.sacd_tracks).split(',') if x.strip().isdigit())
        except Exception:
            pass

    opts = SacdExtractOptions(
        channel_area=getattr(args, 'sacd_channel', '2ch') or '2ch',
        output_format='dsf',
        output_dir=getattr(args, 'sacd_out', None) or None,
        selected_tracks=tracks_tuple,
        apply_toc_tags=True,
        auto_repair=not getattr(args, 'no_tags', False),
        fetch_cover=not getattr(args, 'no_art', False),
        fetch_lyrics=bool(getattr(args, 'lyrics', False) or getattr(args, 'external_lrc', False)),
        dry_run=bool(getattr(args, 'dry_run', False)),
    )

    env, _, _ = _prepare_cli_scan(args, include_art_scan=False)
    runner = _cli_hook('execute_sacd_pipeline', execute_sacd_pipeline)
    res = runner(env, iso_path, opts, log_cb=print)
    if not res.get('success'):
        print(f"提取失败: {res.get('error')}", file=sys.stderr)
        return 1

    prefix = '[预演] 完成：预计提取' if opts.dry_run else '完成：成功提取'
    print(f"{prefix} {res.get('track_count', 0)} 首 DSF 曲目，输出目录: {res.get('output_dir')}")
    return 0


def run_cli(args) -> int:
    if getattr(args, 'sacd_info', ''):
        return _run_sacd_info_cli(args)
    if getattr(args, 'sacd_extract', ''):
        return _run_sacd_extract_cli(args)
    if getattr(args, 'set_tags', None):
        return _run_set_cli(args)
    if getattr(args, 'convert', ''):
        return _run_convert_cli(args)
    if getattr(args, 'zh_convert', False):
        return _run_zh_convert_cli(args)
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
    enable_artist_art = bool(getattr(args, 'artist_art', False) and not getattr(args, 'no_artist_art', False))

    env, tracks, skip_full = _prepare_cli_scan(
        args, include_art_scan=(not args.no_art or not args.no_tags or enable_artist_art), show_unavailable=True,
    )
    opts = FixOptions(
        enable_track=not args.no_track, enable_art=not args.no_art,
        enable_artist_art=enable_artist_art,
        enable_tags=not args.no_tags, enable_extra_tags=not args.no_extra_tags,
        enable_date=not args.no_date, enable_albumartist=not args.no_albumartist,
        enable_artist=not args.no_artist, enable_composer=not args.no_composer,
        enable_genre=not args.no_genre, enable_disc=not args.no_disc,
        enable_lyrics=enable_lyrics, lyrics_mode=lyrics_mode,
        prefer_sources=prefer_sources, only_sources=only_sources, skip_sources=skip_sources,
        netease_api=(getattr(args, 'netease_api', '') or '').strip() or None,
        spotify_client_id=(getattr(args, 'spotify_client_id', None) or None),
        spotify_client_secret=(getattr(args, 'spotify_client_secret', None) or None),
        tadb_api_key=(getattr(args, 'tadb_api_key', None) or None),
        fanart_api_key=(getattr(args, 'fanart_api_key', None) or None),
        fanart_client_key=(getattr(args, 'fanart_client_key', None) or None),
        discogs_token=(getattr(args, 'discogs_token', None) or None),
        sync_musicbee_artist=not getattr(args, 'no_musicbee_artist', False),
        musicbee_artist_dir=getattr(args, 'musicbee_artist_dir', None) or getattr(env, 'artist_pictures_dir', None),
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
    art_part = f'，缺艺术家图 {sum(1 for t in targets if t.needs_artist_art)}' if enable_artist_art else ''
    stale_cnt = sum(1 for t in targets if t.needs_player_sync)
    p_name = getattr(env, 'player_name', '播放器')
    sync_part = f'，{p_name}缓存待刷新 {stale_cnt}' if (has_player_env(env) or stale_cnt > 0) else ''
    print(
        f'共扫描 {len(tracks)} 首，待处理 {len(targets)} 首（缺音轨 {sum(1 for t in targets if t.needs_tracknumber)}，'
        f'封面待修 {sum(1 for t in targets if t.needs_cover_fix)} / 目录cover {len({t.folder for t in targets if t.folder_cover_action in ("create", "rewrite", "remove")})}，'
        f'标签待补 {tagc} [缺日期 {datec}]{lrc_part}{art_part}{sync_part}）。'
    )
    if not targets:
        print(f'媒体库曲目元数据、封面与 {p_name} 缓存均正常。' if has_player_env(env) else '扫描目录曲目元数据与封面均正常。')
        return 0
    s = _cli_hook('execute_repairs', execute_repairs)(env, targets, opts, log_cb=print)
    prefix = '[预演] 完成：预计写入' if args.dry_run else '完成：文件写入'
    lrc_done = f" / 歌词 {s['lyrics_fixed']}" if s.get('lyrics_fixed') else ''
    lrc_miss = f"，未匹配歌词 {s['lyrics_unmatched']}" if s.get('lyrics_unmatched') else ''
    art_done = f" / 艺术家图 {s['artist_fixed']}" if s.get('artist_fixed') else ''
    fa_done = f"，目录artist {s['folder_artist_ok']}" if s.get('folder_artist_ok') else ''
    print(f"{prefix} {s['written_ok']}（标签 {s['tags_fixed']} / 日期 {s.get('date_fixed', 0)}{lrc_done}{art_done}），目录cover {s['folder_cover_ok']}{fa_done}，纯缓存同步 {s['synced_stale']}，未匹配音轨 {s['unmatched']}，未匹配日期 {s.get('date_unmatched', 0)}{lrc_miss}，失败 {s['written_fail'] + s['folder_cover_fail'] + s.get('folder_artist_fail', 0)}。")
    return 0 if (s['written_fail'] + s['folder_cover_fail'] + s.get('folder_artist_fail', 0)) == 0 else 1


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='MusicMetadata',
        description='MusicMetadata — 通用本地音乐库元数据修复工具（支持 MusicBee/Fooyin/Strawberry/AIMP/foobar2000 等）',
    )
    parser.add_argument('--auto', action='store_true', help='无界面自动扫描并执行修复')
    parser.add_argument('--player', choices=['auto', 'foobar', 'musicbee', 'roon', 'generic'], default='auto', help='指定关联的播放器媒体库（默认 auto 自动检测）')
    parser.add_argument('--musicbee-artist-dir', default=None, help='指定 MusicBee 中央艺术家写真目录')
    parser.add_argument('--no-musicbee-artist', action='store_true', help='禁用向 MusicBee 中央艺术家写真库同步写入图片')
    parser.add_argument('--dry-run', action='store_true', help='只扫描和预览待修复项目，不修改文件')
    parser.add_argument('--sync-only', action='store_true', help='仅检查并刷新播放器（foobar2000 / MusicBee / Roon）元数据缓存')
    parser.add_argument('--no-track', action='store_true', help='跳过音轨号补全')
    parser.add_argument('--no-art', action='store_true', help='跳过封面与目录 cover.jpg 修复')
    parser.add_argument('--artist-art', action='store_true', help='联网获取艺术家图片（头像/写真），内嵌至音频并生成目录 artist.jpg（默认关闭）')
    parser.add_argument('--no-artist-art', action='store_true', help='跳过艺术家图片补全')
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
    parser.add_argument('--tadb-api-key', default='', help='可选 TheAudioDB API Key（留空使用公共 Key）')
    parser.add_argument('--fanart-api-key', default='', help='可选 Fanart.tv Project API Key（留空使用内置 Project Key）')
    parser.add_argument('--fanart-client-key', default='', help='可选 Fanart.tv Personal/Client Key（加速缓存刷新）')
    parser.add_argument('--discogs-token', default='', help='可选 Discogs Personal Access Token（发烧实体库专属，也可通过环境变量 DISCOGS_TOKEN 设置）')
    parser.add_argument('--no-sync', action='store_true', help='跳过写入后时间戳推进与播放器缓存刷新')
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
    parser.add_argument('--zh-convert', action='store_true', help='批量执行华语简繁汉字转换')
    parser.add_argument('--zh-direction', choices=['t2s', 's2t'], default='t2s', help='转换方向：t2s (繁转简, 默认) 或 s2t (简转繁)')
    parser.add_argument('--zh-fields', default='title,artist,albumartist,album', help='待转换字段，逗号分隔（默认: title,artist,albumartist,album）')
    parser.add_argument('--zh-lyrics', action='store_true', help='同步转换歌词（内嵌歌词与同名外置 .lrc）')
    parser.add_argument('--no-zh-jpop-guard', action='store_true', help='禁用日文假名与日文艺术家保护')
    parser.add_argument(
        '--delete', action='append', default=[], metavar='FIELDS',
        help='删除指定元数据类别（逗号分隔或多次传入，如 --delete composer,performer,lyrics）；可选: ' + ','.join(DELETABLE_FIELDS),
    )
    parser.add_argument('--delete-folder-cover', action='store_true', help='删除封面时同时删除同目录外置 cover.jpg')
    parser.add_argument('--delete-folder-artist', action='store_true', help='删除封面-艺术家时同时删除同目录外置 artist.jpg')
    parser.add_argument('--no-delete-lrc', action='store_true', help='删除歌词时不删除同名外置 .lrc 文件')
    parser.add_argument('--no-protect-163', action='store_true', help='删除注释时一并删除网易云 163 key（默认保留）')
    parser.add_argument('--folder', action='append', default=[], help='指定扫描目录（可多次传入以扫描多个目录）')
    parser.add_argument('--include-full-album', action='store_true', help='也尝试处理整轨全长文件')
    parser.add_argument('--sacd-info', metavar='ISO_PATH', default='', help='查看指定 SACD ISO 镜像的光盘信息与音轨目录结构 (TOC)')
    parser.add_argument('--sacd-extract', metavar='ISO_PATH', default='', help='提取 SACD ISO 镜像为 Sony DSF 无损音轨并执行自动化修复')
    parser.add_argument('--sacd-channel', choices=['2ch', 'mch'], default='2ch', help='SACD 提取声道模式：2ch (立体声, 默认) 或 mch (多声道)')
    parser.add_argument('--sacd-out', metavar='DIR', default='', help='SACD 提取目标输出目录（默认输出到 ISO 同级 [专辑名] (DSF) 文件夹）')
    parser.add_argument('--sacd-tracks', metavar='TRACKS', default='', help='提取指定曲目序号（逗号分隔，如 1,2,5；默认提取全盘）')
    return parser
