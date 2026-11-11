"""单元测试：容器探测、音轨号读写、foobar库解析、曲目匹配与联网兜底。"""

from __future__ import annotations

import io
import os
import shutil
import sqlite3
import struct
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.mp3 import MP3

from art_repair import (
    analyze_folder_art_and_tags,
    apply_folder_cover_file,
    apply_track_deletions,
    apply_track_repairs,
    inspect_deletable_fields,
    read_pictures,
)
from foobar_library import (
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
    FoobarEnv,
    LibraryTrack,
    _parse_metadb_info,
    parse_library_folders,
    scan_library,
)
from tag_io import (
    bump_mtime,
    decode_163_key,
    has_valid_tracknumber,
    read_audio_info,
    sniff_container,
    write_tracknumber,
)
from track_lookup import (
    TrackMatch,
    _build_netease_tracks,
    _match_in_tracks,
    _parse_filename_clues,
    clear_caches,
    normalize_release_date,
    resolve_release_date,
    resolve_track_metadata,
    resolve_track_number,
)


class TrackFixerTests(unittest.TestCase):
    def setUp(self):
        clear_caches()
        self.tmp = tempfile.mkdtemp(prefix='fb_track_test_')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_minimal_mp3(self, filename='test.mp3') -> str:
        # 最小有效 MPEG1 Layer3 帧 (128kbps, 44100Hz, 417 bytes)
        frame_header = bytes([0xFF, 0xFB, 0x90, 0x00])
        frame = frame_header + b'\x00' * 413
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(frame * 3)
        return path

    def _make_minimal_flac(self, filename='test.flac') -> str:
        # fLaC + STREAMINFO (last-metadata-block=1, type=0, len=34) + 34 bytes
        streaminfo = struct.pack('>BBH', 0x80, 0x00, 34)[:4]
        # 正确 4-byte block header: 0x80000022
        header = b'fLaC' + struct.pack('>I', 0x80000022) + (b'\x00' * 34)
        # 补最小采样率/声道，避免 mutagen streaminfo 校验异常：min_block=4096, max_block=4096, sr=44100, ch=2, bps=16
        si = bytearray(34)
        struct.pack_into('>HH', si, 0, 4096, 4096)
        # bytes 10..17: sample_rate (20 bits)=44100 (0x0AC44), ch-1 (3 bits)=1, bps-1 (5 bits)=15, total_samples (36 bits)=44100
        val = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
        struct.pack_into('>Q', si, 10, val)
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(b'fLaC' + struct.pack('>I', 0x80000022) + bytes(si))
        return path

    def _make_minimal_wav(self, filename='test.wav') -> str:
        pcm_data = b'\x00\x00' * 2205
        fmt_chunk = b'fmt ' + struct.pack('<IHHIIHH', 16, 1, 1, 44100, 88200, 2, 16)
        data_chunk = b'data' + struct.pack('<I', len(pcm_data)) + pcm_data
        riff = b'RIFF' + struct.pack('<I', 4 + len(fmt_chunk) + len(data_chunk)) + b'WAVE' + fmt_chunk + data_chunk
        path = os.path.join(self.tmp, filename)
        with open(path, 'wb') as fp:
            fp.write(riff)
        return path

    def test_sniff_and_roundtrip_mp3_flac_wav(self):
        mp3_path = self._make_minimal_mp3()
        flac_path = self._make_minimal_flac()
        wav_path = self._make_minimal_wav()

        self.assertEqual(sniff_container(mp3_path), 'mp3')
        self.assertEqual(sniff_container(flac_path), 'flac')
        self.assertEqual(sniff_container(wav_path), 'wav')

        for fp, num in ((mp3_path, 7), (flac_path, 12), (wav_path, 3)):
            info_before = read_audio_info(fp)
            self.assertFalse(has_valid_tracknumber(info_before), fp)
            ok, msg = write_tracknumber(fp, num)
            self.assertTrue(ok, f'{fp}: {msg}')
            info_after = read_audio_info(fp)
            self.assertEqual(info_after['track_int'], num)
            self.assertTrue(has_valid_tracknumber(info_after))
            # 确保没有留下 .musictmp 临时文件
            leftover = [f for f in os.listdir(self.tmp) if f.endswith('.musictmp')]
            self.assertEqual(leftover, [])

        # 非 overwrite 模式不覆盖已有有效音轨号
        ok2, msg2 = write_tracknumber(flac_path, 99, overwrite=False)
        self.assertFalse(ok2)
        self.assertIn('跳过覆盖', msg2)

    def test_decode_163_key(self):
        sample = (
            "163 key(Don't modify):L64FU3W4YxX3ZFTmbZ+8/eam10XVQrPtLahYtNC7enAJ9mlpBgvX0sr9GKudLNFQ"
            "VqcxU007NFWqDNgyHWMFmx/37KP97Ivn3ajP/wIGzSmzQKvvty3u0gg+28Au2RZXnwfUFj1HoVoD5tNzv43Fe"
            "Rhba8rBx+opuKBKoylQgQ06mjmx9CF5o9WhjPHqM5amcFZH8uRY1bC5xQrntTzvfOb47xtQDR+8/DhYvOjZ6"
            "GrVD4wgUjmx9Y+fcSSg7cMwu0uPzCfPrs5aYeyLvNjJZD4giJqztMUu+JHGWPVEOCGh27ppeYGQkUFY/yX7b"
            "OxO7/fM4KP8rSr3NVHX1DJ7YpohwTuET67c398SW3CJzpZTVRs7XjCrjnkjH47dmPDDcTAl1NgcXX9zDWya"
            "igNOisUzGCa4hBKK3ws64NUj2b1/RC5+6s5tboJINozD5ikBiV3SRqy4aHIQvEQ2I5JhdUW8xXqdYKhPvkDu"
            "vlS8tuPEFz9cBUmprnw7+K1CiXQzCEzcNVM1G2krN+jUBL2XHmEyiQAyt9BMxC8OAdQJS3eEBRLUPr+wvmWt"
            "0daegjPsfErLy61v+0SVDqyHokAJQbYX/rdcDato6fIaIrw9xAk="
        )
        decoded = decode_163_key(sample)
        self.assertIsNotNone(decoded)
        self.assertEqual(str(decoded.get('albumId')), '120605500')
        self.assertEqual(str(decoded.get('musicId')), '1805289472')

    def test_parse_library_folders_and_metadb_blob(self):
        fb_dir = os.path.join(self.tmp, 'fb')
        prof_dir = os.path.join(fb_dir, 'profile')
        lib_dir = os.path.join(prof_dir, 'library-v2.0')
        music_dir = os.path.join(self.tmp, 'MyMusic')
        os.makedirs(lib_dir, exist_ok=True)
        os.makedirs(music_dir, exist_ok=True)

        uri = f'file://{music_dir}\\'.encode('utf-8')
        blob = struct.pack('<II', 1, len(uri)) + uri + struct.pack('<I', 0)
        with open(os.path.join(lib_dir, 'folders'), 'wb') as fp:
            fp.write(blob)

        parsed, unavail = parse_library_folders(prof_dir, fb_dir)
        self.assertEqual(parsed, [os.path.abspath(music_dir)])
        self.assertEqual(unavail, [])

        info_blob = b'\x00' * 24 + b'title\x00Hello\x00\x00tracknumber\x0014\x00\x00\x00codec\x00FLAC\x00\x00'
        tags = _parse_metadb_info(info_blob)
        self.assertEqual(tags['title'], ['Hello'])
        self.assertEqual(tags['tracknumber'], ['14'])

    def test_scan_detects_missing_and_stale_foobar(self):
        fb_dir = os.path.join(self.tmp, 'fb2')
        prof_dir = os.path.join(fb_dir, 'profile')
        music_dir = os.path.join(self.tmp, 'Lib')
        os.makedirs(prof_dir, exist_ok=True)
        os.makedirs(music_dir, exist_ok=True)

        f_missing = self._make_minimal_flac(os.path.join(music_dir, 'a_missing.flac'))
        f_stale = self._make_minimal_flac(os.path.join(music_dir, 'b_stale.flac'))
        f_ok = self._make_minimal_flac(os.path.join(music_dir, 'c_ok.flac'))
        self.assertTrue(write_tracknumber(f_stale, 5)[0])
        self.assertTrue(write_tracknumber(f_ok, 6)[0])

        db_path = os.path.join(prof_dir, 'metadb.sqlite')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE metadb (name TEXT PRIMARY KEY, info BLOB, size INTEGER, lastModified INTEGER)')
        stale_blob = b'\x00' * 24 + b'title\x00B\x00\x00\x00'
        ok_blob = b'\x00' * 24 + b'title\x00C\x00\x00tracknumber\x006\x00\x00\x00'
        conn.execute('INSERT INTO metadb VALUES (?, ?, ?, ?)', (f'0+file://{f_missing}', stale_blob, os.path.getsize(f_missing), 0))
        conn.execute('INSERT INTO metadb VALUES (?, ?, ?, ?)', (f'0+file://{f_stale}', stale_blob, 10, 0))
        conn.execute('INSERT INTO metadb VALUES (?, ?, ?, ?)', (f'0+file://{f_ok}', ok_blob, os.path.getsize(f_ok), 0))
        conn.commit()
        conn.close()

        env = FoobarEnv(fb_dir=fb_dir, profile_dir=prof_dir, library_folders=[music_dir])
        tracks = scan_library(env)
        by_name = {t.filename: t for t in tracks}
        self.assertEqual(by_name['a_missing.flac'].status, STATUS_MISSING_DISK)
        self.assertEqual(by_name['b_stale.flac'].status, STATUS_STALE_FOOBAR)
        self.assertEqual(by_name['c_ok.flac'].status, STATUS_OK)

    def test_build_netease_tracks_skips_full_album_and_keeps_multidisc(self):
        songs = [
            {'id': 100, 'name': '冀西南林路行 (专辑全长)', 'dt': 2662000, 'no': 1, 'cd': '01'},
            {'id': 101, 'name': '早', 'dt': 83650, 'no': 1, 'cd': '01'},
            {'id': 102, 'name': '泥河', 'dt': 348318, 'no': 2, 'cd': '01'},
            {'id': 103, 'name': '第三首', 'dt': 200000, 'no': 1, 'cd': '02'},
        ]
        built = _build_netease_tracks(songs)
        self.assertEqual([(t['name'], t['number'], t['disc']) for t in built], [
            ('早', 1, 1),
            ('泥河', 2, 1),
            ('第三首', 3, 2),
        ])

    def test_match_in_tracks_live_and_duration_tiebreak(self):
        tracks = [
            {'number': 1, 'id': '1', 'name': '水边的阿狄丽娜', 'duration': 155.0},
            {'number': 12, 'id': '2', 'name': '水边的阿狄丽娜 (Live)', 'duration': 185.0},
            {'number': 5, 'id': '3', 'name': '同一首歌', 'duration': 180.0},
            {'number': 9, 'id': '4', 'name': '同一首歌', 'duration': 240.0},
        ]
        m1 = _match_in_tracks(tracks, '水边的阿狄丽娜 (Live)', 185.0)
        self.assertEqual(m1['number'], 12)
        m2 = _match_in_tracks(tracks, '同一首歌', 241.0)
        self.assertEqual(m2['number'], 9)
        # 无法区分时长时不瞎猜
        m3 = _match_in_tracks(tracks, '同一首歌', 0.0)
        self.assertIsNone(m3)

    def test_filename_clues_and_mocked_online_lookup(self):
        tr = LibraryTrack(
            path=r'F:\Music\The Nights - Avicii[The Days ~ Nights (Remixes ~EP)].flac',
            filename='The Nights - Avicii[The Days ~ Nights (Remixes ~EP)].flac',
            folder=r'F:\Music',
        )
        clues = _parse_filename_clues(tr)
        self.assertEqual(clues['inferred_title'], 'The Nights')
        self.assertEqual(clues['inferred_artist'], 'Avicii')
        self.assertEqual(clues['inferred_album'], 'The Days / Nights (Remixes /EP)')

        tr2 = LibraryTrack(
            path=r'F:\Music\TestArtist\MyAlbum\Blue Rondo Ala Turque - 003jpWS23C3yuL.flac',
            filename='Blue Rondo Ala Turque - 003jpWS23C3yuL.flac',
            folder=r'F:\Music\TestArtist\MyAlbum',
            title='Blue Rondo Ala Turque',
            artist='Richard Clayderman',
            album='理查德·克莱德曼钢琴曲-命运',
            duration=190.0,
        )

        import json

        class FakeUrlResp:
            def __init__(self, payload):
                self._raw = json.dumps(payload).encode('utf-8')

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=12):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'fcg_play_single_song' in url:
                return FakeUrlResp({
                    'data': [{
                        'album': {'mid': '002esRFk3AIcMc', 'name': '理查德·克莱德曼钢琴曲-命运'},
                        'index_album': 12,
                    }]
                })
            if 'fcg_v8_album_info_cp' in url:
                return FakeUrlResp({
                    'data': {
                        'list': [
                            {'songmid': '00000000000001', 'songname': 'Track 1', 'interval': 120},
                            {'songmid': '003jpWS23C3yuL', 'songname': 'Blue Rondo Ala Turque', 'interval': 190},
                        ]
                    }
                })
            return FakeUrlResp({})

        with patch('track_lookup.urllib.request.urlopen', side_effect=fake_urlopen):
            res = resolve_track_number(tr2)
        self.assertEqual(res.tracknumber, 2)
        self.assertIn('QQ音乐', res.source)

    def test_gui_mainwindow_smoke_offscreen(self):
        import time
        import tkinter as tk
        import app as app_mod

        root = tk.Tk()
        root.withdraw()
        try:
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
                self.assertEqual(root.title(), 'SongMetadata')
                m_env.assert_not_called()
                m_scan.assert_not_called()
                self.assertFalse(win._has_scanned)
                self.assertEqual(str(win.btn_rescan.cget('text')), '扫描媒体库')

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
        finally:
            root.destroy()

    def _make_jpeg_bytes(self, width=200, height=200, color=(40, 120, 200)) -> bytes:
        im = Image.new('RGB', (width, height), color)
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=90)
        return buf.getvalue()

    def test_bump_mtime_advances_under_2s_rounding(self):
        fp = self._make_minimal_flac('exfat_test.flac')
        state = {'mtime': 1794315144.0}

        def fake_getmtime(_p):
            return state['mtime']

        def fake_utime(_p, times):
            # 模拟 FAT/exFAT 2 秒向下舍入
            state['mtime'] = float(int(times[1]) // 2 * 2)

        with patch('tag_io.os.path.getmtime', side_effect=fake_getmtime), \
             patch('tag_io.os.utime', side_effect=fake_utime), \
             patch('tag_io.time.time', return_value=1794315144.5):
            new_mt = bump_mtime(fp, min_prev_mtime=1794315144.0)
        self.assertGreater(new_mt, 1794315144.0)

    def test_scan_detects_full_metadata_desync_when_size_and_mtime_equal(self):
        fb_dir = os.path.join(self.tmp, 'fb3')
        prof_dir = os.path.join(fb_dir, 'profile')
        music_dir = os.path.join(self.tmp, 'LibTags')
        os.makedirs(prof_dir, exist_ok=True)
        os.makedirs(music_dir, exist_ok=True)

        fp = self._make_minimal_flac(os.path.join(music_dir, 'song_aa.flac'))
        a = FLAC(fp)
        a['TITLE'] = ['奇妙能力歌']
        a['ARTIST'] = ['陈粒']
        a['ALBUM'] = ['如也']
        a['ALBUMARTIST'] = ['陈粒']
        a['DATE'] = ['2015']
        a['TRACKNUMBER'] = ['3']
        a.save()

        st = os.stat(fp)
        db_path = os.path.join(prof_dir, 'metadb.sqlite')
        conn = sqlite3.connect(db_path)
        conn.execute('CREATE TABLE metadb (name TEXT PRIMARY KEY, info BLOB, size INTEGER, lastModified INTEGER)')
        # 故意缺少 album artist，且 size 与 mtime 都和磁盘完全一致（模拟 Padding + exFAT 同2秒窗口）
        lm_filetime = int(st.st_mtime * 10000000) + 116444736000000000
        blob = (
            b'\x00' * 24
            + 'title\x00奇妙能力歌\x00\x00artist\x00陈粒\x00\x00album\x00如也\x00\x00date\x002015\x00\x00tracknumber\x003\x00\x00\x00'.encode('utf-8')
        )
        conn.execute('INSERT INTO metadb VALUES (?, ?, ?, ?)', (f'0+file://{fp}', blob, st.st_size, lm_filetime))
        conn.commit()
        conn.close()

        env = FoobarEnv(fb_dir=fb_dir, profile_dir=prof_dir, library_folders=[music_dir])
        tracks = scan_library(env, include_art_scan=False)
        self.assertEqual(len(tracks), 1)
        tr = tracks[0]
        self.assertEqual(tr.status, STATUS_STALE_FOOBAR)
        self.assertTrue(tr.needs_foobar_sync)
        self.assertTrue(any('专辑艺术家' in r for r in tr.foobar_stale_reasons))

    def test_art_repair_single_and_multi_album_atomic_fix(self):
        # 1) 单专辑目录：track1 健康，track2(mp3) 缺封面+缺分组标签+缺音轨号
        single_dir = os.path.join(self.tmp, '2026-11-10-12-00-00 MyAlbum')
        os.makedirs(single_dir, exist_ok=True)
        t1 = self._make_minimal_flac(os.path.join(single_dir, '01 - Intro.flac'))
        jpg_small = self._make_jpeg_bytes(220, 220, (10, 90, 180))
        a1 = FLAC(t1)
        a1['TITLE'] = ['Intro']
        a1['ARTIST'] = ['Band']
        a1['ALBUM'] = ['MyAlbum']
        a1['ALBUMARTIST'] = ['Band']
        a1['DATE'] = ['2025-05-01']
        a1['TRACKNUMBER'] = ['1']
        pic = Picture()
        pic.type = 3
        pic.mime = 'image/jpeg'
        pic.desc = 'Cover (front)'
        pic.data = jpg_small
        a1.add_picture(pic)
        a1.save()

        t2 = self._make_minimal_mp3(os.path.join(single_dir, 'Outro - 123456.mp3'))
        m2 = MP3(t2)
        m2.add_tags()
        from mutagen.id3 import TPE1
        m2.tags.add(TPE1(encoding=3, text=['Band']))
        m2.save(v2_version=3)

        fplan = analyze_folder_art_and_tags(single_dir, [t1, t2])
        self.assertEqual(fplan.cover_action, 'create')
        p1 = fplan.track_plans[os.path.normcase(os.path.abspath(t1))]
        p2 = fplan.track_plans[os.path.normcase(os.path.abspath(t2))]
        self.assertFalse(p1.needs_cover_fix)
        self.assertFalse(p1.needs_tag_fix)
        self.assertTrue(p2.needs_cover_fix)
        self.assertTrue(p2.needs_tag_fix)
        self.assertEqual(p2.tag_updates.get('album'), 'MyAlbum')
        self.assertEqual(p2.tag_updates.get('albumartist'), 'Band')
        self.assertEqual(p2.tag_updates.get('date'), '2025-05-01')
        self.assertEqual(p2.tag_updates.get('title'), 'Outro')

        ok_dir, _ = apply_folder_cover_file(fplan)
        self.assertTrue(ok_dir)
        self.assertTrue(os.path.isfile(os.path.join(single_dir, 'cover.jpg')))

        ok_t2, msg_t2 = apply_track_repairs(
            t2,
            tag_updates=p2.tag_updates,
            cover_bytes=p2.target_pic_bytes,
            cover_mime=p2.target_pic_mime,
            tracknumber=2,
        )
        self.assertTrue(ok_t2, msg_t2)
        info2 = read_audio_info(t2)
        self.assertEqual(info2['track_int'], 2)
        self.assertEqual(info2['album'], 'MyAlbum')
        self.assertEqual(info2['albumartist'], 'Band')
        self.assertEqual(info2['title'], 'Outro')
        self.assertEqual(len(read_pictures(MP3(t2), 'mp3')), 1)
        self.assertEqual([f for f in os.listdir(single_dir) if f.endswith('.musictmp')], [])

        # 2) 多专辑目录存在误放 cover.jpg：应撤回为 disabled-cover.jpg.bak，且不互相串标签
        multi_dir = os.path.join(self.tmp, 'MixedDir')
        os.makedirs(multi_dir, exist_ok=True)
        ma = self._make_minimal_flac(os.path.join(multi_dir, 'a.flac'))
        mb = self._make_minimal_flac(os.path.join(multi_dir, 'b.flac'))
        fa = FLAC(ma); fa['TITLE'] = ['A']; fa['ARTIST'] = ['ArtA']; fa['ALBUM'] = ['AlbA']; fa['TRACKNUMBER'] = ['1']; fa.save()
        fb = FLAC(mb); fb['TITLE'] = ['B']; fb['ARTIST'] = ['ArtB']; fb['ALBUM'] = ['AlbB']; fb['TRACKNUMBER'] = ['1']; fb.save()
        stray_cover = os.path.join(multi_dir, 'cover.jpg')
        with open(stray_cover, 'wb') as fp:
            fp.write(jpg_small)
        mplan = analyze_folder_art_and_tags(multi_dir, [ma, mb])
        self.assertEqual(mplan.cover_action, 'remove')
        ok_rem, _ = apply_folder_cover_file(mplan)
        self.assertTrue(ok_rem)
        self.assertFalse(os.path.exists(stray_cover))
        self.assertTrue(os.path.isfile(os.path.join(multi_dir, 'disabled-cover.jpg.bak')))

    def test_execute_repairs_sync_only(self):
        import app as app_mod

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

    def test_normalize_release_date(self):
        # 网易云毫秒时间戳按东八区日期解析，0 / 占位值返回空
        self.assertEqual(normalize_release_date(1608566400000, is_ms=True), '2020-12-22')
        self.assertEqual(normalize_release_date(0, is_ms=True), '')
        self.assertEqual(normalize_release_date(1091462400), '2004-08-03')
        self.assertEqual(normalize_release_date(2015), '2015')
        self.assertEqual(normalize_release_date('2022-04-15 00:00:00'), '2022-04-15')
        self.assertEqual(normalize_release_date('2005-03-01T08:00:00Z'), '2005-03-01')
        self.assertEqual(normalize_release_date('2020/5/1'), '2020-05-01')
        self.assertEqual(normalize_release_date('20200501'), '2020-05-01')
        self.assertEqual(normalize_release_date('2020-05'), '2020-05')
        self.assertEqual(normalize_release_date('2020'), '2020')
        self.assertEqual(normalize_release_date('0000-00-00'), '')
        self.assertEqual(normalize_release_date('1970-01-01 08:00:00'), '')
        self.assertEqual(normalize_release_date({'isoString': '2021-06-18T00:00:00Z'}), '2021-06-18')
        self.assertEqual(normalize_release_date({'year': 2019}), '2019')

    def test_pkl_release_date_lookup_offline(self):
        import pickle

        cases = [
            ('ne', 'SongNE - 111111.flac', {
                'source': 'NeteaseMusicClient',
                'identifier': '111111',
                'song_name': 'SongNE',
                'raw_data': {'search': {'al': {'id': 1, 'publishTime': 1608566400000}}, 'download': {}},
            }, '2020-12-22'),
            ('qq', 'SongQQ - 003jpWS23C3yuL.flac', {
                'source': 'QQMusicClient',
                'identifier': '003jpWS23C3yuL',
                'song_name': 'SongQQ',
                'raw_data': {'search': {'time_public': '2019-07-12'}, 'download': {}},
            }, '2019-07-12'),
            ('kg', 'SongKG - abcdef1234.flac', {
                'source': 'KugouMusicClient',
                'identifier': 'abcdef1234',
                'song_name': 'SongKG',
                'raw_data': {'search': {'PublishDate': '2022-04-15 00:00:00'}, 'download': {}},
            }, '2022-04-15'),
            ('ap', 'SongAP - 999999.m4a', {
                'source': 'AppleMusicClient',
                'identifier': '999999',
                'song_name': 'SongAP',
                'raw_data': {'search': {'attributes': {'trackNumber': 4, 'discNumber': 1, 'releaseDate': '2023-11-02T07:00:00Z'}}, 'download': {}},
            }, '2023-11-02'),
            ('sp', 'SongSP - 5aB6cD7eF8.mp3', {
                'source': 'SpotifyMusicClient',
                'identifier': '5aB6cD7eF8',
                'song_name': 'SongSP',
                'raw_data': {'search': {'item': {'data': {'albumOfTrack': {'date': {'isoString': '2024-02-14T00:00:00Z'}}}}}, 'download': {}},
            }, '2024-02-14'),
        ]
        with patch('track_lookup.urllib.request.urlopen', side_effect=AssertionError('不应发起网络请求')):
            for sub, fname, item, expected_date in cases:
                clear_caches()
                d = os.path.join(self.tmp, sub)
                os.makedirs(d, exist_ok=True)
                with open(os.path.join(d, 'download_results.pkl'), 'wb') as fp:
                    pickle.dump([item], fp)
                tr = LibraryTrack(path=os.path.join(d, fname), filename=fname, folder=d, title=item['song_name'])
                res = resolve_release_date(tr)
                self.assertEqual(res.release_date, expected_date, sub)
                if sub == 'ap':
                    res_both = resolve_track_number(tr)
                    self.assertEqual(res_both.tracknumber, 4)
                    self.assertEqual(res_both.release_date, expected_date)

    def test_online_lookup_release_date_and_cover_album_guard(self):
        import json

        class FakeUrlResp:
            def __init__(self, payload):
                self._raw = json.dumps(payload).encode('utf-8')

            def read(self):
                return self._raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=12):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'music.163.com/api/search/get' in url and 'type=10' in url:
                return FakeUrlResp({
                    'result': {'albums': [{'id': 9001, 'name': '七里香', 'artist': {'name': '音屿'}, 'publishTime': 1781884800000}]}
                })
            if 'music.163.com/api/v1/album/9001' in url:
                return FakeUrlResp({
                    'album': {'publishTime': 1781884800000},
                    'songs': [{'id': 1, 'name': '七里香', 'dt': 198000, 'no': 1, 'cd': '01'}],
                })
            if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in url and 't=8' in url:
                return FakeUrlResp({
                    'data': {'album': {'list': [{'albumMID': '003DFRzD192KKD', 'albumName': '七里香', 'singerName': '周杰伦', 'publicTime': '2004-08-03'}]}}
                })
            if 'fcg_v8_album_info_cp' in url:
                return FakeUrlResp({
                    'data': {
                        'aDate': '2004-08-03',
                        'list': [
                            {'songmid': '001', 'songname': '我的地盘', 'interval': 240, 'cdIdx': 0},
                            {'songmid': '002', 'songname': '七里香', 'interval': 299, 'cdIdx': 0},
                        ],
                    }
                })
            if 'itunes.apple.com/search' in url and 'entity=album' in url:
                return FakeUrlResp({'results': [{'collectionId': 777, 'collectionName': 'OnlyItunes', 'artistName': 'ArtistIT', 'releaseDate': '2018-09-14T07:00:00Z'}]})
            if 'itunes.apple.com/lookup' in url:
                return FakeUrlResp({'results': [
                    {'wrapperType': 'collection', 'collectionId': 777, 'releaseDate': '2018-09-14T07:00:00Z'},
                    {'wrapperType': 'track', 'trackId': 11, 'trackName': 'TrackIT', 'trackTimeMillis': 200000, 'discNumber': 1, 'trackNumber': 1, 'releaseDate': '2018-09-14T07:00:00Z'},
                ]})
            if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in url and 't=0' in url:
                return FakeUrlResp({
                    'data': {'song': {'list': [{'songmid': '0099', 'songname': '孤勇', 'singer': [{'name': '歌手A'}], 'albummid': '', 'albumname': '单曲', 'interval': 210, 'pubtime': 1640966400}]}}
                })
            return FakeUrlResp({})

        with patch('track_lookup.urllib.request.urlopen', side_effect=fake_urlopen):
            tr_jay = LibraryTrack(path='jay.flac', filename='七里香.flac', folder=self.tmp, title='七里香', artist='周杰伦', album='七里香', duration=299.0)
            res = resolve_track_metadata(tr_jay, need_track=True, need_date=True)
            self.assertEqual(res.tracknumber, 2)
            self.assertEqual(res.release_date, '2004-08-03')
            self.assertEqual(res.source, 'QQ音乐')

            # 已有音轨号、仅缺日期，命中 iTunes 专辑
            tr_it = LibraryTrack(path='it.flac', filename='TrackIT.flac', folder=self.tmp, title='TrackIT', artist='ArtistIT', album='OnlyItunes', duration=200.0, disk_tracknumber='1', disk_track_int=1)
            res_it = resolve_release_date(tr_it)
            self.assertEqual(res_it.release_date, '2018-09-14')

            # 无专辑名走 QQ 单曲反查获取发布日期
            tr_single = LibraryTrack(path='sg.flac', filename='孤勇.flac', folder=self.tmp, title='孤勇', artist='歌手A', album='', duration=210.0)
            res_sg = resolve_release_date(tr_single)
            self.assertEqual(res_sg.release_date, '2022-01-01')

    def test_scan_and_execute_repairs_date_end_to_end(self):
        import app as app_mod
        from foobar_library import track_has_pending_work

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
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        try:
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
        finally:
            root.destroy()

    def test_extended_tags_roundtrip_flac_mp3_and_163_protection(self):
        flac_path = self._make_minimal_flac('ext.flac')
        mp3_path = self._make_minimal_mp3('ext.mp3')

        sample_163 = (
            "163 key(Don't modify):L64FU3W4YxX3ZFTmbZ+8/eam10XVQrPtLahYtNC7enAJ9mlpBgvX0sr9GKudLNFQ"
            "VqcxU007NFWqDNgyHWMFmx/37KP97Ivn3ajP/wIGzSmzQKvvty3u0gg+28Au2RZXnwfUFj1HoVoD5tNzv43Fe"
            "Rhba8rBx+opuKBKoylQgQ06mjmx9CF5o9WhjPHqM5amcFZH8uRY1bC5xQrntTzvfOb47xtQDR+8/DhYvOjZ6"
            "GrVD4wgUjmx9Y+fcSSg7cMwu0uPzCfPrs5aYeyLvNjJZD4giJqztMUu+JHGWPVEOCGh27ppeYGQkUFY/yX7b"
            "OxO7/fM4KP8rSr3NVHX1DJ7YpohwTuET67c398SW3CJzpZTVRs7XjCrjnkjH47dmPDDcTAl1NgcXX9zDWya"
            "igNOisUzGCa4hBKK3ws64NUj2b1/RC5+6s5tboJINozD5ikBiV3SRqy4aHIQvEQ2I5JhdUW8xXqdYKhPvkDu"
            "vlS8tuPEFz9cBUmprnw7+K1CiXQzCEzcNVM1G2krN+jUBL2XHmEyiQAyt9BMxC8OAdQJS3eEBRLUPr+wvmWt"
            "0daegjPsfErLy61v+0SVDqyHokAJQbYX/rdcDato6fIaIrw9xAk="
        )

        # FLAC 带 DESCRIPTION=163 key 时应允许安全写入 COMMENT 并保留 163 key
        fl = FLAC(flac_path)
        fl['TITLE'] = ['Demo']
        fl['DESCRIPTION'] = [sample_163]
        fl.save()

        ok, msg = apply_track_repairs(
            flac_path,
            tag_updates={
                'albumartist': 'Album Artist X',
                'date': '2020-01-02',
                'composer': 'Composer X',
                'performer': 'Vocal: Singer X',
                'genre': 'Rock',
                'comment': 'Liner Notes',
            },
            tracknumber=4,
            discnumber=2,
        )
        self.assertTrue(ok, msg)
        info_fl = read_audio_info(flac_path)
        self.assertEqual(info_fl['track_int'], 4)
        self.assertEqual(info_fl['disc_int'], 2)
        self.assertEqual(info_fl['albumartist'], 'Album Artist X')
        self.assertEqual(info_fl['date'], '2020-01-02')
        self.assertEqual(info_fl['composer'], 'Composer X')
        self.assertEqual(info_fl['performer'], 'Vocal: Singer X')
        self.assertEqual(info_fl['genre'], 'Rock')
        self.assertEqual(info_fl['comment'], 'Liner Notes')
        self.assertTrue(info_fl['has_163_key'])
        self.assertIsNotNone(info_fl['netease_meta'])
        fl_after = FLAC(flac_path)
        self.assertIn(sample_163, fl_after.get('DESCRIPTION', []))

        # MP3 写入完整扩展标签并在 ID3 v2.3 下正确回读
        ok_m, msg_m = apply_track_repairs(
            mp3_path,
            tag_updates={
                'title': 'Song M',
                'artist': 'Artist M',
                'album': 'Album M',
                'albumartist': 'Album Artist M',
                'date': '2023-06-07',
                'composer': 'Composer M',
                'performer': 'Guitar: Player M',
                'genre': 'Jazz',
                'comment': 'Edition Note',
            },
            tracknumber=5,
            discnumber=1,
        )
        self.assertTrue(ok_m, msg_m)
        info_mp3 = read_audio_info(mp3_path)
        self.assertEqual(info_mp3['track_int'], 5)
        self.assertEqual(info_mp3['disc_int'], 1)
        self.assertEqual(info_mp3['composer'], 'Composer M')
        self.assertEqual(info_mp3['performer'], 'Guitar: Player M')
        self.assertEqual(info_mp3['genre'], 'Jazz')
        self.assertEqual(info_mp3['comment'], 'Edition Note')
        id3 = MP3(mp3_path).tags
        self.assertEqual(str(id3['TCOM']), 'Composer M')
        self.assertEqual(str(id3['TCON']), 'Jazz')
        self.assertEqual(str(id3['TPOS']), '1')

        # MP3 若自带 163 key COMM，则应拒绝覆写 comment，且 scan_library 不将 comment 列为可写缺失项
        mp3_163 = self._make_minimal_mp3('01 - NeteaseProtected.mp3')
        from mutagen.id3 import ID3, COMM, TIT2, TALB, TPE1
        tags_163 = ID3()
        tags_163.add(TIT2(encoding=3, text='Protected Track'))
        tags_163.add(TPE1(encoding=3, text='ArtistP'))
        tags_163.add(TALB(encoding=3, text='AlbumP'))
        tags_163.add(COMM(encoding=3, lang='eng', desc='', text=sample_163))
        tags_163.save(mp3_163, v2_version=3)

        ok_p, _ = apply_track_repairs(mp3_163, tag_updates={'comment': 'Overwrite Attempt', 'genre': 'Pop'})
        self.assertTrue(ok_p)
        info_p = read_audio_info(mp3_163)
        self.assertTrue(info_p['has_163_key'])
        self.assertEqual(info_p['genre'], 'Pop')
        self.assertIn("163 key(Don't modify):", info_p['raw_comment'])

        tr_p = next(t for t in scan_library(FoobarEnv(library_folders=[self.tmp])) if t.filename == '01 - NeteaseProtected.mp3')
        self.assertNotIn('comment', tr_p.missing_extra_fields)

    def test_resolve_track_metadata_cross_platform_merge_and_cover_download(self):
        from track_lookup import (
            _get_album_meta,
            _merge_match,
            _parse_lyric_credits,
            _set_album_meta,
            fetch_cover_bytes,
        )

        # 验证歌词演职员解析
        comp, perf = _parse_lyric_credits(
            '[00:00.00] 作词 : Wordsmith\n'
            '[00:01.00] 作曲 : MelodyA / MelodyB\n'
            '[00:02.00] 演唱 : SingerX\n'
            '[00:03.00] 吉他 : GuitaristY\n'
        )
        self.assertEqual(comp, 'MelodyA, MelodyB')
        self.assertEqual(perf, 'SingerX, GuitaristY (吉他)')

        # 验证 _merge_match 仅补空白不覆盖已有值，且 TrackMatch 不包含 comment 字段
        self.assertFalse(hasattr(TrackMatch(None), 'comment'))
        dst = TrackMatch(tracknumber=2, discnumber=None, totaltracks=8, source='网易云音乐', album_name='AlbumM', release_date='2022-08-01', composer='CompN')
        src = TrackMatch(tracknumber=9, discnumber=2, totaltracks=10, source='QQ音乐', album_name='AlbumQ', release_date='2022-09-09', composer='OtherComp', genre='Synthpop', cover_url='https://example.com/c.jpg')
        merged = _merge_match(dst, src)
        self.assertEqual(merged.tracknumber, 2)
        self.assertEqual(merged.discnumber, 2)
        self.assertEqual(merged.release_date, '2022-08-01')
        self.assertEqual(merged.composer, 'CompN')
        self.assertEqual(merged.genre, 'Synthpop')
        self.assertEqual(merged.cover_url, 'https://example.com/c.jpg')
        self.assertEqual(merged.source, '网易云音乐')

        # 验证跨平台联网聚合：网易云提供曲号/日期/专辑艺术家/封面/歌词演职员，QQ 补充流派/碟片号（不获取注释）
        tr = LibraryTrack(
            path=os.path.join(self.tmp, 'SongMerge.flac'),
            filename='SongMerge.flac',
            folder=self.tmp,
            title='SongMerge',
            artist='SingerM',
            album='AlbumM',
            duration=210.0,
        )

        def fake_ne_album(aid: str):
            _set_album_meta('netease', aid, release_date='2022-08-01', albumartist='SingerM', cover_url='https://example.com/ne.jpg')
            return [
                {'id': '990', 'name': 'Intro', 'artist': 'SingerM', 'number': 1, 'disc': 1, 'duration': 180.0},
                {'id': '991', 'name': 'SongMerge', 'artist': 'SingerM', 'number': 2, 'disc': None, 'duration': 210.0},
            ]

        def fake_qq_album(amid: str):
            _set_album_meta('qq', amid, release_date='2022-08-01', albumartist='SingerM', genre='Indie Pop', cover_url='https://example.com/qq.jpg')
            return [
                {'id': 'qqmid0', 'name': 'Intro', 'artist': 'SingerM', 'number': 1, 'disc': 1, 'duration': 180.0},
                {'id': 'qqmid1', 'name': 'SongMerge', 'artist': 'SingerM', 'number': 2, 'disc': 1, 'duration': 210.0},
            ]

        with patch('track_lookup._search_netease_album', return_value=('1001', 'AlbumM', True)), \
             patch('track_lookup._netease_album_tracks', side_effect=fake_ne_album), \
             patch('track_lookup._fetch_netease_song_credits', return_value={'composer': 'CompNe', 'performer': '贝斯: BassNe'}), \
             patch('track_lookup._search_qq_album', return_value=('qqalb1', 'AlbumM', True)), \
             patch('track_lookup._qq_album_tracks', side_effect=fake_qq_album), \
             patch('track_lookup._fetch_qq_song_extra', return_value={'genre': 'Indie Pop', 'composer': '', 'performer': ''}), \
             patch('track_lookup._search_itunes_album', return_value=('', '', False)), \
             patch('track_lookup._search_musicbrainz_album', return_value=('', '', False)), \
             patch('track_lookup._search_song_and_resolve_album', return_value=None):
            res = resolve_track_metadata(tr, need_track=True, need_date=True, need_cover=True, need_core=True, need_extra=True)

        self.assertEqual(res.tracknumber, 2)
        self.assertEqual(res.discnumber, 1)
        self.assertEqual(res.release_date, '2022-08-01')
        self.assertEqual(res.albumartist, 'SingerM')
        self.assertEqual(res.composer, 'CompNe')
        self.assertEqual(res.performer, '贝斯: BassNe')
        self.assertEqual(res.genre, 'Indie Pop')
        self.assertEqual(res.cover_url, 'https://example.com/ne.jpg')
        self.assertEqual(_get_album_meta('netease', '1001')['cover_url'], 'https://example.com/ne.jpg')

        # 验证 fetch_cover_bytes 将 PNG 规范为 JPEG 并缓存
        png_buf = io.BytesIO()
        Image.new('RGB', (400, 400), (20, 140, 220)).save(png_buf, format='PNG')
        png_bytes = png_buf.getvalue()

        class _FakeResp:
            def __init__(self, data: bytes):
                self._data = data
            def read(self):
                return self._data
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        with patch('track_lookup.urllib.request.urlopen', return_value=_FakeResp(png_bytes)) as m_open:
            cov1 = fetch_cover_bytes('https://example.com/cover.png')
            cov2 = fetch_cover_bytes('https://example.com/cover.png')
        self.assertIsNotNone(cov1)
        self.assertTrue(cov1.startswith(b'\xff\xd8'))
        self.assertEqual(cov1, cov2)
        self.assertEqual(m_open.call_count, 1)

    def test_scan_and_execute_repairs_full_metadata_and_online_cover_end_to_end(self):
        import app as app_mod
        from foobar_library import track_has_pending_work

        alb_dir = os.path.join(self.tmp, 'AlbumMergeDir')
        os.makedirs(alb_dir, exist_ok=True)
        online_jpg = self._make_jpeg_bytes(500, 500, (180, 90, 40))

        sample_163 = (
            "163 key(Don't modify):L64FU3W4YxX3ZFTmbZ+8/eam10XVQrPtLahYtNC7enAJ9mlpBgvX0sr9GKudLNFQ"
            "VqcxU007NFWqDNgyHWMFmx/37KP97Ivn3ajP/wIGzSmzQKvvty3u0gg+28Au2RZXnwfUFj1HoVoD5tNzv43Fe"
            "Rhba8rBx+opuKBKoylQgQ06mjmx9CF5o9WhjPHqM5amcFZH8uRY1bC5xQrntTzvfOb47xtQDR+8/DhYvOjZ6"
            "GrVD4wgUjmx9Y+fcSSg7cMwu0uPzCfPrs5aYeyLvNjJZD4giJqztMUu+JHGWPVEOCGh27ppeYGQkUFY/yX7b"
            "OxO7/fM4KP8rSr3NVHX1DJ7YpohwTuET67c398SW3CJzpZTVRs7XjCrjnkjH47dmPDDcTAl1NgcXX9zDWya"
            "igNOisUzGCa4hBKK3ws64NUj2b1/RC5+6s5tboJINozD5ikBiV3SRqy4aHIQvEQ2I5JhdUW8xXqdYKhPvkDu"
            "vlS8tuPEFz9cBUmprnw7+K1CiXQzCEzcNVM1G2krN+jUBL2XHmEyiQAyt9BMxC8OAdQJS3eEBRLUPr+wvmWt"
            "0daegjPsfErLy61v+0SVDqyHokAJQbYX/rdcDato6fIaIrw9xAk="
        )

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
        fl2['DESCRIPTION'] = [sample_163]
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
            {'composer', 'performer', 'genre', 'discnumber'},
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
                performer='Piano: PerformerM',
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
            self.assertEqual(info['performer'], 'Piano: PerformerM')
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
        import contextlib
        buf = io.StringIO()
        with patch('sys.argv', ['app.py', '--dry-run', '--no-extra-tags', '--folder', alb_dir]), \
             contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_negative_search_cache_platform_filter_and_mb_circuit_breaker(self):
        import json
        import threading
        import track_lookup as tl

        calls: list[str] = []
        lock = threading.Lock()

        class _FakeResp:
            def __init__(self, payload):
                self._raw = json.dumps(payload).encode('utf-8')
            def read(self):
                return self._raw
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=8):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            with lock:
                calls.append(url)
            if 'music.163.com/api/search/get' in url and 'type=10' in url:
                return _FakeResp({'result': {'albums': [{'id': 7001, 'name': 'OnlyNeteaseAlb', 'artist': {'name': 'SingerZ'}}]}})
            if 'music.163.com/api/v1/album/7001' in url:
                return _FakeResp({'album': {'publishTime': 1608566400000}, 'songs': [
                    {'id': 801, 'name': 'SongOne', 'dt': 180000, 'no': 1, 'cd': '01'},
                    {'id': 802, 'name': 'SongTwo', 'dt': 200000, 'no': 2, 'cd': '01'},
                ]})
            if 'music.163.com/api/song/lyric' in url:
                return _FakeResp({'lrc': {'lyric': '[00:00.00] 作曲 : CompZ\n'}})
            return _FakeResp({})

        t1 = LibraryTrack(
            path='s1.flac', filename='SongOne.flac', folder=self.tmp,
            title='SongOne', artist='SingerZ', album='OnlyNeteaseAlb',
            date='2020-12-22', genre='Pop', comment='Note', disk_discnumber='1', disk_disc_int=1, duration=180.0,
        )
        t2 = LibraryTrack(
            path='s2.flac', filename='SongTwo.flac', folder=self.tmp,
            title='SongTwo', artist='SingerZ', album='OnlyNeteaseAlb',
            date='2020-12-22', genre='Pop', comment='Note', disk_discnumber='1', disk_disc_int=1, duration=200.0,
        )
        with patch('track_lookup.urllib.request.urlopen', side_effect=fake_urlopen), \
             patch('track_lookup.time.sleep', return_value=None):
            r1 = resolve_track_metadata(t1, need_track=False, need_date=False, need_cover=False, need_core=False, need_extra=True)
            r2 = resolve_track_metadata(t2, need_track=False, need_date=False, need_cover=False, need_core=False, need_extra=True)

        self.assertEqual(r1.composer, 'CompZ')
        self.assertEqual(r2.composer, 'CompZ')
        # 仅缺失 performer/composer 时，不应请求 iTunes；且 QQ/MB 专辑搜索未命中结果应被负缓存，第二首不再重复查询
        self.assertFalse(any('itunes.apple.com' in u for u in calls))
        self.assertEqual(sum(1 for u in calls if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in u and 't=8' in u), 2)
        self.assertEqual(sum(1 for u in calls if 'musicbrainz.org/ws/2/release/' in u), 1)

        # 未匹配任何平台时，不应再把全部平台重跑第二遍
        calls.clear()
        clear_caches()
        t_miss = LibraryTrack(path='miss.flac', filename='Unknown.flac', folder=self.tmp, title='Unknown', artist='Nobody', album='NoSuchAlbum')
        with patch('track_lookup.urllib.request.urlopen', side_effect=lambda req, timeout=8: (calls.append(req.full_url) or _FakeResp({}))), \
             patch('track_lookup.time.sleep', return_value=None):
            r_miss = resolve_track_metadata(t_miss, need_track=True, need_date=True, need_cover=True, need_core=True, need_extra=True)
        self.assertEqual(r_miss.source, '')
        self.assertEqual(sum(1 for u in calls if 'musicbrainz.org/ws/2/release/' in u), 1)

        # MusicBrainz 连续网络失败达阈值后自动熔断，避免后续专辑阻塞
        clear_caches()
        mb_calls = 0

        def fail_mb(req, timeout=8):
            nonlocal mb_calls
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'musicbrainz.org' in url:
                mb_calls += 1
                raise OSError('network down')
            return _FakeResp({})

        with patch('track_lookup.urllib.request.urlopen', side_effect=fail_mb), \
             patch('track_lookup.time.sleep', return_value=None):
            for idx in range(8):
                tl._search_musicbrainz_album(f'Alb{idx}', f'Art{idx}')
        self.assertEqual(mb_calls, tl._MB_FAIL_THRESHOLD)
        self.assertTrue(tl._MB_CIRCUIT_OPEN)

    def test_execute_repairs_lookup_progress_and_fast_cancel(self):
        import threading
        import time
        import app as app_mod

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

    def test_pick_canonical_album_date_and_album_consistency_repair(self):
        import app as app_mod
        from art_repair import pick_canonical_album_date
        from foobar_library import track_has_pending_work

        # 1) 占位日期 YYYY-01-01 与精确日期、跨年单曲并存时的主日期判定
        self.assertEqual(
            pick_canonical_album_date(['2012-01-01'] * 4 + ['2012-09-04'] * 2 + ['2022-08-15']),
            '2012-09-04',
        )
        self.assertEqual(pick_canonical_album_date(['2005-01-01'] * 3 + ['2005-06-07']), '2005-06-07')
        self.assertEqual(pick_canonical_album_date(['1989', '1989-02-01', '1989-02-01']), '1989-02-01')
        self.assertEqual(pick_canonical_album_date(['2020-01-01'] * 4 + ['2025-05-01']), '2020-01-01')

        # 2) 跨目录同专辑 DATE 不一致 + 混用空/1 碟号 + 遗留 .musictmp + 同名异艺术家隔离
        dir_a = os.path.join(self.tmp, '2026-11-11-10-00-00 SameAlbA')
        dir_b = os.path.join(self.tmp, '2026-11-11-10-05-00 SameAlbB')
        dir_other = os.path.join(self.tmp, '2026-11-11-10-10-00 OtherArtist')
        for d in (dir_a, dir_b, dir_other):
            os.makedirs(d, exist_ok=True)
        jpg = self._make_jpeg_bytes(220, 220, (50, 90, 140))

        def _make_flac(folder: str, name: str, *, title: str, artist: str, album: str, date: str, disc: str, tn: str):
            p = self._make_minimal_flac(os.path.join(folder, name))
            a = FLAC(p)
            a['TITLE'] = [title]
            a['ARTIST'] = [artist]
            a['ALBUMARTIST'] = [artist]
            a['ALBUM'] = [album]
            a['TRACKNUMBER'] = [tn]
            a['GENRE'] = ['Pop']
            a['COMPOSER'] = ['Comp']
            a['PERFORMER'] = ['Perf']
            if date:
                a['DATE'] = [date]
            if disc:
                a['DISCNUMBER'] = [disc]
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, 'image/jpeg', 'Cover (front)', jpg
            a.add_picture(pic)
            a.save()
            return p

        t1 = _make_flac(dir_a, '01.flac', title='T1', artist='ArtistA', album='SharedTitle', date='2012-01-01', disc='1', tn='1')
        t2 = _make_flac(dir_a, '02.flac', title='T2', artist='ArtistA', album='SharedTitle', date='2012-01-01', disc='', tn='2')
        t3 = _make_flac(dir_b, '03.flac', title='T3', artist='ArtistA', album='SharedTitle', date='2012-09-04', disc='1', tn='3')
        t_other = _make_flac(dir_other, '01.flac', title='O1', artist='ArtistB', album='SharedTitle', date='1999-05-01', disc='1', tn='1')
        for d in (dir_a, dir_b, dir_other):
            with open(os.path.join(d, 'cover.jpg'), 'wb') as fp:
                fp.write(jpg)

        # 人造同曲目遗留的 .musictmp
        stale_tmp = os.path.join(dir_a, '02.abc123.musictmp')
        with open(stale_tmp, 'wb') as fp:
            fp.write(b'stale')

        env = FoobarEnv(library_folders=[dir_a, dir_b, dir_other])
        tracks = scan_library(env)
        by_path = {t.path: t for t in tracks}

        self.assertEqual(by_path[t1].tag_updates.get('date'), '2012-09-04')
        self.assertEqual(by_path[t2].tag_updates.get('date'), '2012-09-04')
        self.assertEqual(by_path[t2].target_disc, 1)
        self.assertFalse(by_path[t3].needs_tag_fix)
        self.assertFalse(by_path[t_other].needs_tag_fix)
        self.assertTrue(track_has_pending_work(by_path[t2], True, True, True, True, True, enable_extra_tags=False))

        targets = [t for t in tracks if app_mod.default_checked(t, options=app_mod.FixOptions())]
        with patch('app.sync_files_to_foobar', return_value=(2, True)):
            summary = app_mod.execute_repairs(env, targets, app_mod.FixOptions())
        self.assertEqual(summary['written_fail'], 0)
        self.assertEqual(summary['date_fixed'], 2)
        self.assertEqual(read_audio_info(t1)['date'], '2012-09-04')
        self.assertEqual(read_audio_info(t2)['date'], '2012-09-04')
        self.assertEqual(read_audio_info(t2)['disc_int'], 1)
        self.assertEqual(read_audio_info(t_other)['date'], '1999-05-01')
        self.assertFalse(os.path.exists(stale_tmp))

        tracks_after = scan_library(env)
        for tr in tracks_after:
            self.assertFalse(tr.needs_tag_fix)
            self.assertFalse(track_has_pending_work(tr, True, True, True, True, True, enable_extra_tags=True))

    def test_online_date_conflict_normalization_and_musictmp_retry_cleanup(self):
        import app as app_mod
        from tag_io import _remove_file_with_retry

        # 1) 瞬时 PermissionError 时退避重试删除，不残留 .musictmp
        tmp_f = os.path.join(self.tmp, 'locked.musictmp')
        with open(tmp_f, 'wb') as fp:
            fp.write(b'tmp')
        real_remove = os.remove
        calls = {'n': 0}

        def flaky_remove(p):
            calls['n'] += 1
            if calls['n'] == 1:
                raise PermissionError('locked by foobar')
            return real_remove(p)

        with patch('tag_io.os.remove', side_effect=flaky_remove), \
             patch('tag_io.time.sleep', return_value=None):
            ok_rm = _remove_file_with_retry(tmp_f, attempts=3, base_delay=0.01)
        self.assertTrue(ok_rm)
        self.assertEqual(calls['n'], 2)
        self.assertFalse(os.path.exists(tmp_f))

        # 2) 在线查询同专辑不同单曲日期时，按主发行日统一写回
        alb_dir = os.path.join(self.tmp, '2026-11-11-11-00-00 OnlineAlb')
        os.makedirs(alb_dir, exist_ok=True)
        jpg = self._make_jpeg_bytes(220, 220, (20, 60, 120))
        with open(os.path.join(alb_dir, 'cover.jpg'), 'wb') as fp:
            fp.write(jpg)

        paths = []
        for idx, nm in enumerate(('One', 'Two', 'Three'), 1):
            p = self._make_minimal_flac(os.path.join(alb_dir, f'0{idx} - {nm}.flac'))
            a = FLAC(p)
            a['TITLE'] = [nm]
            a['ARTIST'] = ['ArtistO']
            a['ALBUMARTIST'] = ['ArtistO']
            a['ALBUM'] = ['OnlineAlb']
            a['TRACKNUMBER'] = [str(idx)]
            a['DISCNUMBER'] = ['1']
            a['GENRE'] = ['Pop']
            a['COMPOSER'] = ['Comp']
            a['PERFORMER'] = ['Perf']
            pic = Picture()
            pic.type, pic.mime, pic.desc, pic.data = 3, 'image/jpeg', 'Cover (front)', jpg
            a.add_picture(pic)
            a.save()
            paths.append(p)

        env = FoobarEnv(library_folders=[alb_dir])
        tracks = scan_library(env)

        def fake_resolve(tr, **_kwargs):
            if tr.title == 'One':
                return TrackMatch(None, source='网易云', album_name='OnlineAlb', release_date='2024-07-26')
            return TrackMatch(None, source='网易云', album_name='OnlineAlb', release_date='2024-04-05')

        with patch('app.resolve_track_metadata', side_effect=fake_resolve), \
             patch('app.sync_files_to_foobar', return_value=(3, True)):
            s = app_mod.execute_repairs(env, tracks, app_mod.FixOptions())
        self.assertEqual(s['written_fail'], 0)
        self.assertEqual(s['date_fixed'], 3)
        for p in paths:
            self.assertEqual(read_audio_info(p)['date'], '2024-04-05')

    def test_per_field_repair_selection_buttons_and_execution(self):
        import contextlib
        import tkinter as tk
        import app as app_mod

        expected_buttons = [
            ('cover', '封面'),
            ('date', '日期'),
            ('albumartist', '专辑艺术家'),
            ('artist', '艺术家'),
            ('composer', '作曲'),
            ('performer', '表演者'),
            ('genre', '流派'),
            ('tracknumber', '音轨号'),
            ('discnumber', '碟片编号'),
        ]

        # 1) 验证 GUI 具备 9 个独立修复项按钮，且支持全不选 / 单选过滤 / 全选
        root = tk.Tk()
        root.withdraw()
        try:
            win = app_mod.MainWindow(root, auto_scan=False)
            self.assertEqual(list(win.field_buttons.keys()), [k for k, _ in expected_buttons])
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
        finally:
            root.destroy()

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
                composer='OnlineComp', performer='OnlinePerf', genre='Rock',
                cover_url='https://example.com/s.jpg',
            )

        sel_opts = app_mod.FixOptions(
            enable_track=False, enable_art=False, enable_tags=True, enable_extra_tags=True,
            enable_date=False, enable_albumartist=False, enable_artist=False,
            enable_composer=True, enable_performer=False, enable_genre=False, enable_disc=True,
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

        # 3) 验证 CLI 单字段禁用参数在 --dry-run 下可正常解析运行
        buf = io.StringIO()
        with patch('sys.argv', [
            'app.py', '--dry-run', '--folder', sub,
            '--no-date', '--no-albumartist', '--no-artist', '--no-composer', '--no-performer', '--no-genre', '--no-disc', '--no-art', '--no-track',
        ]), contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit) as cm:
                app_mod.main()
        self.assertEqual(cm.exception.code, 0)

    def test_delete_partial_metadata_flac_mp3_wav_and_163_protection(self):
        from mutagen.id3 import COMM, ID3, TMCL, TXXX
        from mutagen.wave import WAVE

        sample_163 = (
            "163 key(Don't modify):L64FU3W4YxX3ZFTmbZ+8/eam10XVQrPtLahYtNC7enAJ9mlpBgvX0sr9GKudLNFQ"
            "VqcxU007NFWqDNgyHWMFmx/37KP97Ivn3ajP/wIGzSmzQKvvty3u0gg+28Au2RZXnwfUFj1HoVoD5tNzv43Fe"
            "Rhba8rBx+opuKBKoylQgQ06mjmx9CF5o9WhjPHqM5amcFZH8uRY1bC5xQrntTzvfOb47xtQDR+8/DhYvOjZ6"
            "GrVD4wgUjmx9Y+fcSSg7cMwu0uPzCfPrs5aYeyLvNjJZD4giJqztMUu+JHGWPVEOCGh27ppeYGQkUFY/yX7b"
            "OxO7/fM4KP8rSr3NVHX1DJ7YpohwTuET67c398SW3CJzpZTVRs7XjCrjnkjH47dmPDDcTAl1NgcXX9zDWya"
            "igNOisUzGCa4hBKK3ws64NUj2b1/RC5+6s5tboJINozD5ikBiV3SRqy4aHIQvEQ2I5JhdUW8xXqdYKhPvkDu"
            "vlS8tuPEFz9cBUmprnw7+K1CiXQzCEzcNVM1G2krN+jUBL2XHmEyiQAyt9BMxC8OAdQJS3eEBRLUPr+wvmWt"
            "0daegjPsfErLy61v+0SVDqyHokAJQbYX/rdcDato6fIaIrw9xAk="
        )
        cov = self._make_jpeg_bytes(200, 200, (90, 60, 150))

        # 1) FLAC：保留 title/artist/album/date/genre/track/disc 与 DESCRIPTION 163 key，仅删除 composer/performer/comment/cover
        flac_path = self._make_minimal_flac('del_partial.flac')
        fl = FLAC(flac_path)
        fl['TITLE'] = ['KeepTitle']
        fl['ARTIST'] = ['KeepArtist']
        fl['ALBUMARTIST'] = ['KeepAA']
        fl['ALBUM'] = ['KeepAlbum']
        fl['DATE'] = ['2021-05-01']
        fl['DESCRIPTION'] = [sample_163]
        fl.save()
        ok_w, msg_w = apply_track_repairs(
            flac_path,
            tag_updates={'composer': 'DelComp', 'performer': 'DelPerf', 'genre': 'Rock', 'comment': 'DelComment'},
            cover_bytes=cov, cover_mime='image/jpeg', tracknumber=6, discnumber=2,
        )
        self.assertTrue(ok_w, msg_w)

        deleted: list[str] = []
        ok_d, msg_d = apply_track_deletions(
            flac_path, ['composer', 'performer', 'comment', 'cover'], protect_163_key=True, out_deleted=deleted,
        )
        self.assertTrue(ok_d, msg_d)
        self.assertEqual(set(deleted), {'composer', 'performer', 'comment', 'cover'})
        info_fl = read_audio_info(flac_path)
        self.assertEqual(info_fl['title'], 'KeepTitle')
        self.assertEqual(info_fl['artist'], 'KeepArtist')
        self.assertEqual(info_fl['albumartist'], 'KeepAA')
        self.assertEqual(info_fl['album'], 'KeepAlbum')
        self.assertEqual(info_fl['date'], '2021-05-01')
        self.assertEqual(info_fl['genre'], 'Rock')
        self.assertEqual(info_fl['track_int'], 6)
        self.assertEqual(info_fl['disc_int'], 2)
        self.assertEqual(info_fl['composer'], '')
        self.assertEqual(info_fl['performer'], '')
        self.assertEqual(info_fl['comment'], '')
        self.assertTrue(info_fl['has_163_key'])
        self.assertEqual(read_pictures(FLAC(flac_path), 'flac'), [])

        # 再次以 protect_163_key=True 尝试删除 comment，应跳过并保留 163 key
        deleted2: list[str] = []
        ok_d2, msg_d2 = apply_track_deletions(flac_path, ['comment'], protect_163_key=True, out_deleted=deleted2)
        self.assertTrue(ok_d2)
        self.assertEqual(deleted2, [])
        self.assertIn('163 key', msg_d2)

        # 关闭 163 key 保护后应连同 DESCRIPTION 中的 163 key 一并清除
        deleted3: list[str] = []
        ok_d3, msg_d3 = apply_track_deletions(flac_path, ['comment'], protect_163_key=False, out_deleted=deleted3)
        self.assertTrue(ok_d3, msg_d3)
        self.assertEqual(deleted3, ['comment'])
        self.assertFalse(read_audio_info(flac_path)['has_163_key'])

        # 2) MP3：同时包含 TXXX:PERFORMER、TMCL、无关 TXXX、双 COMM（163 key + 普通注释）和 APIC
        mp3_path = self._make_minimal_mp3('del_partial.mp3')
        ok_mw, msg_mw = apply_track_repairs(
            mp3_path,
            tag_updates={'title': 'Mp3Title', 'artist': 'Mp3Art', 'album': 'Mp3Alb', 'composer': 'Mp3Comp', 'genre': 'Jazz', 'date': '2022-03-04'},
            cover_bytes=cov, cover_mime='image/jpeg', tracknumber=3, discnumber=2,
        )
        self.assertTrue(ok_mw, msg_mw)
        id3 = ID3(mp3_path)
        id3.add(TXXX(encoding=3, desc='PERFORMER', text=['Singer A']))
        id3.add(TXXX(encoding=3, desc='REPLAYGAIN_TRACK_GAIN', text=['-4.5 dB']))
        id3.add(TMCL(encoding=3, people=[['guitar', 'Player B']]))
        id3.add(COMM(encoding=3, lang='eng', desc='', text=[sample_163]))
        id3.add(COMM(encoding=3, lang='eng', desc='extra', text=['Junk Note']))
        id3.save(mp3_path, v2_version=3)

        mp3_deleted: list[str] = []
        ok_md, msg_md = apply_track_deletions(
            mp3_path, ['performer', 'comment', 'cover', 'discnumber'], protect_163_key=True, out_deleted=mp3_deleted,
        )
        self.assertTrue(ok_md, msg_md)
        self.assertEqual(set(mp3_deleted), {'performer', 'comment', 'cover', 'discnumber'})
        info_mp3 = read_audio_info(mp3_path)
        self.assertEqual(info_mp3['title'], 'Mp3Title')
        self.assertEqual(info_mp3['composer'], 'Mp3Comp')
        self.assertEqual(info_mp3['track_int'], 3)
        self.assertEqual(info_mp3['performer'], '')
        self.assertIsNone(info_mp3['disc_int'])
        self.assertTrue(info_mp3['has_163_key'])
        self.assertNotIn('Junk Note', info_mp3['raw_comment'])
        self.assertEqual(read_pictures(MP3(mp3_path), 'mp3'), [])
        id3_after = MP3(mp3_path).tags
        rg = [f for f in id3_after.getall('TXXX') if f.desc == 'REPLAYGAIN_TRACK_GAIN']
        self.assertEqual(len(rg), 1)
        self.assertEqual(leftover := [f for f in os.listdir(self.tmp) if f.endswith('.musictmp')], [])

        # 3) WAV：删除 date 与 tracknumber，保留 title
        wav_path = self._make_minimal_wav('del_partial.wav')
        ok_ww, msg_ww = apply_track_repairs(wav_path, tag_updates={'title': 'WavKeep', 'date': '2020-01-01'}, tracknumber=8)
        self.assertTrue(ok_ww, msg_ww)
        wav_del: list[str] = []
        ok_wd, msg_wd = apply_track_deletions(wav_path, ['date', 'tracknumber'], out_deleted=wav_del)
        self.assertTrue(ok_wd, msg_wd)
        self.assertEqual(set(wav_del), {'date', 'tracknumber'})
        info_wav = read_audio_info(wav_path)
        self.assertEqual(info_wav['title'], 'WavKeep')
        self.assertEqual(info_wav['date'], '')
        self.assertIsNone(info_wav['track_int'])
        self.assertEqual(WAVE(wav_path).tags.getall('TRCK'), [])

    def test_execute_deletions_folder_cover_dry_run_and_cli(self):
        import contextlib
        import app as app_mod

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
        import time
        import tkinter as tk
        import app as app_mod

        sub = os.path.join(self.tmp, 'GuiDel')
        os.makedirs(sub, exist_ok=True)
        cov = self._make_jpeg_bytes(200, 200, (120, 90, 40))
        with open(os.path.join(sub, 'cover.jpg'), 'wb') as out:
            out.write(cov)
        fp1 = self._make_minimal_flac(os.path.join(sub, '01.flac'))
        fp2 = self._make_minimal_flac(os.path.join(sub, '02.flac'))
        for idx, fp in enumerate((fp1, fp2), 1):
            apply_track_repairs(
                fp,
                tag_updates={
                    'title': f'Song{idx}', 'artist': 'Art', 'albumartist': 'Art', 'album': 'Alb',
                    'date': '2023-01-02', 'composer': f'Comp{idx}', 'performer': f'Perf{idx}', 'genre': 'Pop',
                },
                cover_bytes=cov, cover_mime='image/jpeg', tracknumber=idx, discnumber=1,
            )

        env = FoobarEnv(library_folders=[sub])
        scanned = scan_library(env)

        root = tk.Tk()
        root.withdraw()
        try:
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
                    # 验证全选/全不选及封面联动开关状态
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
        finally:
            root.destroy()


if __name__ == '__main__':
    unittest.main(verbosity=2)
