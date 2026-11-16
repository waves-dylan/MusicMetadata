"""单元测试：CLI 子命令/批量执行与 Tkinter GUI 主窗口及各类交互弹窗。"""

from __future__ import annotations

import contextlib
import io
import os
import shutil
import threading
import time
import types
from unittest.mock import patch
from tkinter import ttk

from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from PIL import Image

import app as app_mod
from core.constants import (
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_PLAYER,
)
from core.models import FoobarEnv, LibraryTrack, TrackMatch
from infra.integrations.foobar import scan_library
from infra.scanner import track_has_pending_work
from services.art import (
    apply_track_repairs,
    apply_track_set_tags,
    convert_track_format,
    parse_set_tags,
    read_pictures,
)
from tag_io import read_audio_info, write_lrc_file
from tests.common import SAMPLE_163_KEY, TempTrackTestCase, offscreen_tk


class CliGuiTests(TempTrackTestCase):
    def test_gui_mainwindow_smoke_offscreen(self):
        with offscreen_tk() as root:
            fake_env = FoobarEnv(library_folders=[self.tmp])
            tr_miss = LibraryTrack(
                path=os.path.join(self.tmp, 'song1.flac'),
                filename='song1.flac',
                folder=self.tmp,
                title='Song One',
                artist='Artist',
                album='Album',
                status=STATUS_MISSING_DISK,
            )
            tr_stale = LibraryTrack(
                path=os.path.join(self.tmp, 'song2.flac'),
                filename='song2.flac',
                folder=self.tmp,
                title='Song Two',
                artist='Artist',
                album='Album',
                disk_tracknumber='5',
                disk_track_int=5,
                status=STATUS_STALE_PLAYER,
            )

            with patch('app.detect_foobar_env', return_value=fake_env) as m_env, \
                 patch('app.scan_library', return_value=[tr_miss, tr_stale]) as m_scan, \
                 patch('app.messagebox.showinfo') as m_info:
                # 默认打开界面时不应自动扫描
                win = app_mod.MainWindow(root)
                root.update()
                self.assertEqual(root.title(), 'MusicMetadata')
                m_env.assert_not_called()
                m_scan.assert_not_called()
                self.assertFalse(win._has_scanned)
                self.assertEqual(str(win.btn_rescan.cget('text')), '扫描媒体库')
                prefer_values = list(win.cmb_prefer_source.cget('values'))
                self.assertNotIn('Apple/MB优先', prefer_values)
                self.assertIn('默认(免梯直连)', prefer_values)
                self.assertIn('华语/国内优先', prefer_values)
                self.assertIn('欧美/国际优先', prefer_values)
                self.assertIn('Apple/iTunes优先', prefer_values)
                self.assertIn('MusicBrainz优先', prefer_values)
                self.assertEqual(win._PREFER_PRESETS['默认(免梯直连)'], ())
                self.assertEqual(win._PREFER_PRESETS['华语/国内优先'], ('netease', 'qq', 'kugou', 'migu', 'baidu'))
                self.assertEqual(win._PREFER_PRESETS['Apple/iTunes优先'], ('itunes',))
                self.assertEqual(win._PREFER_PRESETS['MusicBrainz优先'], ('mb',))

                # 尚未扫描时点击修复或同步应提示先扫描
                win.btn_one_click.invoke()
                win.btn_sync_only.invoke()
                self.assertEqual(m_info.call_count, 2)

                # 验证底部进度与状态栏在扫描阶段（含目录发现与逐首检查）均正常挂载于页面底部且不被 paned 挤压
                self.assertEqual(win.lbl_status.master.pack_info().get('side'), 'bottom')
                win._ev_q.put(('scan_prog', (0, 0, os.path.join(self.tmp, 'Rock'))))
                win._poll_queue()
                self.assertEqual(win.progress.winfo_manager(), 'pack')
                self.assertEqual(str(win.progress.cget('mode')), 'indeterminate')
                self.assertIn('扫描目录', win.var_status.get())

                win._ev_q.put(('scan_prog', (3, 10, tr_miss.path)))
                win._poll_queue()
                self.assertEqual(win.progress.winfo_manager(), 'pack')
                self.assertEqual(str(win.progress.cget('mode')), 'determinate')
                self.assertEqual(float(win.progress['value']), 3.0)
                self.assertIn('扫描检查 (3/10)', win.var_status.get())

                # 点击「扫描媒体库」按钮后才开始扫描，完成后隐藏进度栏并变为「重新扫描」
                win.btn_rescan.invoke()
                deadline = time.monotonic() + 3.0
                while not win._has_scanned and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                m_env.assert_called_once()
                m_scan.assert_called_once()
                self.assertEqual(win.progress.winfo_manager(), '')

            self.assertTrue(win._has_scanned)
            self.assertEqual(str(win.btn_rescan.cget('text')), '重新扫描')
            self.assertEqual(len(win.tree.get_children()), 2)
            win._on_fix_row_updated(tr_stale.path, {
                'status_text': '已同步',
                'matched_track': 5,
                'source_note': '测试同步',
                'color': 'ok',
            })
            self.assertEqual(tr_stale.status, STATUS_OK)
            self.assertNotIn(tr_stale.path, win.checked_paths)
            self.assertTrue(hasattr(win, 'btn_sync_only'))
            self.assertEqual(len(win.tree['columns']), 9)

            # 验证 busy 模式下所有控件与选项（含挑选封面、SACD提取、搜索栏、艺术家图、跳过整轨、只看待处理等）均被正确置灰禁用
            win._set_busy(True, '正在处理测试...')
            self.assertEqual(str(win.chk_artist_art.cget('state')), 'disabled')
            self.assertEqual(str(win.chk_skip_full.cget('state')), 'disabled')
            self.assertEqual(str(win.chk_only_pending.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_one_click.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_pick_cover.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_pick_artist.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_pick_lyric.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_sacd.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_search.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_clear_search.cget('state')), 'disabled')
            self.assertEqual(str(win.cmb_search_type.cget('state')), 'disabled')
            self.assertEqual(str(win.ent_search.cget('state')), 'disabled')
            for b in win.field_buttons.values():
                self.assertEqual(str(b.cget('state')), 'disabled')

            win._set_busy(False)
            self.assertEqual(str(win.chk_artist_art.cget('state')), 'normal')
            self.assertEqual(str(win.chk_skip_full.cget('state')), 'normal')
            self.assertEqual(str(win.chk_only_pending.cget('state')), 'normal')
            self.assertEqual(str(win.btn_one_click.cget('state')), 'normal')
            self.assertEqual(str(win.btn_pick_cover.cget('state')), 'normal')
            self.assertEqual(str(win.btn_pick_artist.cget('state')), 'normal')
            self.assertEqual(str(win.btn_pick_lyric.cget('state')), 'normal')
            self.assertEqual(str(win.btn_sacd.cget('state')), 'normal')
            self.assertEqual(str(win.btn_search.cget('state')), 'normal')
            self.assertEqual(str(win.btn_clear_search.cget('state')), 'normal')
            self.assertEqual(str(win.cmb_search_type.cget('state')), 'readonly')
            self.assertEqual(str(win.ent_search.cget('state')), 'normal')
            for b in win.field_buttons.values():
                self.assertEqual(str(b.cget('state')), 'normal')

            # 验证点击 _on_pick_artist 正确解析文件名线索/标签并拉起 ArtistPickerDialog
            win.checked_paths.add(tr_stale.path)
            with patch('ui.main_window.ArtistPickerDialog') as mock_art_dlg:
                win._on_pick_artist()
                self.assertTrue(mock_art_dlg.called)

    def test_execute_repairs_sync_only(self):
        fp = self._make_minimal_flac(os.path.join(self.tmp, 'sync_me.flac'))
        tr = LibraryTrack(
            path=fp,
            filename='sync_me.flac',
            folder=self.tmp,
            title='Sync',
            disk_tracknumber='1',
            disk_track_int=1,
            status=STATUS_STALE_PLAYER,
        )
        env = FoobarEnv(library_folders=[self.tmp])
        with patch('app.sync_files_to_foobar', return_value=(1, True)) as mock_sync:
            summary = app_mod.execute_repairs(
                env,
                [tr],
                app_mod.FixOptions(sync_only=True),
            )
        self.assertEqual(summary['synced_stale'], 1)
        self.assertEqual(summary['touched'], 1)
        mock_sync.assert_called_once()

    def test_scan_and_execute_repairs_date_end_to_end(self):
        alb_dir = os.path.join(self.tmp, '2026-11-11-10-00-00 AlbumX')
        os.makedirs(alb_dir, exist_ok=True)
        jpg = self._make_jpeg_bytes(220, 220, (30, 100, 190))

        def _prep_flac(name: str, title: str, tn: str = ''):
            p = self._make_minimal_flac(os.path.join(alb_dir, name))
            a = FLAC(p)
            a['TITLE'] = [title]
            a['ARTIST'] = ['ArtistX']
            a['ALBUM'] = ['AlbumX']
            a['ALBUMARTIST'] = ['ArtistX']
            if tn:
                a['TRACKNUMBER'] = [tn]
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, 'image/jpeg', 'Cover (front)', jpg
            a.add_picture(pic)
            a.save()
            return p

        f1 = _prep_flac('01 - First.flac', 'First', tn='')
        f2 = _prep_flac('02 - Second.flac', 'Second', tn='2')
        f3 = _prep_flac('03 - Bonus.flac', 'Bonus Demo', tn='3')
        with open(os.path.join(alb_dir, 'cover.jpg'), 'wb') as fp:
            fp.write(jpg)

        env = FoobarEnv(library_folders=[alb_dir])
        tracks = scan_library(env)
        by_name = {t.filename: t for t in tracks}
        self.assertTrue(by_name['01 - First.flac'].needs_tracknumber)
        self.assertTrue(by_name['01 - First.flac'].needs_date)
        self.assertFalse(by_name['02 - Second.flac'].needs_tracknumber)
        self.assertTrue(by_name['02 - Second.flac'].needs_date)
        self.assertTrue(track_has_pending_work(by_name['02 - Second.flac'], enable_track=True, enable_art=True, enable_tags=True, enable_sync=True))
        self.assertFalse(track_has_pending_work(by_name['02 - Second.flac'], enable_track=True, enable_art=True, enable_tags=False, enable_sync=True))

        def fake_resolve(tr, need_track=True, need_date=True, **_kwargs):
            if tr.title == 'First':
                return TrackMatch(1 if need_track else None, 1, 3, 'QQ音乐', 'AlbumX', release_date='2021-05-20' if need_date else '')
            if tr.title == 'Second':
                return TrackMatch(None, None, None, 'QQ音乐', 'AlbumX', release_date='2021-05-20' if need_date else '')
            return TrackMatch(None, reason='未匹配')

        # enable_tags=False 时不应联网补写 date
        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.sync_files_to_foobar', return_value=(0, False)):
            s_no_tags = app_mod.execute_repairs(
                env,
                [by_name['02 - Second.flac']],
                app_mod.FixOptions(enable_track=True, enable_art=True, enable_tags=False, enable_sync=False, enable_extra_tags=False),
            )
        self.assertEqual(s_no_tags['date_fixed'], 0)
        self.assertEqual(read_audio_info(f2)['date'], '')

        # 开启全部修复：f1 补音轨号+日期，f2 补日期，f3 通过同专辑传播补日期
        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.sync_files_to_foobar', return_value=(3, True)):
            s_ok = app_mod.execute_repairs(env, tracks, app_mod.FixOptions())

        self.assertEqual(s_ok['written_fail'], 0)
        self.assertEqual(s_ok['written_ok'], 3)
        self.assertEqual(s_ok['date_fixed'], 3)
        self.assertEqual(read_audio_info(f1)['track_int'], 1)
        self.assertEqual(read_audio_info(f1)['date'], '2021-05-20')
        self.assertEqual(read_audio_info(f2)['date'], '2021-05-20')
        self.assertEqual(read_audio_info(f3)['date'], '2021-05-20')

        # 再次扫描应全部正常，无需待处理
        tracks_after = scan_library(env)
        for tr in tracks_after:
            self.assertFalse(tr.needs_date)
            self.assertFalse(track_has_pending_work(tr, True, True, True, True, True))

        # 验证 GUI 行更新能清除 needs_date 与勾选状态
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            tr_ui = LibraryTrack(path=f2, filename='02 - Second.flac', folder=alb_dir, title='Second', disk_tracknumber='2', disk_track_int=2, needs_date=True)
            win._on_scan_finished(env, [tr_ui])
            self.assertIn(f2, win.checked_paths)
            win._on_fix_row_updated(f2, {
                'status_text': '已修复',
                'source_note': 'date=2021-05-20',
                'color': 'ok',
                'fixed_tags': True,
                'written_tags': {'date': '2021-05-20'},
            })
            self.assertEqual(tr_ui.date, '2021-05-20')
            self.assertFalse(tr_ui.needs_date)
            self.assertNotIn(f2, win.checked_paths)

    def test_scan_and_execute_repairs_full_metadata_and_online_cover_end_to_end(self):
        alb_dir = os.path.join(self.tmp, 'AlbumMergeDir')
        os.makedirs(alb_dir, exist_ok=True)
        online_jpg = self._make_jpeg_bytes(500, 500, (180, 90, 40))

        f1 = self._make_minimal_flac(os.path.join(alb_dir, '01 - Alpha.flac'))
        fl1 = FLAC(f1)
        fl1['TITLE'] = ['Alpha']
        fl1['ALBUM'] = ['AlbumM']
        fl1.save()

        f2 = self._make_minimal_flac(os.path.join(alb_dir, '02 - Beta.flac'))
        fl2 = FLAC(f2)
        fl2['TITLE'] = ['Beta']
        fl2['ALBUM'] = ['AlbumM']
        fl2['TRACKNUMBER'] = ['2']
        fl2['DESCRIPTION'] = [SAMPLE_163_KEY]
        fl2.save()

        env = FoobarEnv(library_folders=[alb_dir])
        tracks = scan_library(env)
        by_name = {t.filename: t for t in tracks}
        self.assertTrue(by_name['01 - Alpha.flac'].needs_cover_online)
        self.assertTrue(by_name['01 - Alpha.flac'].needs_tracknumber)
        self.assertEqual(
            set(by_name['01 - Alpha.flac'].missing_core_fields),
            {'artist', 'albumartist', 'date'},
        )
        self.assertEqual(
            set(by_name['01 - Alpha.flac'].missing_extra_fields),
            {'composer', 'genre', 'discnumber'},
        )
        self.assertTrue(track_has_pending_work(by_name['02 - Beta.flac'], True, True, True, True, True, enable_extra_tags=True))

        def fake_resolve(tr, **_kwargs):
            return TrackMatch(
                tracknumber=1 if tr.title == 'Alpha' else 2,
                discnumber=1,
                totaltracks=2,
                source='网易云音乐 + QQ音乐',
                album_name='AlbumM',
                release_date='2024-03-15',
                title=tr.title,
                artist='ArtistM',
                albumartist='AlbumArtistM',
                composer='ComposerM',
                genre='Electronic',
                cover_url='https://example.com/album_m.jpg',
            )

        # 验证 --no-extra-tags (enable_extra_tags=False) 时仅补音轨号/封面/核心标签，跳过扩展字段（使用独立目录避免污染 alb_dir 封面）
        noext_dir = os.path.join(self.tmp, 'AlbumNoExtDir')
        os.makedirs(noext_dir, exist_ok=True)
        f_noext = self._make_minimal_flac(os.path.join(noext_dir, '01 - Alpha.flac'))
        fl_ne = FLAC(f_noext)
        fl_ne['TITLE'] = ['Alpha']
        fl_ne['ALBUM'] = ['AlbumM']
        fl_ne.save()
        tracks_noext = scan_library(FoobarEnv(library_folders=[noext_dir]))
        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.fetch_cover_bytes', return_value=online_jpg) as m_cov_noext, \
             patch('app.sync_files_to_foobar', return_value=(1, True)):
            s_noext = app_mod.execute_repairs(
                env,
                tracks_noext,
                app_mod.FixOptions(enable_track=True, enable_art=True, enable_tags=True, enable_extra_tags=False, enable_sync=False),
            )
        self.assertEqual(s_noext['written_ok'], 1)
        self.assertEqual(m_cov_noext.call_count, 1)
        info1_partial = read_audio_info(f_noext)
        self.assertEqual(info1_partial['track_int'], 1)
        self.assertEqual(info1_partial['artist'], 'ArtistM')
        self.assertEqual(info1_partial['albumartist'], 'AlbumArtistM')
        self.assertEqual(info1_partial['date'], '2024-03-15')
        self.assertEqual(info1_partial['composer'], '')
        self.assertEqual(info1_partial['genre'], '')
        self.assertIsNone(info1_partial['disc_int'])

        # 开启全部修复：两首缺封面曲目一次联网下载封面并补全所有缺失扩展字段，生成 cover.jpg 并保留 f2 的 163 key
        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.fetch_cover_bytes', return_value=online_jpg) as m_cov_full, \
             patch('app.sync_files_to_foobar', return_value=(2, True)):
            s_full = app_mod.execute_repairs(env, tracks, app_mod.FixOptions())

        self.assertEqual(s_full['written_fail'], 0)
        self.assertEqual(s_full['written_ok'], 2)
        self.assertEqual(s_full['cover_fixed'], 2)
        self.assertEqual(s_full['tags_fixed'], 2)
        self.assertEqual(s_full['date_fixed'], 2)
        self.assertEqual(s_full['folder_cover_ok'], 1)
        self.assertEqual(m_cov_full.call_count, 1)
        self.assertTrue(os.path.isfile(os.path.join(alb_dir, 'cover.jpg')))

        for fp in (f1, f2):
            info = read_audio_info(fp)
            self.assertEqual(info['artist'], 'ArtistM')
            self.assertEqual(info['albumartist'], 'AlbumArtistM')
            self.assertEqual(info['date'], '2024-03-15')
            self.assertEqual(info['composer'], 'ComposerM')
            self.assertEqual(info['performer'], '')
            self.assertEqual(info['genre'], 'Electronic')
            self.assertEqual(info['disc_int'], 1)
            self.assertEqual(info['comment'], '')
            pics = read_pictures(FLAC(fp), 'flac')
            self.assertTrue(any(p['type'] == 3 for p in pics))

        info2_final = read_audio_info(f2)
        self.assertTrue(info2_final['has_163_key'])
        self.assertIsNotNone(info2_final['netease_meta'])

        tracks_done = scan_library(env)
        for tr in tracks_done:
            self.assertFalse(tr.needs_cover_online)
            self.assertEqual(tr.missing_core_fields, [])
            self.assertEqual(tr.missing_extra_fields, [])
            self.assertFalse(track_has_pending_work(tr, True, True, True, True, True, enable_extra_tags=True))

        # 验证 CLI 参数 --dry-run --no-extra-tags 正常运行
        buf = io.StringIO()
        with patch('sys.argv', ['app.py', '--dry-run', '--no-extra-tags', '--folder', alb_dir]), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_execute_repairs_lookup_progress_and_fast_cancel(self):
        tracks = []
        for idx in range(12):
            fp = self._make_minimal_flac(os.path.join(self.tmp, f'track_{idx}.flac'))
            tracks.append(LibraryTrack(
                path=fp, filename=f'track_{idx}.flac', folder=self.tmp,
                title=f'Title {idx}', artist='Art', album='Alb',
                needs_tracknumber=True,
            ))
        env = FoobarEnv(library_folders=[self.tmp])

        # 1) 验证联网查询阶段会实时上报 progress_cb 与 log_cb
        prog_events = []
        log_events = []
        with patch('app.resolve_track_metadata', return_value=TrackMatch(None, reason='未匹配')), \
             patch('app.sync_files_to_foobar', return_value=(0, False)):
            app_mod.execute_repairs(
                env, tracks, app_mod.FixOptions(enable_art=False, enable_tags=False, enable_sync=False),
                progress_cb=lambda i, t, n: prog_events.append((i, t, n)),
                log_cb=log_events.append,
            )
        self.assertTrue(any('联网查询:' in name for _, _, name in prog_events))
        self.assertTrue(any('联网查询完成' in msg for msg in log_events))

        # 2) 验证取消后立刻停止提交新任务，不阻塞等待剩余曲目
        started = 0
        slock = threading.Lock()
        cancel_flag = {'stop': False}

        def slow_resolve(tr, **_kwargs):
            nonlocal started
            with slock:
                started += 1
                if started >= 2:
                    cancel_flag['stop'] = True
            time.sleep(0.05)
            return TrackMatch(1, source='test')

        many = [
            LibraryTrack(path=os.path.join(self.tmp, f'm_{i}.flac'), filename=f'm_{i}.flac', folder=self.tmp, title=f'M{i}', needs_tracknumber=True)
            for i in range(40)
        ]
        with patch('app.resolve_track_metadata', side_effect=slow_resolve), \
             patch('app.sync_files_to_foobar', return_value=(0, False)):
            summary = app_mod.execute_repairs(
                env, many, app_mod.FixOptions(enable_art=False, enable_tags=False, enable_sync=False),
                is_cancelled_cb=lambda: cancel_flag['stop'],
            )
        self.assertTrue(summary['cancelled'])
        self.assertLess(started, len(many))

    def test_per_field_repair_selection_buttons_and_execution(self):
        expected_buttons = [
            ('cover', '封面'),
            ('date', '日期'),
            ('albumartist', '专辑艺术家'),
            ('artist', '艺术家'),
            ('composer', '作曲'),
            ('lyricist', '作词'),
            ('arranger', '编曲'),
            ('genre', '流派'),
            ('tracknumber', '音轨号'),
            ('discnumber', '碟片编号'),
        ]

        # 1) 验证 GUI 具备 10 个独立修复项按钮（不含表演者），且支持全不选 / 单选过滤 / 全选
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            self.assertEqual(list(win.field_buttons.keys()), [k for k, _ in expected_buttons])
            self.assertNotIn('performer', win.field_buttons)
            for key, label in expected_buttons:
                self.assertEqual(str(win.field_buttons[key].cget('text')), label)
                self.assertTrue(win.field_vars[key].get())

            t_only_comp = LibraryTrack(
                path=os.path.join(self.tmp, 'only_comp.flac'),
                filename='only_comp.flac', folder=self.tmp,
                title='CompOnly', artist='Art', albumartist='AA', album='Alb', date='2022-01-01',
                performer='Perf', genre='Pop', disk_tracknumber='1', disk_track_int=1,
                disk_discnumber='1', disk_disc_int=1,
                missing_extra_fields=['composer'], needs_extra_tags=True,
            )
            t_only_date = LibraryTrack(
                path=os.path.join(self.tmp, 'only_date.flac'),
                filename='only_date.flac', folder=self.tmp,
                title='DateOnly', artist='Art', albumartist='AA', album='Alb',
                composer='Comp', performer='Perf', genre='Pop', disk_tracknumber='2', disk_track_int=2,
                disk_discnumber='1', disk_disc_int=1,
                missing_core_fields=['date'], needs_date=True,
            )
            win._on_scan_finished(FoobarEnv(library_folders=[self.tmp]), [t_only_comp, t_only_date])
            self.assertEqual(len(win.visible_tracks), 2)

            win.btn_fields_none.invoke()
            win.var_sync.set(False)
            win._on_option_changed()
            self.assertEqual(win.visible_tracks, [])
            self.assertEqual(win.checked_paths, set())

            win.field_buttons['composer'].invoke()
            self.assertEqual([t.path for t in win.visible_tracks], [t_only_comp.path])
            self.assertEqual(win.checked_paths, {t_only_comp.path})

            win.field_buttons['composer'].invoke()
            win.field_buttons['date'].invoke()
            self.assertEqual([t.path for t in win.visible_tracks], [t_only_date.path])
            self.assertEqual(win.checked_paths, {t_only_date.path})

            win.btn_fields_all.invoke()
            self.assertEqual(len(win.visible_tracks), 2)
            self.assertEqual(win.checked_paths, {t_only_comp.path, t_only_date.path})

            # 局部写入回调应只移除实际写入的 planned tag，保留未选中的 tag_updates
            t_only_date.tag_updates = {'albumartist': 'AA2', 'date': '2022-05-05'}
            t_only_date.needs_tag_fix = True
            win._on_fix_row_updated(t_only_date.path, {
                'color': 'ok', 'fixed_tags': True, 'written_tags': {'date': '2022-05-05'},
            })
            self.assertEqual(t_only_date.tag_updates, {'albumartist': 'AA2'})
            self.assertTrue(t_only_date.needs_tag_fix)

        # 2) 验证 execute_repairs 仅执行已勾选字段（例如只选「作曲」与「碟片编号」）
        sub = os.path.join(self.tmp, 'SelectiveDir')
        os.makedirs(sub, exist_ok=True)
        fp = self._make_minimal_flac(os.path.join(sub, 'sel.flac'))
        fl = FLAC(fp)
        fl['TITLE'] = ['SelectiveSong']
        fl['ALBUM'] = ['SelectiveAlb']
        fl.save()

        env = FoobarEnv(library_folders=[sub])
        tracks = scan_library(env)
        seen_need_fields: list = []

        def fake_resolve(tr, **kwargs):
            seen_need_fields.append(kwargs.get('need_fields'))
            return TrackMatch(
                tracknumber=3, discnumber=2, totaltracks=8, source='QQ音乐',
                album_name='SelectiveAlb', release_date='2023-09-01',
                title='SelectiveSong', artist='OnlineArtist', albumartist='OnlineAA',
                composer='OnlineComp', genre='Rock',
                cover_url='https://example.com/s.jpg',
            )

        sel_opts = app_mod.FixOptions(
            enable_track=False, enable_art=False, enable_tags=True, enable_extra_tags=True,
            enable_date=False, enable_albumartist=False, enable_artist=False,
            enable_composer=True, enable_genre=False, enable_disc=True,
            enable_sync=False,
        )
        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.fetch_cover_bytes') as m_cov, \
             patch('app.sync_files_to_foobar', return_value=(0, False)):
            summary = app_mod.execute_repairs(env, tracks, sel_opts)

        self.assertEqual(summary['written_ok'], 1)
        self.assertEqual(seen_need_fields, [frozenset({'composer', 'discnumber'})])
        m_cov.assert_not_called()
        info = read_audio_info(fp)
        self.assertEqual(info['composer'], 'OnlineComp')
        self.assertEqual(info['disc_int'], 2)
        self.assertEqual(info['date'], '')
        self.assertEqual(info['artist'], '')
        self.assertEqual(info['albumartist'], '')
        self.assertEqual(info['performer'], '')
        self.assertEqual(info['genre'], '')
        self.assertIsNone(info['track_int'])
        self.assertEqual(read_pictures(FLAC(fp), 'flac'), [])

        # 3) 验证 CLI 单字段禁用参数在 --dry-run 下可正常解析运行，且 --no-performer 已移除
        self.assertNotIn('performer', app_mod.FixOptions().active_tag_fields())
        buf = io.StringIO()
        with patch('sys.argv', [
            'app.py', '--dry-run', '--folder', sub,
            '--no-date', '--no-albumartist', '--no-artist', '--no-composer', '--no-genre', '--no-disc', '--no-art', '--no-track',
        ]), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)

        err_buf = io.StringIO()
        with patch('sys.argv', ['app.py', '--dry-run', '--no-performer', '--folder', sub]), contextlib.redirect_stderr(err_buf):
            with self.assertRaises(SystemExit) as cm_removed:
                app_mod.main()
        self.assertEqual(cm_removed.exception.code, 2)

    def test_execute_deletions_folder_cover_dry_run_and_cli(self):
        sub = os.path.join(self.tmp, 'DelBatch')
        os.makedirs(sub, exist_ok=True)
        cov = self._make_jpeg_bytes(220, 220, (30, 140, 200))
        cov_file = os.path.join(sub, 'cover.jpg')
        with open(cov_file, 'wb') as out:
            out.write(cov)

        fp1 = self._make_minimal_flac(os.path.join(sub, '01.flac'))
        fp2 = self._make_minimal_flac(os.path.join(sub, '02.flac'))
        for idx, fp in enumerate((fp1, fp2), 1):
            apply_track_repairs(
                fp,
                tag_updates={'title': f'T{idx}', 'artist': 'Art', 'album': 'Alb', 'date': '2024-01-01', 'composer': f'Comp{idx}'},
                cover_bytes=cov, cover_mime='image/jpeg', tracknumber=idx,
            )

        env = FoobarEnv(library_folders=[sub])
        tracks = scan_library(env)

        # 1) 预演模式不修改文件和目录 cover.jpg
        dry_opts = app_mod.DeleteOptions(
            fields=frozenset({'composer', 'cover'}),
            delete_folder_cover=True,
            enable_sync=False,
            dry_run=True,
        )
        s_dry = app_mod.execute_deletions(env, tracks, dry_opts)
        self.assertEqual(s_dry['deleted_ok'], 2)
        self.assertEqual(s_dry['folder_cover_deleted'], 1)
        self.assertTrue(os.path.isfile(cov_file))
        self.assertEqual(read_audio_info(fp1)['composer'], 'Comp1')
        self.assertEqual(len(read_pictures(FLAC(fp1), 'flac')), 1)

        # 2) CLI 支持中文类别名与英文类别名混用，以及 --delete-folder-cover
        buf = io.StringIO()
        with patch('sys.argv', [
            'app.py', '--delete', '作曲,cover', '--delete-folder-cover', '--no-sync', '--folder', sub,
        ]), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)
        self.assertFalse(os.path.exists(cov_file))
        for fp in (fp1, fp2):
            info = read_audio_info(fp)
            self.assertEqual(info['composer'], '')
            self.assertEqual(info['artist'], 'Art')
            self.assertEqual(info['date'], '2024-01-01')
            self.assertEqual(read_pictures(FLAC(fp), 'flac'), [])

        # 3) CLI 传入无效类别应返回退出码 2
        err_buf = io.StringIO()
        with patch('sys.argv', ['app.py', '--delete', 'invalid_tag', '--folder', sub]), contextlib.redirect_stderr(err_buf):
            with self.assertRaises(SystemExit) as cm_err:
                app_mod.main()
        self.assertEqual(cm_err.exception.code, 2)

    def test_gui_delete_partial_metadata_dialog_and_refresh(self):
        sub = os.path.join(self.tmp, 'GuiDel')
        os.makedirs(sub, exist_ok=True)
        cov = self._make_jpeg_bytes(200, 200, (120, 90, 40))
        with open(os.path.join(sub, 'cover.jpg'), 'wb') as out:
            out.write(cov)
        fp1 = self._make_minimal_flac(os.path.join(sub, '01.flac'))
        fp2 = self._make_minimal_flac(os.path.join(sub, '02.flac'))
        for idx, fp in enumerate((fp1, fp2), 1):
            fl = FLAC(fp)
            fl['PERFORMER'] = [f'Perf{idx}']
            fl.save()
            apply_track_repairs(
                fp,
                tag_updates={
                    'title': f'Song{idx}', 'artist': 'Art', 'albumartist': 'Art', 'album': 'Alb',
                    'date': '2023-01-02', 'composer': f'Comp{idx}', 'genre': 'Pop',
                },
                cover_bytes=cov, cover_mime='image/jpeg', tracknumber=idx, discnumber=1,
            )

        env = FoobarEnv(library_folders=[sub])
        scanned = scan_library(env)

        with offscreen_tk() as root:
            with patch('app.messagebox.showinfo') as m_info, \
                 patch('app.messagebox.askyesno', return_value=True) as m_ask, \
                 patch('app.sync_files_to_foobar', return_value=(0, False)):
                win = app_mod.MainWindow(root, auto_scan=False)
                win._on_scan_finished(env, scanned)
                self.assertEqual(win.visible_tracks, [])

                # 未勾选曲目时点击删除应提示
                win.btn_delete_meta.invoke()
                self.assertEqual(m_info.call_count, 1)

                # 显示全部曲目并仅勾选第一首
                win.var_only_pending.set(False)
                win._refresh_table()
                self.assertEqual(len(win.visible_tracks), 2)
                win._toggle_row(fp1)

                def _drive_dialog():
                    dlg = getattr(win, '_last_delete_dialog', None)
                    if dlg is None or not dlg.top.winfo_exists():
                        root.after(15, _drive_dialog)
                        return
                    # 验证全选/全不选、保留表演者删除项及封面联动开关状态
                    self.assertIn('performer', dlg.field_vars)
                    self.assertIn('表演者 (1)', str(dlg.field_checks['performer'].cget('text')))
                    dlg.btn_select_all.invoke()
                    self.assertEqual(str(dlg.chk_delete_folder_cover.cget('state')), 'normal')
                    dlg.btn_select_none.invoke()
                    self.assertEqual(str(dlg.chk_delete_folder_cover.cget('state')), 'disabled')
                    dlg.field_vars['composer'].set(True)
                    dlg.field_vars['genre'].set(True)
                    dlg.var_sync.set(False)
                    dlg.btn_confirm.invoke()

                root.after(20, _drive_dialog)
                win.btn_delete_meta.invoke()

                deadline = time.monotonic() + 5.0
                while win._busy and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                root.update()

                m_ask.assert_called_once()
                self.assertFalse(win._busy)

            info1 = read_audio_info(fp1)
            info2 = read_audio_info(fp2)
            self.assertEqual(info1['composer'], '')
            self.assertEqual(info1['genre'], '')
            self.assertEqual(info1['title'], 'Song1')
            self.assertEqual(info1['date'], '2023-01-02')
            self.assertEqual(info2['composer'], 'Comp2')
            self.assertEqual(info2['genre'], 'Pop')

            tr1 = next(t for t in win.tracks if t.path == fp1)
            self.assertEqual(tr1.composer, '')
            self.assertEqual(tr1.genre, '')
            self.assertIn('composer', tr1.missing_extra_fields)
            self.assertEqual(tr1.tag_updates.get('genre'), 'Pop')
            self.assertTrue(tr1.needs_player_sync)
            self.assertNotIn(fp1, win.checked_paths)
            self.assertEqual(win.tree.item(fp1, 'values')[1], '已删除')

    def test_manual_set_tags_protect_163_cli_and_gui_dialog(self):
        parsed, err = parse_set_tags(['genre=Pop', 'track=3/12', 'custom_mood=Chill', '歌词=[00:00.00]Hi'])
        self.assertEqual(err, '')
        self.assertEqual(parsed, {'genre': 'Pop', 'tracknumber': '3/12', 'custom_mood': 'Chill', 'lyrics': '[00:00.00]Hi'})

        _, bad_err = parse_set_tags(['invalid_without_equals'])
        self.assertTrue(bad_err)

        fp = self._make_minimal_flac('set_test.flac')
        f = FLAC(fp)
        f['title'] = ['Original']
        f['comment'] = ["163 key(Don't modify):PROTECTED"]
        f.save()

        # protect_163_key=True 时拒绝覆盖 163 key，但允许设置标准与自定义标签
        ok, msg = apply_track_set_tags(
            fp,
            {'genre': 'Electronic', 'tracknumber': '4/10', 'CUSTOM_TAG': 'Vibe', 'comment': 'OverwriteAttempt'},
            protect_163_key=True,
        )
        self.assertTrue(ok, msg)
        info = read_audio_info(fp)
        self.assertEqual(info['genre'], 'Electronic')
        self.assertEqual(info['track_int'], 4)
        self.assertIn('163 key', info['comment'])
        f_chk = FLAC(fp)
        self.assertEqual(f_chk.get('CUSTOM_TAG'), ['Vibe'])

        # 扫描并测试 execute_set_tags 与 CLI --set
        env = FoobarEnv('', [self.tmp], '', [], '', False)
        tracks = scan_library(env, folders=[self.tmp], include_art_scan=False)
        res_dry = app_mod.execute_set_tags(env, tracks, app_mod.SetOptions(tag_values={'artist': 'NewArtist'}, dry_run=True, enable_sync=False))
        self.assertEqual(res_dry['set_ok'], 1)
        self.assertEqual(read_audio_info(fp)['artist'], '')

        args = types.SimpleNamespace(
            set_tags=['artist=CLIArtist', 'date=2025'],
            convert='',
            delete=[],
            folder=self.tmp,
            include_full_album=False,
            no_protect_163=False,
            no_sync=True,
            dry_run=False,
        )
        rc = app_mod.run_cli(args)
        self.assertEqual(rc, 0)
        info_after = read_audio_info(fp)
        self.assertEqual(info_after['artist'], 'CLIArtist')
        self.assertEqual(info_after['date'], '2025')

        # Offscreen 测试 SetMetadataDialog
        with offscreen_tk() as root:
            dlg = None
            try:
                dlg = app_mod.SetMetadataDialog(root, tracks, default_sync=False)
                self.assertEqual(dlg.chk_sync.cget('text'), '写入后刷新播放器缓存')
                dlg.var_key.set('genre (流派)')
                dlg.var_val.set('Synthpop')
                dlg._on_add()
                dlg.var_key.set('CUSTOM_NOTE')
                dlg.var_val.set('DemoNote')
                dlg._on_add()
                dlg._on_confirm()
                self.assertIsNotNone(dlg.result)
                self.assertEqual(dlg.result.tag_values, {'genre': 'Synthpop', 'CUSTOM_NOTE': 'DemoNote'})
            finally:
                dlg = None

    def test_audio_convert_preserve_tags_cover_lrc_and_gui_dialog(self):
        # 构造带标签、封面与外置 .lrc 的 FLAC 源文件
        flac_p = self._make_minimal_flac('conv_src.flac')
        img_buf = io.BytesIO()
        Image.new('RGB', (400, 400), color=(30, 120, 220)).save(img_buf, format='JPEG')
        cov_bytes = img_buf.getvalue()
        ok_prep, _ = apply_track_repairs(
            flac_p,
            tag_updates={'title': 'ConvTitle', 'artist': 'ConvArtist', 'album': 'ConvAlbum', 'date': '2024', 'genre': 'Jazz', 'composer': 'ConvComp'},
            cover_bytes=cov_bytes,
            tracknumber=2,
            discnumber=1,
            lyrics='[00:01.00]Embedded line',
            lyrics_mode='embed',
        )
        self.assertTrue(ok_prep)
        write_lrc_file(flac_p, '[00:01.00]Sidecar line', title='ConvTitle')

        bare_mp3 = self._make_minimal_mp3('_template_bare.mp3')

        def _fake_subproc_run(cmd, **_kwargs):
            # cmd[-1] 为临时输出文件路径，故意写入“裸无标签/无封面 MP3”以验证 post_hook 自动补全封面/标签/内嵌歌词
            tmp_out = cmd[-1]
            shutil.copy2(bare_mp3, tmp_out)
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')

        with patch('tag_io.find_ffmpeg', return_value='ffmpeg'), \
             patch('app.find_ffmpeg', return_value='ffmpeg'), \
             patch('tag_io.subprocess.run', side_effect=_fake_subproc_run):
            ok_c, msg_c, out_mp3 = convert_track_format(flac_p, 'mp3', overwrite=False, delete_source=False)
            self.assertTrue(ok_c, msg_c)
            self.assertTrue(os.path.isfile(out_mp3))
            self.assertTrue(os.path.isfile(flac_p))
            info_out = read_audio_info(out_mp3)
            self.assertEqual(info_out['title'], 'ConvTitle')
            self.assertEqual(info_out['artist'], 'ConvArtist')
            self.assertEqual(info_out['album'], 'ConvAlbum')
            self.assertEqual(info_out['date'], '2024')
            self.assertEqual(info_out['genre'], 'Jazz')
            self.assertEqual(info_out['composer'], 'ConvComp')
            self.assertEqual(info_out['track_int'], 2)
            self.assertEqual(info_out['disc_int'], 1)
            self.assertTrue(info_out['has_embedded_lyrics'])
            self.assertTrue(info_out['has_lrc_file'])
            self.assertTrue(bool(read_pictures(MP3(out_mp3), 'mp3')))

            # overwrite=False 时目标已存在应保护拒绝
            ok_no_ow, msg_no_ow, _ = convert_track_format(flac_p, 'mp3', overwrite=False, delete_source=False)
            self.assertFalse(ok_no_ow)
            self.assertIn('已存在', msg_no_ow)

            # delete_source=True + overwrite=True 应在校验成功后删除源文件
            ok_del, _, _ = convert_track_format(flac_p, 'mp3', overwrite=True, delete_source=True)
            self.assertTrue(ok_del)
            self.assertFalse(os.path.exists(flac_p))

            # execute_convert dry_run & CLI --convert dry-run 验证
            os.remove(bare_mp3)
            env = FoobarEnv('', [self.tmp], '', [], '', False)
            tracks = scan_library(env, folders=[self.tmp], include_art_scan=False)
            dry_res = app_mod.execute_convert(env, tracks, app_mod.ConvertOptions(target_format='ogg', dry_run=True, enable_sync=False))
            self.assertEqual(dry_res['conv_ok'], 1)

            rc_cli = app_mod.run_cli(types.SimpleNamespace(
                set_tags=[],
                convert='ogg',
                delete=[],
                folder=self.tmp,
                include_full_album=False,
                force=False,
                delete_source=False,
                no_sync=True,
                dry_run=True,
            ))
            self.assertEqual(rc_cli, 0)

            # Offscreen 测试 ConvertFormatDialog
            with offscreen_tk() as root:
                dlg = None
                try:
                    dlg = app_mod.ConvertFormatDialog(root, tracks, default_sync=False)
                    self.assertEqual(dlg.chk_sync.cget('text'), '转换后刷新播放器缓存')
                    dlg.var_format.set('m4a')
                    dlg.var_overwrite.set(True)
                    dlg.var_delete_source.set(False)
                    dlg._on_confirm()
                    self.assertIsNotNone(dlg.result)
                    self.assertEqual(dlg.result.target_format, 'm4a')
                    self.assertTrue(dlg.result.overwrite)
                finally:
                    dlg = None

    def test_generic_mode_ui_adaptation_and_config_persistence(self):
        dir1 = os.path.join(self.tmp, 'LibA')
        dir2 = os.path.join(self.tmp, 'LibB')
        os.makedirs(dir1, exist_ok=True)
        os.makedirs(dir2, exist_ok=True)
        fp1 = self._make_minimal_flac(os.path.join(dir1, '01 - TrackOne.flac'))
        fl1 = FLAC(fp1)
        fl1['TITLE'] = ['TrackOne']
        fl1['ARTIST'] = ['GenericArtist']
        fl1['ALBUM'] = ['GenericAlbum']
        fl1['TRACKNUMBER'] = ['1']
        fl1.save()

        empty_env = FoobarEnv()
        with offscreen_tk() as root:
            with patch('app.detect_foobar_env', return_value=empty_env), \
                 patch('app.get_default_music_folders', return_value=[dir1]):
                win = app_mod.MainWindow(root, auto_scan=False)
                win.btn_rescan.invoke()
                deadline = time.monotonic() + 3.0
                while not win._has_scanned and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.02)
                root.update()

                # 无 foobar 环境应自动隐藏 foobar 专属控件并使用通用表头/摘要
                self.assertEqual(win.btn_sync_only.winfo_manager(), '')
                self.assertEqual(win.chk_sync.winfo_manager(), '')
                self.assertEqual(win.btn_reset_lib.winfo_manager(), '')
                self.assertEqual(win.tree.heading('track', 'text'), '音轨号')
                self.assertEqual(win.tree.heading('tagsync', 'text'), '标签检查')
                self.assertNotIn('foobar待刷新', win.var_summary.get())
                self.assertIn('系统音乐目录', win.var_env.get())

                # 非播放器环境下更新曲目信息不应误标为缓存过期
                tr = win.tracks[0]
                win._sync_track_from_info(tr, read_audio_info(fp1), synced=False)
                self.assertFalse(tr.needs_player_sync)

                # 模态弹窗在 has_player=False 时隐藏播放器缓存选项
                dlg_set = app_mod.SetMetadataDialog(root, [tr], default_sync=False, has_player=False)
                self.assertEqual(dlg_set.chk_sync.winfo_manager(), '')
                dlg_set._on_cancel()

                dlg_del = app_mod.DeleteMetadataDialog(root, [tr], default_sync=False, has_player=False)
                self.assertEqual(dlg_del.chk_sync.winfo_manager(), '')
                dlg_del._on_cancel()

                dlg_conv = app_mod.ConvertFormatDialog(root, [tr], default_sync=False, has_player=False)
                self.assertEqual(dlg_conv.chk_sync.winfo_manager(), '')
                dlg_conv._on_cancel()

                # 多目录管理弹窗持久化保存 custom_folders
                dlg_dir = app_mod.FolderManagerDialog(root, [dir1])
                dlg_dir._folders.append(dir2)
                dlg_dir._refresh_list()
                dlg_dir._on_confirm()
                self.assertEqual(dlg_dir.result, [os.path.abspath(dir1), os.path.abspath(dir2)])
                win.custom_folders = dlg_dir.result
                win._save_user_state()

            # 重建主窗口应自动恢复上次保存的自定义目录
            win2 = app_mod.MainWindow(root, auto_scan=False)
            self.assertEqual(win2.custom_folders, [os.path.abspath(dir1), os.path.abspath(dir2)])
            self.assertIn('2 个目录', win2.var_env.get())

    def test_cli_multi_folder_and_generic_fallback(self):
        dir1 = os.path.join(self.tmp, 'CliA')
        dir2 = os.path.join(self.tmp, 'CliB')
        os.makedirs(dir1, exist_ok=True)
        os.makedirs(dir2, exist_ok=True)
        fp1 = self._make_minimal_flac(os.path.join(dir1, '01 - A.flac'))
        fp2 = self._make_minimal_flac(os.path.join(dir2, '02 - B.flac'))
        for idx, fp in enumerate((fp1, fp2), 1):
            fl = FLAC(fp)
            fl['TITLE'] = [f'Song{idx}']
            fl['TRACKNUMBER'] = [str(idx)]
            fl.save()

        # 1) CLI 支持多次传入 --folder 扫描多个目录，且在无 foobar 时输出通用提示
        buf = io.StringIO()
        with patch('cli.detect_foobar_env', return_value=FoobarEnv()), \
             patch('sys.argv', ['app.py', '--dry-run', '--folder', dir1, '--folder', dir2]), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)
        out = buf.getvalue()
        self.assertIn('通用本地目录模式', out)
        self.assertIn('共扫描 2 首', out)
        self.assertNotIn('foobar缓存待刷新', out)

        # 2) 未传 --folder 时应自动回落读取配置文件中保存的 custom_folders
        from core.config import save_user_config
        save_user_config({'custom_folders': [dir1, dir2]})
        buf2 = io.StringIO()
        with patch('cli.detect_foobar_env', return_value=FoobarEnv()), \
             patch('sys.argv', ['app.py', '--dry-run']), \
             contextlib.redirect_stdout(buf2):
            with self.assertRaises(SystemExit) as cm2:
                app_mod.main()
        self.assertEqual(cm2.exception.code, 0)
        self.assertIn('共扫描 2 首', buf2.getvalue())

    def test_cross_platform_user_config_fonts_and_reveal(self):
        import core.config as uc

        sample = self._make_minimal_flac('reveal_me.flac')
        abs_sample = os.path.abspath(sample)

        # ui_font 在 Windows 与非 Windows 下的常规/加粗/等宽字体回落
        with patch.object(uc.os, 'name', 'nt'):
            self.assertEqual(uc.ui_font(9), ('Microsoft YaHei UI', 9))
            self.assertEqual(uc.ui_font(9, bold=True), ('Microsoft YaHei UI', 9, 'bold'))
            self.assertEqual(uc.ui_font(9, mono=True), ('Consolas', 9))
        with patch.object(uc.os, 'name', 'posix'):
            self.assertEqual(uc.ui_font(9), ('TkDefaultFont', 9))
            self.assertEqual(uc.ui_font(10, bold=True), ('TkDefaultFont', 10, 'bold'))
            self.assertEqual(uc.ui_font(9, mono=True), ('TkFixedFont', 9))

        # get_config_path 在非 Windows 未设置 MUSICMETADATA_CONFIG 时使用 XDG_CONFIG_HOME
        with patch.dict(os.environ, {'MUSICMETADATA_CONFIG': '', 'XDG_CONFIG_HOME': '/tmp/xdg_cfg'}, clear=False), \
             patch.object(uc.os, 'name', 'posix'):
            cfg_p = uc.get_config_path()
            self.assertTrue(cfg_p.endswith(os.path.join('xdg_cfg', 'MusicMetadata', 'config.json')))

        # reveal_in_file_manager 按平台调用正确的系统文件管理器命令
        with patch('core.config.subprocess.Popen') as m_popen:
            with patch.object(uc.os, 'name', 'nt'):
                uc.reveal_in_file_manager(sample)
                m_popen.assert_called_with(['explorer', '/select,', os.path.normpath(abs_sample)])

            with patch.object(uc.os, 'name', 'posix'), patch.object(uc.sys, 'platform', 'darwin'):
                uc.reveal_in_file_manager(sample)
                m_popen.assert_called_with(['open', '-R', abs_sample])

            with patch.object(uc.os, 'name', 'posix'), patch.object(uc.sys, 'platform', 'linux'):
                uc.reveal_in_file_manager(sample)
                m_popen.assert_called_with(['xdg-open', os.path.dirname(abs_sample)])

    def test_library_scan_standalone_and_foobar_compat_exports(self):
        import infra.integrations.foobar as fb_mod
        import infra.scanner as ls_mod

        self.assertIs(ls_mod.LibraryTrack, fb_mod.LibraryTrack)
        fp = self._make_minimal_flac('standalone.flac')
        fl = FLAC(fp)
        fl['TITLE'] = ['Standalone']
        fl['TRACKNUMBER'] = ['1']
        fl.save()

        tracks = ls_mod.scan_library(folders=[self.tmp], include_art_scan=False)
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, 'Standalone')
        self.assertFalse(tracks[0].needs_player_sync)
        self.assertEqual(ls_mod.touch_files_mtime([fp]), 1)

    def test_cli_artist_art_arguments_and_parser(self):
        from cli import build_arg_parser, parse_delete_fields

        parser = build_arg_parser()
        # 默认不传为 False
        args_default = parser.parse_args([])
        self.assertFalse(args_default.artist_art)
        self.assertFalse(args_default.no_artist_art)
        self.assertFalse(args_default.delete_folder_artist)

        # --artist-art
        args_on = parser.parse_args(['--artist-art'])
        self.assertTrue(args_on.artist_art)
        self.assertFalse(args_on.no_artist_art)

        # --no-artist-art
        args_off = parser.parse_args(['--no-artist-art'])
        self.assertFalse(args_off.artist_art)
        self.assertTrue(args_off.no_artist_art)

        # --delete artist_cover --delete-folder-artist
        args_del = parser.parse_args(['--delete', 'artist_cover', '--delete-folder-artist'])
        self.assertTrue(args_del.delete_folder_artist)
        fields, err = parse_delete_fields(args_del.delete)
        self.assertEqual(err, [])
        self.assertIn('artist_cover', fields)

    def test_delete_metadata_dialog_artist_cover_and_folder_artist(self):
        with offscreen_tk() as root:
            tr = LibraryTrack(path=os.path.join(self.tmp, 'test.flac'), filename='test.flac', folder=self.tmp)
            dlg = app_mod.DeleteMetadataDialog(root, [tr])
            self.assertEqual(dlg.chk_sync.cget('text'), '删除后刷新播放器缓存')
            self.assertIn('artist_cover', dlg.field_vars)
            self.assertTrue(hasattr(dlg, 'chk_delete_folder_artist'))
            self.assertEqual(str(dlg.chk_delete_folder_artist.cget('state')), 'disabled')

            # 勾选 artist_cover 后激活外置 artist.jpg 复选框
            dlg.field_vars['artist_cover'].set(True)
            dlg._on_artist_cover_toggled()
            self.assertEqual(str(dlg.chk_delete_folder_artist.cget('state')), 'normal')

            dlg.var_delete_folder_artist.set(True)
            dlg.var_dry_run.set(True)
            dlg._on_confirm()
            self.assertIsNotNone(dlg.result)
            self.assertIn('artist_cover', dlg.result.fields)
            self.assertTrue(dlg.result.delete_folder_artist)

    def test_gui_artist_art_checkbox_and_options(self):
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            self.assertTrue(hasattr(win, 'chk_artist_art'))
            self.assertTrue(hasattr(win, 'var_artist_art'))
            # 默认不勾选
            self.assertFalse(win.var_artist_art.get())
            opts_def = win._current_options()
            self.assertFalse(opts_def.enable_artist_art)

            # 勾选后 options 包含 enable_artist_art=True
            win.var_artist_art.set(True)
            opts_on = win._current_options()
            self.assertTrue(opts_on.enable_artist_art)

    def test_gui_header_multistate_checkbox(self):
        """测试“选择”列表头的多状态复选框（全选/全不选/半选切换联动）。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            win.var_only_pending.set(False)
            # 未扫描或无曲目时，表头为全不选 '☐ 选择'
            self.assertEqual(win.tree.heading('chk', 'text'), '☐ 选择')

            fp1 = self._make_minimal_flac(os.path.join(self.tmp, 'chk1.flac'))
            fp2 = self._make_minimal_flac(os.path.join(self.tmp, 'chk2.flac'))
            tr1 = LibraryTrack(path=fp1, filename='chk1.flac', container='flac', folder=self.tmp, status=STATUS_OK)
            tr2 = LibraryTrack(path=fp2, filename='chk2.flac', container='flac', folder=self.tmp, status=STATUS_OK)
            win.tracks = [tr1, tr2]

            # 1. 全部未勾选 -> '☐ 选择'
            win.checked_paths.clear()
            win._refresh_table()
            self.assertEqual(win.tree.heading('chk', 'text'), '☐ 选择')

            # 2. 单曲勾选 -> 半选 '▣ 选择'
            win._toggle_row(tr1.path)
            self.assertEqual(win.tree.heading('chk', 'text'), '▣ 选择')

            # 3. 两首全勾选 -> 全选 '☑ 选择'
            win._toggle_row(tr2.path)
            self.assertEqual(win.tree.heading('chk', 'text'), '☑ 选择')

            # 4. 全选状态下触发表头/全选切换 -> 全不选 '☐ 选择'
            win._toggle_select_all()
            self.assertEqual(win.tree.heading('chk', 'text'), '☐ 选择')
            self.assertEqual(len(win.checked_paths), 0)

            # 5. 全不选状态下触发表头/全选切换 -> 全选 '☑ 选择'
            win._toggle_select_all()
            self.assertEqual(win.tree.heading('chk', 'text'), '☑ 选择')
            self.assertEqual(len(win.checked_paths), 2)

            # 6. 取消一首变半选，再点击表头切换 -> 变为全选 '☑ 选择'
            win._toggle_row(tr1.path)
            self.assertEqual(win.tree.heading('chk', 'text'), '▣ 选择')
            win._toggle_select_all()
            self.assertEqual(win.tree.heading('chk', 'text'), '☑ 选择')
            self.assertEqual(len(win.checked_paths), 2)

    def test_gui_artist_art_filtering_and_mp4_nodata(self):
        """测试单选“获取艺术家图”时：目录已有图的MP4不误报缺图，真正缺外置图的nodata条目能正常筛选。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            fp_mp4 = os.path.join(self.tmp, 'test_song.m4a')
            fp_nodata = os.path.join(self.tmp, 'need_art.flac')

            # 1. 模拟 MP4 文件：所在目录已有外置 artist.jpg (folder_artist_action='ok')
            tr_mp4 = LibraryTrack(
                path=fp_mp4, filename='test_song.m4a', container='mp4', folder=self.tmp,
                status=STATUS_OK, has_artist_art=False, needs_artist_art=False,
                artist_status='正常', folder_artist_action='ok',
            )

            # 2. 模拟某曲目：目录缺失 artist.jpg (folder_artist_action='nodata')
            tr_nodata = LibraryTrack(
                path=fp_nodata, filename='need_art.flac', container='flac', folder=self.tmp,
                status=STATUS_OK, has_artist_art=False, needs_artist_art=True,
                artist_status='缺艺术家图', folder_artist_action='nodata', note='缺艺术家图',
            )
            win.tracks = [tr_mp4, tr_nodata]

            # 仅勾选“获取艺术家图”，开启“只显示待处理曲目”
            for v in win.field_vars.values():
                v.set(False)
            win.var_artist_art.set(True)
            win.var_only_pending.set(True)
            win._refresh_table()

            # 验证：MP4曲目不误报待处理，而真正缺艺术家图的 nodata 曲目被正确筛选展示
            self.assertEqual(len(win.visible_tracks), 1)
            self.assertEqual(win.visible_tracks[0].path, tr_nodata.path)
            vals_mp4, _ = win._row_values(tr_mp4)
            self.assertNotIn('缺艺术家图', vals_mp4[8])
            vals_nodata, _ = win._row_values(tr_nodata)
            self.assertIn('缺艺术家图', vals_nodata[8])

    def test_on_fix_finished_does_not_popup_dialog(self):
        """测试修复完成时不弹出提示窗，仅更新日志和状态栏。"""
        with offscreen_tk() as root:
            with patch('app.messagebox.showinfo') as m_info:
                win = app_mod.MainWindow(root, auto_scan=False)
                summary = {
                    'written_ok': 5,
                    'written_fail': 0,
                    'cover_fixed': 2,
                    'tags_fixed': 3,
                    'date_fixed': 1,
                    'lyrics_fixed': 1,
                    'artist_fixed': 1,
                    'folder_cover_ok': 1,
                    'folder_cover_fail': 0,
                    'folder_artist_ok': 1,
                    'folder_artist_fail': 0,
                    'synced_stale': 2,
                    'unmatched': 0,
                    'date_unmatched': 0,
                    'lyrics_unmatched': 0,
                    'touched': 2,
                }
                win._on_fix_finished(summary)
                # 不应弹出任何消息窗
                m_info.assert_not_called()
                # 状态栏应更新修复结果文本
                self.assertIn('修复完成：文件写入 5 首', win.var_status.get())

    def test_gui_table_layout_and_window_geometry(self):
        """测试列表版面布局（列宽、最小宽、伸缩属性、居中）与默认窗口尺寸配置。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)

            # 验证各列属性
            # 1. 选择列：足够宽度显示 '☑ 选择'，居中，不拉伸
            chk_cfg = win.tree.column('chk')
            self.assertGreaterEqual(chk_cfg['width'], 60)
            self.assertEqual(chk_cfg['anchor'], 'center')
            self.assertEqual(chk_cfg['stretch'], 0)

            # 2. 歌曲名、艺术家、专辑、说明 具备 stretch=True，以便随窗口自适应扩展
            for col in ('title', 'artist', 'album', 'note'):
                col_cfg = win.tree.column(col)
                self.assertEqual(col_cfg['stretch'], 1, f'{col} should stretch')
                self.assertGreaterEqual(col_cfg['width'], 140)

            # 3. 封面状态、综合状态、音轨号 居中显示
            for col in ('status', 'track', 'cover'):
                col_cfg = win.tree.column(col)
                self.assertEqual(col_cfg['anchor'], 'center')
                self.assertEqual(col_cfg['stretch'], 0)

            # 4. 验证默认窗口尺寸适应屏幕且保存机制
            win._save_user_state()
            self.assertIn('window_geometry', win._user_config)

    def test_gui_column_header_sorting(self):
        """测试点击表头支持正序、倒序及清除排序（恢复原扫描顺序），包含指示箭头与自然排序。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            t1 = LibraryTrack(
                path='track_b.flac', filename='track_b.flac', container='flac', folder=self.tmp,
                title='Song B', artist='Artist Y', album='Album M', disk_track_int=10, disk_tracknumber='10',
                status=STATUS_OK,
            )
            t2 = LibraryTrack(
                path='track_a.flac', filename='track_a.flac', container='flac', folder=self.tmp,
                title='Song A', artist='Artist Z', album='Album N', disk_track_int=2, disk_tracknumber='2',
                status=STATUS_OK,
            )
            t3 = LibraryTrack(
                path='track_c.flac', filename='track_c.flac', container='flac', folder=self.tmp,
                title='Song C', artist='Artist X', album='Album L', disk_track_int=1, disk_tracknumber='1',
                status=STATUS_OK,
            )
            win.tracks = [t1, t2, t3]
            win.var_only_pending.set(False)
            win._refresh_table()

            # 初始状态：默认排序 [专辑艺术家-艺术家-时间-专辑-音轨号-歌曲名]
            # t3 (Artist X) < t1 (Artist Y) < t2 (Artist Z)
            self.assertEqual([t.path for t in win.visible_tracks], ['track_c.flac', 'track_b.flac', 'track_a.flac'])
            self.assertEqual(win.tree.heading('title', 'text'), '歌曲名')

            # 1. 第一次点击「歌曲名」：升序排列 (▲) -> Song A, Song B, Song C
            win._on_heading_click('title')
            self.assertEqual(win._sort_col, 'title')
            self.assertFalse(win._sort_reverse)
            self.assertEqual(win.tree.heading('title', 'text'), '歌曲名 ▲')
            self.assertEqual([t.title for t in win.visible_tracks], ['Song A', 'Song B', 'Song C'])

            # 2. 第二次点击「歌曲名」：降序排列 (▼) -> Song C, Song B, Song A
            win._on_heading_click('title')
            self.assertEqual(win._sort_col, 'title')
            self.assertTrue(win._sort_reverse)
            self.assertEqual(win.tree.heading('title', 'text'), '歌曲名 ▼')
            self.assertEqual([t.title for t in win.visible_tracks], ['Song C', 'Song B', 'Song A'])

            # 3. 第三次点击「歌曲名」：取消排序，恢复默认排序，箭头移除
            win._on_heading_click('title')
            self.assertIsNone(win._sort_col)
            self.assertFalse(win._sort_reverse)
            self.assertEqual(win.tree.heading('title', 'text'), '歌曲名')
            self.assertEqual([t.path for t in win.visible_tracks], ['track_c.flac', 'track_b.flac', 'track_a.flac'])

            # 4. 点击「音轨号」：按数字自然排序（2 在 10 之前，而非字符串字典序）
            win._on_heading_click('track')
            self.assertEqual(win._sort_col, 'track')
            self.assertEqual(win.tree.heading('track', 'text'), '音轨号 ▲')
            self.assertEqual([t.disk_track_int for t in win.visible_tracks], [1, 2, 10])

            # 5. 点击「艺术家」：旧列「音轨号」箭头移除，新列显示箭头并按艺术家排序
            win._on_heading_click('artist')
            self.assertEqual(win.tree.heading('track', 'text'), '音轨号')
            self.assertEqual(win.tree.heading('artist', 'text'), '艺术家 ▲')
            self.assertEqual([t.artist for t in win.visible_tracks], ['Artist X', 'Artist Y', 'Artist Z'])

    def test_default_sort_order_hierarchy(self):
        """测试默认排序层级 [专辑艺术家-艺术家-时间-专辑-音轨号-歌曲名]。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            tracks = [
                # 同专辑艺术家、同艺术家、同专辑、音轨2
                LibraryTrack(path='p4.flac', filename='p4.flac', container='flac', folder=self.tmp,
                             albumartist='AA1', artist='A1', date='2000', album='Alb1', disk_track_int=2, title='Song2'),
                # 同专辑艺术家、同艺术家、同时间、不同专辑 Alb2
                LibraryTrack(path='p5.flac', filename='p5.flac', container='flac', folder=self.tmp,
                             albumartist='AA1', artist='A1', date='2000', album='Alb2', disk_track_int=1, title='Song1'),
                # 专辑艺术家 AA2 (排在 AA1 之后)
                LibraryTrack(path='p6.flac', filename='p6.flac', container='flac', folder=self.tmp,
                             albumartist='AA2', artist='A1', date='1990', album='Alb1', disk_track_int=1, title='Song1'),
                # 同专辑艺术家、同艺术家、更晚时间 2005
                LibraryTrack(path='p7.flac', filename='p7.flac', container='flac', folder=self.tmp,
                             albumartist='AA1', artist='A1', date='2005', album='Alb1', disk_track_int=1, title='Song1'),
                # 同专辑艺术家、同艺术家、同专辑、音轨1 (应排在音轨2之前)
                LibraryTrack(path='p3.flac', filename='p3.flac', container='flac', folder=self.tmp,
                             albumartist='AA1', artist='A1', date='2000', album='Alb1', disk_track_int=1, title='Song1'),
                # 同专辑艺术家 AA1、不同艺术家 A2
                LibraryTrack(path='p2.flac', filename='p2.flac', container='flac', folder=self.tmp,
                             albumartist='AA1', artist='A2', date='1995', album='Alb1', disk_track_int=1, title='Song1'),
            ]
            win.tracks = tracks
            win.var_only_pending.set(False)
            win._refresh_table()

            expected = ['p3.flac', 'p4.flac', 'p5.flac', 'p7.flac', 'p2.flac', 'p6.flac']
            self.assertEqual([t.path for t in win.visible_tracks], expected)

    def test_main_window_pick_cover_with_checkbox_or_single_track(self):
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            t1 = LibraryTrack(path='D:/Music/test1.flac', filename='test1.flac', folder='D:/Music', title='Test1', artist='Singer')
            t2 = LibraryTrack(path='D:/Music/test2.flac', filename='test2.flac', folder='D:/Music', title='Test2', artist='Singer')
            win.tracks = [t1, t2]
            win.visible_tracks = [t1, t2]

            # 场景 1：无高亮也无勾选 -> 提示弹窗
            with patch('ui.main_window._msgbox.showinfo') as mock_info:
                win._on_pick_cover()
                mock_info.assert_called_once()

            # 场景 2：有复选框勾选（未点成蓝色高亮） -> 自动采纳已勾选曲目并打开弹窗
            win.checked_paths = {'D:/Music/test2.flac'}
            with patch('ui.main_window.CoverPickerDialog') as mock_dlg:
                win._on_pick_cover()
                grps = mock_dlg.call_args[0][1]
                self.assertEqual(grps[0]['rep_track'], t2)

            # 场景 3：无高亮无勾选，但当前视图仅有唯一曲目 -> 贴心自动采纳
            win.checked_paths.clear()
            win.visible_tracks = [t1]
            with patch('ui.main_window.CoverPickerDialog') as mock_dlg:
                win._on_pick_cover()
                grps = mock_dlg.call_args[0][1]
                self.assertEqual(grps[0]['rep_track'], t1)

            # 场景 4：勾选跨越两个不同目录的曲目 -> 自动聚合为 2 个专辑组 (多专辑向导)
            t3 = LibraryTrack(path='D:/Other/t3.flac', filename='t3.flac', folder='D:/Other', title='T3', artist='SingerB')
            win.tracks = [t1, t2, t3]
            win.checked_paths = {'D:/Music/test1.flac', 'D:/Other/t3.flac'}
            with patch('ui.main_window.CoverPickerDialog') as mock_dlg:
                win._on_pick_cover()
                grps = mock_dlg.call_args[0][1]
                self.assertEqual(len(grps), 2)
                self.assertEqual({g['folder'] for g in grps}, {'D:/Music', 'D:/Other'})

    def test_main_window_right_click_copy_column_fields(self):
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            t1 = LibraryTrack(
                path='D:/Music/Jay/01. Nocturne.flac',
                filename='01. Nocturne.flac',
                folder='D:/Music/Jay',
                title='夜曲',
                artist='周杰伦',
                album='十一月的萧邦',
                disk_tracknumber='01',
                status=STATUS_OK,
            )
            t2 = LibraryTrack(
                path='D:/Music/Jay/02. Blue Storm.flac',
                filename='02. Blue Storm.flac',
                folder='D:/Music/Jay',
                title='蓝色风暴',
                artist='周杰伦',
                album='十一月的萧邦',
                disk_tracknumber='02',
                status=STATUS_OK,
            )
            win.tracks = [t1, t2]
            win.visible_tracks = [t1, t2]
            win.var_only_pending.set(False)
            win._refresh_table()

            class FakeEvent:
                def __init__(self, x=10, y=10, x_root=100, y_root=100):
                    self.x = x
                    self.y = y
                    self.x_root = x_root
                    self.y_root = y_root

            # 1. 验证右键点击“艺术家”列：识别列为 artist，生成“📋 复制艺术家: 周杰伦”且可成功复制
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#4'), \
                 patch('tkinter.Menu.post') as mock_post:
                ev = FakeEvent()
                win._on_tree_right_click(ev)
                mock_post.assert_called_once()
                menu = win._last_context_menu

                # 检查菜单项中是否包含复制当前列
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                copy_artist_label = next(l for l in labels if '复制艺术家' in l)
                self.assertIn('周杰伦', copy_artist_label)

                # 触发点击该菜单项并验证剪贴板
                menu_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == copy_artist_label)
                menu.invoke(menu_idx)
                self.assertEqual(root.clipboard_get(), '周杰伦')
                self.assertIn('已复制艺术家到剪贴板', win.var_status.get())

                # 验证联动搜索项“🔍 筛选此艺术家”存在并可正常触发
                filter_artist_label = next(l for l in labels if '筛选此艺术家' in l)
                self.assertIn('周杰伦', filter_artist_label)
                filter_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == filter_artist_label)
                menu.invoke(filter_idx)
                self.assertEqual(win.var_search_type.get(), '艺术家')
                self.assertEqual(win.var_search.get(), '周杰伦')

                # 验证右键菜单去重与项完整性：挑选封面与歌词等命令无重复，且结构完整
                cover_entries = [l for l in labels if '挑选专辑封面' in l]
                lyric_entries = [l for l in labels if '歌词' in l]
                self.assertEqual(len(cover_entries), 1)
                self.assertEqual(cover_entries[0], '🖼️ 挑选专辑封面 (cover.jpg)...')
                self.assertEqual(len(lyric_entries), 1)
                self.assertEqual(lyric_entries[0], '🎵 挑选/更换歌词 (lrc)...')
                self.assertTrue(any('挑选写真' in l for l in labels))
                self.assertTrue(any('设置标签' in l for l in labels))
                self.assertTrue(any('在文件管理器中显示' in l for l in labels))

            # 2. 验证右键点击“歌曲名”列：识别列为 title
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#3'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                copy_title_label = next(l for l in labels if '复制歌曲名' in l)
                self.assertIn('夜曲', copy_title_label)
                menu_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == copy_title_label)
                menu.invoke(menu_idx)
                self.assertEqual(root.clipboard_get(), '夜曲')

            # 3. 验证右键点击“音轨号”列：识别列为 track
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#6'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                copy_track_label = next(l for l in labels if '复制音轨号' in l)
                self.assertIn('01', copy_track_label)
                menu_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == copy_track_label)
                menu.invoke(menu_idx)
                self.assertEqual(root.clipboard_get(), '01')

            # 4. 验证右键点击空字段列时置灰禁用
            t1.album = ''
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#5'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                copy_album_label = next(l for l in labels if '复制专辑' in l)
                self.assertIn('(空)', copy_album_label)
                menu_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == copy_album_label)
                self.assertEqual(str(menu.entrycget(menu_idx, 'state')), 'disabled')

            # 5. 验证多选勾选时更多复制子菜单支持批量复制
            t1.album = '十一月的萧邦'
            win.checked_paths = {t1.path, t2.path}
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#3'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                # 找到子菜单
                cascade_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) == 'cascade')
                sub_menu = menu.nametowidget(menu.entrycget(cascade_idx, 'menu'))
                sub_labels = [sub_menu.entrycget(i, 'label') for i in range(sub_menu.index('end') + 1) if sub_menu.type(i) != 'separator']
                self.assertTrue(any('复制已勾选的歌曲名 (共 2 首)' in l for l in sub_labels))
                self.assertTrue(any('复制已勾选的完整路径 (共 2 首)' in l for l in sub_labels))

                # 点击批量复制歌曲名
                batch_title_idx = next(i for i in range(sub_menu.index('end') + 1) if sub_menu.type(i) != 'separator' and '复制已勾选的歌曲名' in sub_menu.entrycget(i, 'label'))
                sub_menu.invoke(batch_title_idx)
                self.assertEqual(root.clipboard_get(), '夜曲\n蓝色风暴')

            # 6. 验证右键点击状态、封面、说明及复选框列（视点击位置而定）
            # 6.1 综合状态列 (#2)
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#2'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                self.assertTrue(any('复制综合状态' in l for l in labels))

            # 6.2 封面状态列 (#7)
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#7'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                self.assertTrue(any('复制封面状态' in l for l in labels))

            # 6.3 复选框/路径列 (#1)
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#1'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                copy_path_label = next(l for l in labels if '复制文件路径' in l)
                self.assertIn('D:/Music', copy_path_label)
                menu_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) != 'separator' and menu.entrycget(i, 'label') == copy_path_label)
                menu.invoke(menu_idx)
                self.assertEqual(root.clipboard_get(), t1.path)

            # 6.4 点击非指定列区域（如 #0 或超出列范围）：缺省回退为安全复制文件路径
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#0'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                labels = [menu.entrycget(i, 'label') for i in range(menu.index('end') + 1) if menu.type(i) != 'separator']
                self.assertTrue(any('复制文件路径' in l for l in labels))

            # 7. 验证子菜单中的文件名、目录路径与整行 TSV 复制功能
            with patch.object(win.tree, 'identify_row', return_value=t1.path), \
                 patch.object(win.tree, 'identify_column', return_value='#3'), \
                 patch('tkinter.Menu.post'):
                win._on_tree_right_click(FakeEvent())
                menu = win._last_context_menu
                cascade_idx = next(i for i in range(menu.index('end') + 1) if menu.type(i) == 'cascade')
                sub_menu = menu.nametowidget(menu.entrycget(cascade_idx, 'menu'))

                # 复制文件名
                fn_idx = next(i for i in range(sub_menu.index('end') + 1) if sub_menu.type(i) != 'separator' and '复制文件名' in sub_menu.entrycget(i, 'label'))
                sub_menu.invoke(fn_idx)
                self.assertEqual(root.clipboard_get(), '01. Nocturne.flac')

                # 复制所在目录路径
                dir_idx = next(i for i in range(sub_menu.index('end') + 1) if sub_menu.type(i) != 'separator' and '复制所在目录路径' in sub_menu.entrycget(i, 'label'))
                sub_menu.invoke(dir_idx)
                self.assertEqual(root.clipboard_get(), 'D:/Music/Jay')

                # 复制整行数据
                tsv_idx = next(i for i in range(sub_menu.index('end') + 1) if sub_menu.type(i) != 'separator' and '复制整行数据' in sub_menu.entrycget(i, 'label'))
                sub_menu.invoke(tsv_idx)
                self.assertIn('夜曲', root.clipboard_get())
                self.assertIn('周杰伦', root.clipboard_get())

    def test_gui_search_field_and_project_types(self):
        """测试页面搜索框、搜索项目类型选择下拉框（专辑名、艺术家、歌曲名等）及实时过滤筛选联动。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)

            # 1. 验证搜索控件初始化与配置
            self.assertTrue(hasattr(win, 'cmb_search_type'))
            self.assertTrue(hasattr(win, 'ent_search'))
            self.assertIsInstance(win.ent_search, ttk.Entry)
            self.assertTrue(hasattr(win, 'btn_search'))
            self.assertTrue(hasattr(win, 'btn_clear_search'))
            self.assertTrue(hasattr(win, 'lbl_search_info'))

            search_values = list(win.cmb_search_type.cget('values'))
            for expected_type in ('全部', '歌曲名', '艺术家', '专辑名', '专辑艺术家', '音轨号', '文件路径', '说明/备注'):
                self.assertIn(expected_type, search_values)
            self.assertEqual(win.var_search_type.get(), '全部')
            self.assertEqual(win.var_search.get(), '')

            # 2. 构造测试数据
            t1 = LibraryTrack(
                path='D:/Music/Jay/01. Seven.flac',
                filename='01. Seven.flac',
                folder='D:/Music/Jay',
                title='七里香',
                artist='周杰伦',
                albumartist='周杰伦',
                album='七里香',
                disk_tracknumber='01',
                disk_track_int=1,
                status=STATUS_OK,
            )
            t2 = LibraryTrack(
                path='D:/Music/Jay/03. Sunny.mp3',
                filename='03. Sunny.mp3',
                folder='D:/Music/Jay',
                title='晴天',
                artist='周杰伦',
                albumartist='周杰伦',
                album='叶惠美',
                disk_tracknumber='03',
                disk_track_int=3,
                status=STATUS_OK,
            )
            t3 = LibraryTrack(
                path='D:/Music/Eason/01. Ten.flac',
                filename='01. Ten.flac',
                folder='D:/Music/Eason',
                title='十年',
                artist='陈奕迅',
                albumartist='陈奕迅',
                album='黑白灰',
                disk_tracknumber='01',
                disk_track_int=1,
                status=STATUS_OK,
            )

            win.tracks = [t1, t2, t3]
            win._has_scanned = True
            win.var_only_pending.set(False)
            win._refresh_table()

            self.assertEqual(len(win.visible_tracks), 3)
            self.assertEqual(win.var_search_info.get(), '共 3 首')

            # 3. 按歌曲名搜索
            win.var_search_type.set('歌曲名')
            win.var_search.set('晴天')
            win._apply_search_immediate()
            self.assertEqual([t.path for t in win.visible_tracks], [t2.path])
            self.assertEqual(win.var_search_info.get(), '已筛选: 1 / 3 首')

            # 4. 按艺术家搜索
            win.var_search_type.set('艺术家')
            win.var_search.set('周杰伦')
            win._apply_search_immediate()
            self.assertEqual([t.path for t in win.visible_tracks], [t1.path, t2.path])
            self.assertEqual(win.var_search_info.get(), '已筛选: 2 / 3 首')

            # 5. 按专辑名搜索
            win.var_search_type.set('专辑名')
            win.var_search.set('黑白灰')
            win._apply_search_immediate()
            self.assertEqual([t.path for t in win.visible_tracks], [t3.path])
            self.assertEqual(win.var_search_info.get(), '已筛选: 1 / 3 首')

            # 6. 按音轨号搜索
            win.var_search_type.set('音轨号')
            win.var_search.set('01')
            win._apply_search_immediate()
            self.assertEqual(len(win.visible_tracks), 2)
            self.assertIn(t1, win.visible_tracks)
            self.assertIn(t3, win.visible_tracks)

            # 7. 按文件路径搜索
            win.var_search_type.set('文件路径')
            win.var_search.set('Sunny.mp3')
            win._apply_search_immediate()
            self.assertEqual([t.path for t in win.visible_tracks], [t2.path])

            # 8. 按全部字段多关键字复合搜索
            win.var_search_type.set('全部')
            win.var_search.set('周杰伦 叶惠美')
            win._apply_search_immediate()
            self.assertEqual([t.path for t in win.visible_tracks], [t2.path])

            # 9. 筛选状态下的全选与取消全选联动
            win.var_search_type.set('艺术家')
            win.var_search.set('周杰伦')
            win._apply_search_immediate()
            self.assertEqual(len(win.visible_tracks), 2)
            win.checked_paths.clear()
            win._toggle_select_all()
            self.assertEqual(win.checked_paths, {t1.path, t2.path})
            self.assertNotIn(t3.path, win.checked_paths)

            # 10. 快速清空搜索
            win._on_clear_search()
            self.assertEqual(win.var_search.get(), '')
            self.assertEqual(len(win.visible_tracks), 3)
            self.assertEqual(win.var_search_info.get(), '共 3 首')

            # 11. 快捷搜索接口 _quick_search
            win._quick_search('陈奕迅', '艺术家')
            self.assertEqual(win.var_search_type.get(), '艺术家')
            self.assertEqual(win.var_search.get(), '陈奕迅')
            self.assertEqual([t.path for t in win.visible_tracks], [t3.path])

            # 12. 状态保存与 Ctrl+F 热键
            win._save_user_state()
            self.assertEqual(win._user_config.get('search_type'), '艺术家')
            self.assertEqual(win._on_ctrl_f(), 'break')

            # 13. 模拟中文输入法回车上屏英文字符（如 "beyond" 或 "be'yo'n'd"）防假空白与回车延迟触发验证
            t_beyond = LibraryTrack(
                path='D:/Music/Beyond/01. Glorious.flac',
                filename='01. Glorious.flac',
                folder='D:/Music/Beyond',
                title='光辉岁月',
                artist='Beyond',
                album='乐与怒',
                status=STATUS_OK,
            )
            win.tracks.append(t_beyond)
            win.var_search_type.set('全部')
            win.var_search.set('beyond')
            win._on_entry_key_release()
            ret_code = win._on_search_return()
            self.assertEqual(ret_code, 'break')
            win._apply_search_from_return()
            self.assertEqual([t.path for t in win.visible_tracks], [t_beyond.path])
            self.assertEqual(win.ent_search.get(), 'beyond')

            # 14. 验证 busy 状态下搜索栏相关操作与回调均被严格拦截保护，光标、焦点与编辑状态完全禁用
            win._set_busy(True)
            self.assertEqual(str(win.btn_search.cget('state')), 'disabled')
            self.assertEqual(str(win.btn_clear_search.cget('state')), 'disabled')
            self.assertEqual(str(win.cmb_search_type.cget('state')), 'disabled')
            self.assertEqual(str(win.ent_search.cget('state')), 'disabled')
            self.assertEqual(str(win.ent_search.cget('cursor')), 'arrow')
            self.assertEqual(str(win.ent_search.cget('takefocus')), '0')
            self.assertEqual(win._on_search_button1(), 'break')
            self.assertEqual(win._on_search_focus_in(), 'break')
            self.assertEqual(win._on_entry_key_release(), 'break')

            win.var_search.set('试图修改')
            win._on_clear_search()
            self.assertEqual(win.var_search.get(), '试图修改')
            win._quick_search('周杰伦', '艺术家')
            self.assertEqual(win.var_search.get(), '试图修改')
            self.assertEqual(win._on_ctrl_f(), 'break')
            win._set_busy(False)
            self.assertEqual(str(win.btn_search.cget('state')), 'normal')
            self.assertEqual(str(win.btn_clear_search.cget('state')), 'normal')
            self.assertEqual(str(win.cmb_search_type.cget('state')), 'readonly')
            self.assertEqual(str(win.ent_search.cget('state')), 'normal')
            self.assertEqual(str(win.ent_search.cget('takefocus')), '1')
            self.assertIsNone(win._on_search_button1())
            self.assertIsNone(win._on_search_focus_in())

    def test_dff_lyrics_repair_in_pipeline(self):
        """测试在 execute_repairs 管线中针对 DFF 格式曲目补全歌词（mode=both 时平滑保存 .lrc 且不被内嵌写标拦截报错）。"""
        dff_p = self._make_minimal_dff('01 - 小城故事.dff')
        tr = app_mod.LibraryTrack(
            path=dff_p,
            filename='01 - 小城故事.dff',
            folder=self.tmp,
            container='dff',
            title='小城故事',
            artist='邓丽君',
            album='小城故事',
            needs_lyrics=True,
            status=STATUS_OK,
        )
        env = app_mod.FoobarEnv()
        mock_match = app_mod.TrackMatch(
            tracknumber=1,
            title='小城故事',
            artist='邓丽君',
            album_name='小城故事',
            lyrics='[00:01.00]小城故事多\n[00:04.00]充满喜和乐',
            source='NetEase',
        )
        opts = app_mod.FixOptions(
            enable_track=False,
            enable_art=False,
            enable_tags=False,
            enable_lyrics=True,
            lyrics_mode='both',
        )
        row_logs = []
        with patch('pipelines.repair.resolve_track_metadata', return_value=mock_match):
            summary = app_mod.execute_repairs(
                env,
                [tr],
                opts,
                row_cb=lambda p, d: row_logs.append((p, d)),
            )
        self.assertEqual(summary['written_ok'], 1)
        self.assertEqual(summary['written_fail'], 0)
        self.assertEqual(summary['lyrics_fixed'], 1)
        self.assertTrue(os.path.isfile(dff_p[:-4] + '.lrc'))
        with open(dff_p[:-4] + '.lrc', 'r', encoding='utf-8') as fp:
            lrc_content = fp.read()
        self.assertIn('[ti:小城故事]', lrc_content)
        self.assertIn('[ar:邓丽君]', lrc_content)
        self.assertIn('小城故事多', lrc_content)
        self.assertTrue(any(d.get('fixed_lyrics') for _, d in row_logs))
        self.assertEqual(row_logs[-1][1].get('status_text'), '已修复')

        # 验证即使同时开启了标签与音轨补全，DFF 也不会因为内嵌写标失败而中断歌词写入
        dff_p2 = self._make_minimal_dff('02 - 小城故事2.dff')
        tr2 = app_mod.LibraryTrack(
            path=dff_p2,
            filename='02 - 小城故事2.dff',
            folder=self.tmp,
            container='dff',
            title='小城故事2',
            artist='邓丽君',
            album='小城故事',
            needs_lyrics=True,
            needs_tracknumber=True,
            status=STATUS_OK,
        )
        opts_all = app_mod.FixOptions(
            enable_track=True,
            enable_art=True,
            enable_tags=True,
            enable_lyrics=True,
            lyrics_mode='both',
        )
        row_logs2 = []
        with patch('pipelines.repair.resolve_track_metadata', return_value=mock_match):
            summary2 = app_mod.execute_repairs(
                env,
                [tr2],
                opts_all,
                row_cb=lambda p, d: row_logs2.append((p, d)),
            )
        self.assertEqual(summary2['written_ok'], 1)
        self.assertEqual(summary2['written_fail'], 0)
        self.assertEqual(summary2['lyrics_fixed'], 1)
        self.assertTrue(os.path.isfile(dff_p2[:-4] + '.lrc'))

        # 验证场景 3：DFF 曲目仅有标签待补（无歌词需写）时，必须明确通过 emit_row 提示未能写入标签的原因和建议，不能静默跳过
        dff_p3 = self._make_minimal_dff('03 - 小城故事3.dff')
        tr3 = app_mod.LibraryTrack(
            path=dff_p3,
            filename='03 - 小城故事3.dff',
            folder=self.tmp,
            container='dff',
            title='小城故事3',
            artist='',
            album='',
            needs_lyrics=False,
            has_lyrics=True,
            missing_core_fields=['artist', 'album'],
            status=STATUS_OK,
        )
        opts_tags_only = app_mod.FixOptions(
            enable_track=False,
            enable_art=False,
            enable_tags=True,
            enable_lyrics=False,
        )
        row_logs3 = []
        with patch('pipelines.repair.resolve_track_metadata', return_value=mock_match):
            summary3 = app_mod.execute_repairs(
                env,
                [tr3],
                opts_tags_only,
                row_cb=lambda p, d: row_logs3.append((p, d)),
            )
        self.assertEqual(summary3['written_ok'], 0)
        self.assertEqual(summary3['written_fail'], 1)
        self.assertTrue(len(row_logs3) > 0)
        last_row = row_logs3[-1][1]
        self.assertEqual(last_row.get('status_text'), '只读保护')
        self.assertIn('DFF 格式遵循发烧规范保护', last_row.get('source_note', ''))
        self.assertIn('转封装为 .dsf', last_row.get('source_note', ''))

        # 验证针对缺少封面时，封面单元格与状态徽标提示手动挑选而不是“无可用图源”
        from ui.main_window import _format_cover_cell, _format_status_badge
        tr_dff_cov = app_mod.LibraryTrack(
            path=dff_p,
            filename='01 - 小城故事.dff',
            folder=self.tmp,
            container='dff',
            title='小城故事',
            artist='邓丽君',
            album='小城故事',
            cover_status='缺封面(手动挑选)',
            cover_reasons=['no-cover-source'],
            status=STATUS_OK,
        )
        self.assertEqual(_format_cover_cell(tr_dff_cov), '缺封面(手动挑选)')
        badge, color = _format_status_badge(tr_dff_cov)
        self.assertEqual(badge, '手动挑选')
        self.assertEqual(color, 'warn')

    def test_main_window_search_filter_scoped_actions(self):
        """测试搜索筛选激活时，一键修复、挑选封面与标签修改严格遵循「所见即所选」，仅处理当前筛选视图中勾选的曲目。"""
        with offscreen_tk() as root:
            win = app_mod.MainWindow(root, auto_scan=False)
            t1 = LibraryTrack(path='D:/Music/2002.flac', filename='2002.flac', folder='D:/Music', title='2002年的第一场雪', artist='刀郎')
            t2 = LibraryTrack(path='D:/Music/teng.flac', filename='teng.flac', folder='D:/Music', title='小城故事', artist='邓丽君')
            win.tracks = [t1, t2]
            win.visible_tracks = [t1, t2]
            win._has_scanned = True
            # 初始全量勾选（模拟扫描完成后两首均需修复）
            win.checked_paths = {t1.path, t2.path}

            # 1. 处于搜索筛选状态 (关键字 '2002')，视图仅见 t1
            win.var_search.set('2002')
            win.visible_tracks = [t1]

            # 验证提取操作目标仅返回当前可见且勾选的 t1，完全隔离 t2
            targets = win._get_target_tracks_for_action('修复')
            self.assertEqual(targets, [t1])

            # 验证点击一键修复触发的 _start_fix 仅接收 t1
            with patch.object(win, '_start_fix') as mock_fix:
                win.btn_one_click.invoke()
                mock_fix.assert_called_once()
                self.assertEqual(mock_fix.call_args[0][0], [t1])

            # 2. 如果在筛选状态下取消勾选 t1，点击修复应被安全拦截并提示（不误伤后台 t2）
            win.checked_paths.discard(t1.path)
            with patch('ui.main_window.messagebox.showinfo') as mock_info, \
                 patch.object(win, '_start_fix') as mock_fix:
                win.btn_one_click.invoke()
                mock_fix.assert_not_called()
                mock_info.assert_called_once()
                self.assertIn('视图外另有 1 首', mock_info.call_args[0][1])

            # 3. 清空搜索关键字后，恢复全量勾选处理
            win.var_search.set('')
            win.checked_paths = {t1.path, t2.path}
            targets_all = win._get_target_tracks_for_action('修复')
            self.assertEqual(len(targets_all), 2)



