"""Roon 播放器适配与 MusicBrainz / Roon Tags 自定义标签单元测试。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TXXX
from mutagen.mp3 import MP3

from core.constants import PLAYER_TYPE_FOOBAR, PLAYER_TYPE_GENERIC, PLAYER_TYPE_ROON
from core.models import LibraryTrack, PlayerEnv
from infra.integrations import (
    RoonAdapter,
    detect_active_player,
    detect_roon_env,
    sync_files_to_roon,
)
from pipelines.executor import _sync_player_targets
from services.art.writer import (
    apply_tag_field,
    delete_tag_field_from_audio,
    read_custom_tag_field,
)
from tag_io import sniff_container
from tests.common import SAMPLE_163_KEY, TempTrackTestCase, offscreen_tk


class TestRoonIntegration(TempTrackTestCase):
    def test_detect_roon_env_custom_dir(self):
        fake_roon_dir = os.path.join(self.tmp, 'RoonServer', 'Application')
        os.makedirs(fake_roon_dir, exist_ok=True)
        fake_exe = os.path.join(fake_roon_dir, 'RoonServer.exe')
        with open(fake_exe, 'wb') as fp:
            fp.write(b'MZ_FAKE_PE')

        env = detect_roon_env(custom_dir=fake_roon_dir)
        self.assertEqual(env.player_type, PLAYER_TYPE_ROON)
        self.assertEqual(env.player_name, 'Roon')
        self.assertEqual(os.path.normpath(env.exe_path), os.path.normpath(fake_exe))
        self.assertTrue(env.extra_info.get('is_server'))
        self.assertIn('mtime', env.reload_command)

    def test_detect_active_player_priority(self):
        # 1. 显式指定 preferred='roon'
        env, adapter = detect_active_player(preferred='roon')
        self.assertEqual(env.player_type, PLAYER_TYPE_ROON)
        self.assertIsInstance(adapter, RoonAdapter)

        # 2. 模拟仅 Roon 运行中
        with patch('infra.integrations.get_running_foobar_exes', return_value=[]), \
             patch('infra.integrations.get_running_musicbee_exes', return_value=[]), \
             patch('infra.integrations.get_running_roon_exes', return_value=[r'C:\Users\User\AppData\Local\Roon\Application\Roon.exe']):
            env, adapter = detect_active_player()
            self.assertEqual(env.player_type, PLAYER_TYPE_ROON)
            self.assertIsInstance(adapter, RoonAdapter)

    def test_sync_files_to_roon(self):
        flac_path = self._make_minimal_flac('roon_sync.flac')
        old_mtime = os.path.getmtime(flac_path) - 10
        os.utime(flac_path, (old_mtime, old_mtime))

        env = PlayerEnv(player_type=PLAYER_TYPE_ROON)
        touched, reloaded = sync_files_to_roon(env, [flac_path])
        self.assertEqual(touched, 1)
        self.assertTrue(reloaded)
        self.assertGreater(os.path.getmtime(flac_path), old_mtime)

    def test_roon_and_musicbrainz_tag_write_read_and_delete_flac(self):
        flac_path = self._make_minimal_flac('mb_roon.flac')
        audio = FLAC(flac_path)

        # 写入 Roon 与 MusicBrainz 标签
        mb_album_id = 'c1234567-89ab-cdef-0123-456789abcdef'
        mb_track_id = 'd1234567-89ab-cdef-0123-456789abcdef'
        self.assertTrue(apply_tag_field(audio, 'flac', 'musicbrainz_albumid', mb_album_id))
        self.assertTrue(apply_tag_field(audio, 'flac', 'musicbrainz_trackid', mb_track_id))
        self.assertTrue(apply_tag_field(audio, 'flac', 'roonalbumtag', 'Audiophile Master'))
        self.assertTrue(apply_tag_field(audio, 'flac', 'roontracktag', 'Live Version'))
        self.assertTrue(apply_tag_field(audio, 'flac', 'originaldate', '1982-11-30'))
        self.assertTrue(apply_tag_field(audio, 'flac', 'barcode', '07464381122'))
        self.assertTrue(apply_tag_field(audio, 'flac', 'catalognumber', 'EK 38112'))
        audio.save()

        # 重新读取并验证
        chk_audio = FLAC(flac_path)
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'musicbrainz_albumid'), mb_album_id)
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'musicbrainz_trackid'), mb_track_id)
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'roonalbumtag'), 'Audiophile Master')
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'roontracktag'), 'Live Version')
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'originaldate'), '1982-11-30')
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'barcode'), '07464381122')
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'catalognumber'), 'EK 38112')

        # 删除字段
        self.assertTrue(delete_tag_field_from_audio(chk_audio, 'flac', 'roonalbumtag'))
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'roonalbumtag'), '')
        self.assertTrue(delete_tag_field_from_audio(chk_audio, 'flac', 'musicbrainz_albumid'))
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'musicbrainz_albumid'), '')
        # 验证其它标签未被误删
        self.assertEqual(read_custom_tag_field(chk_audio, 'flac', 'roontracktag'), 'Live Version')

    def test_roon_and_musicbrainz_tag_write_read_and_delete_mp3(self):
        mp3_path = self._make_minimal_mp3('mb_roon.mp3')
        audio = MP3(mp3_path)
        if audio.tags is None:
            audio.add_tags()
        # 植入 163 key 验证互不影响
        audio.tags.add(COMM(encoding=3, lang='eng', desc='', text=[SAMPLE_163_KEY]))

        mb_album_id = 'e1234567-89ab-cdef-0123-456789abcdef'
        self.assertTrue(apply_tag_field(audio, 'mp3', 'musicbrainz_albumid', mb_album_id))
        self.assertTrue(apply_tag_field(audio, 'mp3', 'roonalbumtag', 'Test Album Tag'))
        self.assertTrue(apply_tag_field(audio, 'mp3', 'originaldate', '1995'))
        audio.save()

        chk_audio = MP3(mp3_path)
        self.assertEqual(read_custom_tag_field(chk_audio, 'mp3', 'musicbrainz_albumid'), mb_album_id)
        self.assertEqual(read_custom_tag_field(chk_audio, 'mp3', 'roonalbumtag'), 'Test Album Tag')
        self.assertEqual(read_custom_tag_field(chk_audio, 'mp3', 'originaldate'), '1995')

        # 检查 163 key 依然完好
        comm_texts = [str(f) for f in chk_audio.tags.getall('COMM')]
        self.assertTrue(any('163 key' in t for t in comm_texts))

        # 删除 roonalbumtag
        self.assertTrue(delete_tag_field_from_audio(chk_audio, 'mp3', 'roonalbumtag'))
        self.assertEqual(read_custom_tag_field(chk_audio, 'mp3', 'roonalbumtag'), '')
        # 再次确认 163 key 与 musicbrainz_albumid 未受损
        self.assertEqual(read_custom_tag_field(chk_audio, 'mp3', 'musicbrainz_albumid'), mb_album_id)
        comm_texts = [str(f) for f in chk_audio.tags.getall('COMM')]
        self.assertTrue(any('163 key' in t for t in comm_texts))

    def test_executor_sync_player_targets_roon(self):
        flac_path = self._make_minimal_flac('sync_executor.flac')
        env = PlayerEnv(player_type=PLAYER_TYPE_ROON, player_name='Roon')

        logs = []
        touched, reloaded = _sync_player_targets(
            env=env,
            synced_paths=[flac_path],
            foobar_mtimes=None,
            emit_log=logs.append,
        )
        self.assertEqual(touched, 1)
        self.assertTrue(reloaded)
        self.assertTrue(any('Roon' in line for line in logs))

    def test_gui_roon_mode_adaptation(self):
        from ui.main_window import MainWindow

        with offscreen_tk() as root:
            win = MainWindow(root)
            win._apply_player_mode(is_active=True, player_type=PLAYER_TYPE_ROON)
            self.assertEqual(win.btn_sync_only.cget('text'), '仅刷新 Roon 缓存')
            self.assertEqual(win.chk_sync.cget('text'), '刷新 Roon 缓存')
            self.assertEqual(win.tree.heading('tagsync')['text'], '标签/Roon监控检查')
            # 确认 MusicBee 中央艺术家写真复选框在 Roon 模式下不显示
            self.assertNotEqual(win.chk_sync_mb_artist.winfo_manager(), 'pack')
