"""Discogs 数据源提供者（支持实体版次搜索、黑胶/磁带音轨解析、高清封面与演职员名单）。"""

from __future__ import annotations

import os
import re
import threading
import time

from core.zh_norm import to_traditional
from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    bump_cache_gen,
    cached_album,
    cached_search,
    cached_song_search,
    choose_album_detail,
    extract_artist_names,
    first_artist,
    http_get_json,
    normalize_release_date,
    set_album_meta,
    to_seconds,
)

DISCOGS_UA = 'MusicMetadata/1.0 (+https://github.com/waves-dylan/MusicMetadata)'
DISCOGS_API_BASE = 'https://api.discogs.com'

_DISCOGS_LOCK = threading.Lock()
_DISCOGS_LAST = 0.0
_DISCOGS_CB = ProviderCircuitBreaker(threshold=5)
_DISCOGS_TOKEN = ''

# 角色映射关键字集合
_COMPOSER_ROLES = {'written-by', 'written by', 'composed by', 'music by', 'songwriter', 'composed', 'writer', 'composition'}
_LYRICIST_ROLES = {'lyrics by', 'words by', 'text by', 'lyrics', 'lyricist'}
_ARRANGER_ROLES = {'arranged by', 'orchestrated by', 'arrangement', 'arranger'}


def set_discogs_token(token: str) -> str:
    """设置 Discogs Personal Access Token 并使缓存换代。"""
    global _DISCOGS_TOKEN
    tok = str(token or '').strip()
    with _DISCOGS_LOCK:
        changed = (tok != _DISCOGS_TOKEN)
        _DISCOGS_TOKEN = tok
    if changed:
        bump_cache_gen()
    return tok


def get_discogs_token() -> str:
    """获取当前配置的 Discogs Personal Access Token（支持 DISCOGS_TOKEN 环境变量回落）。"""
    with _DISCOGS_LOCK:
        tok = _DISCOGS_TOKEN
    if not tok:
        tok = str(os.environ.get('DISCOGS_TOKEN') or '').strip()
    return tok


def has_discogs_token() -> bool:
    """检查是否已配置有效的 Discogs Token。"""
    return bool(get_discogs_token())


def is_discogs_circuit_open() -> bool:
    return _DISCOGS_CB.is_open()


def reset_discogs_circuit() -> None:
    _DISCOGS_CB.reset()


def _discogs_get(endpoint: str, params: dict | None = None) -> dict | None:
    """封装带速率限制 (1.05s)、Token 鉴权与熔断保护的 Discogs HTTP 请求。"""
    global _DISCOGS_LAST
    if _DISCOGS_CB.is_open():
        return None

    token = get_discogs_token()
    if not token:
        return None

    with _DISCOGS_LOCK:
        wait = 1.05 - (time.time() - _DISCOGS_LAST)
        if wait > 0:
            time.sleep(wait)
        _DISCOGS_LAST = time.time()

    url = f"{DISCOGS_API_BASE}/{endpoint.lstrip('/')}"
    headers = {
        'User-Agent': DISCOGS_UA,
        'Authorization': f'Discogs token={token}',
    }

    res = http_get_json(url, params=params, headers=headers, timeout=10)
    if res is None:
        _DISCOGS_CB.record_failure()
        return None
    _DISCOGS_CB.record_success()
    return res


def _parse_position(pos: str, default_num: int) -> tuple[int, int]:
    """解析实体唱片 Position（如 A1, B2, 1-2, CD1-03, 5）为 (disc, track)。"""
    p = str(pos or '').strip().upper()
    if not p:
        return 1, default_num

    # 1. CD1-02, 1-02, 2.3 形式
    m = re.match(r'^(?:CD\s*)?(\d+)[-_.](\d+)$', p)
    if m:
        try:
            return int(m.group(1)), int(m.group(2))
        except ValueError:
            pass

    # 2. 黑胶/磁带 Side 形式 (A1, B2, C3, AA1...)
    m = re.match(r'^([A-Z])(\d+)$', p)
    if m:
        try:
            disc = ord(m.group(1)) - ord('A') + 1
            return disc, int(m.group(2))
        except ValueError:
            pass

    # 3. 纯数字
    if p.isdigit():
        try:
            return 1, int(p)
        except ValueError:
            pass

    return 1, default_num


def _extract_credits(extraartists: list[dict]) -> tuple[str, str, str, str]:
    """从 extraartists 列表中提取作曲、作词、编曲与演奏者。"""
    composers: list[str] = []
    lyricists: list[str] = []
    arrangers: list[str] = []
    performers: list[str] = []

    for item in extraartists or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get('name') or '').strip()
        role = str(item.get('role') or '').lower()
        if not name or not role:
            continue

        # 清洗 Discogs 特有的作者尾缀编号，如 "John Doe (2)" -> "John Doe"
        clean_name = re.sub(r'\s*\(\d+\)$', '', name).strip()
        if not clean_name:
            continue

        if any(r in role for r in _COMPOSER_ROLES):
            if clean_name not in composers:
                composers.append(clean_name)
        elif any(r in role for r in _LYRICIST_ROLES):
            if clean_name not in lyricists:
                lyricists.append(clean_name)
        elif any(r in role for r in _ARRANGER_ROLES):
            if clean_name not in arrangers:
                arrangers.append(clean_name)
        else:
            if clean_name not in performers:
                performers.append(clean_name)

    return (
        ', '.join(composers),
        ', '.join(lyricists),
        ', '.join(arrangers),
        ', '.join(performers[:5]),
    )


