"""多平台在线专辑音轨号检索与匹配（基于标准库 HTTP，零第三方网络依赖）。"""

from __future__ import annotations

import ast
import base64
import difflib
import html
import json
import os
import pickle
import re
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from art_repair import EMBED_MAX_SIDE, normalize_jpeg, sniff_image
from foobar_library import LibraryTrack

_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
_MB_UA = 'SongMetadata/1.0 (https://github.com/local/SongMetadata)'
_CN_TZ = timezone(timedelta(hours=8))

_CACHE_LOCK = threading.Lock()
_CACHE_GEN = 0
_ALBUM_CACHE: dict[tuple, list[dict]] = {}
_ALBUM_META_CACHE: dict[tuple[str, str], dict] = {}
_KUGOU_INFO_DONE: set[str] = set()
_SONG_META_CACHE: dict[tuple[str, str], dict] = {}
_SONG_SEARCH_CACHE: dict[tuple[str, str, str], list] = {}
_COVER_CACHE: dict[str, bytes] = {}
_SEARCH_CACHE: dict[tuple, tuple[str, str, bool]] = {}
_PKL_CACHE: dict[str, dict] = {}
_INFLIGHT_ALBUM: dict[tuple, threading.Event] = {}
_INFLIGHT_SEARCH: dict[tuple, threading.Event] = {}
_INFLIGHT_SONG_SEARCH: dict[tuple, threading.Event] = {}
_MB_LOCK = threading.Lock()
_MB_LAST = 0.0
_MB_CONSEC_FAILS = 0
_MB_CIRCUIT_OPEN = False
_MB_FAIL_THRESHOLD = 5

_TEMP_SUFFIX_RE = re.compile(r'^(.+)\.[a-z0-9_]{6,10}$', re.IGNORECASE)
_IDENT_RE = re.compile(r' - ([A-Za-z0-9]{5,32})(?: \(\d+\))?$')
_QQ_MID_RE = re.compile(r'^[0-9A-Za-z]{14}$')
_NETEASE_ID_RE = re.compile(r'^\d{5,15}$')
_FULL_ALBUM_RE = re.compile(r'(专辑全长|full\s*album)', re.IGNORECASE)
_BRACKET_RE = re.compile(r'[\(（\[【].*?[\)）\]】]')
_DATE_DIR_RE = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\s+')
_LRC_TS_RE = re.compile(r'^(?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+\s*')
_GENERIC_DIRS = {
    'music', 'downloads', 'cloudmusicdownload', 'vipsongsdownload', '已下载音乐',
    'applemusicclient', 'kugoumusicclient', 'kuwomusicclient', 'migumusicclient',
    'neteasemusicclient', 'qqmusicclient', 'spotifymusicclient',
}
_VA_ARTISTS = {'群星', 'variousartists', 'various', 'va', 'soundtrack', 'originalsoundtrack', 'ost'}
_COMPOSER_ROLES = {'作曲', '曲', '谱曲', 'composer', 'music', 'musicby', 'composedby'}
_PERFORMER_PLAIN_ROLES = {'演唱', '主唱', '歌手', '唱', '表演者', '演奏', 'rap', 'vocal', 'vocals', 'performer'}
_PERFORMER_INST_ROLES = {
    '和声', '合声', '和音', '伴唱', '独奏', '吉他', '电吉他', '木吉他', '古典吉他', '贝斯',
    '鼓', '打击乐', '键盘', '钢琴', '大提琴', '小提琴', '中提琴', '弦乐', '萨克斯', '小号',
    '长笛', '二胡', '古筝', '琵琶', '笛子', '口琴', '手风琴', 'guitar', 'bass', 'drums',
    'piano', 'keyboard', 'strings', 'violin', 'cello', 'saxophone',
}
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
    performer: str = ''
    genre: str = ''
    cover_url: str = ''


def clear_caches() -> None:
    global _CACHE_GEN, _MB_CONSEC_FAILS, _MB_CIRCUIT_OPEN
    with _CACHE_LOCK:
        _CACHE_GEN += 1
        _ALBUM_CACHE.clear()
        _ALBUM_META_CACHE.clear()
        _KUGOU_INFO_DONE.clear()
        _SONG_META_CACHE.clear()
        _SONG_SEARCH_CACHE.clear()
        _COVER_CACHE.clear()
        _SEARCH_CACHE.clear()
        _PKL_CACHE.clear()
    with _MB_LOCK:
        _MB_CONSEC_FAILS = 0
        _MB_CIRCUIT_OPEN = False


def _cached_call(cache: dict, inflight: dict, key: tuple, fn):
    """线程安全缓存读写，并对同 key 并发请求去重（含负缓存）。"""
    while True:
        with _CACHE_LOCK:
            if key in cache:
                return cache[key]
            ev = inflight.get(key)
            if ev is None:
                ev = threading.Event()
                inflight[key] = ev
                gen = _CACHE_GEN
                break
        ev.wait()
    val_ready = False
    val = None
    try:
        val = fn()
        val_ready = True
        return val
    finally:
        with _CACHE_LOCK:
            if val_ready and _CACHE_GEN == gen:
                cache[key] = val
            inflight.pop(key, None)
            ev.set()


def _http_get_text(url: str, params=None, headers=None, timeout: int = 8) -> str | None:
    try:
        full_url = f"{url}?{urllib.parse.urlencode(params)}" if params else url
        hdrs = {'User-Agent': _UA}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(full_url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception:
        return None


def _http_get_json(url: str, params=None, headers=None, timeout: int = 8):
    text = _http_get_text(url, params=params, headers=headers, timeout=timeout)
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _http_post_json(url: str, payload: dict, headers=None, timeout: int = 8):
    try:
        hdrs = {'User-Agent': _UA, 'Content-Type': 'application/json'}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=hdrs)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8', errors='replace'))
    except Exception:
        return None


def fetch_cover_bytes(cover_url: str, timeout: int = 15) -> bytes | None:
    """下载在线封面图并规范化为 JPEG 字节；内置 URL 内存缓存。"""
    url = str(cover_url or '').strip()
    if not url or not url.startswith('http'):
        return None
    with _CACHE_LOCK:
        if url in _COVER_CACHE:
            return _COVER_CACHE[url]
    try:
        req = urllib.request.Request(url, headers={'User-Agent': _UA})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw or not sniff_image(raw):
            return None
        jpg = normalize_jpeg(raw, EMBED_MAX_SIDE) or raw
        with _CACHE_LOCK:
            _COVER_CACHE[url] = jpg
        return jpg
    except Exception:
        return None


def _to_seconds(value) -> float:
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


def _norm_full(v: str) -> str:
    return re.sub(r'[^\w]+', '', unicodedata.normalize('NFKC', html.unescape(str(v or ''))).lower())


def _norm_core(v: str) -> str:
    return re.sub(r'[^\w]+', '', _BRACKET_RE.sub('', unicodedata.normalize('NFKC', html.unescape(str(v or ''))).lower()))


def _norm_id(v: str) -> str:
    return str(v or '').strip().upper().replace('MUSIC_', '').replace('MUSIC:', '')


def _first_artist(artist: str) -> str:
    return re.split(r'\s*[,/、&;]\s*', str(artist or '').strip())[0].strip() if artist else ''


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
            dt = datetime.fromtimestamp(sec, tz=_CN_TZ)
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


def _set_album_meta(platform: str, album_id: str, **fields: str) -> dict:
    aid = str(album_id or '')
    if not aid:
        return {}
    key = (platform, aid)
    with _CACHE_LOCK:
        cur = dict(_ALBUM_META_CACHE.get(key) or {})
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
        _ALBUM_META_CACHE[key] = cur
        return dict(cur)


def _get_album_meta(platform: str, album_id: str) -> dict:
    if not album_id:
        return {}
    with _CACHE_LOCK:
        return dict(_ALBUM_META_CACHE.get((platform, str(album_id))) or {})


def _set_album_date(platform: str, album_id: str, date_str: str) -> str:
    return _set_album_meta(platform, album_id, release_date=date_str).get('release_date', '')


def _get_album_date(platform: str, album_id: str) -> str:
    return _get_album_meta(platform, album_id).get('release_date', '')


def _parse_lyric_credits(lrc_text: str) -> tuple[str, str]:
    """从歌词头部制作人员行提取 (composer, performer)。"""
    composers: list[str] = []
    performers: list[str] = []
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
        elif role_key in _PERFORMER_PLAIN_ROLES:
            for nm in names:
                if nm not in performers:
                    performers.append(nm)
        elif role_key in _PERFORMER_INST_ROLES or (
            any(tok in role_clean for tok in ('吉他', '贝斯', '鼓', '钢琴', '提琴', '演奏', '演唱', '主唱'))
            and not any(bad in role_clean for bad in ('编写', '制作', '监制', '录音', '混音', '母带', '助理'))
        ):
            for nm in names:
                entry = f'{nm} ({role_clean})'
                if entry not in performers:
                    performers.append(entry)
    return ', '.join(composers), ', '.join(performers)


