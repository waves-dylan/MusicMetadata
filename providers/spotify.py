"""Spotify 数据源提供者（支持可选官方 Web API Client Credentials 搜索与 Embed 页面兜底）。"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time

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
    http_get_json,
    http_get_text,
    http_post_form,
    normalize_release_date,
    set_album_meta,
    to_seconds,
)

_SPOTIFY_CB = ProviderCircuitBreaker(threshold=5)
_SPOTIFY_LOCK = threading.Lock()
_SPOTIFY_CLIENT_ID = ''
_SPOTIFY_CLIENT_SECRET = ''
_SPOTIFY_TOKEN = ''
_SPOTIFY_TOKEN_EXP = 0.0


def set_spotify_credentials(client_id: str, client_secret: str) -> tuple[str, str]:
    global _SPOTIFY_CLIENT_ID, _SPOTIFY_CLIENT_SECRET, _SPOTIFY_TOKEN, _SPOTIFY_TOKEN_EXP
    cid = str(client_id or '').strip()
    csec = str(client_secret or '').strip()
    with _SPOTIFY_LOCK:
        changed = (cid != _SPOTIFY_CLIENT_ID or csec != _SPOTIFY_CLIENT_SECRET)
        _SPOTIFY_CLIENT_ID = cid
        _SPOTIFY_CLIENT_SECRET = csec
        if changed:
            _SPOTIFY_TOKEN = ''
            _SPOTIFY_TOKEN_EXP = 0.0
    if changed:
        bump_cache_gen()
    return cid, csec


def get_spotify_credentials() -> tuple[str, str]:
    with _SPOTIFY_LOCK:
        cid, csec = _SPOTIFY_CLIENT_ID, _SPOTIFY_CLIENT_SECRET
    if not cid:
        cid = str(os.environ.get('SPOTIFY_CLIENT_ID') or '').strip()
    if not csec:
        csec = str(os.environ.get('SPOTIFY_CLIENT_SECRET') or '').strip()
    return cid, csec


def has_spotify_credentials() -> bool:
    cid, csec = get_spotify_credentials()
    return bool(cid and csec)


def _get_spotify_token() -> str:
    global _SPOTIFY_TOKEN, _SPOTIFY_TOKEN_EXP
    if _SPOTIFY_CB.is_open():
        return ''
    cid, csec = get_spotify_credentials()
    if not (cid and csec):
        return ''
    now = time.time()
    with _SPOTIFY_LOCK:
        if _SPOTIFY_TOKEN and now < _SPOTIFY_TOKEN_EXP - 30:
            return _SPOTIFY_TOKEN
    basic = base64.b64encode(f'{cid}:{csec}'.encode('utf-8')).decode('ascii')
    resp = http_post_form(
        'https://accounts.spotify.com/api/token',
        {'grant_type': 'client_credentials'},
        headers={'Authorization': f'Basic {basic}'},
        timeout=10,
    )
    if not isinstance(resp, dict) or not resp.get('access_token'):
        _SPOTIFY_CB.record_failure()
        return ''
    _SPOTIFY_CB.record_success()
    tok = str(resp['access_token'])
    exp = now + float(resp.get('expires_in') or 3600)
    with _SPOTIFY_LOCK:
        _SPOTIFY_TOKEN = tok
        _SPOTIFY_TOKEN_EXP = exp
    return tok


def _spotify_api_get(url: str, params=None):
    if _SPOTIFY_CB.is_open():
        return None
    tok = _get_spotify_token()
    if not tok:
        return None
    d = http_get_json(url, params=params, headers={'Authorization': f'Bearer {tok}'}, timeout=10)
    if d is None:
        _SPOTIFY_CB.record_failure()
        return None
    _SPOTIFY_CB.record_success()
    return d if isinstance(d, dict) else None


def _pick_spotify_cover(images: list | None) -> str:
    if not isinstance(images, list) or not images:
        return ''
    first = images[0] if isinstance(images[0], dict) else {}
    return str(first.get('url') or '').strip()


class SpotifyProvider(MusicProvider):
    id = 'spotify'
    label = 'Spotify'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'discnumber'})
    circuit_breaker = _SPOTIFY_CB

    @property
    def supports_album_search(self) -> bool:
        return has_spotify_credentials()

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        if not has_spotify_credentials():
            return '', '', False

        def _do():
            d = _spotify_api_get(
                'https://api.spotify.com/v1/search',
                params={'q': term, 'type': 'album', 'limit': 10},
            )
            items = ((d or {}).get('albums') or {}).get('items') or [] if isinstance(d, dict) else []
            a, artist_ok = choose_album_detail(
                items,
                album_name,
                singer,
                lambda x: x.get('name'),
                lambda x: extract_artist_names(x.get('artists')),
            )
            if a and a.get('id'):
                aid = str(a['id'])
                set_album_meta(
                    'spotify',
                    aid,
                    release_date=normalize_release_date(a.get('release_date')),
                    album_name=str(a.get('name') or ''),
                    albumartist=extract_artist_names(a.get('artists')),
                    cover_url=_pick_spotify_cover(a.get('images')),
                )
                return aid, str(a.get('name') or album_name), artist_ok
            return '', '', False

        return cached_search('spotify_alb', term, album_name, singer, _do)

    def _fetch_album_via_api(self, aid: str) -> list[dict]:
        d = _spotify_api_get(f'https://api.spotify.com/v1/albums/{aid}')
        if not isinstance(d, dict):
            return []
        genres = [str(g).strip() for g in (d.get('genres') or []) if g]
        cov = _pick_spotify_cover(d.get('images'))
        set_album_meta(
            'spotify',
            aid,
            release_date=normalize_release_date(d.get('release_date')),
            album_name=str(d.get('name') or ''),
            albumartist=extract_artist_names(d.get('artists')),
            genre=', '.join(genres),
            cover_url=cov,
        )
        tracks_obj = d.get('tracks') if isinstance(d.get('tracks'), dict) else {}
        items = [x for x in (tracks_obj.get('items') or []) if isinstance(x, dict)]
        next_url = str(tracks_obj.get('next') or '').strip()
        page = 0
        while next_url and page < 5:
            page += 1
            more = _spotify_api_get(next_url)
            if not isinstance(more, dict):
                break
            items.extend(x for x in (more.get('items') or []) if isinstance(x, dict))
            next_url = str(more.get('next') or '').strip()

        items.sort(key=lambda x: (int(x.get('disc_number') or 1), int(x.get('track_number') or 0)))
        return build_tracks([
            {
                'id': x.get('id'),
                'name': x.get('name') or '',
                'duration': to_seconds(x.get('duration_ms')),
                'disc': int(x.get('disc_number') or 1),
                'artist': extract_artist_names(x.get('artists')),
                'cover_url': cov,
            }
            for x in items
        ])

    def _fetch_album_via_embed(self, aid: str) -> list[dict]:
        txt = http_get_text(f'https://open.spotify.com/embed/album/{aid}')
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', txt or '', re.S)
        if not m:
            return []
        try:
            ent = json.loads(m.group(1))['props']['pageProps']['state']['data']['entity']
            set_album_meta(
                'spotify',
                aid,
                release_date=normalize_release_date(ent.get('releaseDate') or ent.get('date')),
                album_name=str(ent.get('title') or ent.get('name') or ''),
                albumartist=str(ent.get('subtitle') or ''),
            )
            return build_tracks([
                {
                    'id': (t.get('uri') or '').rsplit(':', 1)[-1],
                    'name': t.get('name') or t.get('title') or '',
                    'duration': to_seconds((t.get('duration') or {}).get('totalMilliseconds')),
                }
                for t in (ent.get('trackList') or [])
            ])
        except Exception:
            return []

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            if has_spotify_credentials():
                res = self._fetch_album_via_api(aid)
                if res:
                    return res
            return self._fetch_album_via_embed(aid)

        return cached_album('spotify', album_id, _fetch)

    def search_songs(self, term: str) -> list[dict]:
        if not has_spotify_credentials():
            return []

        def _fetch():
            d = _spotify_api_get(
                'https://api.spotify.com/v1/search',
                params={'q': term, 'type': 'track', 'limit': 10},
            )
            items = ((d or {}).get('tracks') or {}).get('items') or [] if isinstance(d, dict) else []
            return [x for x in items if isinstance(x, dict)]

        return cached_song_search('spotify_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            alb = s.get('album') if isinstance(s.get('album'), dict) else {}
            hits.append({
                'title': str(s.get('name') or ''),
                'artist': extract_artist_names(s.get('artists')),
                'duration': to_seconds(s.get('duration_ms')),
                'song_id': str(s.get('id') or ''),
                'album_id': str(alb.get('id') or ''),
                'album_name': str(alb.get('name') or ''),
                'release_date': normalize_release_date(alb.get('release_date')),
                'cover_url': _pick_spotify_cover(alb.get('images')),
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_detail(self, song_id: str) -> dict | None:
        if not has_spotify_credentials() or not song_id:
            return None
        return _spotify_api_get(f'https://api.spotify.com/v1/tracks/{song_id}')
