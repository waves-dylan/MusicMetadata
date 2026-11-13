"""音乐平台提供者公共基类、数据结构、HTTP 工具与共享缓存。"""

from __future__ import annotations

import difflib
import gzip
import html
import http.client
import json
import queue
import re
import ssl
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from tag_io import merge_lyrics

USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36'
)
CN_TZ = timezone(timedelta(hours=8))

CACHE_LOCK = threading.Lock()
CACHE_GEN = 0
ALBUM_CACHE: dict[tuple, list[dict]] = {}
ALBUM_META_CACHE: dict[tuple[str, str], dict] = {}
KUGOU_INFO_DONE: set[str] = set()
SONG_META_CACHE: dict[tuple[str, str], dict] = {}
SONG_SEARCH_CACHE: dict[tuple[str, str, str], list] = {}
COVER_CACHE: dict[str, bytes] = {}
ARTIST_PIC_CACHE: dict[tuple[str, str], str] = {}
ARTIST_BYTES_CACHE: dict[str, bytes] = {}
SEARCH_CACHE: dict[tuple, tuple[str, str, bool]] = {}
PKL_CACHE: dict[str, dict] = {}
INFLIGHT_ALBUM: dict[tuple, threading.Event] = {}
INFLIGHT_SEARCH: dict[tuple, threading.Event] = {}
INFLIGHT_SONG_SEARCH: dict[tuple, threading.Event] = {}
INFLIGHT_ARTIST: dict[tuple[str, str], threading.Event] = {}

_BRACKET_RE = re.compile(r'[\(（\[【].*?[\)）\]】]')
_LRC_TS_RE = re.compile(r'^(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*')
_VA_ARTISTS = {'群星', 'variousartists', 'various', 'va', 'soundtrack', 'originalsoundtrack', 'ost'}
_COMPOSER_ROLES = {'作曲', '曲', '谱曲', 'composer', 'music', 'musicby', 'composedby'}
_INVALID_CREDITS = {'', '-', '/', '无', '暂无', '纯音乐', 'none', 'null', 'n/a'}


@dataclass
class TrackMatch:
    tracknumber: int | None
    discnumber: int | None = None
    totaltracks: int | None = None
    source: str = ''
    album_name: str = ''
    reason: str = ''
    release_date: str = ''
    title: str = ''
    artist: str = ''
    albumartist: str = ''
    composer: str = ''
    genre: str = ''
    cover_url: str = ''
    artist_pic_url: str = ''
    lyrics: str = ''


def get_cache_gen() -> int:
    with CACHE_LOCK:
        return CACHE_GEN


def bump_cache_gen() -> int:
    global CACHE_GEN
    with CACHE_LOCK:
        CACHE_GEN += 1
        ALBUM_CACHE.clear()
        ALBUM_META_CACHE.clear()
        KUGOU_INFO_DONE.clear()
        SONG_META_CACHE.clear()
        SONG_SEARCH_CACHE.clear()
        COVER_CACHE.clear()
        ARTIST_PIC_CACHE.clear()
        ARTIST_BYTES_CACHE.clear()
        INFLIGHT_ARTIST.clear()
        SEARCH_CACHE.clear()
        PKL_CACHE.clear()
        _HTTP_POOL.clear()
        return CACHE_GEN


def cached_call(cache: dict, inflight: dict, key: tuple, fn):
    """线程安全缓存读写，并对同 key 并发请求去重（含负缓存）。"""
    while True:
        with CACHE_LOCK:
            if key in cache:
                return cache[key]
            ev = inflight.get(key)
            if ev is None:
                ev = threading.Event()
                inflight[key] = ev
                gen = CACHE_GEN
                break
        ev.wait()
    val_ready = False
    val = None
    try:
        val = fn()
        val_ready = True
        return val
    finally:
        with CACHE_LOCK:
            if val_ready and CACHE_GEN == gen:
                cache[key] = val
            inflight.pop(key, None)
            ev.set()


_CIRCUIT_BREAKERS: list['ProviderCircuitBreaker'] = []