def _fetch_netease_song_credits(song_id: str) -> dict:
    sid = str(song_id or '').strip()
    if not sid:
        return {}
    key = ('netease', sid)
    with _CACHE_LOCK:
        if key in _SONG_META_CACHE:
            return dict(_SONG_META_CACHE[key])
    d = _http_get_json('https://music.163.com/api/song/lyric', params={'id': sid, 'lv': 1, 'tv': 1}, headers={'Referer': 'https://music.163.com/'})
    lrc = ((d or {}).get('lrc') or {}).get('lyric') if isinstance(d, dict) else ''
    comp, perf = _parse_lyric_credits(lrc or '')
    res = {'composer': comp, 'performer': perf}
    with _CACHE_LOCK:
        _SONG_META_CACHE[key] = res
    return dict(res)


def _fetch_qq_song_extra(song_mid: str) -> dict:
    smid = str(song_mid or '').strip()
    if not smid:
        return {}
    key = ('qq', smid)
    with _CACHE_LOCK:
        if key in _SONG_META_CACHE:
            return dict(_SONG_META_CACHE[key])

    comp, perf, genre, pub_date = '', '', '', ''
    ql = _http_get_json(
        'https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg',
        params={'songmid': smid, 'g_tk': '5381', 'format': 'json', 'inCharset': 'utf8', 'outCharset': 'utf-8'},
        headers={'Referer': 'https://y.qq.com/portal/player.html'},
    )
    b64 = (ql or {}).get('lyric') if isinstance(ql, dict) else ''
    if b64:
        try:
            comp, perf = _parse_lyric_credits(base64.b64decode(b64).decode('utf-8', errors='replace'))
        except Exception:
            pass

    qd = _http_post_json(
        'https://u.y.qq.com/cgi-bin/musicu.fcg',
        {'songinfo': {'method': 'get_song_detail_yqq', 'module': 'music.pf_song_detail_svr', 'param': {'song_mid': smid}}},
        headers={'Referer': 'https://y.qq.com/'},
    )
    info = ((qd or {}).get('songinfo') or {}).get('data', {}).get('info') or {}
    if isinstance(info, dict):
        genre_items = [str(x.get('value') or '').strip() for x in (info.get('genre') or []) if isinstance(x, dict) and x.get('value')]
        genre = ', '.join(dict.fromkeys(genre_items))
        intro_items = [str(x.get('value') or '').strip() for x in (info.get('intro') or []) if isinstance(x, dict) and x.get('value')]
        intro_text = intro_items[0] if intro_items else ''
        pub_items = [str(x.get('value') or '').strip() for x in (info.get('pub_time') or []) if isinstance(x, dict) and x.get('value')]
        pub_date = normalize_release_date(pub_items[0]) if pub_items else ''
        if not comp and intro_text:
            m_comp = re.search(r'由.*?[、，,]\s*([^、，,。；\s]+)谱曲', intro_text) or re.search(r'([^、，,。；\s]+)谱曲', intro_text)
            if m_comp:
                comp = m_comp.group(1).strip()

    res = {'composer': comp, 'performer': perf, 'genre': genre, 'release_date': pub_date}
    with _CACHE_LOCK:
        _SONG_META_CACHE[key] = res
    return dict(res)


def _extract_pkl_metadata(pkl: dict) -> dict:
    """从 musicdl .pkl 缓存中提取发布日期、封面、艺术家及歌词制作人员等可用字段。"""
    out = {'release_date': _extract_pkl_release_date(pkl), 'cover_url': '', 'title': '', 'artist': '', 'album': '', 'composer': '', 'performer': ''}
    if not isinstance(pkl, dict):
        return out
    cov = str(pkl.get('cover_url') or '').strip()
    if cov.startswith('http'):
        out['cover_url'] = cov
    for dst, src_k in (('title', 'song_name'), ('artist', 'singers'), ('album', 'album')):
        v = str(pkl.get(src_k) or '').strip()
        if v and v.lower() not in {'none', 'null'}:
            out[dst] = v
    lrc = str(pkl.get('lyric') or '')
    if not lrc and isinstance(pkl.get('raw_data'), dict):
        raw_lrc = pkl['raw_data'].get('lyric')
        if isinstance(raw_lrc, dict):
            lrc = str(raw_lrc.get('lrc') or raw_lrc.get('lyric') or '')
    if lrc:
        out['composer'], out['performer'] = _parse_lyric_credits(lrc)
    return out


def _extract_pkl_release_date(pkl: dict) -> str:
    """从 musicdl 保存的 .pkl 缓存结构中提取可能存在的发布日期。"""
    if not isinstance(pkl, dict):
        return ''
    raw = pkl.get('raw_data') if isinstance(pkl.get('raw_data'), dict) else {}
    sd = raw.get('search') if isinstance(raw.get('search'), dict) else {}
    dl = raw.get('download') if isinstance(raw.get('download'), dict) else {}
    src = str(pkl.get('source') or '')
    root_src = str(pkl.get('root_source') or '')

    if 'Netease' in src or root_src == 'netease':
        for cand in (
            (sd.get('al') or {}).get('publishTime') if isinstance(sd.get('al'), dict) else None,
            (sd.get('album') or {}).get('publishTime') if isinstance(sd.get('album'), dict) else None,
            sd.get('publishTime'),
            (((dl.get('songDetail') or {}).get('data') or {}).get('al') or {}).get('publishTime')
            if isinstance(dl.get('songDetail'), dict) else None,
        ):
            r = normalize_release_date(cand, is_ms=True)
            if r:
                return r

    if 'QQ' in src or root_src == 'tencent':
        for cand in (
            sd.get('time_public'),
            (sd.get('album') or {}).get('time_public') if isinstance(sd.get('album'), dict) else None,
            sd.get('pubtime'),
            (dl.get('detail') or {}).get('time_public') if isinstance(dl.get('detail'), dict) else None,
        ):
            r = normalize_release_date(cand)
            if r:
                return r

    if 'Kugou' in src:
        for cand in (sd.get('PublishDate'), sd.get('PublishTime'), sd.get('publishtime')):
            r = normalize_release_date(cand)
            if r:
                return r

    if 'Apple' in src or root_src == 'apple':
        attrs = sd.get('attributes') if isinstance(sd.get('attributes'), dict) else {}
        r = normalize_release_date(attrs.get('releaseDate'))
        if r:
            return r
        dl_data = dl.get('data') if isinstance(dl.get('data'), list) and dl.get('data') else []
        if dl_data and isinstance(dl_data[0], dict):
            r = normalize_release_date((dl_data[0].get('attributes') or {}).get('releaseDate'))
            if r:
                return r

    if 'Spotify' in src:
        item_data = (sd.get('item') or {}).get('data') or (sd.get('itemV2') or {}).get('data') or {}
        alb = item_data.get('albumOfTrack') if isinstance(item_data, dict) else {}
        r = normalize_release_date((alb or {}).get('date'))
        if r:
            return r

    containers = [sd]
    for sub in ('album', 'al', 'attributes'):
        if isinstance(sd.get(sub), dict):
            containers.append(sd[sub])
    containers.append(dl)
    for sub in ('data', 'metadata', 'track_meta', 'detail'):
        if isinstance(dl.get(sub), dict):
            containers.append(dl[sub])
            if isinstance(dl[sub].get('data'), dict):
                containers.append(dl[sub]['data'])

    for box in containers:
        for key, ms_flag in (
            ('releaseDate', False), ('release_date', False), ('release_date_original', False),
            ('time_public', False), ('publishtime', False), ('PublishDate', False),
            ('PublishTime', False), ('aDate', False), ('pub', False), ('publishDate', False),
            ('publishTime', True), ('date', False),
        ):
            if box.get(key) not in (None, ''):
                r = normalize_release_date(box.get(key), is_ms=ms_flag)
                if r:
                    return r
    return ''


# ==================== 本地 pkl 与文件名线索 ====================
def _load_pkl_index(directory: str) -> dict:
    with _CACHE_LOCK:
        if directory in _PKL_CACHE:
            return _PKL_CACHE[directory]
    idx: dict = {}
    for name in ('search_results.pkl', 'download_results.pkl'):
        p = os.path.join(directory, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'rb') as fp:
                items = pickle.load(fp)
        except Exception:
            continue
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                if it.get('identifier'):
                    idx.setdefault(str(it['identifier']).strip(), it)
                if it.get('song_name'):
                    idx.setdefault('name:' + str(it['song_name']).strip(), it)
    with _CACHE_LOCK:
        _PKL_CACHE[directory] = idx
    return idx


