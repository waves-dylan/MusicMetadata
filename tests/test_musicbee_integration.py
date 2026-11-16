"""MusicBee 播放器适配与中央艺术家写真库联动单元测试。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.constants import PLAYER_TYPE_FOOBAR, PLAYER_TYPE_GENERIC, PLAYER_TYPE_MUSICBEE
from core.models import LibraryTrack, PlayerEnv, TrackMatch
from infra.integrations import (
    MusicBeeAdapter,
    detect_active_player,
    detect_musicbee_env,
    parse_musicbee_settings,
    sanitize_filename,
    sync_artist_pictures_to_musicbee,
)
from pipelines.options import FixOptions
from pipelines.repair import execute_repairs


class TestMusicBeeIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='mb_test_')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sanitize_filename(self):
        # 验证 Windows 非法字符与空白/点被安全清理
        self.assertEqual(sanitize_filename('AC/DC'), 'AC_DC')
        self.assertEqual(sanitize_filename('Who? What: When*'), 'Who_ What_ When_')
        self.assertEqual(sanitize_filename('  Artist Name.  '), 'Artist Name')
        self.assertEqual(sanitize_filename(''), 'unknown')

    def test_parse_musicbee_settings_xml(self):
        cfg_dir = os.path.join(self.test_dir, 'AppData')
        os.makedirs(cfg_dir, exist_ok=True)
        xml_content = """<?xml version="1.0" encoding="utf-8"?>
<MusicBeeSettings>
    <LastLibrary>D:\\Music\\MusicBee\\MyLibrary.mbl</LastLibrary>
    <ArtistPicturePath>D:\\Music\\MusicBee\\Artist Pictures</ArtistPicturePath>
    <MonitoredFolders>
        <Folder>{folder1}</Folder>
        <Folder>{folder2}</Folder>
    </MonitoredFolders>
</MusicBeeSettings>
""".format(folder1=self.test_dir, folder2=cfg_dir)

        ini_path = os.path.join(cfg_dir, 'MusicBee3Settings.ini')
        with open(ini_path, 'w', encoding='utf-8') as fp:
            fp.write(xml_content)

        parsed = parse_musicbee_settings(cfg_dir)
        self.assertEqual(parsed['last_library'], r'D:\Music\MusicBee\MyLibrary.mbl')
        self.assertEqual(parsed['artist_pictures_dir'], os.path.normpath(r'D:\Music\MusicBee\Artist Pictures'))
        self.assertIn(os.path.abspath(self.test_dir), parsed['monitored_folders'])
        self.assertIn(os.path.abspath(cfg_dir), parsed['monitored_folders'])

    def test_parse_musicbee_settings_fallback_regex(self):
        # 模拟包含未闭合或畸形字符的配置
        cfg_dir = os.path.join(self.test_dir, 'AppData_Malformed')
        os.makedirs(cfg_dir, exist_ok=True)
        malformed = """
        <LastLibrary>D:\\Lib\\music.mbl</LastLibrary>
        <ArtistPicturePath>{art_dir}</ArtistPicturePath>
        <MonitoredFolder>{fld}</MonitoredFolder>
        <UnclosedTag>Some < Invalid & Content
        """.format(art_dir=cfg_dir, fld=self.test_dir)

        ini_path = os.path.join(cfg_dir, 'MusicBeeSettings.ini')
        with open(ini_path, 'w', encoding='utf-8') as fp:
            fp.write(malformed)

        parsed = parse_musicbee_settings(cfg_dir)
        self.assertEqual(parsed['last_library'], r'D:\Lib\music.mbl')
        self.assertEqual(parsed['artist_pictures_dir'], os.path.normpath(cfg_dir))
        self.assertIn(os.path.abspath(self.test_dir), parsed['monitored_folders'])

    def test_parse_musicbee_null_bytes_and_folder_category_exclusion(self):
        # 验证包含控制字符/空字节及 FolderCategory 标签时，不会误匹配为监控目录
        cfg_dir = os.path.join(self.test_dir, 'AppData_NullBytes')
        os.makedirs(cfg_dir, exist_ok=True)
        xml_content = f"""<?xml version="1.0" encoding="utf-8"?>
<MusicBeeSettings>
    <ThumbBrowserValue>\x00\x00</ThumbBrowserValue>
    <FolderCategory>1</FolderCategory>
    <FolderCategory>11</FolderCategory>
    <FolderCategory>12</FolderCategory>
    <FolderCategory>17</FolderCategory>
    <FolderCategory>25</FolderCategory>
    <MonitoredFolders>
        <Folder>{self.test_dir}</Folder>
    </MonitoredFolders>