class ProviderCircuitBreaker:
    """线程安全的连续失败熔断器，避免不可达外部服务拖慢批量查询。"""

    def __init__(self, threshold: int = 5):
        self.threshold = threshold
        self._lock = threading.Lock()
        self._consec_fails = 0
        self._open = False
        _CIRCUIT_BREAKERS.append(self)

    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def record_success(self) -> None:
        with self._lock:
            self._consec_fails = 0

    def record_failure(self) -> bool:
        with self._lock:
            self._consec_fails += 1
            if self._consec_fails >= self.threshold:
                self._open = True
            return self._open

    def reset(self) -> None:
        with self._lock:
            self._consec_fails = 0
            self._open = False


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


def reset_all_circuit_breakers() -> None:
    for cb in _CIRCUIT_BREAKERS:
        cb.reset()


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


def to_seconds(value) -> float:
    try:
        if isinstance(value, str) and ':' in value:
            total = 0.0
            for part in value.split(':'):
                total = total * 60 + float(part)
            return total
        n = float(value or 0)
        return n / 1000.0 if n > 6000 else n
    except Exception:
        return 0.0


def norm_full(v: str) -> str:
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', html.unescape(str(v or ''))).lower())


def norm_core(v: str) -> str:
    return re.sub(r'[^\w]+', '', _BRACKET_RE.sub('', unicodedata.normalize('NFKC', html.unescape(str(v or ''))).lower()))


def norm_id(v: str) -> str:
    return str(v or '').strip().upper().replace('MUSIC_', '').replace('MUSIC:', '')


def first_artist(artist: str) -> str:
    return re.split(r'\s*[,/、&;]\s*', str(artist or '').strip())[0].strip() if artist else ''


def extract_artist_names(items, *, sep: str = ', ', key: str = 'name') -> str:
    if not isinstance(items, list):
        return ''
    return sep.join(str(a.get(key) or '').strip() for a in items if isinstance(a, dict) and a.get(key))


def normalize_release_date(value, *, is_ms: bool = False) -> str:
    """将各音乐平台返回的发布日期/时间戳统一规范为 YYYY-MM-DD / YYYY-MM / YYYY。"""
    if value is None or isinstance(value, bool):
        return ''
    if isinstance(value, dict):
        for k in ('isoString', 'date', 'releaseDate', 'year'):
            if value.get(k) not in (None, ''):
                res = normalize_release_date(value.get(k), is_ms=is_ms)
                if res:
                    return res
        return ''
    if isinstance(value, (int, float)):
        num = float(value)
        if is_ms:
            if abs(num) < 86_400_000:
                return ''
            sec = num / 1000.0
        else:
            if 1800 <= num <= 2100 and num.is_integer():
                return f'{int(num):04d}'
            if num >= 10_000_000_000:
                sec = num / 1000.0
            elif num >= 86_400:
                sec = num
            else:
                return ''
        try:
            dt = datetime.fromtimestamp(sec, tz=CN_TZ)
        except (OverflowError, OSError, ValueError):
            return ''
        return dt.strftime('%Y-%m-%d') if 1800 <= dt.year <= 2100 else ''

    s = str(value).strip()
    if not s or s.lower() in {'none', 'null', 'nan', '0', '0000-00-00'} or s.startswith(('1970-01-01 0', '1970-01-01T0')):
        return ''
    if re.fullmatch(r'-?\d+', s):
        if len(s) == 8:
            try:
                dt = datetime(int(s[:4]), int(s[4:6]), int(s[6:8]))
                if 1800 <= dt.year <= 2100:
                    return dt.strftime('%Y-%m-%d')
            except ValueError:
                pass
        if len(s) == 4 and 1800 <= int(s) <= 2100:
            return s
        return normalize_release_date(int(s), is_ms=is_ms)

    m = re.match(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[T\s].*)?$', s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            dt = datetime(y, mo, d)
            return dt.strftime('%Y-%m-%d') if 1800 <= y <= 2100 else ''
        except ValueError:
            return ''

    m = re.match(r'^(\d{4})[-/.](\d{1,2})$', s)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        return f'{y:04d}-{mo:02d}' if (1800 <= y <= 2100 and 1 <= mo <= 12) else ''

    m = re.match(r'^(\d{4})$', s)
    if m:
        y = int(m.group(1))
        return f'{y:04d}' if 1800 <= y <= 2100 else ''
    return ''


