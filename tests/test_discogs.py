"""单元测试：Discogs 数据源提供者、Position 解析、制作名单归类与鉴权保护。"""

from __future__ import annotations

import json
from unittest.mock import patch

from providers import (
    ONLINE_PROVIDERS,
    PROVIDERS,
    discogs_provider,
    get_discogs_token,
    has_discogs_token,
    normalize_provider_name,
    set_discogs_token,
)
from providers.base import get_album_meta
from providers.discogs import (
    _extract_credits,
    _parse_position,
    is_discogs_circuit_open,
    reset_discogs_circuit,
)
from services.lookup.resolver import fetch_cover_bytes
from tests.common import FakeHTTPResponse, TempTrackTestCase


class DiscogsProviderTests(TempTrackTestCase):
    def setUp(self):
        super().setUp()
        self._orig_token = get_discogs_token()
        reset_discogs_circuit()

    def tearDown(self):
        set_discogs_token(self._orig_token)
        reset_discogs_circuit()
        super().tearDown()

    def test_token_management(self):
        set_discogs_token('')
        self.assertFalse(has_discogs_token())
        self.assertFalse(discogs_provider.is_available)

        set_discogs_token('test_token_12345')
        self.assertTrue(has_discogs_token())
        self.assertEqual(get_discogs_token(), 'test_token_12345')
        self.assertTrue(discogs_provider.is_available)

    def test_parse_position_formats(self):
        # 1. 纯数字
        self.assertEqual(_parse_position('1', 99), (1, 1))
        self.assertEqual(_parse_position('12', 99), (1, 12))

        # 2. 多碟/分卷形式
        self.assertEqual(_parse_position('1-02', 99), (1, 2))
        self.assertEqual(_parse_position('CD2-05', 99), (2, 5))
        self.assertEqual(_parse_position('2.3', 99), (2, 3))

        # 3. 黑胶/磁带 Side 形式
        self.assertEqual(_parse_position('A1', 99), (1, 1))
        self.assertEqual(_parse_position('A2', 99), (1, 2))
        self.assertEqual(_parse_position('B1', 99), (2, 1))
        self.assertEqual(_parse_position('C3', 99), (3, 3))

        # 4. 空串或无法解析
        self.assertEqual(_parse_position('', 5), (1, 5))
        self.assertEqual(_parse_position('Bonus', 7), (1, 7))

    def test_extract_credits_and_clean_names(self):
        extra = [
            {'name': 'Stock / Aitken / Waterman', 'role': 'Written-By, Producer'},
            {'name': 'Matt Aitken (2)', 'role': 'Guitars, Keyboards'},
            {'name': 'Pete Waterman', 'role': 'Arranged By'},
            {'name': 'Mike Stock (3)', 'role': 'Lyrics By, Vocals'},
        ]
        comp, lyr, arr, perf = _extract_credits(extra)
        self.assertIn('Stock / Aitken / Waterman', comp)
        self.assertIn('Mike Stock', lyr)  # (3) 编号被清理
        self.assertIn('Pete Waterman', arr)
        self.assertIn('Matt Aitken', perf)  # (2) 编号被清理

    def test_search_album_mock(self):
        set_discogs_token('valid_token')
        mock_search_resp = {
            'results': [
                {
                    'id': 249504,
                    'title': 'Rick Astley - Whenever You Need Somebody',
                    'year': '1987',
                    'cover_image': 'https://i.discogs.com/cov123.jpg',
                    'catno': 'PL 71529',
                }
            ]
        }

        with patch('providers.discogs.http_get_json') as mock_get:
            mock_get.return_value = mock_search_resp
            rid, title, artist_ok = discogs_provider.search_album(
                'Whenever You Need Somebody', 'Whenever You Need Somebody', 'Rick Astley'
            )
            self.assertEqual(rid, '249504')
            self.assertEqual(title, 'Whenever You Need Somebody')
            self.assertTrue(artist_ok)

            meta = get_album_meta('discogs', '249504')
            self.assertEqual(meta.get('release_date'), '1987')
            self.assertEqual(meta.get('cover_url'), 'https://i.discogs.com/cov123.jpg')

    def test_get_album_tracks_mock(self):
        set_discogs_token('valid_token')
        mock_release = {
            'id': 249504,
            'title': 'Whenever You Need Somebody',
            'artists': [{'name': 'Rick Astley'}],
            'year': 1987,
            'released': '1987-11-23',
            'genres': ['Electronic', 'Pop'],
            'styles': ['Synth-pop', 'Euro-Disco'],
            'images': [
                {'type': 'secondary', 'uri': 'https://i.discogs.com/sec.jpg'},
                {'type': 'primary', 'uri': 'https://i.discogs.com/primary.jpg'},
            ],
            'extraartists': [
                {'name': 'Stock, Aitken & Waterman', 'role': 'Producer, Written-By'}
            ],
            'tracklist': [
                {'type_': 'heading', 'title': 'Side One'},
                {
                    'type_': 'track',
                    'position': 'A1',
                    'title': 'Never Gonna Give You Up',
                    'duration': '3:32',
                    'extraartists': [
                        {'name': 'Mike Stock', 'role': 'Composed By'},
                    ],
                },
                {
                    'type_': 'track',
                    'position': 'A2',
                    'title': 'Whenever You Need Somebody',
                    'duration': '3:52',
                },
            ],
        }

        with patch('providers.discogs.http_get_json') as mock_get:
            mock_get.return_value = mock_release
            tracks = discogs_provider.get_album_tracks('249504')
            self.assertEqual(len(tracks), 2)  # heading 被成功剔除

            # 验证第 1 首
            t1 = tracks[0]
            self.assertEqual(t1['disc'], 1)
            self.assertEqual(t1['number'], 1)
            self.assertEqual(t1['name'], 'Never Gonna Give You Up')
            self.assertAlmostEqual(t1['duration'], 212.0)
            self.assertEqual(t1['composer'], 'Mike Stock')
            self.assertIn('Synth-pop', t1['genre'])
            self.assertEqual(t1['cover_url'], 'https://i.discogs.com/primary.jpg')

            # 验证第 2 首（继承专辑级 extraartists 作曲者）
            t2 = tracks[1]
            self.assertEqual(t2['number'], 2)
            self.assertEqual(t2['composer'], 'Stock, Aitken & Waterman')

    def test_search_song_hits_mock(self):
        set_discogs_token('valid_token')
        mock_hits_resp = {
            'results': [
                {
                    'id': 1001,
                    'title': 'Artist X - Song Title',
                    'year': '2005',
                    'cover_image': 'https://i.discogs.com/hit.jpg',
                }
            ]
        }
        with patch('providers.discogs.http_get_json') as mock_get:
            mock_get.return_value = mock_hits_resp
            hits = discogs_provider.search_song_hits('Song Title')
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['artist'], 'Artist X')
            self.assertEqual(hits[0]['album_name'], 'Song Title')
            self.assertEqual(hits[0]['cover_url'], 'https://i.discogs.com/hit.jpg')

    def test_circuit_breaker_behavior(self):
        set_discogs_token('valid_token')
        self.assertFalse(is_discogs_circuit_open())

        with patch('providers.discogs.http_get_json', return_value=None), patch('providers.discogs.time.sleep'):
            for i in range(5):
                discogs_provider.search_album(f'Unknown_{i}', f'Unknown_{i}', '')
            self.assertTrue(is_discogs_circuit_open())
            self.assertFalse(discogs_provider.is_available)

    def test_fetch_cover_bytes_injects_discogs_auth(self):
        set_discogs_token('secret_token_abc')
        discogs_img_url = 'https://i.discogs.com/sample_cover.jpg'

        captured_headers = {}

        def fake_http_bytes(url, headers=None, timeout=6):
            nonlocal captured_headers
            captured_headers = dict(headers or {})
            # 返回 1x1 最小 valid jpeg 字节
            return b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a\x1f\x1e\x1d\x1a\x1c\x1c $.\' \",#\x1c\x1c(7),01444\x1f\'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'

        with patch('services.lookup.resolver.http_get_bytes', side_effect=fake_http_bytes):
            res = fetch_cover_bytes(discogs_img_url)
            self.assertIsNotNone(res)
            self.assertEqual(captured_headers.get('Authorization'), 'Discogs token=secret_token_abc')
            self.assertIn('MusicMetadata', captured_headers.get('User-Agent', ''))

    def test_provider_registration(self):
        self.assertIn('discogs', ONLINE_PROVIDERS)
        self.assertIn('discogs', PROVIDERS)
        self.assertEqual(PROVIDERS['discogs'], discogs_provider)
        self.assertEqual(normalize_provider_name('discogs'), 'discogs')
        self.assertEqual(normalize_provider_name('dg'), 'discogs')
        self.assertEqual(normalize_provider_name('dc'), 'discogs')

    def test_source_dialog_and_cli_token(self):
        from cli import build_arg_parser
        from ui.dialogs.source_dlg import SourceConfigDialog
        from tests.common import offscreen_tk

        # 1. 验证 CLI 参数解析
        parser = build_arg_parser()
        args = parser.parse_args(['--discogs-token', 'cli_token_xyz'])
        self.assertEqual(args.discogs_token, 'cli_token_xyz')

        # 2. 验证 SourceConfigDialog
        with offscreen_tk() as root:
            dlg = SourceConfigDialog(
                root,
                prefer_sources=(),
                only_sources=(),
                skip_sources=(),
                netease_api='',
                discogs_token='initial_token',
            )
            self.assertEqual(dlg.var_discogs_token.get(), 'initial_token')
            dlg.var_discogs_token.set('modified_token')
            dlg._on_confirm()
            self.assertEqual(dlg.result.get('discogs_token'), 'modified_token')