</MusicBeeSettings>
"""
        ini_path = os.path.join(cfg_dir, 'MusicBee3Settings.ini')
        with open(ini_path, 'w', encoding='utf-8') as fp:
            fp.write(xml_content)

        parsed = parse_musicbee_settings(cfg_dir)
        self.assertEqual(parsed['monitored_folders'], [os.path.abspath(self.test_dir)])
        for num in ('1', '11', '12', '17', '25'):
            self.assertNotIn(num, parsed['monitored_folders'])

    def test_parse_musicbee_two_tier_settings(self):
        # 验证真实 MusicBee 两级配置：主 ini 记录 ENV_LibPath，媒体库 ini 记录 OrganisationMonitoredFolders
        cfg_dir = os.path.join(self.test_dir, 'MB_AppData')
        lib_dir = os.path.join(self.test_dir, 'MB_Library')
        art_dir = os.path.join(lib_dir, 'Artist Pictures')
        fld1 = os.path.join(self.test_dir, 'Music1')
        fld2 = os.path.join(self.test_dir, 'Music2')
        for d in (cfg_dir, lib_dir, art_dir, fld1, fld2):
            os.makedirs(d, exist_ok=True)

        main_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ApplicationSettings>
    <ENV_LibPath>{lib_dir}\\</ENV_LibPath>
    <ArtistPictureLocation>{art_dir}\\</ArtistPictureLocation>
</ApplicationSettings>
"""
        with open(os.path.join(cfg_dir, 'MusicBee3Settings.ini'), 'w', encoding='utf-8') as fp:
            fp.write(main_xml)

        lib_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<ApplicationSettings>
    <OrganisationMonitoredFolders>
        <string>{fld1}\\</string>
        <string>{fld2}\\</string>
    </OrganisationMonitoredFolders>
