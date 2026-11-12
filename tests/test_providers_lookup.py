"""单元测试：在线音源 provider 搜索、专辑解析、跨平台聚合、负缓存与熔断器。"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pickle
import threading
import urllib.parse
from unittest.mock import patch

from PIL import Image

from app import SourceConfigDialog
from cli import build_arg_parser
from foobar_library import STATUS_OK, LibraryTrack
from providers import (
    TrackMatch,
    baidu_provider,
    deezer_provider,
    get_netease_api_base,
    get_spotify_credentials,
    has_spotify_credentials,
    itunes_provider,
    kugou_provider,
    lrclib_provider,
    mb_provider,
    netease_provider,
    parse_source_list,
    qq_provider,
    resolve_provider_order,
    set_netease_api_base,
    set_spotify_credentials,
    spotify_provider,
    ytmusic_provider,
)
from providers.baidu import _QIANQIAN_SECRET, sign_qianqian_params
from providers.base import (
    get_album_meta,
    match_in_tracks,
    normalize_release_date,
    parse_lyric_credits,
    set_album_meta,
)
from providers.musicbrainz import MB_FAIL_THRESHOLD, is_mb_circuit_open
from providers.netease import build_netease_tracks
from tests.common import FakeHTTPResponse, TempTrackTestCase, offscreen_tk
from track_lookup import (
    _merge_match,
    _parse_filename_clues,
    clear_caches,
    fetch_cover_bytes,
    resolve_release_date,
    resolve_track_metadata,
    resolve_track_number,
)


def _json_resp(payload) -> FakeHTTPResponse:
    return FakeHTTPResponse(json.dumps(payload))


class ProvidersLookupTests(TempTrackTestCase):
    def test_build_netease_tracks_skips_full_album_and_keeps_multidisc(self):
        songs = [
            {'id': 100, 'name': '冀西南林路行 (专辑全长)', 'dt': 2662000, 'no': 1, 'cd': '01'},
            {'id': 101, 'name': '早', 'dt': 83650, 'no': 1, 'cd': '01'},
            {'id': 102, 'name': '泥河', 'dt': 348318, 'no': 2, 'cd': '01'},
            {'id': 103, 'name': '第三首', 'dt': 200000, 'no': 1, 'cd': '02'},
        ]
        built = build_netease_tracks(songs)
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
        m1 = match_in_tracks(tracks, '水边的阿狄丽娜 (Live)', 185.0)
        self.assertEqual(m1['number'], 12)
        m2 = match_in_tracks(tracks, '同一首歌', 241.0)
        self.assertEqual(m2['number'], 9)
        # 无法区分时长时不瞎猜
        m3 = match_in_tracks(tracks, '同一首歌', 0.0)
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

        def fake_urlopen(req, timeout=12):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'fcg_play_single_song' in url:
                return _json_resp({
                    'data': [{
                        'album': {'mid': '002esRFk3AIcMc', 'name': '理查德·克莱德曼钢琴曲-命运'},
                        'index_album': 12,
                    }]
                })
            if 'fcg_v8_album_info_cp' in url:
                return _json_resp({
                    'data': {
                        'list': [
                            {'songmid': '00000000000001', 'songname': 'Track 1', 'interval': 120},
                            {'songmid': '003jpWS23C3yuL', 'songname': 'Blue Rondo Ala Turque', 'interval': 190},
                        ]
                    }
                })
            return _json_resp({})

        with patch('track_lookup.urllib.request.urlopen', side_effect=fake_urlopen):
            res = resolve_track_number(tr2)
        self.assertEqual(res.tracknumber, 2)
        self.assertIn('QQ音乐', res.source)

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
        def fake_urlopen(req, timeout=12):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'music.163.com/api/search/get' in url and 'type=10' in url:
                return _json_resp({
                    'result': {'albums': [{'id': 9001, 'name': '七里香', 'artist': {'name': '音屿'}, 'publishTime': 1781884800000}]}
                })
            if 'music.163.com/api/v1/album/9001' in url:
                return _json_resp({
                    'album': {'publishTime': 1781884800000},
                    'songs': [{'id': 1, 'name': '七里香', 'dt': 198000, 'no': 1, 'cd': '01'}],
                })
            if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in url and 't=8' in url:
                return _json_resp({
                    'data': {'album': {'list': [{'albumMID': '003DFRzD192KKD', 'albumName': '七里香', 'singerName': '周杰伦', 'publicTime': '2004-08-03'}]}}
                })
            if 'fcg_v8_album_info_cp' in url:
                return _json_resp({
                    'data': {
                        'aDate': '2004-08-03',
                        'list': [
                            {'songmid': '001', 'songname': '我的地盘', 'interval': 240, 'cdIdx': 0},
                            {'songmid': '002', 'songname': '七里香', 'interval': 299, 'cdIdx': 0},
                        ],
                    }
                })
            if 'itunes.apple.com/search' in url and 'entity=album' in url:
                return _json_resp({'results': [{'collectionId': 777, 'collectionName': 'OnlyItunes', 'artistName': 'ArtistIT', 'releaseDate': '2018-09-14T07:00:00Z'}]})
            if 'itunes.apple.com/lookup' in url:
                return _json_resp({'results': [
                    {'wrapperType': 'collection', 'collectionId': 777, 'releaseDate': '2018-09-14T07:00:00Z'},
                    {'wrapperType': 'track', 'trackId': 11, 'trackName': 'TrackIT', 'trackTimeMillis': 200000, 'discNumber': 1, 'trackNumber': 1, 'releaseDate': '2018-09-14T07:00:00Z'},
                ]})
            if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in url and 't=0' in url:
                return _json_resp({
                    'data': {'song': {'list': [{'songmid': '0099', 'songname': '孤勇', 'singer': [{'name': '歌手A'}], 'albummid': '', 'albumname': '单曲', 'interval': 210, 'pubtime': 1640966400}]}}
                })
            return _json_resp({})

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

    def test_resolve_track_metadata_cross_platform_merge_and_cover_download(self):
        # 验证歌词作曲解析（不再提取表演者）
        comp = parse_lyric_credits(
            '[00:00.00] 作词 : Wordsmith\n'
            '[00:01.00] 作曲 : MelodyA / MelodyB\n'
            '[00:02.00] 演唱 : SingerX\n'
            '[00:03.00] 吉他 : GuitaristY\n'
        )
        self.assertEqual(comp, 'MelodyA, MelodyB')

        # 验证 _merge_match 仅补空白不覆盖已有值，且 TrackMatch 不包含 comment / performer 字段
        self.assertFalse(hasattr(TrackMatch(None), 'comment'))
        self.assertFalse(hasattr(TrackMatch(None), 'performer'))
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

        # 验证跨平台联网聚合：网易云提供曲号/日期/专辑艺术家/封面/歌词作曲，QQ 补充流派/碟片号（不获取注释与表演者）
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
            set_album_meta('netease', aid, release_date='2022-08-01', albumartist='SingerM', cover_url='https://example.com/ne.jpg')
            return [
                {'id': '990', 'name': 'Intro', 'artist': 'SingerM', 'number': 1, 'disc': 1, 'duration': 180.0},
                {'id': '991', 'name': 'SongMerge', 'artist': 'SingerM', 'number': 2, 'disc': None, 'duration': 210.0},
            ]

        def fake_qq_album(amid: str):
            set_album_meta('qq', amid, release_date='2022-08-01', albumartist='SingerM', genre='Indie Pop', cover_url='https://example.com/qq.jpg')
            return [
                {'id': 'qqmid0', 'name': 'Intro', 'artist': 'SingerM', 'number': 1, 'disc': 1, 'duration': 180.0},
                {'id': 'qqmid1', 'name': 'SongMerge', 'artist': 'SingerM', 'number': 2, 'disc': 1, 'duration': 210.0},
            ]

        with patch.object(netease_provider, 'search_album', return_value=('1001', 'AlbumM', True)), \
             patch.object(netease_provider, 'get_album_tracks', side_effect=fake_ne_album), \
             patch.object(netease_provider, 'get_song_extra', return_value={'composer': 'CompNe'}), \
             patch.object(qq_provider, 'search_album', return_value=('qqalb1', 'AlbumM', True)), \
             patch.object(qq_provider, 'get_album_tracks', side_effect=fake_qq_album), \
             patch.object(qq_provider, 'get_song_extra', return_value={'genre': 'Indie Pop', 'composer': ''}), \
             patch.object(itunes_provider, 'search_album', return_value=('', '', False)), \
             patch.object(mb_provider, 'search_album', return_value=('', '', False)), \
             patch('track_lookup._search_song_and_resolve_album', return_value=None):
            res = resolve_track_metadata(tr, need_track=True, need_date=True, need_cover=True, need_core=True, need_extra=True)

        self.assertEqual(res.tracknumber, 2)
        self.assertEqual(res.discnumber, 1)
        self.assertEqual(res.release_date, '2022-08-01')
        self.assertEqual(res.albumartist, 'SingerM')
        self.assertEqual(res.composer, 'CompNe')
        self.assertEqual(res.genre, 'Indie Pop')
        self.assertEqual(res.cover_url, 'https://example.com/ne.jpg')
        self.assertEqual(get_album_meta('netease', '1001')['cover_url'], 'https://example.com/ne.jpg')

        # 验证 fetch_cover_bytes 将 PNG 规范为 JPEG 并缓存
        png_buf = io.BytesIO()
        Image.new('RGB', (400, 400), (20, 140, 220)).save(png_buf, format='PNG')
        png_bytes = png_buf.getvalue()

        with patch('track_lookup.urllib.request.urlopen', return_value=FakeHTTPResponse(png_bytes)) as m_open:
            cov1 = fetch_cover_bytes('https://example.com/cover.png')
            cov2 = fetch_cover_bytes('https://example.com/cover.png')
        self.assertIsNotNone(cov1)
        self.assertTrue(cov1.startswith(b'\xff\xd8'))
        self.assertEqual(cov1, cov2)
        self.assertEqual(m_open.call_count, 1)

    def test_negative_search_cache_platform_filter_and_mb_circuit_breaker(self):
        calls: list[str] = []
        lock = threading.Lock()

        def fake_urlopen(req, timeout=8):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            with lock:
                calls.append(url)
            if 'music.163.com/api/search/get' in url and 'type=10' in url:
                return _json_resp({'result': {'albums': [{'id': 7001, 'name': 'OnlyNeteaseAlb', 'artist': {'name': 'SingerZ'}}]}})
            if 'music.163.com/api/v1/album/7001' in url:
                return _json_resp({'album': {'publishTime': 1608566400000}, 'songs': [
                    {'id': 801, 'name': 'SongOne', 'dt': 180000, 'no': 1, 'cd': '01'},
                    {'id': 802, 'name': 'SongTwo', 'dt': 200000, 'no': 2, 'cd': '01'},
                ]})
            if 'music.163.com/api/song/lyric' in url:
                return _json_resp({'lrc': {'lyric': '[00:00.00] 演唱 : SingerZ\n'}})
            return _json_resp({})

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

        self.assertEqual(r1.composer, '')
        self.assertEqual(r2.composer, '')
        # 仅缺失 composer 时，不应请求 iTunes；且 QQ/MB 专辑搜索未命中结果应被负缓存，第二首不再重复查询
        self.assertFalse(any('itunes.apple.com' in u for u in calls))
        self.assertEqual(sum(1 for u in calls if 'c.y.qq.com/soso/fcgi-bin/client_search_cp' in u and 't=8' in u), 2)
        self.assertEqual(sum(1 for u in calls if 'musicbrainz.org/ws/2/release/' in u), 1)

        # 当网易云歌词已提供 composer 时，不再因缺少 performer 而继续请求 QQ/iTunes/MusicBrainz
        calls.clear()
        clear_caches()

        def fake_urlopen_with_comp(req, timeout=8):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            if 'music.163.com/api/song/lyric' in url:
                return _json_resp({'lrc': {'lyric': '[00:00.00] 作曲 : CompZ\n'}})
            return fake_urlopen(req, timeout=timeout)

        with patch('track_lookup.urllib.request.urlopen', side_effect=fake_urlopen_with_comp), \
             patch('track_lookup.time.sleep', return_value=None):
            r_comp = resolve_track_metadata(t1, need_track=False, need_date=False, need_cover=False, need_core=False, need_extra=True)
        self.assertEqual(r_comp.composer, 'CompZ')
        self.assertFalse(any('qq.com' in u or 'itunes.apple.com' in u or 'musicbrainz.org' in u for u in calls))

        # 未匹配任何平台时，不应再把全部平台重跑第二遍
        calls.clear()
        clear_caches()
        t_miss = LibraryTrack(path='miss.flac', filename='Unknown.flac', folder=self.tmp, title='Unknown', artist='Nobody', album='NoSuchAlbum')
        with patch('track_lookup.urllib.request.urlopen', side_effect=lambda req, timeout=8: (calls.append(req.full_url) or _json_resp({}))), \
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
            return _json_resp({})

        with patch('track_lookup.urllib.request.urlopen', side_effect=fail_mb), \
             patch('track_lookup.time.sleep', return_value=None):
            for idx in range(8):
                mb_provider.search_album(f'Alb{idx}', f'Alb{idx}', f'Art{idx}')
        self.assertEqual(mb_calls, MB_FAIL_THRESHOLD)
        self.assertTrue(is_mb_circuit_open())

    def test_new_providers_qq_musicu_migu_baidu_kugou_netease_api_and_source_order(self):
        # 验证音源列表解析与排序逻辑
        parsed, invalid = parse_source_list('kugou, 咪咕, qq, unknown_src')
        self.assertEqual(parsed, ('kugou', 'migu', 'qq'))
        self.assertEqual(invalid, ['unknown_src'])
        prim, ext = resolve_provider_order(prefer_sources=('kugou', 'netease'), skip_sources=('mb',))
        self.assertEqual(prim[0], 'kugou')
        self.assertEqual(prim[1], 'netease')
        self.assertNotIn('mb', prim + ext)
        only_p, only_e = resolve_provider_order(only_sources=('migu', 'baidu'), skip_sources=('baidu',))
        self.assertEqual(only_p, ('migu',))
        self.assertEqual(only_e, ())

        # 验证千千音乐 MD5 签名
        signed = baidu_provider.search_songs  # confirm attr exists
        self.assertIsNotNone(signed)
        sp = sign_qianqian_params({'word': '晴天', 'appid': '16073360'}, timestamp='1700000000')
        exp_raw = 'appid=16073360&timestamp=1700000000&word=晴天' + _QIANQIAN_SECRET
        self.assertEqual(sp['sign'], hashlib.md5(exp_raw.encode('utf-8')).hexdigest())

        seen_urls: list[str] = []

        def _fake_resp(body):
            return FakeHTTPResponse(body if isinstance(body, str) else json.dumps(body))

        qq_orig_b64 = base64.b64encode('[00:01.00]Hello\n[00:02.00]World'.encode('utf-8')).decode('ascii')
        qq_trans_b64 = base64.b64encode('[00:01.00]你好\n[00:02.00]世界'.encode('utf-8')).decode('ascii')
        kg_b64 = base64.b64encode('[00:01.00]酷狗歌词\n作词：方文山'.encode('utf-8')).decode('ascii')

        def _fake_urlopen(req, timeout=15):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            seen_urls.append(url)
            if 'u.y.qq.com/cgi-bin/musicu.fcg' in url:
                return _fake_resp({
                    'music.search.SearchCgiService.DoSearchForQQMusicDesktop': {
                        'data': {
                            'body': {
                                'song': {
                                    'list': [{
                                        'mid': 'qq_mid_1',
                                        'title': 'SongQQ',
                                        'name': 'SongQQ',
                                        'singer': [{'name': 'SingerQQ'}],
                                        'album': {'mid': 'qq_alb_1', 'title': 'AlbumQQ', 'name': 'AlbumQQ'},
                                        'interval': 200,
                                        'time_public': '2021-05-20',
                                    }],
                                },
                            },
                        },
                    },
                })
            if 'client_search_cp' in url:
                # 故意返回空列表以触发 Desktop musicu.fcg 兜底
                return _fake_resp({'data': {'song': {'list': []}}})
            if 'fcg_v8_album_info_cp.fcg' in url:
                return _fake_resp({
                    'data': {
                        'aDate': '2021-05-20',
                        'list': [{'songmid': 'qq_mid_1', 'songname': 'SongQQ', 'interval': 200, 'cdIdx': 0}],
                    },
                })
            if 'fcg_query_lyric_new.fcg' in url:
                return _fake_resp({'lyric': qq_orig_b64, 'trans': qq_trans_b64})
            if 'fcg_play_single_song.fcg' in url:
                return _fake_resp({'data': [{'time_public': '2021-05-20'}]})
            if 'pd.musicapp.migu.cn/MIGUM2.0/v1.0/content/search_all.do' in url:
                return _fake_resp({
                    'songResultData': {
                        'result': [{
                            'id': 'mg1',
                            'contentId': 'mg1',
                            'name': 'MiguSong',
                            'singers': [{'name': 'MiguArtist'}],
                            'albums': [{'name': 'MiguAlbum'}],
                            'imgItems': [{'img': '//cdn.migu.cn/cover_s.jpg'}, {'img': '//cdn.migu.cn/cover_l.jpg'}],
                            'lyricUrl': 'http://migu.test/mg1.lrc',
                        }],
                    },
                })
            if url == 'http://migu.test/mg1.lrc':
                return _fake_resp('[00:01.00]咪咕歌词内容\n作曲：咪咕音乐人')
            if 'music.91q.com/v1/search' in url:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                self.assertIn('sign', qs)
                self.assertEqual(qs.get('appid'), ['16073360'])
                return _fake_resp({
                    'data': {
                        'typeTrack': [{
                            'TSID': 'bd_1',
                            'title': 'BaiduSong',
                            'artist': [{'name': 'BaiduArtist'}],
                            'albumTitle': 'BaiduAlbum',
                            'pic': 'https://baidu.test/cov.jpg',
                            'lyric': 'https://baidu.test/bd_1.lrc',
                        }],
                    },
                })
            if url == 'https://baidu.test/bd_1.lrc':
                return _fake_resp('[00:01.00]千千歌词内容')
            if 'mobilecdn.kugou.com/api/v3/search/song' in url:
                return _fake_resp({
                    'status': 1,
                    'data': {
                        'info': [{
                            'hash': 'KG_HASH_1',
                            'songname': 'KugouSong',
                            'singername': 'KugouArtist',
                            'album_name': 'KugouAlbum',
                            'album_id': '',
                            'duration': 210,
                            'trans_param': {'union_cover': 'https://kugou.test/{size}/cov.jpg'},
                        }],
                    },
                })
            if 'lyrics.kugou.com/search' in url:
                return _fake_resp({'candidates': [{'id': '123', 'accesskey': 'abc'}]})
            if 'lyrics.kugou.com/download' in url:
                return _fake_resp({'content': kg_b64})
            if 'http://127.0.0.1:3000/search' in url:
                return _fake_resp({
                    'result': {
                        'songs': [{
                            'id': 888,
                            'name': 'CustomNeSong',
                            'duration': 240000,
                            'artists': [{'name': 'CustomNeArtist'}],
                            'album': {'id': 999, 'name': 'CustomNeAlbum', 'publishTime': 1704067200000, 'picUrl': 'https://ne.test/cov.jpg'},
                        }],
                    },
                })
            if 'http://127.0.0.1:3000/album' in url:
                return _fake_resp({
                    'album': {'id': 999, 'name': 'CustomNeAlbum', 'publishTime': 1704067200000, 'artist': {'name': 'CustomNeArtist'}},
                    'songs': [{'id': 888, 'name': 'CustomNeSong', 'dt': 240000, 'no': 1, 'cd': '1', 'ar': [{'name': 'CustomNeArtist'}]}],
                })
            if 'http://127.0.0.1:3000/lyric' in url:
                return _fake_resp({'lrc': {'lyric': '[00:01.00]自定义网易云歌词'}, 'tlyric': {'lyric': '[00:01.00]Custom NetEase Lyric'}})
            if 'http://127.0.0.1:3000/song/detail' in url:
                return _fake_resp({'songs': [{'id': 888, 'ar': [{'name': 'CustomNeArtist'}], 'al': {'picUrl': 'https://ne.test/cov.jpg'}}]})
            raise AssertionError(f'Unexpected URL in test: {url}')

        try:
            with patch('track_lookup.urllib.request.urlopen', side_effect=_fake_urlopen), \
                 patch('track_lookup.time.sleep'):
                # QQ client_search_cp 空列表 -> Desktop musicu.fcg 兜底 + 双语 Base64 歌词合并
                tr_qq = LibraryTrack(
                    path=os.path.join(self.tmp, 'SongQQ.flac'), folder=self.tmp, filename='SongQQ.flac',
                    container='flac', title='SongQQ', artist='SingerQQ', album='AlbumQQ',
                    duration=200.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_qq = resolve_track_metadata(tr_qq, need_track=True, need_date=False, need_lyrics=True, need_fields={'tracknumber', 'lyrics'}, only_sources=('qq',))
                self.assertEqual(res_qq.tracknumber, 1)
                self.assertIn('[00:01.00]Hello\n[00:01.00]你好', res_qq.lyrics)

                # MiGu 单曲搜索 + 歌词 + // cover URL 规范化
                tr_mg = LibraryTrack(
                    path=os.path.join(self.tmp, 'MiguSong.flac'), folder=self.tmp, filename='MiguSong.flac',
                    container='flac', title='MiguSong', artist='MiguArtist', album='',
                    duration=200.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_mg = resolve_track_metadata(tr_mg, need_track=False, need_date=False, need_cover=True, need_core=True, need_lyrics=True, need_fields={'cover', 'album', 'lyrics'}, only_sources=('migu',))
                self.assertEqual(res_mg.album_name, 'MiguAlbum')
                self.assertEqual(res_mg.cover_url, 'https://cdn.migu.cn/cover_l.jpg')
                self.assertIn('咪咕歌词内容', res_mg.lyrics)

                # Baidu/QianQian MD5 签名搜索 + 歌词
                tr_bd = LibraryTrack(
                    path=os.path.join(self.tmp, 'BaiduSong.flac'), folder=self.tmp, filename='BaiduSong.flac',
                    container='flac', title='BaiduSong', artist='BaiduArtist', album='',
                    duration=200.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_bd = resolve_track_metadata(tr_bd, need_track=False, need_date=False, need_cover=True, need_core=True, need_lyrics=True, need_fields={'cover', 'album', 'lyrics'}, only_sources=('baidu',))
                self.assertEqual(res_bd.album_name, 'BaiduAlbum')
                self.assertIn('千千歌词内容', res_bd.lyrics)

                # Kugou 在线搜索 + Base64 歌词
                kg_hits = kugou_provider.search_songs('KugouSong KugouArtist')
                self.assertEqual(len(kg_hits), 1)
                self.assertEqual(kg_hits[0]['hash'], 'KG_HASH_1')
                tr_kg = LibraryTrack(
                    path=os.path.join(self.tmp, 'KugouSong.flac'), folder=self.tmp, filename='KugouSong.flac',
                    container='flac', title='KugouSong', artist='KugouArtist', album='',
                    duration=210.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_kg = resolve_track_metadata(tr_kg, need_track=False, need_date=False, need_cover=True, need_core=True, need_lyrics=True, need_fields={'cover', 'album', 'lyrics'}, only_sources=('kugou',))
                self.assertEqual(res_kg.cover_url, 'https://kugou.test/400/cov.jpg')
                self.assertIn('酷狗歌词', res_kg.lyrics)

                # 自定义网易云 API 地址
                set_netease_api_base('http://127.0.0.1:3000/')
                self.assertEqual(get_netease_api_base(), 'http://127.0.0.1:3000')
                tr_ne = LibraryTrack(
                    path=os.path.join(self.tmp, 'CustomNeSong.flac'), folder=self.tmp, filename='CustomNeSong.flac',
                    container='flac', title='CustomNeSong', artist='CustomNeArtist', album='CustomNeAlbum',
                    duration=240.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_ne = resolve_track_metadata(tr_ne, need_track=True, need_date=False, need_lyrics=True, need_fields={'tracknumber', 'lyrics'}, only_sources=('netease',))
                self.assertEqual(res_ne.tracknumber, 1)
                self.assertIn('自定义网易云歌词', res_ne.lyrics)
                self.assertIn('Custom NetEase Lyric', res_ne.lyrics)
                self.assertTrue(any('http://127.0.0.1:3000/' in u for u in seen_urls))

                # 验证 SourceConfigDialog 弹窗交互
                with offscreen_tk() as root:
                    dlg = None
                    try:
                        dlg = SourceConfigDialog(
                            root,
                            prefer_sources=('netease',),
                            only_sources=(),
                            skip_sources=('mb',),
                            netease_api='http://127.0.0.1:3000',
                        )
                        dlg.var_prefer.set('kugou, qq')
                        dlg.var_skip.set('mb, baidu')
                        with patch('app.messagebox.showinfo') as m_info:
                            dlg._on_confirm()
                            m_info.assert_not_called()
                        self.assertEqual(dlg.result['prefer_sources'], ('kugou', 'qq'))
                        self.assertEqual(dlg.result['skip_sources'], ('mb', 'baidu'))
                        self.assertEqual(dlg.result['netease_api'], 'http://127.0.0.1:3000')
                    finally:
                        dlg = None
        finally:
            set_netease_api_base('')
            clear_caches()

    def test_intl_providers_ytmusic_deezer_lrclib_and_spotify_api(self):
        seen_requests: list[tuple[str, str]] = []

        def _fake_resp(body):
            return FakeHTTPResponse(body if isinstance(body, str) else json.dumps(body))

        def _fake_urlopen(req, timeout=15):
            url = req.full_url if hasattr(req, 'full_url') else str(req)
            body = req.data.decode('utf-8', errors='replace') if getattr(req, 'data', None) else ''
            seen_requests.append((url, body))

            # Deezer
            if 'api.deezer.com/search/album' in url:
                return _fake_resp({
                    'data': [{
                        'id': 302127,
                        'title': 'Discovery',
                        'artist': {'name': 'Daft Punk'},
                        'cover_xl': 'https://dz.test/discovery_xl.jpg',
                    }],
                })
            if 'api.deezer.com/album/302127' in url:
                return _fake_resp({
                    'id': 302127,
                    'title': 'Discovery',
                    'release_date': '2001-03-12',
                    'artist': {'name': 'Daft Punk'},
                    'genres': {'data': [{'name': 'Electronic'}]},
                    'cover_xl': 'https://dz.test/discovery_xl.jpg',
                    'nb_tracks': 2,
                    'tracks': {
                        'data': [
                            {'id': 3135553, 'title': 'One More Time', 'duration': 320, 'disk_number': 1, 'track_position': 1, 'artist': {'name': 'Daft Punk'}},
                            {'id': 3135554, 'title': 'Aerodynamic', 'duration': 207, 'disk_number': 1, 'track_position': 2, 'artist': {'name': 'Daft Punk'}},
                        ],
                    },
                })
            if 'api.deezer.com/search/track' in url:
                return _fake_resp({
                    'data': [{
                        'id': 3135553,
                        'title': 'One More Time',
                        'duration': 320,
                        'artist': {'name': 'Daft Punk'},
                        'album': {'id': 302127, 'title': 'Discovery', 'cover_xl': 'https://dz.test/discovery_xl.jpg'},
                    }],
                })

            # LRCLIB (1 首纯音乐 + 1 首纯文本 + 1 首同步 LRC，应优先同步 LRC)
            if 'lrclib.net/api/search' in url:
                return _fake_resp([
                    {'id': 1, 'trackName': 'One More Time', 'artistName': 'Daft Punk', 'albumName': 'Discovery', 'duration': 320, 'instrumental': True, 'plainLyrics': '', 'syncedLyrics': ''},
                    {'id': 2, 'trackName': 'One More Time', 'artistName': 'Daft Punk', 'albumName': 'Discovery', 'duration': 320, 'plainLyrics': 'Plain text only', 'syncedLyrics': ''},
                    {'id': 3, 'trackName': 'One More Time', 'artistName': 'Daft Punk', 'albumName': 'Discovery', 'duration': 320, 'plainLyrics': 'Plain', 'syncedLyrics': '[00:15.00]One more time'},
                ])
            if 'lrclib.net/api/get/' in url:
                raise AssertionError('LRCLIB 命中 search 后应直接复用缓存，不应二次请求 /api/get')

            # YouTube Music InnerTube
            if 'music.youtube.com/youtubei/v1/search' in url:
                payload = json.loads(body)
                if payload.get('params') == 'EgWKAQIYAWoKEAMQBBAJEAoQBQ%3D%3D':
                    return _fake_resp({
                        'contents': {'tabbedSearchResultsRenderer': {'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {'contents': [
                            {'musicShelfRenderer': {'contents': [
                                {'musicResponsiveListItemRenderer': {
                                    'navigationEndpoint': {'browseEndpoint': {'browseId': 'MPREb_YTM_ALB1'}},
                                    'thumbnail': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': [{'url': 'https://lh3.googleusercontent.com/abc=w120-h120-l90-rj'}]}}},
                                    'flexColumns': [
                                        {'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [{'text': 'Random Access Memories'}]}}},
                                        {'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [
                                            {'text': 'Album'}, {'text': ' • '},
                                            {'text': 'Daft Punk', 'navigationEndpoint': {'browseEndpoint': {'browseId': 'UC_DAFT'}}},
                                            {'text': ' • '}, {'text': '2013'},
                                        ]}}},
                                    ],
                                }},
                            ]}},
                        ]}}}}]}},
                    })
                return _fake_resp({
                    'contents': {'tabbedSearchResultsRenderer': {'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {'contents': [
                        {'musicShelfRenderer': {'contents': [
                            {'musicResponsiveListItemRenderer': {
                                'playlistItemData': {'videoId': 'ytm_vid_1'},
                                'thumbnail': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': [{'url': 'https://lh3.googleusercontent.com/song=w120-h120-l90-rj'}]}}},
                                'flexColumns': [
                                    {'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [{'text': 'Get Lucky'}]}}},
                                    {'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [
                                        {'text': 'Daft Punk', 'navigationEndpoint': {'browseEndpoint': {'browseId': 'UC_DAFT'}}},
                                        {'text': ' • '},
                                        {'text': 'Random Access Memories', 'navigationEndpoint': {'browseEndpoint': {'browseId': 'MPREb_YTM_ALB1'}}},
                                        {'text': ' • '}, {'text': '4:08'},
                                    ]}}},
                                ],
                            }},
                        ]}},
                    ]}}}}]}},
                })
            if 'music.youtube.com/youtubei/v1/browse' in url:
                payload = json.loads(body)
                if payload.get('browseId') == 'MPREb_YTM_ALB1':
                    return _fake_resp({
                        'contents': {'twoColumnBrowseResultsRenderer': {
                            'tabs': [{'tabRenderer': {'content': {'sectionListRenderer': {'contents': [
                                {'musicResponsiveHeaderRenderer': {
                                    'title': {'runs': [{'text': 'Random Access Memories'}]},
                                    'straplineTextOne': {'runs': [{'text': 'Daft Punk'}]},
                                    'subtitle': {'runs': [{'text': 'Album'}, {'text': ' • '}, {'text': '2013'}]},
                                    'thumbnail': {'musicThumbnailRenderer': {'thumbnail': {'thumbnails': [{'url': 'https://lh3.googleusercontent.com/ram=w226-h226-l90-rj'}]}}},
                                }},
                            ]}}}}],
                            'secondaryContents': {'sectionListRenderer': {'contents': [
                                {'musicShelfRenderer': {'contents': [
                                    {'musicResponsiveListItemRenderer': {
                                        'playlistItemData': {'videoId': 'ytm_vid_0'},
                                        'flexColumns': [{'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [{'text': 'Give Life Back to Music'}]}}}],
                                        'fixedColumns': [{'musicResponsiveListItemFixedColumnRenderer': {'text': {'runs': [{'text': '4:35'}]}}}],
                                    }},
                                    {'musicResponsiveListItemRenderer': {
                                        'playlistItemData': {'videoId': 'ytm_vid_1'},
                                        'flexColumns': [{'musicResponsiveListItemFlexColumnRenderer': {'text': {'runs': [{'text': 'Get Lucky'}]}}}],
                                        'fixedColumns': [{'musicResponsiveListItemFixedColumnRenderer': {'text': {'runs': [{'text': '4:08'}]}}}],
                                    }},
                                ]}},
                            ]}},
                        }},
                    })
                if payload.get('browseId') == 'MPLYt_1':
                    return _fake_resp({
                        'contents': {'sectionListRenderer': {'contents': [
                            {'musicDescriptionShelfRenderer': {'description': {'runs': [{'text': 'Like the legend of the phoenix'}]}}},
                        ]}},
                    })
            if 'music.youtube.com/youtubei/v1/next' in url:
                return _fake_resp({
                    'contents': {'singleColumnMusicWatchNextResultsRenderer': {'tabbedRenderer': {'watchNextTabbedResultsRenderer': {'tabs': [
                        {'tabRenderer': {'title': 'Up next'}},
                        {'tabRenderer': {'title': 'Lyrics', 'endpoint': {'browseEndpoint': {'browseId': 'MPLYt_1'}}}},
                    ]}}}},
                })

            # Spotify Embed (unconfigured) + Official Web API (configured)
            if 'open.spotify.com/embed/album/sp_embed_1' in url:
                next_json = json.dumps({'props': {'pageProps': {'state': {'data': {'entity': {
                    'title': 'EmbedAlb', 'subtitle': 'EmbedArtist', 'releaseDate': '2020-05-01',
                    'trackList': [{'uri': 'spotify:track:sp1', 'name': 'EmbedTrack', 'duration': {'totalMilliseconds': 185000}}],
                }}}}}})
                return _fake_resp(f'<script id="__NEXT_DATA__" type="application/json">{next_json}</script>')
            if 'accounts.spotify.com/api/token' in url:
                return _fake_resp({'access_token': 'tok_sp_123', 'expires_in': 3600})
            if 'api.spotify.com/v1/search' in url:
                qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
                self.assertEqual(qs.get('limit'), ['10'])
                if qs.get('type') == ['album']:
                    return _fake_resp({'albums': {'items': [{
                        'id': 'sp_alb_1', 'name': 'SpotifyAlb', 'release_date': '2024-06-01',
                        'artists': [{'name': 'SpArtist'}],
                        'images': [{'url': 'https://sp.test/alb.jpg'}],
                    }]}})
                return _fake_resp({'tracks': {'items': []}})
            if 'api.spotify.com/v1/albums/sp_alb_1' in url:
                return _fake_resp({
                    'id': 'sp_alb_1', 'name': 'SpotifyAlb', 'release_date': '2024-06-01',
                    'artists': [{'name': 'SpArtist'}], 'genres': ['Synthpop'],
                    'images': [{'url': 'https://sp.test/alb.jpg'}],
                    'tracks': {'items': [
                        {'id': '4uLU6hMCjMI75M1A2tKUQC', 'name': 'SpSong', 'duration_ms': 210000, 'disc_number': 2, 'track_number': 1, 'artists': [{'name': 'SpArtist'}]},
                    ]},
                })
            if 'api.spotify.com/v1/tracks/4uLU6hMCjMI75M1A2tKUQC' in url:
                return _fake_resp({
                    'id': '4uLU6hMCjMI75M1A2tKUQC', 'name': 'SpSong', 'track_number': 1,
                    'artists': [{'name': 'SpArtist'}],
                    'album': {'id': 'sp_alb_1', 'name': 'SpotifyAlb', 'release_date': '2024-06-01', 'images': [{'url': 'https://sp.test/alb.jpg'}]},
                })
            raise AssertionError(f'Unexpected URL in test: {url}')

        try:
            set_spotify_credentials('', '')
            clear_caches()
            self.assertFalse(has_spotify_credentials())
            self.assertFalse(spotify_provider.supports_album_search)

            # 音源别名解析
            srcs, inv = parse_source_list('ytm, deezer, lrclib, sp')
            self.assertEqual(srcs, ('ytmusic', 'deezer', 'lrclib', 'spotify'))
            self.assertEqual(inv, [])

            # CLI 参数解析验证
            ns = build_arg_parser().parse_args(['--auto', '--spotify-client-id', 'cid1', '--spotify-client-secret', 'csec1'])
            self.assertEqual(ns.spotify_client_id, 'cid1')
            self.assertEqual(ns.spotify_client_secret, 'csec1')

            with patch('track_lookup.urllib.request.urlopen', side_effect=_fake_urlopen):
                # 1. Deezer 专辑匹配 + LRCLIB 同步歌词跨平台聚合
                tr_dz = LibraryTrack(
                    path=os.path.join(self.tmp, 'One More Time.flac'), folder=self.tmp, filename='One More Time.flac',
                    container='flac', title='One More Time', artist='Daft Punk', album='Discovery',
                    duration=320.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_dz = resolve_track_metadata(
                    tr_dz,
                    need_track=True, need_date=True, need_cover=True, need_core=True, need_extra=True, need_lyrics=True,
                    only_sources=('deezer', 'lrclib'),
                )
                self.assertEqual(res_dz.tracknumber, 1)
                self.assertEqual(res_dz.release_date, '2001-03-12')
                self.assertEqual(res_dz.genre, 'Electronic')
                self.assertEqual(res_dz.cover_url, 'https://dz.test/discovery_xl.jpg')
                self.assertEqual(res_dz.lyrics, '[00:15.00]One more time')

                # 2. YouTube Music 专辑匹配 + 封面升级 + 按需歌词
                tr_ytm = LibraryTrack(
                    path=os.path.join(self.tmp, 'Get Lucky.flac'), folder=self.tmp, filename='Get Lucky.flac',
                    container='flac', title='Get Lucky', artist='Daft Punk', album='Random Access Memories',
                    duration=248.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_ytm = resolve_track_metadata(
                    tr_ytm,
                    need_track=True, need_date=True, need_cover=True, need_lyrics=True,
                    only_sources=('ytmusic',),
                )
                self.assertEqual(res_ytm.tracknumber, 2)
                self.assertEqual(res_ytm.release_date, '2013')
                self.assertEqual(res_ytm.cover_url, 'https://lh3.googleusercontent.com/ram=w1200-h1200-l90-rj')
                self.assertEqual(res_ytm.lyrics, 'Like the legend of the phoenix')

                # 2b. YouTube Music 单曲反查 + 不请求歌词时不触发 next 接口
                clear_caches()
                seen_requests.clear()
                tr_ytm_song = LibraryTrack(
                    path=os.path.join(self.tmp, 'Get Lucky_single.flac'), folder=self.tmp, filename='Get Lucky.flac',
                    container='flac', title='Get Lucky', artist='Daft Punk', album='',
                    duration=248.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_ytm_song = resolve_track_metadata(
                    tr_ytm_song,
                    need_track=True, need_date=True, need_cover=True, need_lyrics=False,
                    only_sources=('ytmusic',),
                )
                self.assertEqual(res_ytm_song.tracknumber, 2)
                self.assertEqual(res_ytm_song.album_name, 'Random Access Memories')
                self.assertFalse(any('youtubei/v1/next' in u for u, _ in seen_requests))

                # 3. Spotify 未配置凭据时：搜索直接跳过，Embed 专辑读取保持可用
                self.assertEqual(spotify_provider.search_album('SpotifyAlb', 'SpotifyAlb', 'SpArtist'), ('', '', False))
                self.assertEqual(spotify_provider.search_song_hits('SpSong'), [])
                emb_tracks = spotify_provider.get_album_tracks('sp_embed_1')
                self.assertEqual(emb_tracks[0]['name'], 'EmbedTrack')

                # 4. 配置 Spotify 官方凭据后：OAuth Token 复用 + 专辑检索 + 22位歌曲ID直查
                set_spotify_credentials('cid_demo', 'csec_demo')
                self.assertTrue(has_spotify_credentials())
                self.assertTrue(spotify_provider.supports_album_search)

                tr_sp = LibraryTrack(
                    path=os.path.join(self.tmp, 'SpSong.flac'), folder=self.tmp, filename='SpSong.flac',
                    container='flac', title='SpSong', artist='SpArtist', album='SpotifyAlb',
                    duration=210.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_sp = resolve_track_metadata(
                    tr_sp,
                    need_track=True, need_date=True, need_cover=True, need_extra=True,
                    only_sources=('spotify',),
                )
                self.assertEqual(res_sp.tracknumber, 1)
                self.assertEqual(res_sp.discnumber, 2)
                self.assertEqual(res_sp.release_date, '2024-06-01')
                self.assertEqual(res_sp.genre, 'Synthpop')
                self.assertEqual(res_sp.cover_url, 'https://sp.test/alb.jpg')

                tr_sp_id = LibraryTrack(
                    path=os.path.join(self.tmp, 'SpSong - 4uLU6hMCjMI75M1A2tKUQC.flac'),
                    folder=self.tmp,
                    filename='SpSong - 4uLU6hMCjMI75M1A2tKUQC.flac',
                    container='flac',
                    title='SpSong',
                    duration=210.0, disk_size=100, disk_mtime=1.0, foobar_mtime=1.0, status=STATUS_OK,
                )
                res_sp_id = resolve_track_metadata(tr_sp_id, need_track=True, need_date=True, only_sources=('spotify',))
                self.assertEqual(res_sp_id.tracknumber, 1)
                self.assertIn('Spotify', res_sp_id.source)
                # 验证同一凭据下 token 仅请求一次
                self.assertEqual(sum(1 for u, _ in seen_requests if 'accounts.spotify.com/api/token' in u), 1)

                # 5. 验证 SourceConfigDialog 支持配置并保存 Spotify Client ID / Secret
                with offscreen_tk() as root:
                    dlg = SourceConfigDialog(
                        root,
                        prefer_sources=(),
                        only_sources=(),
                        skip_sources=(),
                        netease_api='',
                        spotify_client_id='cid_init',
                        spotify_client_secret='sec_init',
                    )
                    dlg.var_sp_id.set('cid_new')
                    dlg.var_sp_secret.set('sec_new')
                    with patch('app.messagebox.showinfo'):
                        dlg._on_confirm()
                    self.assertEqual(dlg.result['spotify_client_id'], 'cid_new')
                    self.assertEqual(dlg.result['spotify_client_secret'], 'sec_new')

            # 6. 国际源连续网络失败自动熔断并在 clear_caches 后重置
            clear_caches()
            dz_calls = 0

            def _fail_deezer(req, timeout=10):
                nonlocal dz_calls
                dz_calls += 1
                raise OSError('network unreachable')

            with patch('track_lookup.urllib.request.urlopen', side_effect=_fail_deezer):
                for idx in range(7):
                    deezer_provider.search_album(f'DZAlb{idx}', f'DZAlb{idx}', f'DZArt{idx}')
            self.assertEqual(dz_calls, 5)
            clear_caches()
            with patch('track_lookup.urllib.request.urlopen', side_effect=_fake_urlopen):
                aid, _, _ = deezer_provider.search_album('Discovery Daft Punk', 'Discovery', 'Daft Punk')
            self.assertEqual(aid, '302127')
        finally:
            set_spotify_credentials('', '')
            clear_caches()
