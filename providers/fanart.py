"""Fanart.tv 数据源提供者（支持高清透明艺术家写真、背景图与专辑封面）。"""

from __future__ import annotations

import os
import threading

from core.zh_norm import to_simplified, to_traditional
from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    cached_artist_url,
    http_get_json,
)
from providers.musicbrainz import mb_get

_FANART_CB = ProviderCircuitBreaker(threshold=5)
_FANART_LOCK = threading.Lock()
_FANART_DEFAULT_PROJECT_KEY = '61a7d0ab4e67162b7a0c7c35915cd48e'
_FANART_API_KEY = ''
_FANART_CLIENT_KEY = ''


def set_fanart_keys(api_key: str = '', client_key: str = '') -> tuple[str, str]:
    global _FANART_API_KEY, _FANART_CLIENT_KEY
    ak = str(api_key or '').strip()
    ck = str(client_key or '').strip()
    with _FANART_LOCK:
        _FANART_API_KEY = ak
        _FANART_CLIENT_KEY = ck
    return ak, ck


def get_fanart_keys() -> tuple[str, str]:
    with _FANART_LOCK:
        ak, ck = _FANART_API_KEY, _FANART_CLIENT_KEY
    if not ak:
        ak = str(os.environ.get('FANART_API_KEY') or os.environ.get('FANARTTV_API_KEY') or '').strip()
    if not ck:
        ck = str(os.environ.get('FANART_CLIENT_KEY') or os.environ.get('FANARTTV_CLIENT_KEY') or '').strip()
    return ak or _FANART_DEFAULT_PROJECT_KEY, ck


def _fanart_get(endpoint: str, params=None):
    if _FANART_CB.is_open():
        return None
    api_key, client_key = get_fanart_keys()
    url = f'https://webservice.fanart.tv/v3.2/{endpoint}'
    query_params = dict(params or {})
    query_params['api_key'] = api_key
    if client_key:
        query_params['client_key'] = client_key
    headers = {'User-Agent': 'MusicMetadata/1.0'}
    if client_key:
        headers['client-key'] = client_key
    d = http_get_json(url, params=query_params, headers=headers, timeout=10)
    if d is None:
        _FANART_CB.record_failure()
        return None
    _FANART_CB.record_success()
    return d


def _pick_best_image_url(items: list[dict] | None) -> str:
    if not items or not isinstance(items, list):
        return ''
    valid = [x for x in items if isinstance(x, dict) and str(x.get('url') or '').startswith('http')]
    if not valid:
        return ''
    best = sorted(valid, key=lambda x: int(x.get('likes') or 0), reverse=True)[0]
    return str(best.get('url') or '').strip()


class FanartTvProvider(MusicProvider):
    id = 'fanart'
    label = 'Fanart.tv'
    capabilities = frozenset({'cover', 'artist'})
    supports_album_search = False
    circuit_breaker = _FANART_CB

    def search_artist_candidates(self, artist_name: str, *, track_hint: str = '') -> list[dict]:
        name = (artist_name or '').strip()
        if not name:
            return []

        # 1. 通过 MusicBrainz 检索艺术家 MBID
        mb_res = mb_get('https://musicbrainz.org/ws/2/artist/', params={'query': f'artist:{name}', 'fmt': 'json'})
        artists = (mb_res or {}).get('artists') or [] if isinstance(mb_res, dict) else []
        if not artists:
            trad_n = to_traditional(name)
            if trad_n != name:
                mb_res = mb_get('https://musicbrainz.org/ws/2/artist/', params={'query': f'artist:{trad_n}', 'fmt': 'json'})
                artists = (mb_res or {}).get('artists') or [] if isinstance(mb_res, dict) else []
        if not artists:
            simp_n = to_simplified(name)
            if simp_n != name:
                mb_res = mb_get('https://musicbrainz.org/ws/2/artist/', params={'query': f'artist:{simp_n}', 'fmt': 'json'})
                artists = (mb_res or {}).get('artists') or [] if isinstance(mb_res, dict) else []
        if not artists:
            return []

        matched_artist = artists[0]
        mbid = str(matched_artist.get('id') or '').strip()
        if not mbid:
            return []
        artist_display = str(matched_artist.get('name') or '').strip() or name
        disambig = str(matched_artist.get('disambiguation') or '').strip()

        # 2. 从 Fanart.tv 获取艺人高清写真
        data = _fanart_get(f'music/{mbid}')
        if not isinstance(data, dict):
            return []

        results: list[dict] = []
        seen_pics: set[str] = set()

        # 2.1 宽幅舞台写真壁纸 (artistbackground, 1920x1080)
        bg_items = data.get('artistbackground') or []
        if isinstance(bg_items, list):
            valid_bg = [x for x in bg_items if isinstance(x, dict) and str(x.get('url') or '').startswith('http')]
            valid_bg.sort(key=lambda x: int(x.get('likes') or 0), reverse=True)
            for item in valid_bg:
                url = str(item.get('url') or '').strip()
                if not url or url in seen_pics:
                    continue
                seen_pics.add(url)
                likes = int(item.get('likes') or 0)
                clue = f'社区舞台写真 · {likes} 赞'
                if disambig:
                    clue = f'{disambig} · {clue}'
                results.append({
                    'source': self.label,
                    'platform': self.id,
                    'artist_name': artist_display,
                    'pic_url': url,
                    'aspect_type': 'landscape',
                    'resolution': '1920×1080 横版',
                    'width': 1920,
                    'height': 1080,
                    'clues': clue,
                    'raw_meta': item,
                })

        # 2.2 高清头像缩略图 (artistthumb, 1000x1000)
        thumb_items = data.get('artistthumb') or []
        if isinstance(thumb_items, list):
            valid_th = [x for x in thumb_items if isinstance(x, dict) and str(x.get('url') or '').startswith('http')]
            valid_th.sort(key=lambda x: int(x.get('likes') or 0), reverse=True)
            for item in valid_th:
                url = str(item.get('url') or '').strip()
                if not url or url in seen_pics:
                    continue
                seen_pics.add(url)
                likes = int(item.get('likes') or 0)
                clue = f'精选肖像头像 · {likes} 赞'
                if disambig:
                    clue = f'{disambig} · {clue}'
                results.append({
                    'source': self.label,
                    'platform': self.id,
                    'artist_name': artist_display,
                    'pic_url': url,
                    'aspect_type': 'square',
                    'resolution': '1000×1000 方图',
                    'width': 1000,
                    'height': 1000,
                    'clues': clue,
                    'raw_meta': item,
                })

        return results

    def search_artist(self, artist_name: str) -> str:
        name = (artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            cands = self.search_artist_candidates(name)
            if not cands:
                return ''
            # 优先 1:1 头像，次选横版壁纸
            thumbs = [c for c in cands if c['aspect_type'] == 'square']
            if thumbs:
                return thumbs[0]['pic_url']
            return cands[0]['pic_url']

        return cached_artist_url('fanart', name, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        return []