def set_album_meta(platform: str, album_id: str, **fields: str) -> dict:
    aid = str(album_id or '')
    if not aid:
        return {}
    key = (platform, aid)
    with CACHE_LOCK:
        cur = dict(ALBUM_META_CACHE.get(key) or {})
        for k, v in fields.items():
            txt = str(v or '').strip()
            if not txt:
                continue
            if k == 'release_date':
                txt = normalize_release_date(txt)
                if txt and (not cur.get(k) or len(txt) > len(cur[k])):
                    cur[k] = txt
            elif not cur.get(k):
                cur[k] = txt
        ALBUM_META_CACHE[key] = cur
        return dict(cur)


def get_album_meta(platform: str, album_id: str) -> dict:
    if not album_id:
        return {}
    with CACHE_LOCK:
        return dict(ALBUM_META_CACHE.get((platform, str(album_id))) or {})


def set_album_date(platform: str, album_id: str, date_str: str) -> str:
    return set_album_meta(platform, album_id, release_date=date_str).get('release_date', '')


def get_album_date(platform: str, album_id: str) -> str:
    return get_album_meta(platform, album_id).get('release_date', '')


def parse_lyric_credits(lrc_text: str) -> str:
    """从歌词头部制作人员行提取作曲者 (composer)。"""
    composers: list[str] = []
    for raw_line in str(lrc_text or '').replace('\r\n', '\n').splitlines()[:45]:
        line = _LRC_TS_RE.sub('', raw_line).strip()
        if not line or (':' not in line and '：' not in line):
            continue
        sep = '：' if ('：' in line and (':' not in line or line.index('：') < line.index(':'))) else ':'
        role_raw, val_raw = line.split(sep, 1)
        role_clean = role_raw.strip()
        role_key = re.sub(r'[\s/\-_]+', '', role_clean).lower()
        val_clean = val_raw.strip()
        if not role_key or len(role_clean) > 20 or val_clean.lower() in _INVALID_CREDITS:
            continue
        names = [p.strip() for p in re.split(r'\s*[/、,;]\s*', val_clean) if p.strip() and p.strip().lower() not in _INVALID_CREDITS]
        if not names:
            continue
        if role_key in _COMPOSER_ROLES:
            for nm in names:
                if nm not in composers:
                    composers.append(nm)
    return ', '.join(composers)


