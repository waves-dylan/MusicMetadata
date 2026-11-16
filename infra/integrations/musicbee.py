"""MusicBee 媒体库集成：进程探测、XML 配置解析、中央艺术家图库与热重载。"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET

from core.constants import PLAYER_TYPE_MUSICBEE
from core.models import PlayerEnv
from infra.integrations.base import BasePlayerAdapter, get_running_process_exes, sanitize_filename
from infra.scanner import touch_files_mtime


def get_running_musicbee_exes() -> list[str]:
    """查询当前运行的 MusicBee.exe 路径列表。"""
    res = get_running_process_exes('musicbee.exe')
    return res.get('musicbee.exe', [])


def _extract_from_musicbee_xml_string(content: str) -> dict:
    """从 MusicBee XML 配置文本中提取曲库路径、中央写真库与监控目录列表（XML/正则双模容错）。"""
    info: dict[str, Any] = {
        'env_lib_path': '',
        'last_library': '',
        'artist_pictures_dir': '',
        'monitored_folders': [],
        'last_scanned_folders': [],
    }
    if not content or not content.strip():
        return info

    def _clean_path(val: str | None) -> str:
        if not val:
            return ''
        s = val.strip()
        if not s or s == '/' or s.isdigit() or s.lower() in ('true', 'false'):
            return ''
        return os.path.normpath(s)

    # 1. 优先尝试标准 XML 解析（先清洗掉 XML 规范不允许的 ASCII 控制字符，如 \x00）
    sanitized_content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
    try:
        root = ET.fromstring(sanitized_content)
        for tag in ('ENV_LibPath', 'LibPath'):
            val = root.findtext(tag)
            if val and val.strip():
                p = _clean_path(val)
                if p:
                    info['env_lib_path'] = p
                    break

        for tag in ('LastLibrary', 'MusicLibraryPath', 'Path'):
            val = root.findtext(tag)
            if val and val.strip():
                p = _clean_path(val)
                if p:
                    info['last_library'] = p
                    break

        for tag in ('ArtistPictureLocation', 'ArtistPicturePath', 'ArtistPicturesDirectory'):
            val = root.findtext(tag)
            if val and val.strip():
                p = _clean_path(val)
                if p:
                    info['artist_pictures_dir'] = p
                    break

        for ptag in ('OrganisationMonitoredFolders', 'MonitoredFolders', 'MonitoredFolderList'):
            node = root.find(ptag)
            if node is not None:
                for child in node:
                    t = _clean_path(child.text)
                    if t and t not in info['monitored_folders']:
                        info['monitored_folders'].append(t)

        for fld in root.findall('.//MonitoredFolder'):
            t = _clean_path(fld.text)
            if t and t not in info['monitored_folders']:
                info['monitored_folders'].append(t)

        last_scan_node = root.find('OrganisationLastScannedFolders')
        if last_scan_node is not None:
            for child in last_scan_node:
                t = _clean_path(child.text)
                if t and t not in info['last_scanned_folders']:
                    info['last_scanned_folders'].append(t)

    except Exception:
        # 2. 正则降级提取
        m = re.search(r'<(?:ENV_LibPath|LibPath)>([^<]+)</', sanitized_content)
        if m:
            p = _clean_path(m.group(1))
            if p:
                info['env_lib_path'] = p

        m = re.search(r'<(?:LastLibrary|MusicLibraryPath|Path)>([^<]+)</', sanitized_content)
        if m:
            p = _clean_path(m.group(1))
            if p:
                info['last_library'] = p

        m = re.search(r'<(?:ArtistPictureLocation|ArtistPicturePath|ArtistPicturesDirectory)>([^<]+)</', sanitized_content)
        if m:
            p = _clean_path(m.group(1))
            if p:
                info['artist_pictures_dir'] = p

        for m_sec in re.finditer(r'<(?:OrganisationMonitoredFolders|MonitoredFolders)>([\s\S]*?)</(?:OrganisationMonitoredFolders|MonitoredFolders)>', sanitized_content):
            for sm in re.finditer(r'<(?:string|Folder|MonitoredFolder)\b[^>]*>([^<]+)</', m_sec.group(1)):
                t = _clean_path(sm.group(1))
                if t and t not in info['monitored_folders']:
                    info['monitored_folders'].append(t)

        for sm in re.finditer(r'<(?:Folder|MonitoredFolder)\b[^>]*>([^<]+)</(?:Folder|MonitoredFolder)>', sanitized_content):
            t = _clean_path(sm.group(1))
            if t and t not in info['monitored_folders']:
                info['monitored_folders'].append(t)

        for m_sec in re.finditer(r'<OrganisationLastScannedFolders>([\s\S]*?)</OrganisationLastScannedFolders>', sanitized_content):
            for sm in re.finditer(r'<string\b[^>]*>([^<]+)</', m_sec.group(1)):
                t = _clean_path(sm.group(1))
                if t and t not in info['last_scanned_folders']:
                    info['last_scanned_folders'].append(t)

    return info


def parse_musicbee_settings(config_dir: str, mb_dir: str = '') -> dict:
    """
    解析 MusicBee3Settings.ini / MusicBeeSettings.ini 配置文件及曲库设置 MusicBeeLibrarySettings.ini。
    返回包含 monitored_folders, last_library, artist_pictures_dir 等字典。
    """
    res = {
        'monitored_folders': [],
        'last_library': '',
        'artist_pictures_dir': '',
    }
    if not config_dir or not os.path.isdir(config_dir):
        return res

    main_settings_file = ''
    for fname in ('MusicBee3Settings.ini', 'MusicBeeSettings.ini', 'MusicBee3Settings.bak', 'MusicBeeSettings.bak'):
        cand = os.path.join(config_dir, fname)
        if os.path.isfile(cand):
            main_settings_file = cand
            break

    main_info = {
        'env_lib_path': '',
        'last_library': '',
        'artist_pictures_dir': '',
        'monitored_folders': [],
        'last_scanned_folders': [],
    }
    if main_settings_file:
        try:
            with open(main_settings_file, 'r', encoding='utf-8', errors='replace') as fp:
                main_info = _extract_from_musicbee_xml_string(fp.read())
        except OSError:
            pass

    folders = list(main_info['monitored_folders'])
    last_lib = main_info['last_library']
    env_lib = main_info['env_lib_path']
    art_dir = main_info['artist_pictures_dir']

    # 寻找曲库目录并解析其内部的 MusicBeeLibrarySettings.ini
    lib_dir_cands: list[str] = []
    if env_lib:
        lib_dir_cands.append(env_lib)
    if last_lib:
        if os.path.isfile(last_lib):
            lib_dir_cands.append(os.path.dirname(last_lib))
        else:
            lib_dir_cands.append(last_lib)
    lib_dir_cands.extend([
        config_dir,
        os.path.join(mb_dir, 'AppData') if mb_dir else '',
        os.path.expanduser(r'~\Music\MusicBee'),
    ])

    for ldir in lib_dir_cands:
        if not ldir or not os.path.isdir(ldir):
            continue
        lib_settings = ''
        for lfname in ('MusicBeeLibrarySettings.ini', 'MusicBeeLibrarySettings.bak'):
            lcand = os.path.join(ldir, lfname)
            if os.path.isfile(lcand):
                lib_settings = lcand
                break
        if lib_settings:
            try:
                with open(lib_settings, 'r', encoding='utf-8', errors='replace') as fp:
                    lib_info = _extract_from_musicbee_xml_string(fp.read())
                    for f in lib_info['monitored_folders']:
                        if f not in folders:
                            folders.append(f)
                    # 若主配置与曲库均未配置监控目录，但有最近扫描目录，作为备选
                    if not folders and lib_info['last_scanned_folders']:
                        for f in lib_info['last_scanned_folders']:
                            if f not in folders and os.path.isdir(f):
                                folders.append(f)
                    if not art_dir and lib_info['artist_pictures_dir']:
                        art_dir = lib_info['artist_pictures_dir']
                    if not last_lib and lib_info['last_library']:
                        last_lib = lib_info['last_library']
            except OSError:
                pass
            break

    # 兜底解析中央写真目录
    if not art_dir:
        for cand_art in [
            os.path.join(env_lib, 'Artist Pictures') if env_lib else '',
            os.path.join(config_dir, 'Artist Pictures'),
            os.path.join(mb_dir, 'AppData', 'Artist Pictures') if mb_dir else '',
            os.path.expanduser(r'~\Music\MusicBee\Artist Pictures'),
        ]:
            if cand_art and os.path.isdir(cand_art):
                art_dir = os.path.normpath(cand_art)
                break
    if not art_dir:
        art_dir = os.path.join(config_dir, 'Artist Pictures')

    # 若仍然没有任何监控目录，但已找到曲库主目录，将其作为备选监控目录
    if not folders and (last_lib or env_lib):
        target_dir = env_lib or (os.path.dirname(last_lib) if os.path.isfile(last_lib) else last_lib)
        if target_dir and os.path.isdir(target_dir):
            folders.append(os.path.normpath(target_dir))

    res['monitored_folders'] = sorted(list(dict.fromkeys(folders)))
    res['last_library'] = last_lib or env_lib
    res['artist_pictures_dir'] = os.path.normpath(art_dir)
    return res


def detect_musicbee_env(custom_mb_dir: str = '') -> PlayerEnv:
    """自动发现 MusicBee 安装路径、profile 与媒体库目录。"""
    cands = ([custom_mb_dir] if custom_mb_dir and os.path.isdir(custom_mb_dir) else [])
    for exe in get_running_musicbee_exes():
        d = os.path.dirname(exe)
        if d not in cands:
            cands.append(d)

    std_dirs = [
        os.path.join(os.environ.get('ProgramFiles(x86)', r'C:\Program Files (x86)'), 'MusicBee'),
        os.path.join(os.environ.get('ProgramFiles', r'C:\Program Files'), 'MusicBee'),
        r'D:\Program Files (x86)\MusicBee',
        r'D:\Program Files\MusicBee',
        r'D:\MusicBee',
        r'E:\MusicBee',
    ]
    for d in std_dirs:
        if d and os.path.isdir(d) and d not in cands:
            cands.append(d)

    mb_exe, mb_dir = '', ''
    for c in cands:
        exe = os.path.join(c, 'MusicBee.exe')
        if os.path.isfile(exe):
            mb_exe, mb_dir = exe, c
            break

    # 确定配置目录（便携模式优先）
    config_dir = ''
    if mb_dir and os.path.isdir(os.path.join(mb_dir, 'AppData')):
        config_dir = os.path.join(mb_dir, 'AppData')
    if not config_dir and os.environ.get('APPDATA'):
        appdata_mb = os.path.join(os.environ['APPDATA'], 'MusicBee')
        if os.path.isdir(appdata_mb):
            config_dir = appdata_mb
    if not config_dir and mb_dir:
        config_dir = mb_dir

    parsed = parse_musicbee_settings(config_dir, mb_dir)

    available_folders = [f for f in parsed['monitored_folders'] if os.path.isdir(f)]
    unavailable = [f for f in parsed['monitored_folders'] if not os.path.isdir(f)]

    art_dir = parsed['artist_pictures_dir']
    if not art_dir and config_dir:
        art_dir = os.path.join(config_dir, 'Artist Pictures')

    return PlayerEnv(
        player_type=PLAYER_TYPE_MUSICBEE,
        player_name='MusicBee',
        exe_path=mb_exe,
        app_dir=mb_dir,
        config_dir=config_dir,
        library_folders=available_folders,
        unavailable_folders=unavailable,
        reload_command='自动监控重新载入',
        artist_pictures_dir=art_dir,
        extra_info=parsed,
    )


def sync_artist_pictures_to_musicbee(
    artist_pictures_dir: str,
    artist_names: list[str] | str,
    jpeg_bytes: bytes,
) -> list[str]:
    """
    将规范化的艺术家写真写入 MusicBee 的中央 Artist Pictures 目录。
    支持传入单个艺术家或多个艺术家名称（支持以 '/'、';' 或 '&' 分隔分割）。
    """
    if not artist_pictures_dir or not jpeg_bytes:
        return []

    try:
        os.makedirs(artist_pictures_dir, exist_ok=True)
    except OSError:
        return []

    raw_names = [artist_names] if isinstance(artist_names, str) else list(artist_names)
    names_to_save: set[str] = set()
    for raw in raw_names:
        if not raw or not raw.strip():
            continue
        cleaned_whole = sanitize_filename(raw.strip())
        if cleaned_whole:
            names_to_save.add(cleaned_whole)
        # 如果艺术家字段包含多位艺术家，拆分并为各主艺人生成头像（保护 AC/DC 等非空格斜杠不被切碎）
        for part in re.split(r'\s*;\s*|\s+/\s+|\s+&\s+|\s*、\s*|\s*,\s*', raw):
            part_clean = sanitize_filename(part.strip())
            if part_clean and len(part_clean) > 1:
                names_to_save.add(part_clean)

    saved_paths: list[str] = []
    for name in names_to_save:
        target_path = os.path.join(artist_pictures_dir, f'{name}.jpg')
        try:
            # 若文件已存在且大小完全一致，可跳过写盘
            if os.path.isfile(target_path) and os.path.getsize(target_path) == len(jpeg_bytes):
                saved_paths.append(target_path)
                continue
            with open(target_path, 'wb') as fp:
                fp.write(jpeg_bytes)
            saved_paths.append(target_path)
        except OSError:
            pass

    return saved_paths


def sync_files_to_musicbee(
    env: PlayerEnv,
    paths: list[str],
) -> tuple[int, bool]:
    """
    通过更新文件修改时间戳，触发 MusicBee 的内置 FileSystemWatcher 自动重新扫描载入。
    """
    valid_paths = [p for p in paths if p and os.path.isfile(p)]
    if not valid_paths:
        return 0, False

    touched = touch_files_mtime(valid_paths)
    return touched, touched > 0


class MusicBeeAdapter(BasePlayerAdapter):
    """MusicBee 音乐播放器适配器。"""

    player_type: str = PLAYER_TYPE_MUSICBEE
    player_name: str = 'MusicBee'

    def detect_env(self, custom_dir: str = '') -> PlayerEnv:
        return detect_musicbee_env(custom_dir)

    def is_running(self) -> bool:
        return len(get_running_musicbee_exes()) > 0

    def get_library_folders(self, env: PlayerEnv) -> tuple[list[str], list[str]]:
        return env.library_folders, env.unavailable_folders

    def notify_reload(self, env: PlayerEnv, paths: list[str]) -> tuple[int, bool]:
        return sync_files_to_musicbee(env, paths)

    def sync_artist_pictures(self, env: PlayerEnv, artist_names: list[str] | str, jpeg_bytes: bytes) -> list[str]:
        return sync_artist_pictures_to_musicbee(env.artist_pictures_dir, artist_names, jpeg_bytes)
