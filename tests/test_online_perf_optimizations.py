"""在线查询性能优化单元测试：长连接池、反向专辑缓存、熔断器与智能扩展剪枝。"""

import gzip
import http.server
import json
import os
import socketserver
import threading
import time
import unittest
import urllib.request
import zlib
from unittest.mock import MagicMock, patch

from core.models import LibraryTrack
from providers import PROVIDERS
from providers.base import (
    HTTPConnectionPool,
    ProviderCircuitBreaker,
    TrackMatch,
    _is_urlopen_mocked,
    cache_resolved_album,
    http_get_bytes,
    http_get_json,
    http_get_text,
    http_post_json,
    reset_all_circuit_breakers,
)
from services.lookup import clear_caches, fetch_cover_bytes, resolve_track_metadata


class _MockHTTPHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # 静默测试输出

    def do_GET(self):
        if self.path == '/plain':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            body = b'Hello Plain'
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == '/gzip':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Encoding', 'gzip')
            raw = json.dumps({'msg': 'Hello Gzip'}).encode('utf-8')
            compressed = gzip.compress(raw)
            self.send_header('Content-Length', str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        elif self.path == '/deflate':
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Encoding', 'deflate')
            raw = json.dumps({'msg': 'Hello Deflate'}).encode('utf-8')
            compressed = zlib.compress(raw)
            self.send_header('Content-Length', str(len(compressed)))
            self.end_headers()
            self.wfile.write(compressed)
        elif self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', '/plain')
            self.end_headers()
        elif self.path == '/404':
            self.send_response(404)
            self.end_headers()
        else:
            self.send_response(500)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length > 0 else b''
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        data = json.loads(body.decode('utf-8')) if body else {}
        resp = json.dumps({'echo': data}).encode('utf-8')
        self.send_header('Content-Length', str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)


class TestOnlinePerfOptimizations(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = socketserver.TCPServer(('127.0.0.1', 0), _MockHTTPHandler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        clear_caches()
        reset_all_circuit_breakers()

    def test_http_connection_pool_keepalive_and_decompression(self):
        pool = HTTPConnectionPool(max_idle_per_host=4)
        base = f'http://127.0.0.1:{self.port}'

        # 普通文本 GET
        res_plain = pool.request('GET', f'{base}/plain')
        self.assertIsNotNone(res_plain)
        status, data = res_plain
        self.assertEqual(status, 200)
        self.assertEqual(data.decode('utf-8'), 'Hello Plain')

        # Gzip 自动解压缩
        res_gz = pool.request('GET', f'{base}/gzip')
        self.assertIsNotNone(res_gz)
        status, data = res_gz
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode('utf-8')), {'msg': 'Hello Gzip'})

        # Deflate 自动解压缩
        res_def = pool.request('GET', f'{base}/deflate')
        self.assertIsNotNone(res_def)
        status, data = res_def
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data.decode('utf-8')), {'msg': 'Hello Deflate'})

        # 302 重定向跟随
        res_redir = pool.request('GET', f'{base}/redirect')
        self.assertIsNotNone(res_redir)
        status, data = res_redir
        self.assertEqual(status, 200)
        self.assertEqual(data.decode('utf-8'), 'Hello Plain')

        # 404 返回非 2xx
        res_404 = pool.request('GET', f'{base}/404')
        self.assertEqual(res_404[0], 404)

        # POST JSON
        res_post = pool.request('POST', base, body=b'{"key": "val"}')
        self.assertIsNotNone(res_post)
        self.assertEqual(json.loads(res_post[1].decode('utf-8')), {'echo': {'key': 'val'}})

        pool.clear()

    def test_http_tools_wrapper(self):
        base = f'http://127.0.0.1:{self.port}'
        txt = http_get_text(f'{base}/plain')
        self.assertEqual(txt, 'Hello Plain')

        js = http_get_json(f'{base}/gzip')
        self.assertEqual(js, {'msg': 'Hello Gzip'})

        raw = http_get_bytes(f'{base}/plain')
        self.assertEqual(raw, b'Hello Plain')

        post_js = http_post_json(base, {'num': 42})
        self.assertEqual(post_js, {'echo': {'num': 42}})

    def test_mock_detection(self):
        self.assertFalse(_is_urlopen_mocked())
        with patch('urllib.request.urlopen'):
            self.assertTrue(_is_urlopen_mocked())

    def test_cache_resolved_album(self):
        from providers.base import SEARCH_CACHE, norm_full
        cache_resolved_album('netease', 'Fantasy', 'Jay Chou', '6422', 'Fantasy')
        k1 = ('netease', norm_full('Fantasy Jay Chou'), norm_full('Fantasy'), 'jaychou')
        self.assertIn(k1, SEARCH_CACHE)
        self.assertEqual(SEARCH_CACHE[k1], ('6422', 'Fantasy', True))

    def test_provider_circuit_breaker_is_available(self):
        ne = PROVIDERS['netease']
        self.assertTrue(ne.is_available)
        self.assertIsNotNone(ne.circuit_breaker)
        for _ in range(5):
            ne.circuit_breaker.record_failure()
        self.assertTrue(ne.circuit_breaker.is_open())
        self.assertFalse(ne.is_available)
        reset_all_circuit_breakers()
        self.assertTrue(ne.is_available)