def build_tracks(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, it in enumerate((x for x in (items or []) if isinstance(x, dict)), 1):
        rec = {
            'number': i,
            'disc': int(it.get('disc') or 1),
            'id': it.get('id'),
            'name': str(it.get('name') or ''),
            'duration': float(it.get('duration') or 0.0),
        }
        for opt_k in ('artist', 'composer', 'genre', 'cover_url'):
            if it.get(opt_k):
                rec[opt_k] = str(it[opt_k]).strip()
        out.append(rec)
    return out


def cached_album(platform: str, album_id: str, fetcher) -> list[dict]:
    aid = str(album_id or '')
    if not aid:
        return []
    return cached_call(ALBUM_CACHE, INFLIGHT_ALBUM, (platform, aid), lambda: fetcher(aid))


def cached_search(platform: str, term: str, album_name: str, singer: str, fn) -> tuple[str, str, bool]:
    key = (platform, norm_full(term), norm_full(album_name), norm_core(singer))
    return cached_call(SEARCH_CACHE, INFLIGHT_SEARCH, key, fn)


def cached_song_search(platform: str, scope: str, term: str, fn) -> list:
    key = (platform, scope, norm_full(term))
    return cached_call(SONG_SEARCH_CACHE, INFLIGHT_SONG_SEARCH, key, fn)


def cached_song_meta(platform: str, cache_id: str, fn) -> dict:
    """读取或填充 SONG_META_CACHE 缓存条目（按 (platform, cache_id) 键存储）。"""
    cid = str(cache_id or '').strip()
    if not cid:
        return {}
    key = (platform, cid)
    with CACHE_LOCK:
        if key in SONG_META_CACHE:
            return dict(SONG_META_CACHE[key])
    res = fn(cid) or {}
    with CACHE_LOCK:
        SONG_META_CACHE[key] = res
    return dict(res)


def cached_artist_url(platform: str, artist_name: str, fn) -> str:
    """读取或填充 ARTIST_PIC_CACHE 缓存条目（按 (platform, norm_core(artist_name)) 键存储）。"""
    name = str(artist_name or '').strip()
    if not name:
        return ''
    key = (platform, norm_core(name))
    return cached_call(ARTIST_PIC_CACHE, INFLIGHT_ARTIST, key, fn) or ''


def is_artist_compatible(album_artist: str, singer: str) -> bool:
    sk = norm_core(singer)
    if not sk:
        return True
    ca = norm_core(album_artist)
    if not ca:
        return False
    return ca == sk or (sk in ca) or (ca in sk) or (ca in _VA_ARTISTS)


def choose_album_detail(
    candidates: list[dict], album_name: str, singer: str, name_fn, artist_fn,
) -> tuple[dict | None, bool]:
    tc, tf, sk = norm_core(album_name), norm_full(album_name), norm_core(singer)
    if not (tc or tf) or not candidates:
        return None, False
    best, best_score, best_artist_ok = None, 0, False
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        raw = name_fn(cand) or ''
        cf, cc, raw_artist = norm_full(raw), norm_core(raw), artist_fn(cand) or ''
        ca = norm_core(raw_artist)
        if not (cc or cf):
            continue
        if cf and tf and cf == tf:
            score = 5
        elif cc and tc and cc == tc:
            score = 4
        elif tc and cc and len(tc) >= 3 and (tc in cc or cc in tc):
            score = 2
        else:
            continue
        artist_ok = True
        if sk:
            if ca == sk:
                score += 3
            elif ca and (sk in ca or ca in sk):
                score += 2
            elif ca in _VA_ARTISTS:
                score += 1
            elif score < 5:
                continue
            else:
                artist_ok = False
        if score > best_score:
            best, best_score, best_artist_ok = cand, score, artist_ok
    if best_score >= (5 if sk else 4):
        return best, best_artist_ok
    return None, False


def match_in_tracks(tracks: list[dict], title: str, duration: float, identifier: str = '') -> dict | None:
    if not tracks:
        return None
    nid = norm_id(identifier)
    if nid:
        for tr in tracks:
            if tr.get('id') and norm_id(tr['id']) == nid:
                return tr
    tf, tc = norm_full(title), norm_core(title)
    if not (tf or tc):
        return None

    cands = [tr for tr in tracks if norm_full(tr.get('name')) == tf] if tf else []
    if not cands and tc:
        cands = [tr for tr in tracks if norm_core(tr.get('name')) == tc]
    if not cands and tc and len(tc) >= 2:
        cands = [tr for tr in tracks if norm_core(tr.get('name')) and (tc in norm_core(tr.get('name')) or norm_core(tr.get('name')) in tc)]
    if not cands and tc:
        scored = sorted(
            ((difflib.SequenceMatcher(None, tc, norm_core(tr.get('name'))).ratio(), tr)
             for tr in tracks if norm_core(tr.get('name'))),
            key=lambda p: -p[0],
        )
        if scored and scored[0][0] >= 0.84:
            cands = [tr for r, tr in scored if r >= scored[0][0] - 0.03]

    if not cands:
        return None
    if len(cands) > 1 and duration > 0:
        close = [tr for tr in cands if tr.get('duration') and abs(float(tr['duration']) - duration) <= 6.0]
        if close:
            cands = close
    return cands[0] if len(cands) == 1 else None


def song_matches(song_name: str, song_duration: float, song_artist: str, tc: str, duration: float, ac: str) -> bool:
    sn = norm_core(song_name or '')
    if not sn or (sn != tc and tc not in sn):
        return False
    if duration > 0 and song_duration > 0 and abs(song_duration - duration) > 8.0:
        return False
    sa = norm_core(song_artist or '')
    if ac and sa and ac not in sa and sa not in ac:
        return False
    return True


def cache_resolved_album(platform: str, album_name: str, singer: str, album_id: str, resolved_album_name: str) -> None:
    """将已由单曲反查或详情命中的专辑反向填充至 SEARCH_CACHE，供同专辑其他曲目直接命中。"""
    aid = str(album_id or '').strip()
    aname = str(resolved_album_name or album_name or '').strip()
    alb_raw = str(album_name or '').strip()
    art_raw = str(singer or '').strip()
    if not (platform and aid and alb_raw):
        return
    with CACHE_LOCK:
        if art_raw:
            k1 = (platform, norm_full(f'{alb_raw} {art_raw}'.strip()), norm_full(alb_raw), norm_core(art_raw))
            SEARCH_CACHE[k1] = (aid, aname, True)
        k2 = (platform, norm_full(alb_raw), norm_full(alb_raw), norm_core(art_raw))
        SEARCH_CACHE[k2] = (aid, aname, True)
        k3 = (platform, norm_full(alb_raw), norm_full(alb_raw), '')
        SEARCH_CACHE[k3] = (aid, aname, True)


class MusicProvider:
    """统一的在线音乐数据源接口基类。"""

    id: str = ''
    label: str = ''
    capabilities: frozenset[str] = frozenset()
    supports_album_search: bool = False
    _lyric_url_headers: dict[str, str] | None = None
    circuit_breaker: ProviderCircuitBreaker | None = None

    @property
    def is_available(self) -> bool:
        return self.circuit_breaker is None or not self.circuit_breaker.is_open()

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        return '', '', False

    def get_album_tracks(self, album_id: str) -> list[dict]:
        return []

    def search_songs(self, term: str) -> list[dict]:
        return []

    def search_artist(self, artist_name: str) -> str:
        """根据艺术家名字检索艺术家图片（头像/写真）的 URL，未找到返回空串。"""
        return ''

    def search_song_hits(self, term: str) -> list[dict]:
        """将单曲搜索结果规整为统一字段字典列表，供在线反查统一消费。"""
        return []

    def get_song_extra(self, song_id: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        return {}

    def get_lyrics_by_url(self, song_id: str, lrc_url: str) -> dict:
        url = str(lrc_url or '').strip()
        if not url:
            return {}

        def _load(_cid: str) -> dict:
            txt = http_get_text(url, headers=self._lyric_url_headers) or ''
            lyrics = txt.strip()
            return {'lyrics': lyrics, 'composer': parse_lyric_credits(lyrics) if lyrics else ''}

        return cached_song_meta(self.id, song_id or url, _load)


__all__ = [
    'ALBUM_CACHE', 'ALBUM_META_CACHE', 'ARTIST_BYTES_CACHE', 'ARTIST_PIC_CACHE', 'CACHE_LOCK', 'COVER_CACHE',
    'HTTPConnectionPool', 'INFLIGHT_ALBUM', 'INFLIGHT_ARTIST', 'INFLIGHT_SEARCH', 'INFLIGHT_SONG_SEARCH',
    'KUGOU_INFO_DONE', 'PKL_CACHE', 'SEARCH_CACHE', 'SONG_META_CACHE', 'SONG_SEARCH_CACHE',
    'MusicProvider', 'ProviderCircuitBreaker', 'TrackMatch', 'USER_AGENT',
    'build_tracks', 'bump_cache_gen', 'cache_resolved_album', 'cached_album', 'cached_artist_url', 'cached_call',
    'cached_search', 'cached_song_meta', 'cached_song_search', 'choose_album_detail',
    'extract_artist_names', 'first_artist', 'get_album_date', 'get_album_meta', 'get_cache_gen',
    'http_get_bytes', 'http_get_json', 'http_get_text', 'http_post_form', 'http_post_json',
    'is_artist_compatible', 'match_in_tracks', 'merge_lyrics',
    'norm_core', 'norm_full', 'norm_id', 'normalize_release_date',
    'parse_lyric_credits', 'reset_all_circuit_breakers', 'set_album_date', 'set_album_meta',
    'song_matches', 'time', 'to_seconds',
]