def _clean_stem(filename: str) -> str:
    stem = os.path.splitext(os.path.basename(filename))[0]
    m = _TEMP_SUFFIX_RE.match(stem)
    return m.group(1) if m else stem


def _parse_filename_clues(track: LibraryTrack) -> dict:
    stem = _clean_stem(track.filename)
    m = _IDENT_RE.search(stem)
    ident, part = (m.group(1), stem[:m.start()].strip()) if m else ('', stem)
    title, artist, album = '', '', ''
    bm = re.match(r'^(.+?)\s*-\s*([^\[\]]+?)\[([^\[\]]+)\]$', part)
    if bm:
        title, artist, album = bm.group(1).strip(), bm.group(2).strip(), bm.group(3).strip().replace('~', '/')
    elif not ident and ' - ' in part:
        left, right = part.split(' - ', 1)
        if re.match(r'^\d{1,3}$', left.strip()):
            title = right.strip()
        else:
            artist, title = left.strip(), right.strip()
    else:
        title = part

    if not album:
        dn = os.path.basename(track.folder).strip()
        if dn and dn.lower() not in _GENERIC_DIRS:
            cd = _DATE_DIR_RE.sub('', dn).strip()
            if cd and cd.lower() not in _GENERIC_DIRS:
                album = cd
    return {'identifier': ident, 'inferred_title': title, 'inferred_artist': artist, 'inferred_album': album}


def _find_pkl_item(track: LibraryTrack, ident: str) -> dict | None:
    idx = _load_pkl_index(track.folder)
    if not idx:
        return None
    if ident and ident in idx:
        return idx[ident]
    stem = _clean_stem(track.filename)
    if ' - ' in stem and idx.get('name:' + stem.rsplit(' - ', 1)[0]):
        return idx['name:' + stem.rsplit(' - ', 1)[0]]
    return idx.get('name:' + track.title) if track.title else None


# ==================== 各平台专辑曲目表 ====================
def _format_mb_artist_credit(credits) -> str:
    if not isinstance(credits, list):
        return ''
    parts = []
    for ac in credits:
        if not isinstance(ac, dict):
            continue
        name = str(ac.get('name') or (ac.get('artist') or {}).get('name') or '').strip()
        join = str(ac.get('joinphrase') or '')
        if name:
            parts.append(name + join)
    return ''.join(parts).strip()


def _itunes_cover_url(url: str, size: int = 1200) -> str:
    s = str(url or '').strip()
    if not s:
        return ''
    return re.sub(r'/\d+x\d+bb\.(jpg|png)$', f'/{size}x{size}bb.jpg', s)


def _build_tracks(items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, it in enumerate((x for x in (items or []) if isinstance(x, dict)), 1):
        rec = {
            'number': i,
            'disc': int(it.get('disc') or 1),
            'id': it.get('id'),
            'name': str(it.get('name') or ''),
            'duration': float(it.get('duration') or 0.0),
        }
        for opt_k in ('artist', 'composer', 'performer', 'genre', 'cover_url'):
            if it.get(opt_k):
                rec[opt_k] = str(it[opt_k]).strip()
        out.append(rec)
    return out


def _build_netease_tracks(songs: list[dict]) -> list[dict]:
    raw = [s for s in (songs or []) if isinstance(s, dict)]
    out: list[dict] = []
    for idx, s in enumerate(raw):
        name, dur, no = str(s.get('name') or ''), _to_seconds(s.get('dt') or s.get('duration')), int(s.get('no') or 0)
        if _FULL_ALBUM_RE.search(name) and dur >= 1200.0:
            if no in (0, 1) or any(int(o.get('no') or 0) == 1 for o in raw[idx + 1:idx + 3]):
                continue
        cd = str(s.get('cd') or '1').strip()
        artists = ', '.join(str(a.get('name') or '').strip() for a in (s.get('ar') or s.get('artists') or []) if isinstance(a, dict) and a.get('name'))
        out.append({'id': s.get('id'), 'name': name, 'duration': dur, 'disc': int(cd) if cd.isdigit() else 1, 'artist': artists})
    return _build_tracks(out)


def _cached_album(platform: str, album_id: str, fetcher) -> list[dict]:
    aid = str(album_id or '')
    if not aid:
        return []
    return _cached_call(_ALBUM_CACHE, _INFLIGHT_ALBUM, (platform, aid), lambda: fetcher(aid))


def _netease_album_tracks(album_id: str) -> list[dict]:
    def _fetch(aid: str) -> list[dict]:
        for url in (f'https://music.163.com/api/v1/album/{aid}', f'https://music.163.com/api/album/{aid}'):
            d = _http_get_json(url, headers={'Referer': 'https://music.163.com/'})
            if isinstance(d, dict):
                alb_obj = d.get('album') if isinstance(d.get('album'), dict) else {}
                aa = ', '.join(str(a.get('name') or '').strip() for a in (alb_obj.get('artists') or []) if isinstance(a, dict) and a.get('name'))
                if not aa and isinstance(alb_obj.get('artist'), dict):
                    aa = str(alb_obj['artist'].get('name') or '').strip()
                _set_album_meta(
                    'netease', aid,
                    release_date=normalize_release_date(alb_obj.get('publishTime') or d.get('publishTime'), is_ms=True),
                    album_name=str(alb_obj.get('name') or ''),
                    albumartist=aa,
                    cover_url=str(alb_obj.get('picUrl') or ''),
                )
                songs = d.get('songs') or (alb_obj.get('songs') or [])
                if songs:
                    return _build_netease_tracks(songs)
        return []
    return _cached_album('netease', album_id, _fetch)


def _qq_album_tracks(album_mid: str) -> list[dict]:
    def _fetch(amid: str) -> list[dict]:
        d = _http_get_json(
            'https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg',
            params={'albummid': amid, 'g_tk': '5381', 'format': 'json', 'inCharset': 'utf8', 'outCharset': 'utf-8', 'notice': '0', 'platform': 'yqq.json', 'needNewCode': '0'},
            headers={'Referer': 'https://y.qq.com/'},
        )
        data = (d or {}).get('data') or {}
        if isinstance(data, dict):
            _set_album_meta(
                'qq', amid,
                release_date=normalize_release_date(data.get('aDate') or data.get('publicTime') or data.get('time_public')),
                album_name=str(data.get('name') or ''),
                albumartist=str(data.get('singername') or ''),
                genre=str(data.get('genre') or ''),
                cover_url=f'https://y.gtimg.cn/music/photo_new/T002R800x800M000{amid}.jpg',
            )
        items = data.get('list') or [] if isinstance(data, dict) else []
        return _build_tracks([
            {
                'id': x.get('songmid'),
                'name': x.get('songname'),
                'duration': _to_seconds(x.get('interval')),
                'disc': int(x.get('cdIdx') or 0) + 1,
                'artist': ', '.join(str(a.get('name') or '').strip() for a in (x.get('singer') or []) if isinstance(a, dict) and a.get('name')),
            }
            for x in items if isinstance(x, dict)
        ])
    return _cached_album('qq', album_mid, _fetch)


def _itunes_album_tracks(cid: str) -> list[dict]:
    def _fetch(col_id: str) -> list[dict]:
        d = _http_get_json('https://itunes.apple.com/lookup', params={'id': col_id, 'entity': 'song', 'limit': 250})
        all_res = [x for x in ((d or {}).get('results') or []) if isinstance(x, dict)]
        col = next((x for x in all_res if x.get('wrapperType') == 'collection'), None)
        res = [x for x in all_res if x.get('wrapperType') == 'track']
        _set_album_meta(
            'itunes', col_id,
            release_date=normalize_release_date((col or {}).get('releaseDate') or (res[0].get('releaseDate') if res else '')),
            album_name=str((col or {}).get('collectionName') or (res[0].get('collectionName') if res else '')),
            albumartist=str((col or {}).get('collectionArtistName') or (col or {}).get('artistName') or ''),
            genre=str((col or {}).get('primaryGenreName') or (res[0].get('primaryGenreName') if res else '')),
            cover_url=_itunes_cover_url((col or {}).get('artworkUrl100') or (res[0].get('artworkUrl100') if res else '')),
        )
        res.sort(key=lambda x: (int(x.get('discNumber') or 1), int(x.get('trackNumber') or 0)))
        return _build_tracks([
            {
                'id': x.get('trackId'),
                'name': x.get('trackName'),
                'duration': _to_seconds(x.get('trackTimeMillis')),
                'disc': int(x.get('discNumber') or 1),
                'artist': str(x.get('artistName') or ''),
                'genre': str(x.get('primaryGenreName') or ''),
            }
            for x in res
        ])
    return _cached_album('itunes', cid, _fetch)


def _kuwo_album_tracks(album_id: str) -> list[dict]:
    def _fetch(aid: str) -> list[dict]:
        txt = _http_get_text('http://search.kuwo.cn/r.s', params={'stype': 'albuminfo', 'albumid': aid, 'vipver': '1', 'encoding': 'utf8', 'rformat': 'json'})
        if not txt:
            return []
        try:
            data = ast.literal_eval(txt.strip())
            if isinstance(data, dict):
                _set_album_meta(
                    'kuwo', aid,
                    release_date=normalize_release_date(data.get('pub') or data.get('releaseDate')),
                    album_name=str(data.get('name') or ''),
                    albumartist=str(data.get('artist') or ''),
                    cover_url=str(data.get('hts_img') or data.get('img') or ''),
                )
            items = (data.get('musiclist') or []) if isinstance(data, dict) else []
            return _build_tracks([
                {'id': x.get('musicrid'), 'name': x.get('songname'), 'duration': _to_seconds(x.get('duration')), 'artist': str(x.get('artist') or '')}
                for x in items if isinstance(x, dict)
            ])
        except Exception:
            return []
    return _cached_album('kuwo', album_id, _fetch)


def _kugou_album_info(album_id: str) -> dict:
    aid = str(album_id or '')
    if not aid:
        return {}
    meta = _get_album_meta('kugou', aid)
    with _CACHE_LOCK:
        if aid in _KUGOU_INFO_DONE or (meta.get('release_date') and meta.get('cover_url')):
            return meta
    d = _http_get_json('http://mobilecdnbj.kugou.com/api/v3/album/info', params={'albumid': aid}, headers={'Referer': 'https://www.kugou.com/'})
    info = (d or {}).get('data') if isinstance(d, dict) else {}
    with _CACHE_LOCK:
        _KUGOU_INFO_DONE.add(aid)
    if isinstance(info, dict):
        img = str(info.get('imgurl') or '').replace('{size}', '400')
        return _set_album_meta(
            'kugou', aid,
            release_date=normalize_release_date(info.get('publishtime') or ''),
            album_name=str(info.get('albumname') or ''),
            albumartist=str(info.get('singername') or ''),
            cover_url=img,
        )
    return meta


def _kugou_album_date(album_id: str) -> str:
    return _kugou_album_info(album_id).get('release_date', '')


def _kugou_album_tracks(album_id: str) -> list[dict]:
    def _fetch(aid: str) -> list[dict]:
        items, page = [], 1
        while page <= 20:
            d = _http_get_json('http://mobilecdnbj.kugou.com/api/v3/album/song', params={'albumid': aid, 'page': page, 'pagesize': 100, 'version': '9108', 'plat': '0'}, headers={'Referer': 'https://www.kugou.com/'})
            pl = (d or {}).get('data') or {}
            info = pl.get('info') or []
            for x in info:
                if isinstance(x, dict):
                    fn = x.get('filename') or ''
                    name = fn.split(' - ', 1)[-1] if ' - ' in fn else (x.get('songname') or fn)
                    art = fn.split(' - ', 1)[0].strip() if ' - ' in fn else str(x.get('singername') or '')
                    items.append({'id': x.get('hash'), 'name': name, 'duration': _to_seconds(x.get('duration')), 'artist': art})
            total = int(pl.get('total') or 0)
            page += 1
            if not info or len(info) < 100 or (total and len(items) >= total):
                break
        return _build_tracks(items)
    return _cached_album('kugou', album_id, _fetch)


def _spotify_album_tracks(album_id: str) -> list[dict]:
    def _fetch(aid: str) -> list[dict]:
        txt = _http_get_text(f'https://open.spotify.com/embed/album/{aid}')
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt or '', re.S)
        if not m:
            return []
        try:
            ent = json.loads(m.group(1))['props']['pageProps']['state']['data']['entity']
            _set_album_meta(
                'spotify', aid,
                release_date=normalize_release_date(ent.get('releaseDate') or ent.get('date')),
                album_name=str(ent.get('title') or ent.get('name') or ''),
                albumartist=str(ent.get('subtitle') or ''),
            )
            return _build_tracks([
                {'id': (t.get('uri') or '').rsplit(':', 1)[-1], 'name': t.get('name') or t.get('title') or '', 'duration': _to_seconds((t.get('duration') or {}).get('totalMilliseconds'))}
                for t in (ent.get('trackList') or [])
            ])
        except Exception:
            return []
    return _cached_album('spotify', album_id, _fetch)


