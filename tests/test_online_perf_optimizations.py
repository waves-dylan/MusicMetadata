"""在线查询性能优化单元测试：长连接池、反向专辑缓存、熔断器与智能扩展剪枝。"""

import gzip
import http.server
import json
import socketserver
import threading
import time
import unittest
import urllib.request
import zlib
from unittest.mock import MagicMock, patch

from library_scan import LibraryTrack
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
from track_lookup import clear_caches, fetch_cover_bytes, resolve_track_metadata


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


if __name__ == '__main__':
    unittest.main()
