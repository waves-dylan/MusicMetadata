"""Fanart.tv 数据源提供者（支持高清透明艺术家写真、背景图与专辑封面）。"""

from __future__ import annotations

import os
import threading

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

    def search_artist(self, artist_name: str) -> str:
        name = (artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            # 1. 通过 MusicBrainz 检索艺术家 MBID
            mb_res = mb_get('https://musicbrainz.org/ws/2/artist/', params={'query': f'artist:{name}', 'fmt': 'json'})
            artists = (mb_res or {}).get('artists') or [] if isinstance(mb_res, dict) else []
            if not artists:
                return ''
            mbid = str(artists[0].get('id') or '').strip()
            if not mbid:
                return ''

            # 2. 从 Fanart.tv 获取艺人高清写真
            data = _fanart_get(f'music/{mbid}')
            if not isinstance(data, dict):
                return ''

            # 优先使用 1:1 高清头像/缩略图 (artistthumb)，回退使用背景壁纸 (artistbackground)
            url = _pick_best_image_url(data.get('artistthumb'))
            if not url:
                url = _pick_best_image_url(data.get('artistbackground'))
            return url

        return cached_artist_url('fanart', name, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        return []
