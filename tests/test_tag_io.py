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
from infra.integrations.foobar import scan_library
from services.art import (
    analyze_folder_art_and_tags,
    apply_track_deletions,
    apply_track_repairs,
    inspect_deletable_fields,
    read_pictures,
)
from tag_io import (
    bump_mtime,
    decode_163_key,
    delete_lrc_file,
    get_lrc_path,
    has_valid_tracknumber,
    merge_lyrics,
    parse_filename_clues,
    read_audio_info,
    read_lrc_file,
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


if __name__ == '__main__':
    unittest.main()
