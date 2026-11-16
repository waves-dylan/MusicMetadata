"""单元测试：模式感知歌词健全度判定、零网络本地歌词互转与 UI 响应式徽章联动。"""

from __future__ import annotations

import os
import unittest

from core.models import LibraryTrack, PlayerEnv
from infra.scanner.local_scanner import scan_library, track_has_pending_work, track_needs_lyrics
from pipelines.options import FixOptions, default_checked
from pipelines.repair import execute_repairs
from services.art.writer import apply_track_repairs
from tag_io import read_audio_info, write_lrc_file
from tests.common import TempTrackTestCase
from ui.main_window import _format_status_badge, _format_tag_sync_cell


class TestLyricsModePromotion(TempTrackTestCase):
    """测试歌词模式感知与本地互转快车道。"""

    def test_track_needs_lyrics_evaluation(self):
        # 场景 1: DSF 仅有内嵌歌词，无外置 .lrc
        tr_emb = LibraryTrack(
            path='test.dsf', filename='test.dsf', folder='',
            container='dsf', has_lyrics=True, has_embedded_lyrics=True,
            has_lrc_file=False, needs_lyrics=False,
        )
        self.assertFalse(track_needs_lyrics(tr_emb, 'auto'))
        self.assertFalse(track_needs_lyrics(tr_emb, 'embed'))
        self.assertTrue(track_needs_lyrics(tr_emb, 'lrc'))
        self.assertTrue(track_needs_lyrics(tr_emb, 'both'))

        # 场景 2: FLAC 仅有外置 .lrc，无内嵌歌词
        tr_lrc = LibraryTrack(
            path='test.flac', filename='test.flac', folder='',
            container='flac', has_lyrics=True, has_embedded_lyrics=False,
            has_lrc_file=True, needs_lyrics=False,
        )
        self.assertFalse(track_needs_lyrics(tr_lrc, 'auto'))
        self.assertTrue(track_needs_lyrics(tr_lrc, 'embed'))
        self.assertFalse(track_needs_lyrics(tr_lrc, 'lrc'))
        self.assertTrue(track_needs_lyrics(tr_lrc, 'both'))

        # 场景 3: FLAC 内嵌与外置均具备 (双全)
        tr_both = LibraryTrack(
            path='test.flac', filename='test.flac', folder='',
            container='flac', has_lyrics=True, has_embedded_lyrics=True,
            has_lrc_file=True, needs_lyrics=False,
        )
        self.assertFalse(track_needs_lyrics(tr_both, 'auto'))
        self.assertFalse(track_needs_lyrics(tr_both, 'embed'))
        self.assertFalse(track_needs_lyrics(tr_both, 'lrc'))
        self.assertFalse(track_needs_lyrics(tr_both, 'both'))

        # 场景 4: DFF 格式遵循规范只读保护，不支持内嵌歌词
        tr_dff = LibraryTrack(
            path='test.dff', filename='test.dff', folder='',
            container='dff', has_lyrics=True, has_embedded_lyrics=False,
            has_lrc_file=True, needs_lyrics=False,
        )
        self.assertFalse(track_needs_lyrics(tr_dff, 'auto'))
        self.assertFalse(track_needs_lyrics(tr_dff, 'embed'))
        self.assertFalse(track_needs_lyrics(tr_dff, 'both'))

    def test_default_checked_with_lyrics_mode(self):
        tr_emb = LibraryTrack(
            path='test.dsf', filename='test.dsf', folder='',
            container='dsf', has_lyrics=True, has_embedded_lyrics=True,
            has_lrc_file=False, needs_lyrics=False,
        )
        # 在 auto 模式下不勾选，在 both 模式下自动勾选待处理
        opts_auto = FixOptions(enable_lyrics=True, lyrics_mode='auto')
        opts_both = FixOptions(enable_lyrics=True, lyrics_mode='both')
        self.assertFalse(default_checked(tr_emb, options=opts_auto))
        self.assertTrue(default_checked(tr_emb, options=opts_both))

    def test_local_promotion_embedded_to_lrc(self):
        """测试已有内嵌歌词的 DSF 文件在 both 模式下一键零网络导出同名 .lrc 文件。"""
        dsf_path = self._make_minimal_dsf('track01.dsf')
        lrc_text = "[ti:Test Title]\n[ar:Test Artist]\n[00:01.00]Embedded Lyric Line"
        ok_emb, _ = apply_track_repairs(dsf_path, lyrics=lrc_text, lyrics_mode='embed')
        self.assertTrue(ok_emb)

        lrc_path = os.path.splitext(dsf_path)[0] + '.lrc'
        self.assertFalse(os.path.exists(lrc_path), "初态不应存在外置 .lrc")

        tracks = scan_library(folders=[self.tmp], include_art_scan=False)
        self.assertEqual(len(tracks), 1)
        tr = tracks[0]
        self.assertTrue(tr.has_embedded_lyrics)
        self.assertFalse(tr.has_lrc_file)

        # 执行修复管线 (both 模式，仅专注歌词，关闭其他标签以走纯本地快车道)
        opts = FixOptions(
            enable_track=False,
            enable_tags=False,
            enable_art=False,
            enable_lyrics=True,
            lyrics_mode='both',
        )
        env = PlayerEnv(player_type='generic')
        res = execute_repairs(env, [tr], opts)

        self.assertEqual(res['written_ok'], 1)
        self.assertEqual(res['lyrics_fixed'], 1)
        self.assertTrue(os.path.isfile(lrc_path), "修复后应自动导出外置 .lrc 文件")
        with open(lrc_path, 'r', encoding='utf-8') as fp:
            content = fp.read()
        self.assertIn("Embedded Lyric Line", content)

    def test_local_promotion_lrc_to_embedded(self):
        """测试已有外置 .lrc 的 FLAC 文件在 both 模式下一键零网络注入内嵌歌词标签。"""
        flac_path = self._make_minimal_flac('track02.flac')
        lrc_text = "[00:02.50]External LRC Line to Embed"
        ok_lrc, _ = write_lrc_file(flac_path, lrc_text)
        self.assertTrue(ok_lrc)

        info_before = read_audio_info(flac_path)
        self.assertFalse(info_before['has_embedded_lyrics'])
        self.assertTrue(info_before['has_lrc_file'])

        tracks = scan_library(folders=[self.tmp], include_art_scan=False)
        self.assertEqual(len(tracks), 1)
        tr = tracks[0]

        opts = FixOptions(
            enable_track=False,
            enable_tags=False,
            enable_art=False,
            enable_lyrics=True,
            lyrics_mode='both',
        )
        env = PlayerEnv(player_type='generic')
        res = execute_repairs(env, [tr], opts)

        self.assertEqual(res['written_ok'], 1)
        self.assertEqual(res['lyrics_fixed'], 1)

        info_after = read_audio_info(flac_path)
        self.assertTrue(info_after['has_embedded_lyrics'], "修复后应已写入内嵌歌词")
        self.assertTrue(info_after['has_lrc_file'], "外置歌词应保持存在")

    def test_ui_badges_reactive_to_lyrics_mode(self):
        """测试主界面状态徽章与单元格文字随 lyrics_mode 响应式展示。"""
        tr = LibraryTrack(
            path='demo.dsf', filename='demo.dsf', folder='',
            container='dsf', has_lyrics=True, has_embedded_lyrics=True,
            has_lrc_file=False, needs_lyrics=False,
        )
        # auto 模式下判定为正常
        st_auto, _ = _format_status_badge(tr, include_lyrics=True, lyrics_mode='auto')
        self.assertEqual(st_auto, '正常')
        cell_auto = _format_tag_sync_cell(tr, include_lyrics=True, lyrics_mode='auto')
        self.assertEqual(cell_auto, '正常')

        # both 模式下准确展示缺外置歌词
        st_both, color = _format_status_badge(tr, include_lyrics=True, lyrics_mode='both')
        self.assertEqual(st_both, '缺外置歌词')
        self.assertEqual(color, 'warn')
        cell_both = _format_tag_sync_cell(tr, include_lyrics=True, lyrics_mode='both')
        self.assertIn('待补外置.lrc', cell_both)


if __name__ == '__main__':
    unittest.main()