def _mb_get(url: str, params=None):
    global _MB_LAST, _MB_CONSEC_FAILS, _MB_CIRCUIT_OPEN
    with _MB_LOCK:
        if _MB_CIRCUIT_OPEN:
            return None
        wait = 1.05 - (time.time() - _MB_LAST)
        if wait > 0:
            time.sleep(wait)
        _MB_LAST = time.time()
    res = _http_get_json(url, params=params, headers={'User-Agent': _MB_UA}, timeout=10)
    with _MB_LOCK:
        if res is None:
            _MB_CONSEC_FAILS += 1
            if _MB_CONSEC_FAILS >= _MB_FAIL_THRESHOLD:
                _MB_CIRCUIT_OPEN = True
        else:
            _MB_CONSEC_FAILS = 0
    return res


def _extract_mb_recording_credits(rec: dict) -> tuple[str, str, str]:
    composers: list[str] = []
    performers: list[str] = []
    for rel in (rec.get('relations') or []):
        if not isinstance(rel, dict):
            continue
        rtype = str(rel.get('type') or '').lower()
        if rel.get('target-type') == 'work':
            work = rel.get('work') or {}
            for wrel in (work.get('relations') or []):
                if not isinstance(wrel, dict):
                    continue
                wrtype = str(wrel.get('type') or '').lower()
                if wrtype in {'composer', 'writer'}:
                    aname = str((wrel.get('artist') or {}).get('name') or '').strip()
                    if aname and aname not in composers:
                        composers.append(aname)
        elif rtype in {'instrument', 'vocal', 'performer'}:
            aname = str((rel.get('artist') or {}).get('name') or '').strip()
            if not aname:
                continue
            attrs = [str(a).strip() for a in (rel.get('attributes') or []) if str(a).strip()]
            entry = f'{aname} ({", ".join(attrs)})' if attrs else aname
            if entry not in performers:
                performers.append(entry)
    genres = sorted([g for g in (rec.get('genres') or []) if isinstance(g, dict) and g.get('name')], key=lambda g: -int(g.get('count') or 0))
    genre = str(genres[0]['name']).strip() if genres else ''
    return ', '.join(composers), ', '.join(performers), genre


def _musicbrainz_album_tracks(release_id: str) -> list[dict]:
    def _fetch(rid: str) -> list[dict]:
        d = _mb_get(
            f'https://musicbrainz.org/ws/2/release/{rid}',
            params={'inc': 'recordings+artist-credits+genres+recording-level-rels+work-rels+work-level-rels+artist-rels', 'fmt': 'json'},
        )
        if isinstance(d, dict):
            ev_date = next((ev.get('date') for ev in (d.get('release-events') or []) if isinstance(ev, dict) and ev.get('date')), '')
            genres = sorted([g for g in (d.get('genres') or []) if isinstance(g, dict) and g.get('name')], key=lambda g: -int(g.get('count') or 0))
            caa = d.get('cover-art-archive') if isinstance(d.get('cover-art-archive'), dict) else {}
            cov_url = f'https://coverartarchive.org/release/{rid}/front-1200' if caa.get('front') else ''
            _set_album_meta(
                'mb', rid,
                release_date=normalize_release_date(d.get('date') or ev_date),
                album_name=str(d.get('title') or ''),
                albumartist=_format_mb_artist_credit(d.get('artist-credit')),
                genre=str(genres[0]['name']).strip() if genres else '',
                cover_url=cov_url,
            )
        items = []
        for disc_idx, med in enumerate((d or {}).get('media') or [], 1):
            for tr in (med.get('tracks') or []):
                rec = tr.get('recording') if isinstance(tr.get('recording'), dict) else {}
                comp, perf, rgenre = _extract_mb_recording_credits(rec)
                art = _format_mb_artist_credit(tr.get('artist-credit') or rec.get('artist-credit'))
                items.append({
                    'id': tr.get('id'),
                    'name': tr.get('title') or rec.get('title') or '',
                    'duration': _to_seconds(tr.get('length')),
                    'disc': disc_idx,
                    'artist': art,
                    'composer': comp,
                    'performer': perf,
                    'genre': rgenre,
                })
        return _build_tracks(items)
    return _cached_album('mb', release_id, _fetch)


# ==================== 搜索与曲目匹配 ====================
def _is_artist_compatible(album_artist: str, singer: str) -> bool:
    sk = _norm_core(singer)
    if not sk:
        return True
    ca = _norm_core(album_artist)
    if not ca:
        return False
    return ca == sk or (sk in ca) or (ca in sk) or (ca in _VA_ARTISTS)


