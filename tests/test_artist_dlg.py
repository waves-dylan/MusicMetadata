"""单元测试：多版本艺术家写真检索、ArtistPickerDialog 弹窗与 CoverPickerDialog 伴随联动。"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch
from PIL import Image

from core.models import LibraryTrack
from services.lookup.resolver import (
    search_candidate_artist_pics,
    ARTIST_BYTES_CACHE,
)
from tests.common import offscreen_tk
from ui.dialogs.artist_dlg import ArtistPickerDialog
from ui.dialogs.cover_dlg import CoverPickerDialog


class TestArtistSearchAndPickerDialog(unittest.TestCase):
    """测试艺术家写真检索、弹窗交互以及多专辑封面向导联动体系。"""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix='mm_art_test_')

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _make_dummy_jpeg(self, width: int = 200, height: int = 200) -> bytes:
        im = Image.new('RGB', (width, height), color=(120, 160, 200))
        buf = io.BytesIO()
        im.save(buf, format='JPEG')
        return buf.getvalue()

    def test_search_candidate_artist_pics_events_and_scoring(self):
        """测试多音源并发写真探测、事件流触发与加权打分排序。"""
        events: list[str] = []

        mock_prov_tadb = MagicMock()
        mock_prov_tadb.is_available = True
        mock_prov_tadb.label = 'TheAudioDB'
        mock_prov_tadb.search_artist_candidates.return_value = [
            {
                'source': 'TheAudioDB',
                'platform': 'tadb',
                'artist_name': '李娜',
                'pic_url': 'https://r2.theaudiodb.com/images/fanart_1.jpg',
                'aspect_type': 'landscape',
                'resolution': '1920×1080 横版',
                'width': 1920,
                'height': 1080,
                'clues': '民族/声乐 · 舞台写真',
            },
            {
                'source': 'TheAudioDB',
                'platform': 'tadb',
                'artist_name': '李娜',
                'pic_url': 'https://r2.theaudiodb.com/images/thumb_1.jpg',
                'aspect_type': 'square',
                'resolution': '1000×1000 方图',
                'width': 1000,
                'height': 1000,
                'clues': '流行 · 肖像',
            },
        ]

        mock_prov_qq = MagicMock()
        mock_prov_qq.is_available = True
        mock_prov_qq.label = 'QQ音乐'
        mock_prov_qq.search_artist_candidates.return_value = [
            {
                'source': 'QQ音乐',
                'platform': 'qq',
                'artist_name': '李娜',
                'pic_url': 'https://y.gtimg.cn/music/lina_folk.jpg',
                'aspect_type': 'square',
                'resolution': '800×800',
                'width': 800,
                'height': 800,
                'clues': '代表作:《青藏高原》· 专辑 26 张',
            },
        ]

        with patch.dict('services.lookup.resolver.PROVIDERS', {'tadb': mock_prov_tadb, 'qq': mock_prov_qq}, clear=False):
            cands = search_candidate_artist_pics(
                '李娜',
                track_hint='青藏高原',
                limit=10,
                prefer_sources=['tadb', 'qq'],
                on_progress=lambda e: events.append(e.get('type')),
            )

        self.assertIn('start', events)
        self.assertIn('platform_start', events)
        self.assertIn('platform_done', events)
        self.assertIn('scoring', events)
        self.assertIn('done', events)

        self.assertGreaterEqual(len(cands), 3)

        # 命中代表作《青藏高原》的李娜应当获得同名线索加分 (+30) 并在方图中置顶
        qq_cand = next(c for c in cands if c['platform'] == 'qq')
        self.assertIn('青藏高原', qq_cand['clues'])
        self.assertIn(qq_cand['badge'], ('★首选', '推荐'))

        # 1920x1080 横版写真壁纸应有完整 resolution 与 aspect 标注
        tadb_landscape = next(c for c in cands if c['aspect_type'] == 'landscape')
        self.assertEqual(tadb_landscape['resolution'], '1920×1080 横版')
        self.assertEqual(tadb_landscape['width'], 1920)

    def test_search_candidate_artist_pics_va_guard(self):
        """测试合辑/群星/Various Artists 输入时自动拒绝。"""
        cands = search_candidate_artist_pics('Various Artists')
        self.assertEqual(cands, [])
        cands_qx = search_candidate_artist_pics('群星')
        self.assertEqual(cands_qx, [])

    def test_artist_picker_dialog_ui_init_single(self):
        """测试 ArtistPickerDialog 单艺人模式初始化及控件状态。"""
        with offscreen_tk() as root:
            track = LibraryTrack(
                path=os.path.join(self.test_dir, '01. Beyond - 海阔天空.flac'),
                filename='01. Beyond - 海阔天空.flac',
                folder=self.test_dir,
                title='海阔天空',
                artist='Beyond',
                album='海阔天空',
            )

            with patch('ui.dialogs.artist_dlg.search_candidate_artist_pics', return_value=[]):
                dlg = ArtistPickerDialog(
                    root,
                    'Beyond',
                    folder=self.test_dir,
                    target_tracks=[track],
                    auto_search=False,
                )

                self.assertFalse(dlg.is_multi)
                self.assertEqual(dlg.var_query.get(), 'Beyond')
                self.assertTrue(dlg.var_save_dir.get())
                self.assertTrue(dlg.var_embed_tag.get())
                self.assertIn('● 写真大图', dlg.btn_tab_preview['text'])
                self.assertIn('检索看板', dlg.btn_tab_dash['text'])

                # 切换视图模式
                dlg._switch_view_mode('dash')
                self.assertEqual(dlg._view_mode, 'dash')
                dlg._switch_view_mode('preview')
                self.assertEqual(dlg._view_mode, 'preview')

                dlg.top.destroy()
                del dlg
                import gc
                gc.collect()

    def test_artist_picker_dialog_ui_init_multi_and_nav(self):
        """测试 ArtistPickerDialog 多艺人模式下的下拉切换与向导导航。"""
        with offscreen_tk() as root:
            groups = [
                {'artist': 'Beyond', 'folder': self.test_dir, 'tracks': [], 'hint': '光辉岁月', 'saved': False},
                {'artist': '邓丽君', 'folder': self.test_dir, 'tracks': [], 'hint': '甜蜜蜜', 'saved': False},
            ]

            with patch('ui.dialogs.artist_dlg.search_candidate_artist_pics', return_value=[]):
                dlg = ArtistPickerDialog(
                    root,
                    groups,
                    auto_search=False,
                )

                self.assertTrue(dlg.is_multi)
                self.assertEqual(dlg._current_index, 0)
                self.assertEqual(dlg.var_query.get(), 'Beyond')

                # 切换下一个
                dlg._on_next_artist()
                self.assertEqual(dlg._current_index, 1)
                self.assertEqual(dlg.var_query.get(), '邓丽君')
                self.assertEqual(dlg.btn_confirm['text'], '★ 应用并完成退出')

                # 切换上一个
                dlg._on_prev_artist()
                self.assertEqual(dlg._current_index, 0)
                self.assertEqual(dlg.var_query.get(), 'Beyond')

                dlg.top.destroy()
                del dlg
                import gc
                gc.collect()

    def test_artist_picker_dialog_apply_actions(self):
        """测试选中写真后的多目标组合落盘分发（目录外置、内嵌标签与MusicBee中央库）。"""
        with offscreen_tk() as root:
            mb_dir = os.path.join(self.test_dir, 'MusicBeePictures')
            os.makedirs(mb_dir, exist_ok=True)

            audio_path = os.path.join(self.test_dir, 'test_song.flac')
            # 创建空白测试文件
            with open(audio_path, 'wb') as fp:
                fp.write(b'fLaCdummydata')

            track = LibraryTrack(
                path=audio_path,
                filename='test_song.flac',
                folder=self.test_dir,
                title='真的爱你',
                artist='Beyond',
            )

            dummy_jpeg = self._make_dummy_jpeg(800, 800)
            cands = [{
                'badge': '★首选',
                'resolution': '800×800',
                'aspect_type': 'square',
                'clues': '流行 · 摇滚',
                'source': 'QQ音乐',
                'artist_name': 'Beyond',
                'pic_url': 'https://mock.com/beyond.jpg',
                'width': 800,
                'height': 800,
            }]

            applied_data: list[tuple[dict, dict]] = []

            with patch('ui.dialogs.artist_dlg.search_candidate_artist_pics', return_value=cands):
                with patch('ui.dialogs.artist_dlg.http_get_bytes', return_value=dummy_jpeg):
                    dlg = ArtistPickerDialog(
                        root,
                        'Beyond',
                        folder=self.test_dir,
                        target_tracks=[track],
                        musicbee_artist_dir=mb_dir,
                        on_apply=lambda g, r: applied_data.append((g, r)),
                        auto_search=False,
                    )

                    # 手动完成检索装载
                    with patch.object(dlg, '_load_candidate_preview'):
                        dlg._on_search_finished(cands, dlg._search_thread_id)

                    self.assertEqual(len(dlg.tree.get_children()), 1)

                    # 模拟应用
                    with patch('ui.dialogs.artist_dlg.apply_artist_picture', return_value=True):
                        with patch('ui.dialogs.artist_dlg._msgbox'):
                            dlg._apply_current_group(advance=False)

                    # 验证回调执行
                    self.assertEqual(len(applied_data), 1)
                    grp, res = applied_data[0]
                    self.assertEqual(res['artist'], 'Beyond')

                    # 验证外置目录 artist.jpg 已写入
                    art_file = os.path.join(self.test_dir, 'artist.jpg')
                    self.assertTrue(os.path.isfile(art_file))

                    # 验证 MusicBee 中央图库生成了 Beyond.jpg
                    mb_art_file = os.path.join(mb_dir, 'Beyond.jpg')
                    self.assertTrue(os.path.isfile(mb_art_file))

                    dlg.top.destroy()
                    del dlg
                    import gc
                    gc.collect()

    def test_cover_picker_artist_linkage_companion_capsule(self):
        """测试多专辑封面向导内嵌伴随胶囊的状态反馈与会话记忆池。"""
        with offscreen_tk() as root:
            folder_a = os.path.join(self.test_dir, 'AlbumA')
            folder_b = os.path.join(self.test_dir, 'AlbumB')
            os.makedirs(folder_a, exist_ok=True)
            os.makedirs(folder_b, exist_ok=True)

            t1 = LibraryTrack(path=os.path.join(folder_a, '01.flac'), filename='01.flac', folder=folder_a, title='大地', artist='Beyond', album='秘密警察')
            t2 = LibraryTrack(path=os.path.join(folder_b, '02.flac'), filename='02.flac', folder=folder_b, title='长城', artist='Beyond', album='继续革命')

            groups = [
                {'folder': folder_a, 'rep_track': t1, 'tracks': [t1], 'album_name': '秘密警察', 'artist': 'Beyond', 'title': '大地', 'saved': False},
                {'folder': folder_b, 'rep_track': t2, 'tracks': [t2], 'album_name': '继续革命', 'artist': 'Beyond', 'title': '长城', 'saved': False},
            ]

            with patch('ui.dialogs.cover_dlg.search_candidate_album_covers', return_value=[]):
                dlg = CoverPickerDialog(
                    root,
                    groups,
                    auto_search=False,
                )

                # 初始状态：未配置且本地无写真 -> 显示缺失警示
                self.assertIn('缺失', dlg.btn_artist_capsule['text'])

                # 模拟在第一张专辑为 Beyond 配置了写真并注入会话记忆池
                fake_pic = self._make_dummy_jpeg(600, 600)
                dlg._wizard_session_memory['Beyond'] = fake_pic
                dlg._update_artist_capsule()

                # 胶囊状态应立即更新为已配置
                self.assertIn('本次已配置', dlg.btn_artist_capsule['text'])

                # 切换到 Beyond 的第二张专辑，记忆池生效，胶囊依然展示已配置
                dlg._load_group(1, auto_search=False)
                self.assertIn('本次已配置', dlg.btn_artist_capsule['text'])

                dlg.top.destroy()
                del dlg
                import gc
                gc.collect()

    def test_cover_picker_post_wizard_chaining_settlement(self):
        """测试多专辑向导结束时盘点未配置艺人并启动补漏向导。"""
        with offscreen_tk() as root:
            folder_a = os.path.join(self.test_dir, 'Album1')
            folder_b = os.path.join(self.test_dir, 'Album2')
            os.makedirs(folder_a, exist_ok=True)
            os.makedirs(folder_b, exist_ok=True)

            t1 = LibraryTrack(path=os.path.join(folder_a, '01.flac'), filename='01.flac', folder=folder_a, title='真的爱你', artist='Beyond')
            t2 = LibraryTrack(path=os.path.join(folder_b, '02.flac'), filename='02.flac', folder=folder_b, title='石头记', artist='达明一派')

            groups = [
                {'folder': folder_a, 'rep_track': t1, 'tracks': [t1], 'album_name': 'Beyond IV', 'artist': 'Beyond', 'title': '真的爱你', 'saved': True},
                {'folder': folder_b, 'rep_track': t2, 'tracks': [t2], 'album_name': '石头记', 'artist': '达明一派', 'title': '石头记', 'saved': True},
            ]

            with patch('ui.dialogs.cover_dlg.search_candidate_album_covers', return_value=[]):
                dlg = CoverPickerDialog(
                    root,
                    groups,
                    auto_search=False,
                )

                # 将 Beyond 标记为已在会话配置
                dlg._wizard_session_memory['Beyond'] = b'dummy'

                # 收集缺失写真艺人：Beyond 已配置应被排除，达明一派应被命中
                missing = dlg._collect_missing_artist_groups()
                self.assertNotIn('Beyond', missing)
                self.assertIn('达明一派', missing)

                dlg.top.destroy()
                del dlg
                import gc
                gc.collect()


if __name__ == '__main__':
    unittest.main()
