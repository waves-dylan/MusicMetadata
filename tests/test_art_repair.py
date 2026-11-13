"""单元测试：foobar 库扫描、专辑内/跨目录封面与标签修复、发行日期一致性及并发重命名清理。"""

from __future__ import annotations

import io
import os
import sqlite3
import struct
from unittest.mock import patch

from mutagen.flac import FLAC, Picture
from mutagen.id3 import TPE1
from mutagen.mp3 import MP3

import app as app_mod
from art_repair import (
    ARTIST_MAX_SIDE,
    ARTIST_PIC_TYPE,
    analyze_folder_art_and_tags,
    apply_artist_picture,
    apply_folder_artist_file,
    apply_folder_cover_file,
    apply_single_front_cover,
    apply_track_repairs,
    delete_artist_pictures_from_audio,
    delete_folder_artist_file,
    normalize_artist_jpeg,
    pick_canonical_album_date,
    read_pictures,
)
from foobar_library import (
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
    FoobarEnv,
    _parse_metadb_info,
    parse_library_folders,
    scan_library,
    track_has_pending_work,
)
from tag_io import _remove_file_with_retry, read_audio_info, write_tracknumber
from tests.common import TempTrackTestCase
from track_lookup import TrackMatch


class ArtRepairTests(TempTrackTestCase):
    def _make_tagged_flac(
        self,
        folder: str,
        name: str,
        *,
        title: str,
        artist: str,
        album: str,
        tn: str,
        jpg: bytes,
        date: str = '',
        disc: str = '',
    ) -> str:
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

    def test_pick_canonical_album_date_and_album_consistency_repair(self):
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

        t1 = self._make_tagged_flac(dir_a, '01.flac', title='T1', artist='ArtistA', album='SharedTitle', date='2012-01-01', disc='1', tn='1', jpg=jpg)
        t2 = self._make_tagged_flac(dir_a, '02.flac', title='T2', artist='ArtistA', album='SharedTitle', date='2012-01-01', disc='', tn='2', jpg=jpg)
        t3 = self._make_tagged_flac(dir_b, '03.flac', title='T3', artist='ArtistA', album='SharedTitle', date='2012-09-04', disc='1', tn='3', jpg=jpg)
        t_other = self._make_tagged_flac(dir_other, '01.flac', title='O1', artist='ArtistB', album='SharedTitle', date='1999-05-01', disc='1', tn='1', jpg=jpg)
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

        paths = [
            self._make_tagged_flac(
                alb_dir, f'0{idx} - {nm}.flac',
                title=nm, artist='ArtistO', album='OnlineAlb',
                disc='1', tn=str(idx), jpg=jpg,
            )
            for idx, nm in enumerate(('One', 'Two', 'Three'), 1)
        ]

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

    def test_artist_art_coexistence_and_deletion(self):
        # 1) FLAC: 封面 (Type 3) 与 艺术家图 (Type 8) 共存与无损删除
        flac_p = self._make_minimal_flac('flac_artist_test.flac')
        c_bytes = self._make_jpeg_bytes(300, 300, (255, 0, 0))
        a_bytes = self._make_jpeg_bytes(400, 400, (0, 255, 0))

        audio_flac = FLAC(flac_p)
        self.assertTrue(apply_single_front_cover(audio_flac, 'flac', c_bytes))
        audio_flac.save()

        audio_flac = FLAC(flac_p)
        pics = read_pictures(audio_flac, 'flac')
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0]['type'], 3)

        self.assertTrue(apply_artist_picture(audio_flac, 'flac', a_bytes))
        audio_flac.save()

        audio_flac = FLAC(flac_p)
        pics = read_pictures(audio_flac, 'flac')
        self.assertEqual(len(pics), 2)
        types = {p['type'] for p in pics}
        self.assertEqual(types, {3, ARTIST_PIC_TYPE})

        self.assertTrue(delete_artist_pictures_from_audio(audio_flac, 'flac'))
        audio_flac.save()

        audio_flac = FLAC(flac_p)
        pics = read_pictures(audio_flac, 'flac')
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0]['type'], 3)

        # 2) MP3: APIC 封面与艺术家图共存与无损删除
        mp3_p = self._make_minimal_mp3('mp3_artist_test.mp3')
        audio_mp3 = MP3(mp3_p)
        self.assertTrue(apply_single_front_cover(audio_mp3, 'mp3', c_bytes))
        audio_mp3.save()

        audio_mp3 = MP3(mp3_p)
        pics = read_pictures(audio_mp3, 'mp3')
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0]['type'], 3)

        self.assertTrue(apply_artist_picture(audio_mp3, 'mp3', a_bytes))
        audio_mp3.save()

        audio_mp3 = MP3(mp3_p)
        pics = read_pictures(audio_mp3, 'mp3')
        self.assertEqual(len(pics), 2)
        types = {p['type'] for p in pics}
        self.assertEqual(types, {3, ARTIST_PIC_TYPE})

        self.assertTrue(delete_artist_pictures_from_audio(audio_mp3, 'mp3'))
        audio_mp3.save()

        audio_mp3 = MP3(mp3_p)
        pics = read_pictures(audio_mp3, 'mp3')
        self.assertEqual(len(pics), 1)
        self.assertEqual(pics[0]['type'], 3)

    def test_normalize_artist_jpeg(self):
        from PIL import Image
        import io

        big_bytes = self._make_jpeg_bytes(1600, 1200, (30, 90, 150))
        # 默认最大边不超过 1000px，质量 100，体积在 50~200KB 范围
        norm = normalize_artist_jpeg(big_bytes)
        self.assertIsNotNone(norm)
        im = Image.open(io.BytesIO(norm))
        self.assertLessEqual(max(im.size), 1000)
        self.assertEqual(im.format, 'JPEG')
        self.assertLessEqual(len(norm), 200 * 1024)

        # 显式指定 max_side 仍可正常回退
        norm_800 = normalize_artist_jpeg(big_bytes, max_side=800)
        self.assertIsNotNone(norm_800)
        im_800 = Image.open(io.BytesIO(norm_800))
        self.assertLessEqual(max(im.size), 1000)
        self.assertLessEqual(max(im_800.size), 800)
        self.assertLessEqual(len(norm_800), 200 * 1024)

    def test_single_album_folder_artist_file_plan_and_apply(self):
        from PIL import Image

        alb_dir = os.path.join(self.tmp, 'FolderArtistAlb')
        os.makedirs(alb_dir, exist_ok=True)
        with open(os.path.join(alb_dir, 'artist.png'), 'wb') as fp:
            Image.new('RGB', (1000, 1000), (50, 100, 150)).save(fp, 'PNG')

        p = self._make_minimal_flac(os.path.join(alb_dir, '01.flac'))
        a = FLAC(p)
        a['TITLE'] = ['Track 1']
        a['ARTIST'] = ['Singer']
        a['ALBUM'] = ['FolderArtistAlb']
        a['TRACKNUMBER'] = ['1']
        a.save()

        plan = analyze_folder_art_and_tags(alb_dir, [p])
        self.assertIn(plan.artist_cover_action, ('create', 'rewrite'))
        self.assertIsNotNone(plan.artist_cover_jpeg)

        ok, msg = apply_folder_artist_file(plan)
        self.assertTrue(ok, msg)
        art_file = os.path.join(alb_dir, 'artist.jpg')
        self.assertTrue(os.path.isfile(art_file))

        ok_del, msg_del = delete_folder_artist_file(alb_dir)
        self.assertTrue(ok_del, msg_del)
        self.assertFalse(os.path.isfile(art_file))

    def test_folder_art_plan_artist_path_property_and_runner_batch_download(self):
        from PIL import Image
        from art_repair import FolderArtPlan
        from runner import _download_and_apply_online_artist_pics

        fp = FolderArtPlan(folder=self.tmp, artist_cover_path=os.path.join(self.tmp, 'artist.jpg'))
        self.assertEqual(fp.artist_path, fp.artist_cover_path)
        fp.artist_path = os.path.join(self.tmp, 'custom_artist.jpg')
        self.assertEqual(fp.artist_cover_path, os.path.join(self.tmp, 'custom_artist.jpg'))

        # 测试 runner 中 seen_nodata 写回目录 artist.jpg 不再抛出 AttributeError
        alb_dir = os.path.join(self.tmp, 'OnlineArtistAlb')
        os.makedirs(alb_dir, exist_ok=True)
        img_buf = io.BytesIO()
        Image.new('RGB', (400, 400), (80, 120, 160)).save(img_buf, 'JPEG')
        fake_pic_bytes = img_buf.getvalue()

        fp2 = FolderArtPlan(
            folder=alb_dir,
            artist_cover_action='nodata',
            artist_cover_path=os.path.join(alb_dir, 'artist.jpg'),
        )
        p = self._make_minimal_flac(os.path.join(alb_dir, '01.flac'))
        tr = app_mod.LibraryTrack(
            path=p,
            filename='01.flac',
            folder=alb_dir,
            artist='TestSinger',
            needs_artist_art=True,
            folder_plan=fp2,
        )
        matches = {p: TrackMatch(1, source='test', artist='TestSinger', artist_pic_url='http://example.com/artist.jpg')}
        opts = app_mod.FixOptions(enable_artist_art=True)

        with patch('runner.resolve_hook', return_value=lambda **kw: fake_pic_bytes):
            cancelled, online_artists, ok_cnt, fail_cnt = _download_and_apply_online_artist_pics(
                [tr], [tr], matches, set(), None, lambda _m: None, lambda: False, opts,
            )
        self.assertFalse(cancelled)
        self.assertEqual(ok_cnt, 1)
        self.assertTrue(os.path.isfile(os.path.join(alb_dir, 'artist.jpg')))