def _choose_album_detail(
    candidates: list[dict], album_name: str, singer: str, name_fn, artist_fn,
) -> tuple[dict | None, bool]:
    tc, tf, sk = _norm_core(album_name), _norm_full(album_name), _norm_core(singer)
    if not (tc or tf) or not candidates:
        return None, False
    best, best_score, best_artist_ok = None, 0, False
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        raw = name_fn(cand) or ''
        cf, cc, raw_artist = _norm_full(raw), _norm_core(raw), artist_fn(cand) or ''
        ca = _norm_core(raw_artist)
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


def _choose_album(candidates: list[dict], album_name: str, singer: str, name_fn, artist_fn):
    cand, _ = _choose_album_detail(candidates, album_name, singer, name_fn, artist_fn)
    return cand


def _cached_search(platform: str, term: str, album_name: str, singer: str, fn) -> tuple[str, str, bool]:
    key = (platform, _norm_full(term), _norm_full(album_name), _norm_core(singer))
    return _cached_call(_SEARCH_CACHE, _INFLIGHT_SEARCH, key, fn)


def _cached_song_search(platform: str, scope: str, term: str, fn) -> list:
    key = (platform, scope, _norm_full(term))
    return _cached_call(_SONG_SEARCH_CACHE, _INFLIGHT_SONG_SEARCH, key, fn)


def _search_netease_album(term: str, alb: str, singer: str) -> tuple[str, str, bool]:
    def _do():
        d = _http_get_json('https://music.163.com/api/search/get', params={'s': term, 'type': 10, 'limit': 10, 'offset': 0}, headers={'Referer': 'https://music.163.com/'})
        a, artist_ok = _choose_album_detail(((d or {}).get('result') or {}).get('albums') or [], alb, singer, lambda x: x.get('name'), lambda x: (x.get('artist') or {}).get('name'))
        if a and a.get('id'):
            aid = str(a['id'])
            _set_album_date('netease', aid, normalize_release_date(a.get('publishTime'), is_ms=True))
            return aid, str(a.get('name') or alb), artist_ok
        return '', '', False
    return _cached_search('netease_alb', term, alb, singer, _do)


def _search_qq_album(term: str, alb: str, singer: str) -> tuple[str, str, bool]:
    def _do():
        d = _http_get_json('https://c.y.qq.com/soso/fcgi-bin/client_search_cp', params={'w': term, 'format': 'json', 'p': 1, 'n': 10, 't': 8}, headers={'Referer': 'https://y.qq.com/'})
        a, artist_ok = _choose_album_detail((((d or {}).get('data') or {}).get('album') or {}).get('list') or [], alb, singer, lambda x: x.get('albumName'), lambda x: x.get('singerName'))
        if a and a.get('albumMID'):
            amid = str(a['albumMID'])
            _set_album_date('qq', amid, normalize_release_date(a.get('publicTime') or a.get('time_public')))
            return amid, str(a.get('albumName') or alb), artist_ok
        return '', '', False
    return _cached_search('qq_alb', term, alb, singer, _do)


def _search_itunes_album(term: str, alb: str, singer: str) -> tuple[str, str, bool]:
    def _do():
        for country in ('CN', 'US'):
            d = _http_get_json('https://itunes.apple.com/search', params={'term': term, 'entity': 'album', 'limit': 10, 'country': country})
            a, artist_ok = _choose_album_detail((d or {}).get('results') or [], alb, singer, lambda x: x.get('collectionName'), lambda x: x.get('artistName'))
            if a and a.get('collectionId'):
                cid = str(a['collectionId'])
                _set_album_date('itunes', cid, normalize_release_date(a.get('releaseDate')))
                return cid, str(a.get('collectionName') or alb), artist_ok
        return '', '', False
    return _cached_search('itunes_alb', term, alb, singer, _do)


def _search_musicbrainz_album(alb: str, singer: str) -> tuple[str, str, bool]:
    def _do():
        q = f'release:"{alb}"' + (f' AND artist:"{singer}"' if singer else '')
        d = _mb_get('https://musicbrainz.org/ws/2/release/', params={'query': q, 'limit': 8, 'fmt': 'json'})
        b, artist_ok = _choose_album_detail(
            (d or {}).get('releases') or [], alb, singer, lambda x: x.get('title'),
            lambda x: ' '.join((ac.get('name') or (ac.get('artist') or {}).get('name') or '') for ac in (x.get('artist-credit') or []) if isinstance(ac, dict)),
        )
        if b and b.get('id'):
            rid = str(b['id'])
            _set_album_date('mb', rid, normalize_release_date(b.get('date')))
            return rid, str(b.get('title') or alb), artist_ok
        return '', '', False
    return _cached_search('mb_alb', alb, alb, singer, _do)


