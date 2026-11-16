"""单元测试：容器探测、音轨号/扩展标签读写、163 key 保护、歌词合并与部分删除。"""

from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

from mutagen.flac import FLAC
from mutagen.id3 import COMM, ID3, TALB, TIT2, TMCL, TPE1, TXXX
from mutagen.mp3 import MP3
from mutagen.wave import WAVE

from core.models import FoobarEnv
from infra.integrations.foobar import find_foobar_cache_diffs, scan_library
from services.art import (
    analyze_folder_art_and_tags,
    apply_track_deletions,
    apply_track_repairs,
    inspect_deletable_fields,
    read_pictures,
)
from tag_io import (
    bump_mtime,
    convert_audio_file,
    decode_163_key,
    delete_lrc_file,
    get_lrc_path,
    has_valid_tracknumber,
    merge_lyrics,
    parse_filename_clues,
    read_audio_info,
    read_lrc_file,
    remux_dff_to_dsf,
    sniff_container,
    write_lrc_file,
    write_tracknumber,
)
from tests.common import SAMPLE_163_KEY, TempTrackTestCase


class TagIOTests(TempTrackTestCase):
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
            leftover = [f for f in os.listdir(self.tmp) if f.endswith('.musictmp')]
            self.assertEqual(leftover, [])

        ok2, msg2 = write_tracknumber(flac_path, 99, overwrite=False)
        self.assertFalse(ok2)
        self.assertIn('跳过覆盖', msg2)

    def test_decode_163_key(self):
        decoded = decode_163_key(SAMPLE_163_KEY)
        self.assertIsNotNone(decoded)
        self.assertEqual(str(decoded.get('albumId')), '120605500')
        self.assertEqual(str(decoded.get('musicId')), '1805289472')

    def test_bump_mtime_advances_under_2s_rounding(self):
        fp = self._make_minimal_flac('exfat_test.flac')
        state = {'mtime': 1794315144.0}

        def fake_getmtime(_p):
            return state['mtime']

        def fake_utime(_p, times):
            state['mtime'] = float(int(times[1]) // 2 * 2)

        with patch('tag_io.os.path.getmtime', side_effect=fake_getmtime), \
             patch('tag_io.os.utime', side_effect=fake_utime), \
             patch('tag_io.time.time', return_value=1794315144.5):
            new_mt = bump_mtime(fp, min_prev_mtime=1794315144.0)
        self.assertGreater(new_mt, 1794315144.0)

    def test_extended_tags_roundtrip_flac_mp3_and_163_protection(self):
        flac_path = self._make_minimal_flac('ext.flac')
        mp3_path = self._make_minimal_mp3('ext.mp3')

        fl = FLAC(flac_path)
        fl['TITLE'] = ['Demo']
        fl['DESCRIPTION'] = [SAMPLE_163_KEY]
        fl.save()

        ok, msg = apply_track_repairs(
            flac_path,
            tag_updates={
                'albumartist': 'Album Artist X',
                'date': '2020-01-02',
                'composer': 'Composer X',
                'performer': 'Ignored Performer',
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
        self.assertEqual(info_fl['performer'], '')
        self.assertEqual(info_fl['genre'], 'Rock')
        self.assertEqual(info_fl['comment'], 'Liner Notes')
        self.assertTrue(info_fl['has_163_key'])
        self.assertIsNotNone(info_fl['netease_meta'])
        fl_after = FLAC(flac_path)
        self.assertIn(SAMPLE_163_KEY, fl_after.get('DESCRIPTION', []))

        ok_m, msg_m = apply_track_repairs(
            mp3_path,
            tag_updates={
                'title': 'Song M',
                'artist': 'Artist M',
                'album': 'Album M',
                'albumartist': 'Album Artist M',
                'date': '2023-06-07',
                'composer': 'Composer M',
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
        self.assertEqual(info_mp3['performer'], '')
        self.assertEqual(info_mp3['genre'], 'Jazz')
        self.assertEqual(info_mp3['comment'], 'Edition Note')
        id3 = MP3(mp3_path).tags
        self.assertEqual(str(id3['TCOM']), 'Composer M')
        self.assertEqual(str(id3['TCON']), 'Jazz')
        self.assertEqual(str(id3['TPOS']), '1')

        mp3_163 = self._make_minimal_mp3('01 - NeteaseProtected.mp3')
        tags_163 = ID3()
        tags_163.add(TIT2(encoding=3, text='Protected Track'))
        tags_163.add(TPE1(encoding=3, text='ArtistP'))
        tags_163.add(TALB(encoding=3, text='AlbumP'))
        tags_163.add(COMM(encoding=3, lang='eng', desc='', text=SAMPLE_163_KEY))
        tags_163.save(mp3_163, v2_version=3)

        ok_p, _ = apply_track_repairs(mp3_163, tag_updates={'comment': 'Overwrite Attempt', 'genre': 'Pop'})
        self.assertTrue(ok_p)
        info_p = read_audio_info(mp3_163)
        self.assertTrue(info_p['has_163_key'])
        self.assertEqual(info_p['genre'], 'Pop')
        self.assertIn("163 key(Don't modify):", info_p['raw_comment'])

        tr_p = next(t for t in scan_library(FoobarEnv(library_folders=[self.tmp])) if t.filename == '01 - NeteaseProtected.mp3')
        self.assertNotIn('comment', tr_p.missing_extra_fields)

    def test_delete_partial_metadata_flac_mp3_wav_and_163_protection(self):
        cov = self._make_jpeg_bytes(200, 200, (90, 60, 150))

        flac_path = self._make_minimal_flac('del_partial.flac')
        fl = FLAC(flac_path)
        fl['TITLE'] = ['KeepTitle']
        fl['ARTIST'] = ['KeepArtist']
        fl['ALBUMARTIST'] = ['KeepAA']
        fl['ALBUM'] = ['KeepAlbum']
        fl['DATE'] = ['2021-05-01']
        fl['PERFORMER'] = ['DelPerf']
        fl['DESCRIPTION'] = [SAMPLE_163_KEY]
        fl.save()
        ok_w, msg_w = apply_track_repairs(
            flac_path,
            tag_updates={'composer': 'DelComp', 'genre': 'Rock', 'comment': 'DelComment'},
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

        deleted2: list[str] = []
        ok_d2, msg_d2 = apply_track_deletions(flac_path, ['comment'], protect_163_key=True, out_deleted=deleted2)
        self.assertTrue(ok_d2)
        self.assertEqual(deleted2, [])
        self.assertIn('163 key', msg_d2)

        deleted3: list[str] = []
        ok_d3, msg_d3 = apply_track_deletions(flac_path, ['comment'], protect_163_key=False, out_deleted=deleted3)
        self.assertTrue(ok_d3, msg_d3)
        self.assertEqual(deleted3, ['comment'])
        self.assertFalse(read_audio_info(flac_path)['has_163_key'])

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
        id3.add(COMM(encoding=3, lang='eng', desc='', text=[SAMPLE_163_KEY]))
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
        self.assertEqual([f for f in os.listdir(self.tmp) if f.endswith('.musictmp')], [])

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

    def test_safe_filename_parsing_and_missing_title_artist_backfill(self):
        c1 = parse_filename_clues('01 - 周杰伦 - 晴天.flac')
        self.assertEqual(c1['inferred_artist'], '周杰伦')
        self.assertEqual(c1['inferred_title'], '晴天')

        c2 = parse_filename_clues('许嵩-放飞.flac')
        self.assertEqual(c2['inferred_artist'], '许嵩')
        self.assertEqual(c2['inferred_title'], '放飞')

        c3 = parse_filename_clues('03 - 1973.flac')
        self.assertEqual(c3['inferred_title'], '1973')

        c4 = parse_filename_clues('2002年的第一场雪.flac')
        self.assertEqual(c4['inferred_title'], '2002年的第一场雪')

        c5 = parse_filename_clues('05. 7 Years.flac')
        self.assertEqual(c5['inferred_title'], '7 Years')

        c6 = parse_filename_clues('Anti-Hero.flac')
        self.assertEqual(c6['inferred_title'], 'Anti-Hero')
        self.assertEqual(c6['inferred_artist'], '')

        c7 = parse_filename_clues('Jay Chou - 2002.flac')
        self.assertEqual(c7['inferred_artist'], 'Jay Chou')
        self.assertEqual(c7['inferred_title'], '2002')

        alb_dir = os.path.join(self.tmp, '叶惠美')
        os.makedirs(alb_dir, exist_ok=True)
        src = self._make_minimal_flac()
        fp = os.path.join(alb_dir, '02 - Jay Chou - 晴天.flac')
        shutil.move(src, fp)
        f = FLAC(fp)
        f['album'] = ['叶惠美']
        f.save()
        info = read_audio_info(fp)
        plan = analyze_folder_art_and_tags(alb_dir, [fp], {fp: info})
        tp = plan.track_plans[os.path.normcase(os.path.abspath(fp))]
        self.assertEqual(tp.tag_updates.get('title'), '晴天')
        self.assertEqual(tp.tag_updates.get('artist'), 'Jay Chou')

    def test_lyrics_merge_embed_lrc_modes_and_delete(self):
        orig = '[00:01.000]Hello world\n[00:05.50]Second line\n[00:08.00]Third'
        trans = '[00:01.00]你好世界\n[00:05.500]第二行\n[00:08.00]//'
        merged = merge_lyrics(orig, trans)
        self.assertIn('[00:01.000]Hello world\n[00:01.000]你好世界', merged)
        self.assertIn('[00:05.50]Second line\n[00:05.50]第二行', merged)
        self.assertNotIn('//', merged)

        flac_p = self._make_minimal_flac('song.flac')
        f = FLAC(flac_p)
        f['title'] = ['晴天']
        f['artist'] = ['周杰伦']
        f['album'] = ['叶惠美']
        f['comment'] = ['163 key(Don\'t modify):DUMMYKEY']
        f.save()
        ok, _ = apply_track_repairs(flac_p, lyrics='[00:01.00]故事的小黄花', lyrics_mode='auto')
        self.assertTrue(ok)
        info_flac = read_audio_info(flac_p)
        self.assertTrue(info_flac['has_embedded_lyrics'])
        self.assertFalse(info_flac['has_lrc_file'])
        self.assertTrue(info_flac['has_lyrics'])
        self.assertIn('故事的小黄花', info_flac['lyrics'])
        self.assertFalse(os.path.exists(get_lrc_path(flac_p)))

        wav_p = self._make_minimal_wav('wave_song.wav')
        wv = WAVE(wav_p)
        wv.add_tags()
        wv.tags.add(TIT2(encoding=3, text=['WaveSong']))
        wv.tags.add(TPE1(encoding=3, text=['ArtistW']))
        wv.tags.add(TALB(encoding=3, text=['AlbumW']))
        wv.save()
        ok_w, _ = apply_track_repairs(wav_p, lyrics='[00:02.00]Wave Lyric', lyrics_mode='auto')
        self.assertTrue(ok_w)
        info_w = read_audio_info(wav_p)
        self.assertTrue(info_w['has_lrc_file'])
        self.assertTrue(info_w['has_lyrics'])
        lrc_txt = read_lrc_file(wav_p)
        self.assertIn('[ti:WaveSong]', lrc_txt)
        self.assertIn('[ar:ArtistW]', lrc_txt)
        self.assertIn('[al:AlbumW]', lrc_txt)
        self.assertIn('[00:02.00]Wave Lyric', lrc_txt)

        mp3_p = self._make_minimal_mp3('dual.mp3')
        m = MP3(mp3_p)
        m.add_tags()
        m.tags.add(TIT2(encoding=3, text=['Dual']))
        m.save(v2_version=3)
        ok_m, _ = apply_track_repairs(mp3_p, lyrics='[00:03.00]Dual Lyric', lyrics_mode='both')
        self.assertTrue(ok_m)
        info_m = read_audio_info(mp3_p)
        self.assertTrue(info_m['has_embedded_lyrics'])
        self.assertTrue(info_m['has_lrc_file'])

        _, insp_del, _ = inspect_deletable_fields(mp3_p, {'lyrics'})
        self.assertIn('lyrics', insp_del)
        ok_dm, _ = apply_track_deletions(mp3_p, {'lyrics'}, delete_lrc=True)
        self.assertTrue(ok_dm)
        info_m2 = read_audio_info(mp3_p)
        self.assertFalse(info_m2['has_embedded_lyrics'])
        self.assertFalse(info_m2['has_lrc_file'])
        self.assertFalse(info_m2['has_lyrics'])

        ok_w_keep, _ = write_lrc_file(flac_p, '[00:01.00]External', title='晴天')
        self.assertTrue(ok_w_keep)
        ok_df, _ = apply_track_deletions(flac_p, {'lyrics'}, delete_lrc=False, protect_163_key=True)
        self.assertTrue(ok_df)
        info_f2 = read_audio_info(flac_p)
        self.assertFalse(info_f2['has_embedded_lyrics'])
        self.assertTrue(info_f2['has_lrc_file'])
        self.assertIn('163 key', info_f2['comment'])
        delete_lrc_file(flac_p)

    def test_sniff_and_roundtrip_dsf_tak_mpc_tta(self):
        dsf_path = self._make_minimal_dsf('sample.dsf')
        tta_path = self._make_minimal_tta('sample.tta')
        tak_path = self._make_minimal_tak('sample.tak')
        mpc_path = self._make_minimal_mpc('sample.mpc')

        self.assertEqual(sniff_container(dsf_path), 'dsf')
        self.assertEqual(sniff_container(tta_path), 'tta')
        self.assertEqual(sniff_container(tak_path), 'tak')
        self.assertEqual(sniff_container(mpc_path), 'mpc')

        # 写入与校验音轨号
        for fp, num in ((dsf_path, 2), (tta_path, 4), (tak_path, 6), (mpc_path, 8)):
            info_b = read_audio_info(fp)
            self.assertFalse(has_valid_tracknumber(info_b), fp)
            ok, msg = write_tracknumber(fp, num)
            self.assertTrue(ok, f'{fp}: {msg}')
            info_a = read_audio_info(fp)
            self.assertEqual(info_a['track_int'], num, fp)
            self.assertTrue(has_valid_tracknumber(info_a), fp)

        cov_bytes = self._make_jpeg_bytes(120, 120, (10, 50, 90))
        art_bytes = self._make_jpeg_bytes(100, 100, (90, 50, 10))

        # 1. DSF: ID3v2.3 标签、APIC 双封面与内嵌歌词
        ok_dsf, msg_dsf = apply_track_repairs(
            dsf_path,
            tag_updates={'title': 'DSF Track', 'artist': 'DSF Artist', 'album': 'DSF Album', 'composer': 'DSF Comp', 'lyricist': 'DSF Lyr', 'arranger': 'DSF Arr'},
            cover_bytes=cov_bytes,
            artist_bytes=art_bytes,
            lyrics='[00:01.00]DSF Lyrics Line 1\n[00:05.00]DSF Lyrics Line 2',
        )
        self.assertTrue(ok_dsf, msg_dsf)
        info_dsf = read_audio_info(dsf_path)
        self.assertEqual(info_dsf['title'], 'DSF Track')
        self.assertEqual(info_dsf['artist'], 'DSF Artist')
        self.assertEqual(info_dsf['album'], 'DSF Album')
        self.assertEqual(info_dsf['composer'], 'DSF Comp')
        self.assertEqual(info_dsf['lyricist'], 'DSF Lyr')
        self.assertEqual(info_dsf['arranger'], 'DSF Arr')
        self.assertTrue(info_dsf['has_embedded_lyrics'])
        self.assertGreaterEqual(len(info_dsf['pics']), 2)

        # 2. TTA: ID3v2 标签、APIC 封面与内嵌歌词
        ok_tta, msg_tta = apply_track_repairs(
            tta_path,
            tag_updates={'title': 'TTA Track', 'artist': 'TTA Artist', 'album': 'TTA Album'},
            cover_bytes=cov_bytes,
            lyrics='[00:01.00]TTA Lyrics Line',
        )
        self.assertTrue(ok_tta, msg_tta)
        info_tta = read_audio_info(tta_path)
        self.assertEqual(info_tta['title'], 'TTA Track')
        self.assertEqual(info_tta['artist'], 'TTA Artist')
        self.assertTrue(info_tta['has_embedded_lyrics'])
        self.assertGreaterEqual(len(info_tta['pics']), 1)

        # 3. TAK: APEv2 标签与歌词
        ok_tak, msg_tak = apply_track_repairs(
            tak_path,
            tag_updates={'title': 'TAK Track', 'artist': 'TAK Artist', 'album': 'TAK Album', 'genre': 'Rock'},
            lyrics='[00:02.00]TAK Lyrics Line',
            lyrics_mode='embed',
        )
        self.assertTrue(ok_tak, msg_tak)
        info_tak = read_audio_info(tak_path)
        self.assertEqual(info_tak['title'], 'TAK Track')
        self.assertEqual(info_tak['artist'], 'TAK Artist')
        self.assertEqual(info_tak['genre'], 'Rock')
        self.assertTrue(info_tak['has_embedded_lyrics'])

        # 4. MPC: APEv2 标签与歌词
        ok_mpc, msg_mpc = apply_track_repairs(
            mpc_path,
            tag_updates={'title': 'MPC Track', 'artist': 'MPC Artist', 'album': 'MPC Album'},
            lyrics='[00:03.00]MPC Lyrics Line',
            lyrics_mode='embed',
        )
        self.assertTrue(ok_mpc, msg_mpc)
        info_mpc = read_audio_info(mpc_path)
        self.assertEqual(info_mpc['title'], 'MPC Track')
        self.assertEqual(info_mpc['artist'], 'MPC Artist')
        self.assertTrue(info_mpc['has_embedded_lyrics'])

        # 局部字段删除测试
        ok_del, _ = apply_track_deletions(dsf_path, {'composer', 'lyricist', 'arranger'})
        self.assertTrue(ok_del)
        info_dsf2 = read_audio_info(dsf_path)
        self.assertEqual(info_dsf2['composer'], '')
        self.assertEqual(info_dsf2['lyricist'], '')
        self.assertEqual(info_dsf2['arranger'], '')
        self.assertEqual(info_dsf2['title'], 'DSF Track')

        ok_del_tak, _ = apply_track_deletions(tak_path, {'genre'})
        self.assertTrue(ok_del_tak)
        info_tak2 = read_audio_info(tak_path)
        self.assertEqual(info_tak2['genre'], '')
        self.assertEqual(info_tak2['title'], 'TAK Track')

    def test_id3_multiple_comments_dedup_and_foobar_sync(self):
        """测试 MP3 同时包含标准 COMM 与 ID3v1 Comment 时的注释去重与 foobar 同步判定。"""
        mp3_path = self._make_minimal_mp3('multi_comm.mp3')
        id3 = ID3()
        # 添加标准 ID3v2 注释和 ID3v1 兼容注释副本
        id3.add(COMM(encoding=0, lang='eng', desc='ID3v1 Comment', text=['EAC FLAC -8']))
        id3.add(COMM(encoding=1, lang='eng', desc='', text=['EAC FLAC -8']))
        id3.save(mp3_path)

        info = read_audio_info(mp3_path)
        # 应该只返回单份去重后的注释，而不是逗号拼接的双份 'EAC FLAC -8, EAC FLAC -8'
        self.assertEqual(info['comment'], 'EAC FLAC -8')

        st = os.stat(mp3_path)
        # 模拟 foobar metadb 中记录的 comment 单值
        fb_entry = {
            'tracknumber': '',
            'size': st.st_size,
            'mtime': st.st_mtime,
            'title': '',
            'album': '',
            'artist': '',
            'albumartist': '',
            'date': '',
            'composer': '',
            'performer': '',
            'genre': '',
            'discnumber': '',
            'comment': 'EAC FLAC -8',
        }
        diffs = find_foobar_cache_diffs(info, fb_entry, st.st_size, st.st_mtime, True, True)
        self.assertNotIn('注释未同步', diffs)

    def test_dff_sniff_and_read_only_protection(self):
        dff_path = self._make_minimal_dff('sample.dff')
        self.assertEqual(sniff_container(dff_path), 'dff')

        info = read_audio_info(dff_path)
        self.assertEqual(info['container'], 'dff')
        self.assertGreater(info['duration'], 0.0)
        self.assertIsNone(info['error'])

        # 测试在 DFF 实体文件上内嵌写标被安全拦截
        ok, msg = write_tracknumber(dff_path, 1)
        self.assertFalse(ok)
        self.assertIn('DFF 格式遵循发烧规范保护', msg)

        # 测试外挂歌词正常生效
        ok_lrc, _ = write_lrc_file(dff_path, '[00:01.00]DFF External Lyric Line', title='DFF Title')
        self.assertTrue(ok_lrc)
        info2 = read_audio_info(dff_path)
        self.assertTrue(info2['has_lrc_file'])
        self.assertFalse(info2['has_embedded_lyrics'])

        # 测试通过 apply_track_repairs 补全 DFF 歌词（模式为 both 时平滑写入外置 .lrc，不触发内嵌写标拦截）
        dff_p2 = self._make_minimal_dff('song_both.dff')
        ok_both, msg_both = apply_track_repairs(
            dff_p2,
            lyrics='[00:02.00]Both Lyric Line',
            lyrics_mode='both',
            lrc_title='Both Title',
            lrc_artist='Both Artist',
        )
        self.assertTrue(ok_both)
        self.assertIn('.lrc', msg_both)
        info_both = read_audio_info(dff_p2)
        self.assertTrue(info_both['has_lrc_file'])
        self.assertFalse(info_both['has_embedded_lyrics'])
        self.assertTrue(info_both['has_lyrics'])
        lrc_both_content = read_lrc_file(dff_p2)
        self.assertIn('[ti:Both Title]', lrc_both_content)
        self.assertIn('[ar:Both Artist]', lrc_both_content)
        self.assertIn('[00:02.00]Both Lyric Line', lrc_both_content)

        # 测试模式为 embed 时平滑降级写入外置 .lrc
        dff_p3 = self._make_minimal_dff('song_embed.dff')
        ok_emb, _ = apply_track_repairs(dff_p3, lyrics='[00:03.00]Embed Fallback', lyrics_mode='embed')
        self.assertTrue(ok_emb)
        info_emb = read_audio_info(dff_p3)
        self.assertTrue(info_emb['has_lrc_file'])
        self.assertFalse(info_emb['has_embedded_lyrics'])

        # 测试同时传入不可写标签与歌词时，外置 .lrc 依然能被成功写入并给出保护提示
        dff_p4 = self._make_minimal_dff('song_mixed.dff')
        ok_mix, msg_mix = apply_track_repairs(
            dff_p4,
            tag_updates={'artist': '邓丽君'},
            lyrics='[00:04.00]Mixed Lyric',
            lyrics_mode='both',
        )
        self.assertTrue(ok_mix)
        self.assertIn('跳过', msg_mix)
        info_mix = read_audio_info(dff_p4)
        self.assertTrue(info_mix['has_lrc_file'])

        # 测试在无歌词且仅传入标签时依然严格拦截
        dff_p5 = self._make_minimal_dff('song_tag_only.dff')
        ok_tag, msg_tag = apply_track_repairs(dff_p5, tag_updates={'artist': '邓丽君'})
        self.assertFalse(ok_tag)
        self.assertIn('DFF 格式遵循发烧规范保护', msg_tag)

    def test_dff_to_dsf_lossless_remux_and_convert(self):
        dff_path = self._make_minimal_dff('song.dff')
        from mutagen.dsdiff import DSDIFF
        dff = DSDIFF(dff_path)
        dff.add_tags()
        dff.tags.add(TIT2(encoding=3, text=['DFF Original Title']))
        dff.tags.add(TPE1(encoding=3, text=['DFF Original Artist']))
        dff.save()

        # 生成同名外置歌词
        write_lrc_file(dff_path, '[00:01.00]DFF Lyric', title='DFF Original Title')

        # 执行 DFF ➜ DSF 极速无损转封装
        ok, msg, dsf_path = convert_audio_file(dff_path, 'dsf', overwrite=True, copy_sidecar_lrc=True)
        self.assertTrue(ok, msg)
        self.assertTrue(os.path.isfile(dsf_path))

        # 校验生成的 DSF 信息与继承的元数据
        info_dsf = read_audio_info(dsf_path)
        self.assertEqual(info_dsf['container'], 'dsf')
        self.assertEqual(info_dsf['title'], 'DFF Original Title')
        self.assertEqual(info_dsf['artist'], 'DFF Original Artist')
        self.assertTrue(info_dsf['has_lrc_file'])
        self.assertGreater(info_dsf['duration'], 0.0)

        # 校验转为 DSF 后解锁完整内嵌能力
        ok_tn, _ = write_tracknumber(dsf_path, 5)
        self.assertTrue(ok_tn)
        cov_bytes = self._make_jpeg_bytes(100, 100)
        ok_art, _ = apply_track_repairs(dsf_path, cover_bytes=cov_bytes)
        self.assertTrue(ok_art)

        info_dsf_after = read_audio_info(dsf_path)
        self.assertEqual(info_dsf_after['track_int'], 5)
        self.assertGreaterEqual(len(info_dsf_after['pics']), 1)

    def test_mp4_lyricist_and_arranger_freeform_atoms(self):
        """测试 MP4/M4A 容器下 lyricist 与 arranger 自由原子标签的安全写入与删除（避免 str 拼接 bytes 异常）。"""
        from mutagen.mp4 import MP4Tags
        from services.art.writer import apply_tag_field, delete_tag_field_from_audio

        class MockMP4Audio:
            def __init__(self):
                self.tags = MP4Tags()

        aud = MockMP4Audio()
        self.assertTrue(apply_tag_field(aud, 'mp4', 'lyricist', '郑容和, 林俊杰'))
        self.assertTrue(apply_tag_field(aud, 'mp4', 'arranger', '郑容和, 李尚浩'))
        self.assertEqual(aud.tags.get('----:com.apple.iTunes:LYRICIST'), ['郑容和, 林俊杰'.encode('utf-8')])
        self.assertEqual(aud.tags.get('----:com.apple.iTunes:ARRANGER'), ['郑容和, 李尚浩'.encode('utf-8')])

        self.assertTrue(delete_tag_field_from_audio(aud, 'mp4', 'lyricist'))
        self.assertTrue(delete_tag_field_from_audio(aud, 'mp4', 'arranger'))
        self.assertNotIn('----:com.apple.iTunes:LYRICIST', aud.tags)
        self.assertNotIn('----:com.apple.iTunes:ARRANGER', aud.tags)


if __name__ == '__main__':
    unittest.main()
