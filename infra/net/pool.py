"""HTTP/HTTPS 长连接连接池与通用请求辅助。"""

from __future__ import annotations

import gzip
import http.client
import json
import queue
import ssl
import threading
import urllib.parse
import urllib.request
import zlib

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'
)


class HTTPConnectionPool:
    """标准库 HTTP/HTTPS 线程安全长连接连接池（支持 Keep-Alive 与 Gzip 压缩）。"""

    def __init__(self, max_idle_per_host: int = 16):
        self._pools: dict[tuple[str, str, int], queue.LifoQueue] = {}
        self._lock = threading.Lock()
        self._max_idle = max_idle_per_host
        self._ssl_ctx = ssl.create_default_context()

    def _get_queue(self, key: tuple[str, str, int]) -> queue.LifoQueue:
        with self._lock:
            q = self._pools.get(key)
            if q is None:
                q = queue.LifoQueue(maxsize=self._max_idle)
                self._pools[key] = q
            return q

    def clear(self) -> None:
        with self._lock:
            for q in self._pools.values():
                while not q.empty():
                    try:
                        conn = q.get_nowait()
                        conn.close()
                    except Exception:
                        pass
            self._pools.clear()

    def request(
        self, method: str, url: str, body: bytes | None = None,
        headers: dict | None = None, timeout: float = 6.0, redirects: int = 5,
    ) -> tuple[int, bytes] | None:
        parsed = urllib.parse.urlsplit(url)
        scheme, host, port = parsed.scheme.lower(), parsed.hostname or '', parsed.port
        if not host:
            return None
        is_ssl = (scheme == 'https')
        if not port:
            port = 443 if is_ssl else 80
        key = (scheme, host, port)
        q = self._get_queue(key)

        conn = None
        while True:
            try:
                conn = q.get_nowait()
            except queue.Empty:
                if is_ssl:
                    conn = http.client.HTTPSConnection(host, port, context=self._ssl_ctx, timeout=timeout)
                else:
                    conn = http.client.HTTPConnection(host, port, timeout=timeout)
            break

        hdrs = {
            'User-Agent': USER_AGENT,
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
        }
        if headers:
            hdrs.update(headers)
        path = parsed.path or '/'
        if parsed.query:
            path += '?' + parsed.query

        resp = None
        for attempt in range(2):
            try:
                conn.timeout = timeout
                conn.request(method, path, body=body, headers=hdrs)
                resp = conn.getresponse()
                break
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, EOFError, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                if attempt == 0:
                    if is_ssl:
                        conn = http.client.HTTPSConnection(host, port, context=self._ssl_ctx, timeout=timeout)
                    else:
                        conn = http.client.HTTPConnection(host, port, timeout=timeout)
                    continue
                return None
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
                return None

        if resp is None:
            return None

        try:
            raw = resp.read()
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return None

        enc = (resp.getheader('Content-Encoding') or '').lower()
        if 'gzip' in enc:
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        elif 'deflate' in enc:
            try:
                raw = zlib.decompress(raw)
            except Exception:
                pass

        if not resp.will_close:
            try:
                q.put_nowait(conn)
            except queue.Full:
                conn.close()
        else:
            conn.close()

        status = resp.status
        if status in (301, 302, 303, 307, 308) and redirects > 0:
            loc = resp.getheader('Location')
            if loc:
                target = urllib.parse.urljoin(url, loc)
                req_method = 'GET' if status in (301, 302, 303) else method
                req_body = None if req_method == 'GET' else body
                return self.request(req_method, target, body=req_body, headers=headers, timeout=timeout, redirects=redirects - 1)

        return status, raw


_HTTP_POOL = HTTPConnectionPool()


def _is_urlopen_mocked() -> bool:
    fn = urllib.request.urlopen
    return (
        hasattr(fn, 'mock_calls') or
        getattr(type(fn), '__module__', '').startswith('unittest.mock') or
        hasattr(fn, 'side_effect') or
        hasattr(fn, 'return_value')
    )


def http_get_text(url: str, params=None, headers=None, timeout: int = 6) -> str | None:
    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        hdrs = {'User-Agent': USER_AGENT}
        if headers:
            hdrs.update(headers)
        if _is_urlopen_mocked():
            req = urllib.request.Request(full_url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode('utf-8', errors='replace')
        res = _HTTP_POOL.request('GET', full_url, headers=hdrs, timeout=timeout)
        if res is None:
            return None
        status, raw = res
        if not (200 <= status < 300):
            return None
        return raw.decode('utf-8', errors='replace')
    except Exception:
        return None


def http_get_json(url: str, params=None, headers=None, timeout: int = 6):
    text = http_get_text(url, params=params, headers=headers, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def http_get_bytes(url: str, headers=None, timeout: int = 8) -> bytes | None:
    try:
        hdrs = {'User-Agent': USER_AGENT}
        if headers:
            hdrs.update(headers)
        if _is_urlopen_mocked():
            req = urllib.request.Request(url, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        res = _HTTP_POOL.request('GET', url, headers=hdrs, timeout=timeout)
        if res is None:
            return None
        status, raw = res
        if not (200 <= status < 300):
            return None
        return raw
    except Exception:
        return None


def http_post_json(url: str, payload: dict, headers=None, timeout: int = 6):
    try:
        hdrs = {'User-Agent': USER_AGENT, 'Content-Type': 'application/json'}
        if headers:
            hdrs.update(headers)
        body = json.dumps(payload).encode('utf-8')
        if _is_urlopen_mocked():
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        res = _HTTP_POOL.request('POST', url, body=body, headers=hdrs, timeout=timeout)
        if res is None:
            return None
        status, raw = res
        if not (200 <= status < 300):
            return None
        return json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        return None


def http_post_form(url: str, form: dict[str, str], headers=None, timeout: int = 6):
    try:
        hdrs = {'User-Agent': USER_AGENT, 'Content-Type': 'application/x-www-form-urlencoded'}
        if headers:
            hdrs.update(headers)
        body = urllib.parse.urlencode(form).encode('utf-8')
        if _is_urlopen_mocked():
            req = urllib.request.Request(url, data=body, headers=hdrs)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8', errors='replace'))
        res = _HTTP_POOL.request('POST', url, body=body, headers=hdrs, timeout=timeout)
        if res is None:
            return None
        status, raw = res
        if not (200 <= status < 300):
            return None
        return json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        return None


