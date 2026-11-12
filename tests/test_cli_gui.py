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

from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3
from PIL import Image

import app as app_mod
from art_repair import (
    apply_track_repairs,
    apply_track_set_tags,
    convert_track_format,
    parse_set_tags,
    read_pictures,
)
from foobar_library import (
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
    FoobarEnv,
    LibraryTrack,
    scan_library,
    track_has_pending_work,
)
from tag_io import read_audio_info, write_lrc_file
from tests.common import SAMPLE_163_KEY, TempTrackTestCase, offscreen_tk
from track_lookup import TrackMatch


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
                status=STATUS_STALE_FOOBAR,
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
                self.assertIn('Apple/iTunes优先', prefer_values)
                self.assertIn('MusicBrainz优先', prefer_values)
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

    def test_execute_repairs_sync_only(self):
        fp = self._make_minimal_flac(os.path.join(self.tmp, 'sync_me.flac'))
        tr = LibraryTrack(
            path=fp,
            filename='sync_me.flac',
            folder=self.tmp,
            title='Sync',
            disk_tracknumber='1',
            disk_track_int=1,
            status=STATUS_STALE_FOOBAR,
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
            ('genre', '流派'),
            ('tracknumber', '音轨号'),
            ('discnumber', '碟片编号'),
        ]

        # 1) 验证 GUI 具备 8 个独立修复项按钮（不含表演者），且支持全不选 / 单选过滤 / 全选
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
            self.assertTrue(tr1.needs_foobar_sync)
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
                    dlg.var_format.set('m4a')
                    dlg.var_overwrite.set(True)
                    dlg.var_delete_source.set(False)
                    dlg._on_confirm()
                    self.assertIsNotNone(dlg.result)
                    self.assertEqual(dlg.result.target_format, 'm4a')
                    self.assertTrue(dlg.result.overwrite)
                finally:
                    dlg = None