</ApplicationSettings>
"""
        with open(os.path.join(lib_dir, 'MusicBeeLibrarySettings.ini'), 'w', encoding='utf-8') as fp:
            fp.write(lib_xml)

        parsed = parse_musicbee_settings(cfg_dir)
        self.assertEqual(parsed['artist_pictures_dir'], os.path.normpath(art_dir))
        self.assertIn(os.path.normpath(fld1), parsed['monitored_folders'])
        self.assertIn(os.path.normpath(fld2), parsed['monitored_folders'])

    def test_sync_artist_pictures_to_musicbee(self):
        art_dir = os.path.join(self.test_dir, 'Artist Pictures')
        fake_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF' + b'\x00' * 50

        # 单艺人保存
        saved1 = sync_artist_pictures_to_musicbee(art_dir, 'Taylor Swift', fake_jpeg)
        self.assertEqual(len(saved1), 1)
        expected_path1 = os.path.join(art_dir, 'Taylor Swift.jpg')
        self.assertTrue(os.path.isfile(expected_path1))
        self.assertEqual(os.path.getsize(expected_path1), len(fake_jpeg))

        # 带非法字符与多艺人拆分
        saved2 = sync_artist_pictures_to_musicbee(art_dir, 'AC/DC; Queen', fake_jpeg)
        self.assertTrue(os.path.isfile(os.path.join(art_dir, 'AC_DC; Queen.jpg')))
        self.assertTrue(os.path.isfile(os.path.join(art_dir, 'AC_DC.jpg')))
        self.assertTrue(os.path.isfile(os.path.join(art_dir, 'Queen.jpg')))

    def test_detect_active_player_priority(self):
        # 1. 显式指定优先项
        env_mb, adapter_mb = detect_active_player(preferred='musicbee')
        self.assertEqual(env_mb.player_type, PLAYER_TYPE_MUSICBEE)
        self.assertIsInstance(adapter_mb, MusicBeeAdapter)

        env_fb, adapter_fb = detect_active_player(preferred='foobar')
        self.assertEqual(env_fb.player_type, PLAYER_TYPE_FOOBAR)

        # 2. 自动检测中：若 MusicBee 正在运行
        with patch('infra.integrations.get_running_foobar_exes', return_value=[]), \
             patch('infra.integrations.get_running_musicbee_exes', return_value=[r'C:\MusicBee\MusicBee.exe']):
            env_run, adapter_run = detect_active_player()
            self.assertEqual(env_run.player_type, PLAYER_TYPE_MUSICBEE)

        # 3. 自动检测中：若 foobar2000 正在运行
        with patch('infra.integrations.get_running_foobar_exes', return_value=[r'C:\foobar2000\foobar2000.exe']), \
             patch('infra.integrations.get_running_musicbee_exes', return_value=[]):
            env_run2, adapter_run2 = detect_active_player()
            self.assertEqual(env_run2.player_type, PLAYER_TYPE_FOOBAR)

        # 4. 均未安装运行回退通用模式
        with patch('infra.integrations.get_running_foobar_exes', return_value=[]), \
             patch('infra.integrations.get_running_musicbee_exes', return_value=[]), \
             patch('infra.integrations.foobar.detect_foobar_env', return_value=PlayerEnv(player_type=PLAYER_TYPE_FOOBAR)), \
             patch('infra.integrations.musicbee.detect_musicbee_env', return_value=PlayerEnv(player_type=PLAYER_TYPE_MUSICBEE)):
            env_gen, adapter_gen = detect_active_player()
            self.assertIn(env_gen.player_type, (PLAYER_TYPE_GENERIC, PLAYER_TYPE_FOOBAR))

    def test_execute_repairs_syncs_musicbee_artist_pictures(self):
        # 验证批量修复管线执行时，能将下载的艺人写真同步写入 MusicBee 中央写真库
        art_dir = os.path.join(self.test_dir, 'Artist Pictures')
        os.makedirs(art_dir, exist_ok=True)

        env = PlayerEnv(
            player_type=PLAYER_TYPE_MUSICBEE,
            player_name='MusicBee',
            artist_pictures_dir=art_dir,
        )

        dummy_audio = os.path.join(self.test_dir, 'song.flac')
        with open(dummy_audio, 'wb') as fp:
            fp.write(b'fake flac')

        tr = LibraryTrack(
            path=dummy_audio,
            folder=self.test_dir,
            filename='song.flac',
            artist='TestSinger',
            albumartist='TestSinger',
            needs_artist_art=True,
            container='flac',
        )

        fake_artist_pic = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' ",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'

        opts = FixOptions(
            enable_track=False,
            enable_art=False,
            enable_tags=False,
            enable_artist_art=True,
            sync_musicbee_artist=True,
            musicbee_artist_dir=art_dir,
        )

        with patch('pipelines.repair.resolve_track_metadata', return_value=TrackMatch(1, artist='TestSinger', artist_pic_url='http://fake/artist.jpg')), \
             patch('pipelines.repair.fetch_artist_bytes', return_value=fake_artist_pic), \
             patch('pipelines.repair.apply_track_repairs', return_value=(True, 'ok')):
            res = execute_repairs(env, [tr], opts)

        self.assertFalse(res['cancelled'])
        expected_art = os.path.join(art_dir, 'TestSinger.jpg')
        self.assertTrue(os.path.isfile(expected_art))

    def test_gui_musicbee_mode_adaptation(self):
        from tests.common import offscreen_tk
        import app as app_mod
        mb_env = PlayerEnv(
            player_type=PLAYER_TYPE_MUSICBEE,
            player_name='MusicBee',
            exe_path=r'C:\MusicBee\MusicBee.exe',
            library_folders=[self.test_dir],
            artist_pictures_dir=os.path.join(self.test_dir, 'Artist Pictures'),
        )
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            win._on_scan_finished(mb_env, [], None)
            self.assertEqual(win.btn_sync_only.winfo_manager(), 'pack')
            self.assertIn('MusicBee', win.btn_sync_only.cget('text'))
            self.assertEqual(win.chk_sync_mb_artist.winfo_manager(), 'pack')
            self.assertIn('MusicBee', win.var_env.get())

    def test_gui_musicbee_mode_adaptation_empty_folders_keeps_musicbee_label(self):
        from tests.common import offscreen_tk
        import app as app_mod
        mb_env = PlayerEnv(
            player_type=PLAYER_TYPE_MUSICBEE,
            player_name='MusicBee',
            exe_path=r'C:\MusicBee\MusicBee.exe',
            library_folders=[],
            artist_pictures_dir=os.path.join(self.test_dir, 'Artist Pictures'),
        )
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            win._on_scan_finished(mb_env, [], ['C:\\Fallback\\Music'])
            self.assertIn('MusicBee', win.var_env.get())
            self.assertNotIn('未检测到外部音乐播放器', win.var_env.get())

    def test_gui_startup_instant_adaptation(self):
        # 验证启动时无需触发扫描即可根据当前活跃播放器自适应按钮文案
        from tests.common import offscreen_tk
        import app as app_mod
        with offscreen_tk() as root:
            with patch('ui.main_window.get_running_musicbee_exes', return_value=[r'C:\MusicBee\MusicBee.exe']), \
                 patch('ui.main_window.get_running_foobar_exes', return_value=[]), \
                 patch('ui.main_window.get_running_roon_exes', return_value=[]):
                win = app_mod.MainWindow(root, auto_scan=False)
                self.assertEqual(win.btn_sync_only.cget('text'), '仅刷新 MusicBee 缓存')
                self.assertEqual(win.chk_sync.cget('text'), '刷新 MusicBee 缓存')
                self.assertEqual(win.chk_sync_mb_artist.winfo_manager(), 'pack')
                self.assertIn('MusicBee', win.var_env.get())


if __name__ == '__main__':
    unittest.main()
