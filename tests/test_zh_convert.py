"""单元测试：华语简繁文本转换、消歧引擎、LRC时间轴保护、流水线与界面交互。"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from mutagen.flac import FLAC

from core.models import FoobarEnv, LibraryTrack
from core.zh_norm import (
    KNOWN_JPOP_ARTISTS,
    convert_chinese,
    convert_lrc,
    convert_track_tags,
    diff_tags,
    is_japanese_text,
    is_known_jpop_artist,
    to_simplified,
    to_traditional,
)
from pipelines.options import ZhConvertOptions
from pipelines.zh_convert import execute_zh_convert
from tag_io import get_lrc_path, read_audio_info, write_lrc_file
from tests.common import TempTrackTestCase, offscreen_tk
from ui.dialogs.zh_convert_dlg import ZhConvertDialog


class ZhConvertTests(TempTrackTestCase):
    def test_zh_norm_basic_and_phrases(self):
        # 1. 经典港台歌名与歌手
        self.assertEqual(to_simplified('鄧麗君'), '邓丽君')
        self.assertEqual(to_traditional('邓丽君'), '鄧麗君')
        self.assertEqual(to_simplified('島國之情歌 第六集'), '岛国之情歌 第六集')
        self.assertEqual(to_traditional('岛国之情歌 第六集'), '島國之情歌 第六集')

        # 2. 简繁同形字
        self.assertEqual(to_simplified('小城故事'), '小城故事')
        self.assertEqual(to_traditional('小城故事'), '小城故事')

        # 3. 一简对多繁消歧 (发 -> 發 / 髮)
        self.assertEqual(to_traditional('头发乱了'), '頭髮亂了')
        self.assertEqual(to_traditional('发生何事'), '發生何事')
        self.assertEqual(to_traditional('发行大碟'), '發行大碟')
        self.assertEqual(to_traditional('理发店'), '理髮店')

        # 4. 一简对多繁消歧 (后 -> 后 / 後)
        self.assertEqual(to_traditional('天后'), '天后')
        self.assertEqual(to_traditional('王后'), '王后')
        self.assertEqual(to_traditional('后来'), '後來')
        self.assertEqual(to_traditional('最后'), '最後')

        # 5. 一简对多繁消歧 (复 / 面)
        self.assertEqual(to_traditional('恢复'), '恢復')
        self.assertEqual(to_traditional('复杂'), '複雜')
        self.assertEqual(to_traditional('见面'), '見面')
        self.assertEqual(to_traditional('吃方便面'), '吃方便麵')

        # 6. 音乐专属专有名词白名单（绝不误篡改）
        self.assertEqual(to_traditional('伍佰'), '伍佰')
        self.assertEqual(to_simplified('伍佰'), '伍佰')
        self.assertEqual(to_traditional('伍佰 & China Blue'), '伍佰 & China Blue')
        self.assertEqual(to_traditional('草蜢'), '草蜢')
        self.assertEqual(to_traditional('陶喆'), '陶喆')

    def test_jpop_guard(self):
        # 假名检测
        self.assertTrue(is_japanese_text('宇多田ヒカル'))
        self.assertTrue(is_japanese_text('さくら'))
        self.assertFalse(is_japanese_text('邓丽君'))
        self.assertFalse(is_japanese_text('小城故事'))

        # 知名纯汉字 J-Pop 艺人检测
        self.assertTrue(is_known_jpop_artist('坂本龍一'))
        self.assertTrue(is_known_jpop_artist('久石譲'))
        self.assertTrue(is_known_jpop_artist('米津玄師'))
        self.assertFalse(is_known_jpop_artist('邓丽君'))

        # 转换时保护测试
        self.assertEqual(convert_chinese('宇多田ヒカル', 't2s', preserve_jpop=True), '宇多田ヒカル')
        self.assertEqual(convert_chinese('坂本龍一', 't2s', preserve_jpop=True), '坂本龍一')
        # 若强制关闭保护，则按常规字形转
        self.assertEqual(convert_chinese('坂本龍一', 't2s', preserve_jpop=False), '坂本龙一')

    def test_lrc_conversion_structured(self):
        lrc_raw = (
            "[ti:小城故事]\n"
            "[ar:鄧麗君]\n"
            "[al:島國之情歌 第六集]\n"
            "[00:00.00]小城故事多\n"
            "[00:12.34][00:45.67]頭髮亂了，遇見天后，後來發生了奇蹟\n"
            "純文本歌詞結尾\n"
        )
        lrc_conv = convert_lrc(lrc_raw, direction='t2s')
        lines = lrc_conv.split('\n')
        self.assertIn('[ti:小城故事]', lines)
        self.assertIn('[ar:邓丽君]', lines)
        self.assertIn('[al:岛国之情歌 第六集]', lines)
        self.assertIn('[00:00.00]小城故事多', lines)
        self.assertIn('[00:12.34][00:45.67]头发乱了，遇见天后，后来发生了奇迹', lines)
        self.assertIn('纯文本歌词结尾', lines)

    def test_diff_tags(self):
        sample = {
            'title': '小城故事',
            'artist': '鄧麗君',
            'album': '島國之情歌 第六集',
            'albumartist': '鄧麗君',
            'date': '1979',
        }
        diffs = diff_tags(sample, ('title', 'artist', 'album', 'albumartist', 'date'), direction='t2s')
        diff_dict = {fld: (old, new) for fld, old, new in diffs}
        # 小城故事和 1979 无变化，不应出现在 diffs 中
        self.assertNotIn('title', diff_dict)
        self.assertNotIn('date', diff_dict)
        self.assertEqual(diff_dict['artist'], ('鄧麗君', '邓丽君'))
        self.assertEqual(diff_dict['album'], ('島國之情歌 第六集', '岛国之情歌 第六集'))
        self.assertEqual(diff_dict['albumartist'], ('鄧麗君', '邓丽君'))

        updates = convert_track_tags(sample, ('title', 'artist', 'album'), direction='t2s')
        self.assertEqual(updates, {'artist': '邓丽君', 'album': '岛国之情歌 第六集'})

    def test_execute_zh_convert_pipeline(self):
        # 构造真实临时音频文件
        flac_path = self._make_minimal_flac('teresa.flac')
        flac_obj = FLAC(flac_path)
        flac_obj['title'] = ['小城故事']
        flac_obj['artist'] = ['鄧麗君']
        flac_obj['album'] = ['島國之情歌 第六集']
        flac_obj['albumartist'] = ['鄧麗君']
        flac_obj.save()

        # 写入伴生 .lrc
        lrc_text = "[00:10.00]這是一首小城故事\n[00:20.00]頭髮亂了"
        write_lrc_file(flac_path, lrc_text)

        tr = LibraryTrack(
            path=flac_path,
            filename='teresa.flac',
            folder=self.tmp,
            title='小城故事',
            artist='鄧麗君',
            albumartist='鄧麗君',
            album='島國之情歌 第六集',
        )

        fake_env = FoobarEnv(library_folders=[self.tmp])

        # 1. 预演模式 (dry_run=True)
        opts_dry = ZhConvertOptions(
            direction='t2s',
            fields=('title', 'artist', 'album', 'albumartist'),
            convert_lyrics=True,
            enable_sync=False,
            dry_run=True,
        )
        res_dry = execute_zh_convert(fake_env, [tr], opts_dry)
        self.assertTrue(res_dry['dry_run'])
        self.assertEqual(res_dry['zh_ok'], 1)
        self.assertEqual(res_dry['zh_fail'], 0)
        # 文件应保持原样未改动
        info_check = read_audio_info(flac_path)
        self.assertEqual(info_check['artist'], '鄧麗君')

        # 2. 真实执行 (dry_run=False)
        opts_real = ZhConvertOptions(
            direction='t2s',
            fields=('title', 'artist', 'album', 'albumartist'),
            convert_lyrics=True,
            enable_sync=False,
            dry_run=False,
        )
        res_real = execute_zh_convert(fake_env, [tr], opts_real)
        self.assertFalse(res_real['dry_run'])
        self.assertEqual(res_real['zh_ok'], 1)
        self.assertEqual(res_real['zh_fail'], 0)

        # 校验写盘结果
        info_after = read_audio_info(flac_path)
        self.assertEqual(info_after['artist'], '邓丽君')
        self.assertEqual(info_after['albumartist'], '邓丽君')
        self.assertEqual(info_after['album'], '岛国之情歌 第六集')
        self.assertEqual(info_after['title'], '小城故事')
        # 校验模型内存更新
        self.assertEqual(tr.artist, '邓丽君')
        self.assertEqual(tr.album, '岛国之情歌 第六集')

        # 校验外置 .lrc 写回
        lrc_after = read_audio_info(flac_path).get('lrc_content', '') or ''
        # 直接读取文件
        with open(get_lrc_path(flac_path), 'r', encoding='utf-8') as f:
            content_lrc = f.read()
        self.assertIn('[00:10.00]这是一首小城故事', content_lrc)
        self.assertIn('[00:20.00]头发乱了', content_lrc)

        # 3. 再次执行无变化应跳过 (zh_skipped)
        res_skip = execute_zh_convert(fake_env, [tr], opts_real)
        self.assertEqual(res_skip['zh_ok'], 0)
        self.assertEqual(res_skip['zh_skipped'], 1)

    def test_zh_convert_dialog_offscreen(self):
        with offscreen_tk() as root:
            tr = LibraryTrack(
                path=os.path.join(self.tmp, 'test.flac'),
                filename='test.flac',
                folder=self.tmp,
                title='小城故事',
                artist='鄧麗君',
            )
            dlg = ZhConvertDialog(root, [tr], default_sync=False, has_player=True)
            root.update()
            self.assertEqual(dlg.var_direction.get(), 't2s')
            self.assertTrue(dlg.field_vars['title'].get())
            self.assertTrue(dlg.field_vars['artist'].get())
            self.assertTrue(dlg.field_vars['album'].get())
            self.assertTrue(dlg.field_vars['albumartist'].get())
            self.assertFalse(dlg.var_convert_lyrics.get())

            # 触发预览比对
            dlg._refresh_diff_preview()
            root.update()

            # 确认按钮触发
            dlg._on_confirm()
            self.assertIsNotNone(dlg.result)
            self.assertEqual(dlg.result.direction, 't2s')
            self.assertIn('artist', dlg.result.fields)

    def test_cli_zh_convert(self):
        import types
        from cli import run_cli
        from mutagen.flac import FLAC

        flac_path = self._make_minimal_flac('cli_teresa.flac')
        fl = FLAC(flac_path)
        fl['TITLE'] = ['小城故事']
        fl['ARTIST'] = ['鄧麗君']
        fl['ALBUM'] = ['島國之情歌 第六集']
        fl.save()

        args = types.SimpleNamespace(
            zh_convert=True,
            zh_direction='t2s',
            zh_fields='title,artist,album',
            zh_lyrics=False,
            no_zh_jpop_guard=False,
            folder=self.tmp,
            include_full_album=False,
            no_sync=True,
            dry_run=False,
            set_tags=[],
            convert='',
            delete=[],
            sacd_info='',
            sacd_extract='',
        )
        rc = run_cli(args)
        self.assertEqual(rc, 0)

        info = read_audio_info(flac_path)
        self.assertEqual(info['artist'], '邓丽君')
        self.assertEqual(info['album'], '岛国之情歌 第六集')
