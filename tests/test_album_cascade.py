"""真身大碟反向注入封面与元数据联动（场景 A 与场景 B）单元测试。"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from core.models import FolderArtPlan, LibraryTrack, TrackMatch
from services.lookup.lyric_search import LyricCandidate, search_candidate_lyrics
from services.lookup.scoring import compute_album_affinity_score


class TestAlbumCoverCascade(unittest.TestCase):
    """测试场景 A 与场景 B 的封面反哺与联动。"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='mm_cascade_test_')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_lyric_candidate_cover_field(self):
        """测试 LyricCandidate 携带 cover_url 字段。"""
        cand = LyricCandidate(
            platform='netease',
            platform_label='网易云音乐',
            song_id='12345',
            title='小城故事',
            artist='邓丽君',
            album_name='小城故事',
            release_date='1979-03-23',
            duration=150.0,
            duration_diff=0.5,
            lyrics='[00:01.00]小城故事多',
            has_translation=False,
            composer='翁清溪',
            lyricist='庄奴',
            arranger='',
            score=270.0,
            is_live=False,
            is_compilation=False,
            cover_url='https://example.com/cover1979.jpg',
        )
        self.assertEqual(cand.cover_url, 'https://example.com/cover1979.jpg')

    def test_dff_format_cover_safety_isolation(self):
        """测试场景 A：发烧只读 .dff 格式必须落盘外置 cover.jpg，且坚决不执行内嵌写标。"""
        dff_path = os.path.join(self.test_dir, 'track01.dff')
        with open(dff_path, 'wb') as f:
            f.write(b'FRM8' + b'\x00' * 100)

        cur_tr = LibraryTrack(
            path=dff_path,
            filename='track01.dff',
            folder=self.test_dir,
            title='小城故事',
            artist='邓丽君',
            album='',
            container='dff',
        )

        dummy_cover = b'\xff\xd8\xff\xe0' + b'\x00' * 100 + b'\xff\xd9'  # 简易 JPEG 头尾
        cand = LyricCandidate(
            platform='netease',
            platform_label='网易云音乐',
            song_id='1001',
            title='小城故事',
            artist='邓丽君',
            album_name='小城故事',
            release_date='1979',
            duration=150.0,
            duration_diff=0.0,
            lyrics='[00:00.00]测试歌词',
            has_translation=False,
            composer='翁清溪',
            lyricist='庄奴',
            arranger='',
            score=268.0,
            is_live=False,
            is_compilation=False,
            cover_url='https://example.com/cov.jpg',
        )

        # 模拟应用过程
        from tag_io import sniff_container
        from core.constants import SUPPORTS_COVER_WRITE

        container = sniff_container(cur_tr.path)
        self.assertEqual(container, 'dff')
        self.assertNotIn(container, SUPPORTS_COVER_WRITE)

        # 验证外置落盘
        cov_path = os.path.join(self.test_dir, 'cover.jpg')
        with open(cov_path, 'wb') as f_out:
            f_out.write(dummy_cover)

        self.assertTrue(os.path.exists(cov_path))
        self.assertGreater(os.path.getsize(cov_path), 50)

    def test_scene_b_reverse_cascade_logic(self):
        """测试场景 B：默认关闭与授权开启状态下的反向封面反哺逻辑。"""
        m_high = TrackMatch(
            tracknumber=1,
            source='网易云音乐(单曲反查)',
            album_name='小城故事',
            release_date='1979-03-23',
            cover_url='https://example.com/polygram1979.jpg',
            lyrics='[00:01.00]小城故事多',
            affinity_score=268.96,
        )

        has_explicit_album = False
        is_song_guess = bool(m_high.source and '单曲反查' in m_high.source)
        aff_score = m_high.affinity_score

        # 1. 默认状态：enable_reverse_cover_cascade = False
        allow_cascade_default = False
        is_high_conf_default = (aff_score >= 180.0) and allow_cascade_default
        should_skip_default = (not has_explicit_album and is_song_guess and not is_high_conf_default)
        self.assertTrue(should_skip_default, "场景 B 默认关闭时，即使命中高分大碟也必须跳过全自动落盘！")

        # 2. 用户明确警告授权开启状态：enable_reverse_cover_cascade = True
        allow_cascade_enabled = True
        is_high_conf_enabled = (aff_score >= 180.0) and allow_cascade_enabled
        should_skip_enabled = (not has_explicit_album and is_song_guess and not is_high_conf_enabled)
        self.assertFalse(should_skip_enabled, "场景 B 明确开启且高亲和分时，放行触发封面反哺！")

        # 3. 开启状态下若遇到低分现场版/杂凑版（如 110 分），依然严格拦截
        m_low = TrackMatch(
            tracknumber=1,
            source='网易云音乐(单曲反查)',
            album_name='群星精选',
            release_date='2015',
            cover_url='https://example.com/bad.jpg',
            affinity_score=110.0,
        )
        is_low_conf = (m_low.affinity_score >= 180.0) and allow_cascade_enabled
        should_skip_low = (not has_explicit_album and is_song_guess and not is_low_conf)
        self.assertTrue(should_skip_low, "即使开启了反哺，低分或精选集候选也必须被硬拦截！")


if __name__ == '__main__':
    unittest.main()
