import os
import sys
import unittest
import tkinter as tk

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from ui.main_window import MainWindow
from core.models import LibraryTrack

class TestUIRefactorPhase1And2(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.win = MainWindow(self.root, auto_scan=False)
        self.win._apply_layout_mode('bottom')

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_menubar_structure(self):
        menubar = self.win.menubar
        self.assertIsNotNone(menubar)
        self.assertEqual(menubar.entrycget(1, 'label'), '文件 / 媒体库 (F)')
        self.assertEqual(menubar.entrycget(2, 'label'), '编辑与挑选 (E)')
        self.assertEqual(menubar.entrycget(3, 'label'), '音频工具 (T)')
        self.assertEqual(menubar.entrycget(4, 'label'), '视图与过滤 (V)')
        self.assertEqual(menubar.entrycget(5, 'label'), '帮助 (H)')

    def test_toolbar_layout_and_grouping(self):
        # 验证核心主按钮
        self.assertIn('一键修复', str(self.win.btn_one_click.cget('text')))
        self.assertIn('刷新', str(self.win.btn_sync_only.cget('text')))
        
        # 验证分隔线存在
        self.assertIsNotNone(self.win.sep_act1)
        self.assertIsNotNone(self.win.sep_act2)

        # 验证精修按钮
        self.assertIn('封面', str(self.win.btn_pick_cover.cget('text')))
        self.assertIn('写真', str(self.win.btn_pick_artist.cget('text')))
        self.assertIn('歌词', str(self.win.btn_pick_lyric.cget('text')))

        # 验证 SACD 与更多工具下拉菜单
        self.assertIn('SACD', str(self.win.btn_sacd.cget('text')))
        self.assertIn('更多工具', str(self.win.btn_tools_menu.cget('text')))
        self.assertIsNotNone(self.win.menu_more_tools)

    def test_buttons_relocated(self):
        # 扫描按钮应该移到了顶部媒体库状态卡片中
        parent_widget = self.win.btn_rescan.nametowidget(self.win.btn_rescan.winfo_parent())
        top_card = self.win.btn_manage_dirs.nametowidget(self.win.btn_manage_dirs.winfo_parent())
        self.assertEqual(parent_widget, top_card)

        # 全选/取消与只显示待处理应该在搜索栏中
        search_parent = self.win.btn_search.nametowidget(self.win.btn_search.winfo_parent())
        toggle_parent = self.win.btn_toggle_all.nametowidget(self.win.btn_toggle_all.winfo_parent())
        pending_parent = self.win.chk_only_pending.nametowidget(self.win.chk_only_pending.winfo_parent())
        self.assertEqual(toggle_parent, search_parent)
        self.assertEqual(pending_parent, search_parent)

        # 默认跳过整轨全长已移至菜单栏，界面不 pack，不占横向空间
        self.assertEqual(self.win.chk_skip_full.winfo_manager(), '')

    def test_selection_helpers(self):
        self.win.var_only_pending.set(False)
        t1 = LibraryTrack(path='p1.flac', filename='p1.flac', folder='D:/Music', title='T1')
        t2 = LibraryTrack(path='p2.flac', filename='p2.flac', folder='D:/Music', title='T2')
        self.win.tracks = [t1, t2]
        self.win.visible_tracks = [t1, t2]

        # 全选
        self.win._set_all_selection(True)
        self.assertIn('p1.flac', self.win.checked_paths)
        self.assertIn('p2.flac', self.win.checked_paths)

        # 反选
        self.win.checked_paths.discard('p1.flac')
        self.win._invert_selection()
        self.assertIn('p1.flac', self.win.checked_paths)
        self.assertNotIn('p2.flac', self.win.checked_paths)

        # 取消全选
        self.win._set_all_selection(False)
        self.assertEqual(len(self.win.checked_paths), 0)

    def test_busy_state_protection(self):
        self.win._set_busy(True)
        self.assertEqual(str(self.win.btn_tools_menu.cget('state')), 'disabled')
        self.assertEqual(str(self.win.btn_one_click.cget('state')), 'disabled')
        self.assertEqual(str(self.win.btn_rescan.cget('state')), 'disabled')

        self.win._set_busy(False)
        self.assertEqual(str(self.win.btn_tools_menu.cget('state')), 'normal')
        self.assertEqual(str(self.win.btn_one_click.cget('state')), 'normal')
        self.assertEqual(str(self.win.btn_rescan.cget('state')), 'normal')

    def test_track_detail_layout_modes(self):
        # 默认模式为 bottom
        self.assertEqual(self.win.layout_mode, 'bottom')
        self.assertEqual(self.win.detail_widget.orient, 'horizontal')

        # 切换到 right 模式
        self.win._apply_layout_mode('right')
        self.assertEqual(self.win.layout_mode, 'right')
        self.assertEqual(self.win.detail_widget.orient, 'vertical')

        # 切换到 hidden 模式
        self.win._apply_layout_mode('hidden')
        self.assertEqual(self.win.layout_mode, 'hidden')

        # Ctrl+P 切换回 bottom
        self.win._on_toggle_detail_panel()
        self.assertEqual(self.win.layout_mode, 'bottom')

    def test_track_detail_update_selection(self):
        t1 = LibraryTrack(
            path='song1.flac', filename='song1.flac', folder='D:/Music',
            title='夜曲', artist='周杰伦', album='十一月的萧邦', composer='周杰伦',
            lyricist='方文山', arranger='林迈可', date='2005-11-01', genre='Pop',
            duration=227.0, has_embedded_lyrics=True,
        )
        t2 = LibraryTrack(
            path='song2.flac', filename='song2.flac', folder='D:/Music',
            title='发如雪', artist='周杰伦', album='十一月的萧邦', composer='周杰伦',
            lyricist='方文山', arranger='林迈可', date='2005-11-01', genre='Pop',
            duration=301.0,
        )

        # 单选测试
        self.win.detail_widget.update_selection([t1])
        self.assertIn('夜曲', self.win.detail_widget.lbl_title.cget('text'))
        self.assertIn('周杰伦', self.win.detail_widget.lbl_artist.cget('text'))
        self.assertIn('十一月的萧邦', self.win.detail_widget.lbl_album.cget('text'))
        self.assertIn('方文山', self.win.detail_widget.lbl_lyricist.cget('text'))
        self.assertIn('已内嵌歌词', self.win.detail_widget.lbl_lyrics_status.cget('text'))

        # 多选智能对比测试 (相同专辑和创作者聚合显示)
        self.win.detail_widget.update_selection([t1, t2])
        self.assertIn('已选中 2 首曲目', self.win.detail_widget.lbl_title.cget('text'))
        self.assertIn('周杰伦', self.win.detail_widget.lbl_artist.cget('text'))
        self.assertIn('十一月的萧邦', self.win.detail_widget.lbl_album.cget('text'))

        # 清空测试
        self.win.detail_widget.set_empty()
        self.assertIn('未选中曲目', self.win.detail_widget.lbl_title.cget('text'))

if __name__ == '__main__':
    unittest.main()