class DiscogsProvider(MusicProvider):
    """Discogs 实体唱片与发烧元数据提供者。"""

    id = 'discogs'
    label = 'Discogs'
    capabilities = frozenset({
        'track', 'date', 'cover', 'title', 'artist',
        'album', 'albumartist', 'genre', 'composer',
        'lyricist', 'arranger', 'performer', 'discnumber',
    })
    supports_album_search = True
    circuit_breaker = _DISCOGS_CB

    @property
    def is_available(self) -> bool:
        return has_discogs_token() and not _DISCOGS_CB.is_open()

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        if not has_discogs_token():
            return '', '', False

        def _do():
            clean_alb = str(album_name or '').strip()
            clean_art = str(singer or '').strip()
            params = {
                'release_title': clean_alb,
                'type': 'release',
                'per_page': 8,
            }
            if clean_art:
                params['artist'] = clean_art

            data = _discogs_get('database/search', params=params)
            results = (data or {}).get('results') or []

            # 繁体回退尝试（港台/日版唱片繁体字支持）
            if not results and to_traditional(clean_alb) != clean_alb:
                params['release_title'] = to_traditional(clean_alb)
                if clean_art:
                    params['artist'] = to_traditional(clean_art)
                data = _discogs_get('database/search', params=params)
                results = (data or {}).get('results') or []

            # Discogs 的 title 字段通常为: "Artist - Album Title"
            def _extract_name(cand: dict) -> str:
                t = str(cand.get('title') or '')
                return t.split(' - ', 1)[1] if ' - ' in t else t

            def _extract_artist(cand: dict) -> str:
                t = str(cand.get('title') or '')
                return t.split(' - ', 1)[0] if ' - ' in t else ''

            best, artist_ok = choose_album_detail(
                results, album_name, singer, _extract_name, _extract_artist,
            )
            if best and best.get('id'):
                rid = str(best['id'])
                r_date = normalize_release_date(best.get('year'))
                cov = str(best.get('cover_image') or '')
                set_album_meta('discogs', rid, release_date=r_date, cover_url=cov)
                return rid, _extract_name(best) or album_name, artist_ok
            return '', '', False

        return cached_search('discogs_alb', album_name, album_name, singer, _do)

    def get_album_tracks(self, release_id: str) -> list[dict]:
        if not has_discogs_token():
            return []

        def _fetch(rid: str) -> list[dict]:
            data = _discogs_get(f'releases/{rid}')
            if not data:
                return []

            # 专辑全局元数据
            rel_date = normalize_release_date(data.get('released') or data.get('year'))
            album_artist = extract_artist_names(data.get('artists'))
            genres = [str(g).strip() for g in (data.get('styles') or data.get('genres') or []) if g]
            genre_str = ', '.join(genres)

            # 优先选择 primary 类型的正面封面
            cover_url = ''
            images = data.get('images') or []
            if images:
                primary = next((img for img in images if isinstance(img, dict) and img.get('type') == 'primary'), images[0])
                if isinstance(primary, dict):
                    cover_url = str(primary.get('uri') or primary.get('resource_url') or '')

            # 专辑级 extraartists 作为保底
            alb_comp, alb_lyr, alb_arr, alb_perf = _extract_credits(data.get('extraartists') or [])

            set_album_meta(
                'discogs', rid,
                release_date=rel_date,
                albumartist=album_artist,
                genre=genre_str,
                cover_url=cover_url,
            )

            # 解析曲目列表
            tracklist = data.get('tracklist') or []
            out_tracks: list[dict] = []
            valid_idx = 1

            for tr in tracklist:
                if not isinstance(tr, dict):
                    continue
                # 过滤标题栏与章节标记
                if tr.get('type_') in ('heading', 'index'):
                    continue

                disc_no, track_no = _parse_position(tr.get('position'), valid_idx)
                tr_artists = extract_artist_names(tr.get('artists')) or album_artist
                tr_comp, tr_lyr, tr_arr, tr_perf = _extract_credits(tr.get('extraartists') or [])

                rec = {
                    'disc': disc_no,
                    'number': track_no,
                    'name': str(tr.get('title') or '').strip(),
                    'duration': to_seconds(tr.get('duration')),
                    'artist': tr_artists,
                    'composer': tr_comp or alb_comp,
                    'lyricist': tr_lyr or alb_lyr,
                    'arranger': tr_arr or alb_arr,
                    'genre': genre_str,
                    'cover_url': cover_url,
                }
                out_tracks.append(rec)
                valid_idx += 1

            return build_tracks(out_tracks)

        return cached_album('discogs', release_id, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        """单曲检索（规整为统一字典列表）。"""
        if not has_discogs_token():
            return []

        def _do() -> list[dict]:
            data = _discogs_get('database/search', params={'track': term, 'type': 'release', 'per_page': 6})
            results = (data or {}).get('results') or []
            hits: list[dict] = []
            for r in results:
                if not isinstance(r, dict):
                    continue
                t = str(r.get('title') or '')
                art = t.split(' - ', 1)[0] if ' - ' in t else ''
                alb = t.split(' - ', 1)[1] if ' - ' in t else t
                hits.append({
                    'id': str(r.get('id')),
                    'song_id': str(r.get('id')),
                    'name': term,
                    'artist': art,
                    'album_name': alb,
                    'release_date': normalize_release_date(r.get('year')),
                    'cover_url': str(r.get('cover_image') or ''),
                    'duration': 0.0,
                })
            return hits

        return cached_song_search('discogs', 'song', term, _do)

    def search_songs(self, term: str) -> list[dict]:
        return self.search_song_hits(term)
