"""单元测试：多版本歌词检索、时间轴偏置与 LyricPickerDialog 弹窗交互。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch
import tkinter as tk

from core.models import LibraryTrack
from services.lookup.lyric_search import (
    LyricCandidate,
    apply_lrc_offset,
    detect_has_translation,
    search_candidate_lyrics,
)
from ui.dialogs.lyric_dlg import LyricPickerDialog


class TestLyricSearchAndPicker(unittest.TestCase):
    """测试歌词检索、LRC 时间轴平移与弹窗。"""

    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception:
            self.root = None

    def tearDown(self):
        if self.root:
            try:
                self.root.destroy()
            except Exception:
                pass

    def test_detect_has_translation(self):
        # 纯原文歌词
        plain_lrc = (
            "[ti:小城故事]\n"
            "[00:01.20]小城故事多\n"
            "[00:05.40]充满喜和乐\n"
            "[00:10.10]若是你到小城来\n"
        )
        self.assertFalse(detect_has_translation(plain_lrc))

        # 双语合并歌词 (多行相同时间戳)
        bilingual_lrc = (
            "[ti:Small Town Stories]\n"
            "[00:01.20]小城故事多\n"
            "[00:01.20]There are many stories in town\n"
            "[00:05.40]充满喜和乐\n"
            "[00:05.40]Full of joy and happiness\n"
            "[00:10.10]若是你到小城来\n"
            "[00:10.10]If you come to the small town\n"
        )
        self.assertTrue(detect_has_translation(bilingual_lrc))

    def test_apply_lrc_offset(self):
        lrc = "[00:01.20]小城故事多\n[00:05.50]充满喜和乐\n"

        # 延后 300ms
        shifted_plus = apply_lrc_offset(lrc, 300)
        self.assertIn("[00:01.50]小城故事多", shifted_plus)
        self.assertIn("[00:05.80]充满喜和乐", shifted_plus)

        # 提前 500ms
        shifted_minus = apply_lrc_offset(lrc, -500)
        self.assertIn("[00:00.70]小城故事多", shifted_minus)
        self.assertIn("[00:05.00]充满喜和乐", shifted_minus)

        # 提前过多下限钳位在 00:00.00
        shifted_zero = apply_lrc_offset(lrc, -5000)
        self.assertIn("[00:00.00]小城故事多", shifted_zero)

    def test_search_candidate_lyrics_ranking(self):
        mock_prov = MagicMock()
        mock_prov.is_available = True
        mock_prov.search_song_hits.return_value = [
            {
                'song_id': 's1_comp',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_name': '邓丽君极品精选 (Greatest Hits)',
                'duration': 150.0,
                'release_date': '2019-01-01',
            },
            {
                'song_id': 's2_studio',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_name': '岛国之情歌第六集',
                'duration': 150.0,
                'release_date': '1979-03-01',
            },
        ]
        mock_prov.get_song_extra.side_effect = lambda sid, **kw: {
            'lyrics': '[00:01.00]小城故事多\n[00:01.00]Many stories',
            'composer': '汤尼',
            'lyricist': '庄奴',
        }

        with patch.dict('services.lookup.lyric_search.PROVIDERS', {'mock_p': mock_prov}):
            with patch.dict('services.lookup.lyric_search.PROVIDER_LABELS', {'mock_p': '测试音源'}):
                cands = search_candidate_lyrics(
                    '小城故事', '邓丽君', album='岛国之情歌第六集', duration=150.0,
                    prefer_sources=['mock_p'],
                )

        self.assertEqual(len(cands), 2)
        # 录音室原版母碟得分最高置顶
        self.assertEqual(cands[0].song_id, 's2_studio')
        self.assertEqual(cands[0].album_name, '岛国之情歌第六集')
        self.assertTrue(cands[0].has_translation)
        self.assertEqual(cands[0].composer, '汤尼')

    def test_lyric_picker_dialog_ui_init(self):
        if not self.root:
            self.skipTest("无图形界面环境，跳过 Tk 控件测试")

        track = LibraryTrack(
            path='D:/Music/01 - 小城故事.flac',
            filename='01 - 小城故事.flac',
            folder='D:/Music',
            title='小城故事',
            artist='邓丽君',
            album='岛国之情歌第六集',
            duration=150.0,
        )

        with patch('ui.dialogs.lyric_dlg.search_candidate_lyrics', return_value=[]):
            dlg = LyricPickerDialog(
                self.root,
                [track],
                auto_search=False,
            )

            # 验证各控件初始化
            self.assertEqual(dlg.var_title.get(), '小城故事')
            self.assertEqual(dlg.var_artist.get(), '邓丽君')
            self.assertEqual(dlg.var_album.get(), '岛国之情歌第六集')
            self.assertEqual(dlg.var_offset.get(), '0')

            # 测试微调时间偏置
            dlg._adjust_offset(200)
            self.assertEqual(dlg._current_offset_ms, 200)
            self.assertEqual(dlg.var_offset.get(), '200')

            dlg._reset_offset()
            self.assertEqual(dlg._current_offset_ms, 0)
            self.assertEqual(dlg.var_offset.get(), '0')

            # 销毁窗口
            dlg.top.destroy()


if __name__ == '__main__':
    unittest.main()