class TestPipelinedAlbumExecution(unittest.TestCase):
    """验证按专辑流水线（Per-Album Pipelining）边查边写与重叠并发特性。"""

    def setUp(self):
        import tempfile, shutil
        clear_caches()
        self.tmp = tempfile.mkdtemp(prefix='pipe_test_')

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_flac(self, folder: str, name: str) -> str:
        import struct
        si = bytearray(34)
        struct.pack_into('>HH', si, 0, 4096, 4096)
        val = (44100 << 44) | (1 << 41) | (15 << 36) | 44100
        struct.pack_into('>Q', si, 10, val)
        p = os.path.join(folder, name)
        with open(p, 'wb') as fp:
            fp.write(b'fLaC' + struct.pack('>I', 0x80000022) + bytes(si))
        return p

    def test_per_album_pipelined_streaming_execution(self):
        from core.models import PlayerEnv
        from pipelines.options import FixOptions
        from pipelines.repair import execute_repairs
        from tag_io import read_audio_info

        dir_fast = os.path.join(self.tmp, 'FastAlbumDir')
        dir_slow = os.path.join(self.tmp, 'SlowAlbumDir')
        os.makedirs(dir_fast, exist_ok=True)
        os.makedirs(dir_slow, exist_ok=True)

        f1 = self._make_flac(dir_fast, '01.flac')
        f2 = self._make_flac(dir_fast, '02.flac')
        s1 = self._make_flac(dir_slow, '01.flac')
        s2 = self._make_flac(dir_slow, '02.flac')

        t_fast1 = LibraryTrack(path=f1, filename='01.flac', folder=dir_fast, album='FastAlbum', title='F1', needs_tracknumber=True, needs_date=True)
        t_fast2 = LibraryTrack(path=f2, filename='02.flac', folder=dir_fast, album='FastAlbum', title='F2', needs_tracknumber=True, needs_date=True)
        t_slow1 = LibraryTrack(path=s1, filename='01.flac', folder=dir_slow, album='SlowAlbum', title='S1', needs_tracknumber=True, needs_date=True)
        t_slow2 = LibraryTrack(path=s2, filename='02.flac', folder=dir_slow, album='SlowAlbum', title='S2', needs_tracknumber=True, needs_date=True)

        tracks = [t_fast1, t_fast2, t_slow1, t_slow2]
        fix_events: list[tuple[str, float]] = []

        def fake_resolve(tr, **_kw):
            if 'FastAlbum' in tr.folder or tr.album == 'FastAlbum':
                time.sleep(0.01)
                return TrackMatch(tracknumber=1 if '01' in tr.filename else 2, release_date='2020-01-01', source='fake_fast', album_name='FastAlbum')
            else:
                time.sleep(0.3)
                return TrackMatch(tracknumber=1 if '01' in tr.filename else 2, release_date='2021-02-02', source='fake_slow', album_name='SlowAlbum')

        def on_row(path, payload):
            if payload.get('status_text') == '已修复':
                fix_events.append((path, time.monotonic()))

        env = PlayerEnv(library_folders=[dir_fast, dir_slow])
        opts = FixOptions(enable_art=False, enable_tags=True, enable_sync=False)

        with patch('pipelines.repair.resolve_hook', return_value=fake_resolve):
            summary = execute_repairs(env, tracks, opts, row_cb=on_row)

        self.assertFalse(summary['cancelled'])
        self.assertEqual(summary['written_ok'], 4)
        self.assertEqual(summary['date_fixed'], 4)
        self.assertEqual(len(fix_events), 4)

        # 验证 fast 专辑的曲目先于 slow 专辑全部完成写入
        fast_paths = {f1, f2}
        slow_paths = {s1, s2}
        first_two_paths = {fix_events[0][0], fix_events[1][0]}
        last_two_paths = {fix_events[2][0], fix_events[3][0]}
        self.assertEqual(first_two_paths, fast_paths)
        self.assertEqual(last_two_paths, slow_paths)

        # 验证 fast 专辑完成写入与 slow 专辑完成写入之间有明显的并发流式时间差
        fast_done_time = max(fix_events[0][1], fix_events[1][1])
        slow_start_time = min(fix_events[2][1], fix_events[3][1])
        self.assertGreaterEqual(slow_start_time - fast_done_time, 0.15)

        # 验证文件标签真实写入成功且符合各专辑独立传播结果
        self.assertEqual(read_audio_info(f1)['date'], '2020-01-01')
        self.assertEqual(read_audio_info(s1)['date'], '2021-02-02')


if __name__ == '__main__':
    unittest.main()
