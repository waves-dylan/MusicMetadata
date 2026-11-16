"""单元测试：元数据与歌词亲和度智能加权打分引擎（scoring.py 与 resolver 联动）。"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from core.models import LibraryTrack
from services.lookup.scoring import (
    clean_album_noise,
    clean_title_noise,
    compute_album_affinity_score,
    is_disqualified,
    rank_song_hits,
    text_similarity,
)
from services.lookup.resolver import _search_song_and_resolve_album


class TestLyricScoringEngine(unittest.TestCase):
    """测试专辑亲和度打分引擎与降噪规则。"""

    def test_clean_album_noise(self):
        cases = [
            ("岛国之情歌第六集 (Remastered 2020) [Deluxe Edition]", "岛国之情歌第六集"),
            ("【环球复黑王】小城故事 [首版]", "小城故事"),
            ("BTB 邓丽君极品 - 小城故事", "邓丽君极品 - 小城故事"),
            ("邓丽君 15周年 (24bit/96kHz Hi-Res)", "邓丽君 15周年"),
            ("普通大碟名称", "普通大碟名称"),
        ]
        for raw, expected in cases:
            self.assertEqual(clean_album_noise(raw), expected)

    def test_clean_title_noise(self):
        cases = [
            ("小城故事 (Live 1982)", "小城故事"),
            ("小城故事 (2020 Remaster)", "小城故事"),
            ("小城故事 [现场版]", "小城故事"),
            ("小城故事", "小城故事"),
        ]
        for raw, expected in cases:
            self.assertEqual(clean_title_noise(raw), expected)

    def test_is_disqualified(self):
        self.assertTrue(is_disqualified("小城故事 (伴奏)", "岛国之情歌第六集"))
        self.assertTrue(is_disqualified("小城故事", "小城故事 (纯音乐)"))
        self.assertTrue(is_disqualified("小城故事 [Karaoke]", "小城故事"))
        self.assertTrue(is_disqualified("小城故事 - Instrumental", "小城故事"))
        self.assertFalse(is_disqualified("小城故事", "岛国之情歌第六集"))

    def test_affinity_score_studio_beats_compilation_and_live(self):
        """测试原版录音室大碟的得分必须显著压制同名精选集与演唱会 Live 版。"""
        t_title = "小城故事"
        t_artist = "邓丽君"
        t_album = "岛国之情歌第六集"

        # 录音室原版母碟 (1979 年)
        score_studio = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album=t_album,
            target_title=t_title,
            target_artist=t_artist,
            cand_artist=t_artist,
            cand_title=t_title,
            release_date="1979-03-01",
            duration_diff=0.0,
        )

        # 2018 年后期拼盘精选集
        score_compilation = compute_album_affinity_score(
            cand_album="邓丽君 经典极品精选 (Greatest Hits)",
            target_album=t_album,
            target_title=t_title,
            target_artist=t_artist,
            cand_artist=t_artist,
            cand_title=t_title,
            release_date="2018-05-01",
            duration_diff=0.0,
        )

        # 1982 年现场演唱会 Live 版
        score_live = compute_album_affinity_score(
            cand_album="邓丽君难忘的演唱会 Live",
            target_album=t_album,
            target_title=t_title,
            target_artist=t_artist,
            cand_artist=t_artist,
            cand_title=t_title,
            release_date="1982-01-01",
            duration_diff=1.5,
        )

        self.assertGreater(score_studio, 220.0, "原版录音室大碟应获得 220+ 高分")
        self.assertLess(score_compilation, 120.0, "精选集应被重度扣分")
        self.assertLess(score_live, 140.0, "现场演唱会版应被扣分")
        self.assertGreater(score_studio - score_compilation, 100.0, "原版大碟相对精选集优势应 > 100 分")

    def test_affinity_score_duration_hard_cutoff(self):
        """时差超过 8.0 秒公差上限的项必须致命淘汰 (-9999)，而 8.0 秒以内微小公差应平滑保留。"""
        # 时差超过 8.0 秒 -> 致命淘汰
        fatal_score = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            duration_diff=8.5,
        )
        self.assertEqual(fatal_score, -9999.0)

        # 5.4 秒微小公差（发烧碟前奏/尾奏静音差异）应平滑扣分保留，不被断崖式淘汰
        grace_score = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            duration_diff=5.4,
        )
        self.assertGreater(grace_score, 180.0)

    def test_affinity_score_prefer_rich_lyrics(self):
        """测试开启 prefer_rich_lyrics 选项时双语翻译与词曲信息的加权表现。"""
        # 未开启富歌词偏好时
        s_plain = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            has_translation=True,
            has_credits=True,
            prefer_rich_lyrics=False,
        )
        s_empty = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            has_translation=False,
            has_credits=False,
            prefer_rich_lyrics=False,
        )
        # 默认只差 5 分微调
        self.assertAlmostEqual(s_plain - s_empty, 5.0, places=1)

        # 开启偏好后：双语(+20) + 词曲(+15) = +35 分
        s_rich_pref = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            has_translation=True,
            has_credits=True,
            prefer_rich_lyrics=True,
        )
        s_plain_pref = compute_album_affinity_score(
            cand_album="岛国之情歌第六集",
            target_album="岛国之情歌第六集",
            target_title="小城故事",
            target_artist="邓丽君",
            has_translation=False,
            has_credits=False,
            prefer_rich_lyrics=True,
        )
        self.assertAlmostEqual(s_rich_pref - s_plain_pref, 35.0, places=1)

    def test_rank_song_hits_reorders_compilation_to_bottom(self):
        """测试 rank_song_hits 将默认排在首位的精选集打到后位，将原版大碟置顶。"""
        raw_hits = [
            {
                'song_id': '101',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_name': '邓丽君 极品精选金曲 (Greatest Hits)',
                'duration': 155.0,
                'release_date': '2019-01-01',
            },
            {
                'song_id': '102',
                'title': '小城故事 (伴奏)',
                'artist': '邓丽君',
                'album_name': '小城故事伴奏原声',
                'duration': 155.0,
                'release_date': '2019-01-01',
            },
            {
                'song_id': '103',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_name': '岛国之情歌第六集',
                'duration': 155.0,
                'release_date': '1979-03-01',
            },
        ]

        ranked = rank_song_hits(
            raw_hits,
            target_title="小城故事",
            target_artist="邓丽君",
            target_album="岛国之情歌第六集",
            duration=155.0,
        )

        # 录音室原版大碟应被推至首位
        self.assertEqual(ranked[0]['song_id'], '103')
        self.assertEqual(ranked[0]['album_name'], '岛国之情歌第六集')
        # 伴奏被标记淘汰 (-9999) 且排在末尾
        self.assertEqual(ranked[-1]['song_id'], '102')
        self.assertEqual(ranked[-1]['affinity_score'], -9999.0)

    def test_search_song_and_resolve_album_selects_studio_over_compilation(self):
        """集成测试：_search_song_and_resolve_album 在多候选时优先挑选录音室母碟。"""
        mock_provider = MagicMock()
        mock_provider.search_song_hits.return_value = [
            {
                'song_id': 's_comp',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_id': 'a_comp',
                'album_name': '邓丽君 经典至尊精选',
                'duration': 150.0,
                'release_date': '2020-01-01',
                'cover_url': 'http://example.com/comp.jpg',
            },
            {
                'song_id': 's_studio',
                'title': '小城故事',
                'artist': '邓丽君',
                'album_id': 'a_studio',
                'album_name': '岛国之情歌第六集',
                'duration': 150.0,
                'release_date': '1979-03-01',
                'cover_url': 'http://example.com/studio.jpg',
            },
        ]
        mock_provider.get_song_extra.return_value = {
            'lyrics': '[00:01.00]小城故事多',
            'composer': '汤尼',
            'lyricist': '庄奴',
        }

        # 模拟对应专辑的曲目列表
        def _mock_tracks(*args, **kwargs):
            aid = args[1] if len(args) > 1 else (args[0] if args else '')
            if aid == 'a_studio':
                return [{'id': 's_studio', 'name': '小城故事', 'artist': '邓丽君', 'duration': 150.0, 'number': 1}]
            return [{'id': 's_comp', 'name': '小城故事', 'artist': '邓丽君', 'duration': 150.0, 'number': 5}]

        mock_provider.get_album_tracks.side_effect = _mock_tracks

        track = LibraryTrack(
            path='D:/Music/01 - 小城故事.flac',
            filename='01 - 小城故事.flac',
            folder='D:/Music',
            title='小城故事',
            artist='邓丽君',
            album='岛国之情歌第六集',
            duration=150.0,
        )

        with patch.dict('services.lookup.resolver.PROVIDERS', {'netease': mock_provider}):
            with patch('services.lookup.resolver._fetch_album_tracks_for', side_effect=_mock_tracks):
                with patch('services.lookup.resolver.get_album_date', return_value='1979-03-01'):
                    res = _search_song_and_resolve_album(
                        '小城故事', '邓丽君', 150.0, track,
                        need_track=True, need_date=True, need_lyrics=True,
                        plat_order=('netease',),
                    )

        self.assertIsNotNone(res)
        self.assertEqual(res.album_name, '岛国之情歌第六集', "应准确解析并返回录音室原版大碟而非精选集")
        self.assertEqual(res.tracknumber, 1)


if __name__ == '__main__':
    unittest.main()