def _match_in_tracks(tracks: list[dict], title: str, duration: float, identifier: str = '') -> dict | None:
    if not tracks:
        return None
    nid = _norm_id(identifier)
    if nid:
        for tr in tracks:
            if tr.get('id') and _norm_id(tr['id']) == nid:
                return tr
    tf, tc = _norm_full(title), _norm_core(title)
    if not (tf or tc):
        return None

    cands = [tr for tr in tracks if _norm_full(tr.get('name')) == tf] if tf else []
    if not cands and tc:
        cands = [tr for tr in tracks if _norm_core(tr.get('name')) == tc]
    if not cands and tc and len(tc) >= 2:
        cands = [tr for tr in tracks if _norm_core(tr.get('name')) and (tc in _norm_core(tr.get('name')) or _norm_core(tr.get('name')) in tc)]
    if not cands and tc:
        scored = sorted(
            ((difflib.SequenceMatcher(None, tc, _norm_core(tr.get('name'))).ratio(), tr)
             for tr in tracks if _norm_core(tr.get('name'))),
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


def _song_matches(song_name: str, song_duration: float, song_artist: str, tc: str, duration: float, ac: str) -> bool:
    sn = _norm_core(song_name or '')
    if not sn or (sn != tc and tc not in sn):
        return False
    if duration > 0 and song_duration > 0 and abs(song_duration - duration) > 8.0:
        return False
    sa = _norm_core(song_artist or '')
    if ac and sa and ac not in sa and sa not in ac:
        return False
    return True


_TEXT_MERGE_FIELDS = (
    'album_name', 'release_date', 'title', 'artist', 'albumartist',
    'composer', 'performer', 'genre', 'cover_url',
)


def _make_match(
    plat: str, album_id: str, m: dict | None, total: int | None, source: str, album_name: str = '', **overrides,
) -> TrackMatch:
    alb_meta = _get_album_meta(plat, album_id) if (plat and album_id) else {}
    tr = m or {}
    match = TrackMatch(
        tracknumber=tr.get('number'),
        discnumber=tr.get('disc'),
        totaltracks=total,
        source=source,
        album_name=str(overrides.get('album_name') or album_name or alb_meta.get('album_name') or ''),
        release_date=str(overrides.get('release_date') or alb_meta.get('release_date') or ''),
        title=str(overrides.get('title') or tr.get('name') or ''),
        artist=str(overrides.get('artist') or tr.get('artist') or ''),
        albumartist=str(overrides.get('albumartist') or alb_meta.get('albumartist') or ''),
        composer=str(overrides.get('composer') or tr.get('composer') or ''),
        performer=str(overrides.get('performer') or tr.get('performer') or ''),
        genre=str(overrides.get('genre') or tr.get('genre') or alb_meta.get('genre') or ''),
        cover_url=str(overrides.get('cover_url') or tr.get('cover_url') or alb_meta.get('cover_url') or ''),
    )
    return match


def _merge_match(dst: TrackMatch, src: TrackMatch | None) -> TrackMatch:
    if src is None:
        return dst
    if dst.tracknumber is None and src.tracknumber is not None:
        dst.tracknumber = src.tracknumber
        if dst.totaltracks is None:
            dst.totaltracks = src.totaltracks
    if dst.discnumber is None and src.discnumber is not None:
        dst.discnumber = src.discnumber
    for k in _TEXT_MERGE_FIELDS:
        if not getattr(dst, k) and getattr(src, k):
            setattr(dst, k, getattr(src, k))
    if not dst.source and src.source:
        dst.source = src.source
    return dst


def _enrich_song_credits(match: TrackMatch, plat: str, song_id: str, track: LibraryTrack, *, need_extra: bool) -> TrackMatch:
    if not need_extra or not song_id:
        return match
    need_comp = not (track.composer or match.composer)
    need_perf = not (track.performer or match.performer)
    need_genre = not (track.genre or match.genre)
    if plat == 'netease' and (need_comp or need_perf):
        cr = _fetch_netease_song_credits(song_id)
        if not match.composer and cr.get('composer'):
            match.composer = cr['composer']
        if not match.performer and cr.get('performer'):
            match.performer = cr['performer']
    elif plat == 'qq' and (need_comp or need_perf or need_genre):
        ex = _fetch_qq_song_extra(song_id)
        for k in ('composer', 'performer', 'genre', 'release_date'):
            if not getattr(match, k) and ex.get(k):
                setattr(match, k, ex[k])
    return match


def _search_song_and_resolve_album(
    title: str, artist: str, duration: float, track: LibraryTrack | None = None,
    *, need_track: bool = True, need_date: bool = True, need_extra: bool = False,
    skip_plats: set[str] | None = None, allow_itunes_song: bool = True,
    tried_out: set[str] | None = None,
) -> TrackMatch | None:
    if not title:
        return None
    skip_plats = skip_plats or set()
    term, tc, ac = f'{title} {artist}'.strip(), _norm_core(title), _norm_core(artist)
    date_fallback: TrackMatch | None = None
    dummy = track or LibraryTrack(path='', filename='', folder='')

    if 'netease' not in skip_plats:
        songs = _cached_song_search(
            'netease_song', 'default', term,
            lambda: ((_http_get_json('https://music.163.com/api/search/get', params={'s': term, 'type': 1, 'limit': 10, 'offset': 0}, headers={'Referer': 'https://music.163.com/'}) or {}).get('result') or {}).get('songs') or [],
        )
        seen_ne_albs: set[str] = set()
        for s in songs:
            if not isinstance(s, dict):
                continue
            sa = ' '.join(a.get('name') or '' for a in (s.get('artists') or []) if isinstance(a, dict))
            if not _song_matches(s.get('name') or '', _to_seconds(s.get('duration')), sa, tc, duration, ac):
                continue
            alb = s.get('album') or {}
            pub_date = normalize_release_date(alb.get('publishTime'), is_ms=True)
            sid = str(s.get('id') or '')
            if alb.get('id'):
                aid = str(alb['id'])
                if aid not in seen_ne_albs and len(seen_ne_albs) >= 3:
                    continue
                seen_ne_albs.add(aid)
                tracks = _netease_album_tracks(aid)
                rdate = _get_album_date('netease', aid) or pub_date
                m = _match_in_tracks(tracks, title, duration, sid)
                if m and (need_track or not need_date or rdate):
                    cand = _make_match('netease', aid, m, len(tracks), '网易云音乐(单曲反查)', str(alb.get('name') or ''), release_date=rdate, cover_url=str(alb.get('picUrl') or ''))
                    return _enrich_song_credits(cand, 'netease', str(m.get('id') or sid), dummy, need_extra=need_extra)
                if not need_track and (rdate or alb.get('picUrl')) and date_fallback is None:
                    date_fallback = _make_match('netease', aid, None, None, '网易云音乐(单曲反查)', str(alb.get('name') or ''), release_date=rdate, artist=sa, title=str(s.get('name') or ''), cover_url=str(alb.get('picUrl') or ''))
            elif not need_track and pub_date and date_fallback is None:
                date_fallback = TrackMatch(None, source='网易云音乐(单曲反查)', album_name=str(alb.get('name') or ''), release_date=pub_date, artist=sa, title=str(s.get('name') or ''))
        if tried_out is not None:
            tried_out.add('netease')

    if 'qq' not in skip_plats:
        qq_songs = _cached_song_search(
            'qq_song', 'default', term,
            lambda: (((_http_get_json('https://c.y.qq.com/soso/fcgi-bin/client_search_cp', params={'w': term, 'format': 'json', 'p': 1, 'n': 10, 't': 0}, headers={'Referer': 'https://y.qq.com/'}) or {}).get('data') or {}).get('song') or {}).get('list') or [],
        )
        seen_qq_albs: set[str] = set()
        for s in qq_songs:
            if not isinstance(s, dict):
                continue
            sa = ', '.join(str(a.get('name') or '').strip() for a in (s.get('singer') or []) if isinstance(a, dict) and a.get('name'))
            if not _song_matches(s.get('songname') or '', _to_seconds(s.get('interval')), sa, tc, duration, ac):
                continue
            alb_mid = str(s.get('albummid') or '')
            pub_date = normalize_release_date(s.get('pubtime'))
            smid = str(s.get('songmid') or '')
            if alb_mid:
                if alb_mid not in seen_qq_albs and len(seen_qq_albs) >= 3:
                    continue
                seen_qq_albs.add(alb_mid)
                tracks = _qq_album_tracks(alb_mid)
                rdate = _get_album_date('qq', alb_mid) or pub_date
                m = _match_in_tracks(tracks, title, duration, smid)
                if m and (need_track or not need_date or rdate):
                    cand = _make_match('qq', alb_mid, m, len(tracks), 'QQ音乐(单曲反查)', str(s.get('albumname') or ''), release_date=rdate)
                    return _enrich_song_credits(cand, 'qq', str(m.get('id') or smid), dummy, need_extra=need_extra)
                if not need_track and rdate and date_fallback is None:
                    date_fallback = _make_match('qq', alb_mid, None, None, 'QQ音乐(单曲反查)', str(s.get('albumname') or ''), release_date=rdate, artist=sa, title=str(s.get('songname') or ''))
            elif not need_track and pub_date and date_fallback is None:
                cand = TrackMatch(None, source='QQ音乐(单曲反查)', album_name=str(s.get('albumname') or ''), release_date=pub_date, artist=sa, title=str(s.get('songname') or ''))
                date_fallback = _enrich_song_credits(cand, 'qq', smid, dummy, need_extra=need_extra)
        if tried_out is not None:
            tried_out.add('qq')

    if 'itunes' not in skip_plats and allow_itunes_song:
        seen_it_albs: set[str] = set()
        for country in ('CN', 'US'):
            it_songs = _cached_song_search(
                'itunes_song', country, term,
                lambda c=country: (_http_get_json('https://itunes.apple.com/search', params={'term': term, 'entity': 'song', 'limit': 10, 'country': c}) or {}).get('results') or [],
            )
            for it in it_songs:
                if not isinstance(it, dict):
                    continue
                if not _song_matches(it.get('trackName') or '', _to_seconds(it.get('trackTimeMillis')), it.get('artistName') or '', tc, duration, ac):
                    continue
                pub_date = normalize_release_date(it.get('releaseDate'))
                if it.get('collectionId'):
                    cid = str(it['collectionId'])
                    if cid not in seen_it_albs and len(seen_it_albs) >= 3:
                        continue
                    seen_it_albs.add(cid)
                    tracks = _itunes_album_tracks(cid)
                    rdate = _get_album_date('itunes', cid) or pub_date
                    m = _match_in_tracks(tracks, title, duration, str(it.get('trackId') or ''))
                    if m and (need_track or not need_date or rdate):
                        return _make_match('itunes', cid, m, len(tracks), 'Apple Music/iTunes(单曲反查)', str(it.get('collectionName') or ''), release_date=rdate)
                    if not need_track and rdate and date_fallback is None:
                        date_fallback = _make_match('itunes', cid, None, None, 'Apple Music/iTunes(单曲反查)', str(it.get('collectionName') or ''), release_date=rdate, artist=str(it.get('artistName') or ''), title=str(it.get('trackName') or ''))
                elif not need_track and pub_date and date_fallback is None:
                    date_fallback = TrackMatch(
                        None, source='Apple Music/iTunes(单曲反查)', album_name=str(it.get('collectionName') or ''),
                        release_date=pub_date, artist=str(it.get('artistName') or ''), title=str(it.get('trackName') or ''),
                        genre=str(it.get('primaryGenreName') or ''), cover_url=_itunes_cover_url(it.get('artworkUrl100') or ''),
                    )
        if tried_out is not None:
            tried_out.add('itunes')
    return date_fallback


def _match_has_requested(
    match: TrackMatch | None, *, need_track: bool, need_date: bool, need_cover: bool, need_core: bool, need_extra: bool,
) -> bool:
    if match is None:
        return False
    if need_track:
        return match.tracknumber is not None
    if need_date and match.release_date:
        return True
    if need_cover and match.cover_url:
        return True
    if need_core and any((match.title, match.artist, match.album_name, match.albumartist, match.release_date)):
        return True
    if need_extra and any((match.composer, match.performer, match.genre, match.discnumber)):
        return True
    return False


_PLAT_CAPABILITIES: dict[str, set[str]] = {
    'netease': {'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'composer', 'performer', 'discnumber'},
    'qq': {'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'composer', 'performer', 'discnumber'},
    'itunes': {'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'discnumber'},
    'mb': {'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'composer', 'performer', 'discnumber'},
}


def _resolve_metadata_core(
    track: LibraryTrack, clues: dict, title: str, artist: str, album: str, duration: float,
    *, need_track: bool, need_date: bool, need_cover: bool = False, need_core: bool = False, need_extra: bool = False,
    skip_plats: set[str] | None = None, allow_mb: bool = True,
    allow_itunes_song: bool = True, tried_out: set[str] | None = None,
) -> TrackMatch:
    ident = clues['identifier']
    skip_plats = skip_plats or set()

    # 1. 本地 .pkl 缓存线索
    pkl = _find_pkl_item(track, ident) if not skip_plats else None
    if pkl:
        src = str(pkl.get('source') or '')
        raw = pkl.get('raw_data') or {}
        sd = raw.get('search') if isinstance(raw, dict) and isinstance(raw.get('search'), dict) else {}
        pid, pt, pd = str(pkl.get('identifier') or ident or ''), str(pkl.get('song_name') or title), _to_seconds(pkl.get('duration_s')) or duration
        pkl_meta = _extract_pkl_metadata(pkl)
        pkl_date = pkl_meta['release_date']
        alb_id, alb_name, label, plat, fetcher, fallback_tn = None, album, '', '', None, None
        if 'QQ' in src:
            alb = sd.get('album') or {}
            alb_id, alb_name, label, plat, fetcher = alb.get('mid') or alb.get('albumMid') or sd.get('albummid'), alb.get('name') or album, 'QQ音乐', 'qq', _qq_album_tracks
            fallback_tn = sd.get('index_album')
        elif 'Netease' in src:
            alb = sd.get('al') or sd.get('album') or {}
            alb_id, alb_name, label, plat, fetcher = alb.get('id') or sd.get('albumId'), alb.get('name') or album, '网易云音乐', 'netease', _netease_album_tracks
        elif 'Kugou' in src:
            alb_id, label, plat, fetcher = sd.get('AlbumID') or sd.get('album_id') or sd.get('albumid'), '酷狗音乐', 'kugou', _kugou_album_tracks
        elif 'Kuwo' in src:
            alb_id, label, plat, fetcher = sd.get('ALBUMID') or sd.get('albumId') or sd.get('albumid'), '酷我音乐', 'kuwo', _kuwo_album_tracks
        elif 'Spotify' in src:
            alb = ((sd.get('item') or {}).get('data') or (sd.get('itemV2') or {}).get('data') or {}).get('albumOfTrack') or {}
            alb_id, alb_name, label, plat, fetcher = alb.get('id') or (alb.get('uri') or '').rsplit(':', 1)[-1], alb.get('name') or album, 'Spotify', 'spotify', _spotify_album_tracks
        elif 'Apple' in src:
            attrs = sd.get('attributes') or {}
            if attrs.get('trackNumber'):
                return TrackMatch(
                    int(attrs['trackNumber']), int(attrs['discNumber']) if attrs.get('discNumber') else None, None,
                    'Apple Music(本地缓存)', album, release_date=pkl_date,
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], performer=pkl_meta['performer'],
                )
        if not need_track and not need_extra and not (need_cover and not pkl_meta['cover_url']) and pkl_date:
            return TrackMatch(
                None, source=f'{label or "musicdl"}(本地缓存)', album_name=alb_name,
                release_date=pkl_date, cover_url=pkl_meta['cover_url'],
                title=pkl_meta['title'], artist=pkl_meta['artist'],
                composer=pkl_meta['composer'], performer=pkl_meta['performer'],
            )
        if alb_id and fetcher:
            tracks = fetcher(str(alb_id))
            if plat == 'kugou' and (need_date or need_cover or need_core or need_extra):
                _kugou_album_info(str(alb_id))
            m = _match_in_tracks(tracks, pt, pd, pid)
            if m:
                cand = _make_match(
                    plat, str(alb_id), m, len(tracks), f'{label}(本地缓存)', alb_name,
                    release_date=pkl_date or _get_album_date(plat, str(alb_id)),
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], performer=pkl_meta['performer'],
                )
                return _enrich_song_credits(cand, plat, str(m.get('id') or pid), track, need_extra=need_extra)
            if not need_track and (_get_album_meta(plat, str(alb_id)) or pkl_date or pkl_meta['cover_url']):
                cand = _make_match(
                    plat, str(alb_id), None, None, f'{label}(本地缓存)', alb_name,
                    release_date=pkl_date or _get_album_date(plat, str(alb_id)),
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], performer=pkl_meta['performer'],
                )
                return _enrich_song_credits(cand, plat, pid, track, need_extra=need_extra)
        if need_track and fallback_tn:
            return TrackMatch(
                int(fallback_tn), None, None, f'{label}(index_album)', alb_name,
                release_date=pkl_date, cover_url=pkl_meta['cover_url'],
                title=pkl_meta['title'], artist=pkl_meta['artist'],
                composer=pkl_meta['composer'], performer=pkl_meta['performer'],
            )

    # 2. 内嵌网易云 163 key
    if 'netease' not in skip_plats and track.netease_meta and track.netease_meta.get('albumId'):
        aid = str(track.netease_meta['albumId'])
        tracks = _netease_album_tracks(aid)
        mid = str(track.netease_meta.get('musicId') or '')
        m = _match_in_tracks(tracks, title, duration, mid)
        cand = _make_match('netease', aid, m, len(tracks) if m else None, '网易云音乐(163 key)', str(track.netease_meta.get('album') or album))
        _enrich_song_credits(cand, 'netease', str((m or {}).get('id') or mid), track, need_extra=need_extra)
        if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
            return cand

    # 3. 文件名自带歌曲 ID 直查
    if 'qq' not in skip_plats and ident and _QQ_MID_RE.match(ident):
        d = _http_get_json('https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg', params={'songmid': ident, 'tpl': 'yqq_song_detail', 'format': 'json'}, headers={'Referer': 'https://y.qq.com/'})
        det = ((d or {}).get('data') or [None])[0]
        if isinstance(det, dict):
            alb = det.get('album') or {}
            song_date = normalize_release_date(det.get('time_public') or alb.get('time_public') or det.get('pubtime'))
            song_art = ', '.join(str(a.get('name') or '').strip() for a in (det.get('singer') or []) if isinstance(a, dict) and a.get('name'))
            song_title = str(det.get('title') or det.get('name') or '')
            if alb.get('mid'):
                amid = str(alb['mid'])
                tracks = _qq_album_tracks(amid)
                m = _match_in_tracks(tracks, title, duration, ident)
                cand = _make_match('qq', amid, m, len(tracks) if m else None, 'QQ音乐(ID直查)', alb.get('name') or album, release_date=_get_album_date('qq', amid) or song_date, artist=song_art, title=song_title)
                _enrich_song_credits(cand, 'qq', ident, track, need_extra=need_extra)
                if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                    return cand
            if need_track and det.get('index_album'):
                cand = TrackMatch(int(det['index_album']), None, None, 'QQ音乐(ID直查)', alb.get('name') or album, release_date=song_date, artist=song_art, title=song_title)
                return _enrich_song_credits(cand, 'qq', ident, track, need_extra=need_extra)
            if not need_track:
                cand = TrackMatch(None, source='QQ音乐(ID直查)', album_name=alb.get('name') or album, release_date=song_date, artist=song_art, title=song_title)
                _enrich_song_credits(cand, 'qq', ident, track, need_extra=need_extra)
                if _match_has_requested(cand, need_track=False, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                    return cand
    elif 'netease' not in skip_plats and ident and _NETEASE_ID_RE.match(ident):
        d = _http_get_json('https://music.163.com/api/song/detail', params={'id': ident, 'ids': f'[{ident}]'}, headers={'Referer': 'https://music.163.com/'})
        det = ((d or {}).get('songs') or [None])[0]
        if isinstance(det, dict):
            alb = det.get('album') or {}
            song_date = normalize_release_date(alb.get('publishTime') or det.get('publishTime'), is_ms=True)
            song_art = ', '.join(str(a.get('name') or '').strip() for a in (det.get('artists') or det.get('ar') or []) if isinstance(a, dict) and a.get('name'))
            song_title = str(det.get('name') or '')
            if alb.get('id'):
                aid = str(alb['id'])
                tracks = _netease_album_tracks(aid)
                m = _match_in_tracks(tracks, title, duration, ident)
                cand = _make_match('netease', aid, m, len(tracks) if m else None, '网易云音乐(ID直查)', alb.get('name') or album, release_date=_get_album_date('netease', aid) or song_date, artist=song_art, title=song_title, cover_url=str(alb.get('picUrl') or ''))
                _enrich_song_credits(cand, 'netease', ident, track, need_extra=need_extra)
                if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                    return cand
            if not need_track:
                cand = TrackMatch(None, source='网易云音乐(ID直查)', album_name=alb.get('name') or album, release_date=song_date, artist=song_art, title=song_title, cover_url=str(alb.get('picUrl') or ''))
                _enrich_song_credits(cand, 'netease', ident, track, need_extra=need_extra)
                if _match_has_requested(cand, need_track=False, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                    return cand

    # 4. 专辑名+歌手依次检索：网易云 -> QQ -> Apple/iTunes -> MusicBrainz
    track_fallback: TrackMatch | None = None
    album_fallback: TrackMatch | None = None
    if album:
        terms = [f'{album} {artist}'.strip(), album] if artist else [album]
        sources = [
            ('netease', '网易云音乐', lambda tm: _search_netease_album(tm, album, artist), _netease_album_tracks),
            ('qq', 'QQ音乐', lambda tm: _search_qq_album(tm, album, artist), _qq_album_tracks),
            ('itunes', 'Apple Music/iTunes', lambda tm: _search_itunes_album(tm, album, artist), _itunes_album_tracks),
        ]
        if allow_mb:
            sources.append(('mb', 'MusicBrainz', lambda _tm: _search_musicbrainz_album(album, artist), _musicbrainz_album_tracks))
        for plat, label, search_fn, fetch_fn in sources:
            if plat in skip_plats:
                continue
            query_terms = [album] if plat == 'mb' else terms
            for term in query_terms:
                aid, aname, artist_ok = search_fn(term)
                if not aid:
                    continue
                tracks = fetch_fn(aid)
                m = _match_in_tracks(tracks, title, duration) if title else None
                if (
                    m and not artist_ok and len(tracks) == 1
                    and duration > 0 and m.get('duration') and abs(float(m['duration']) - duration) > 25.0
                ):
                    m = None
                if m:
                    cand = _make_match(plat, aid, m, len(tracks), label, aname)
                    _enrich_song_credits(cand, plat, str(m.get('id') or ''), track, need_extra=need_extra)
                    if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                        if artist_ok:
                            return cand
                        if track_fallback is None:
                            track_fallback = cand
                elif not need_track and artist_ok:
                    cand = _make_match(plat, aid, None, None, label, aname)
                    if _match_has_requested(cand, need_track=False, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
                        if album_fallback is None:
                            album_fallback = cand
                if plat == 'mb':
                    break
            if tried_out is not None and plat == 'mb':
                tried_out.add('mb')
        if need_track and track_fallback is not None:
            return track_fallback
        if skip_plats and not need_track and album_fallback is not None and 'itunes' not in skip_plats:
            return album_fallback

    # 5. 单曲反查兜底
    song_match = _search_song_and_resolve_album(
        title, artist, duration, track,
        need_track=need_track, need_date=need_date, need_extra=need_extra,
        skip_plats=skip_plats, allow_itunes_song=allow_itunes_song, tried_out=tried_out,
    )
    if _match_has_requested(song_match, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra):
        return song_match
    if not need_track and album_fallback is not None:
        return album_fallback
    if not need_track and track_fallback is not None:
        return track_fallback
    return TrackMatch(
        None,
        reason='未在网易云/QQ/Apple/MusicBrainz 唯一匹配到专辑曲目' if need_track else '未在网易云/QQ/Apple/MusicBrainz 匹配到元数据',
    )


def _remaining_needs(
    res: TrackMatch, track: LibraryTrack, *,
    need_date: bool, need_cover: bool, need_core: bool, need_extra: bool,
    need_fields: set[str] | frozenset[str] | None = None,
) -> dict[str, bool]:
    def _want(k: str) -> bool:
        return need_fields is None or k in need_fields

    return {
        'date': bool(need_date and _want('date') and not (track.date or res.release_date)),
        'cover': bool(need_cover and _want('cover') and not res.cover_url),
        'title': bool(need_core and _want('title') and not (track.title or res.title)),
        'artist': bool(need_core and _want('artist') and not (track.artist or res.artist)),
        'album': bool(need_core and _want('album') and not (track.album or res.album_name)),
        'albumartist': bool(need_core and _want('albumartist') and not (track.albumartist or res.albumartist)),
        'genre': bool(need_extra and _want('genre') and not (track.genre or res.genre)),
        'composer': bool(need_extra and _want('composer') and not (track.composer or res.composer)),
        'performer': bool(need_extra and _want('performer') and not (track.performer or res.performer)),
        'discnumber': bool(need_extra and _want('discnumber') and not (track.disk_disc_int or (track.disk_discnumber or '').strip()) and res.discnumber is None),
    }


def _needs_further_enrichment(
    res: TrackMatch, track: LibraryTrack, *,
    need_date: bool, need_cover: bool, need_core: bool, need_extra: bool,
    need_fields: set[str] | frozenset[str] | None = None,
) -> tuple[bool, bool, bool, bool]:
    rem = _remaining_needs(
        res, track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra,
        need_fields=need_fields,
    )
    rem_date = rem['date']
    rem_cover = rem['cover']
    rem_core = any(rem[k] for k in ('title', 'artist', 'album', 'albumartist'))
    rem_extra = any(rem[k] for k in ('genre', 'composer', 'performer', 'discnumber'))
    return rem_date, rem_cover, rem_core, rem_extra


def resolve_track_metadata(
    track: LibraryTrack, *,
    need_track: bool = True,
    need_date: bool = True,
    need_cover: bool = False,
    need_core: bool = False,
    need_extra: bool = False,
    need_fields: set[str] | frozenset[str] | None = None,
) -> TrackMatch:
    """为单首歌曲联网解析并合并多平台元数据（音轨/碟号/日期/封面/艺术家/作曲/表演者/流派）。"""
    clues = _parse_filename_clues(track)
    ident = clues['identifier']
    title = (track.title or clues['inferred_title']).strip()
    artist = _first_artist(track.artist or track.albumartist or clues['inferred_artist'])
    album = (track.album or clues['inferred_album']).strip()
    duration = float(track.duration or 0.0)
    if not title and not (not need_track and (album or ident or track.netease_meta or _find_pkl_item(track, ident))):
        return TrackMatch(None, reason='缺少标题与文件名线索')

    tried: set[str] = set()
    res = _resolve_metadata_core(
        track, clues, title, artist, album, duration,
        need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra,
        tried_out=tried,
    )
    primary_matched = bool(res.source)

    pkl = _find_pkl_item(track, ident)
    if pkl:
        pm = _extract_pkl_metadata(pkl)
        _merge_match(res, TrackMatch(
            None, source=res.source or 'musicdl缓存', album_name=pm['album'], release_date=pm['release_date'],
            title=pm['title'], artist=pm['artist'], composer=pm['composer'], performer=pm['performer'], cover_url=pm['cover_url'],
        ))

    # 若主平台缺少部分字段（如网易云缺流派、QQ 缺乐手/表演者），仅针对有能力补齐缺失字段的剩余平台做补充合并
    rem_date, rem_cover, rem_core, rem_extra = _needs_further_enrichment(
        res, track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra,
        need_fields=need_fields,
    )
    if primary_matched and any((rem_date, rem_cover, rem_core, rem_extra)) and (album or title):
        src_l = res.source
        if '网易云' in src_l:
            tried.add('netease')
        elif 'QQ' in src_l:
            tried.add('qq')
        elif 'Apple' in src_l:
            tried.add('itunes')
        elif 'MusicBrainz' in src_l:
            tried.add('mb')
        for plat in ('netease', 'qq', 'itunes', 'mb'):
            if plat in tried:
                continue
            rem = _remaining_needs(
                res, track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra,
                need_fields=need_fields,
            )
            missing_keys = {k for k, v in rem.items() if v}
            if not missing_keys:
                break
            if not (missing_keys & _PLAT_CAPABILITIES[plat]):
                continue
            if plat == 'mb' and (not album or _MB_CIRCUIT_OPEN):
                continue
            rem_date = rem['date']
            rem_cover = rem['cover']
            rem_core = any(rem[k] for k in ('title', 'artist', 'album', 'albumartist'))
            rem_extra = any(rem[k] for k in ('genre', 'composer', 'performer', 'discnumber'))
            allow_itunes_song = any(rem[k] for k in ('date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre'))
            skip = {'netease', 'qq', 'itunes', 'mb'} - {plat}
            extra_res = _resolve_metadata_core(
                track, clues, title, artist, album, duration,
                need_track=False, need_date=rem_date, need_cover=rem_cover, need_core=rem_core, need_extra=rem_extra,
                skip_plats=skip, allow_mb=(plat == 'mb'), allow_itunes_song=allow_itunes_song,
            )
            _merge_match(res, extra_res)

    return res


def resolve_release_date(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑发布日期。"""
    return resolve_track_metadata(track, need_track=False, need_date=True)


def resolve_track_number(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑内音轨号（同时尽可能附带发布日期）。"""
    return resolve_track_metadata(track, need_track=True, need_date=True)
