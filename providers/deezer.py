"""Deezer 公开数据源提供者（免 Key JSON API，支持专辑/单曲搜索、音轨、流派、发行日与高清封面）。"""

from __future__ import annotations

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    cached_album,
    cached_artist_url,
    cached_search,
    cached_song_search,
    choose_album_detail,
    http_get_json,
    norm_core,
    normalize_release_date,
    set_album_meta,
    to_seconds,
)

_DEEZER_CB = ProviderCircuitBreaker(threshold=5)


def _deezer_get(url: str, params=None):
    if _DEEZER_CB.is_open():
        return None
    d = http_get_json(url, params=params, timeout=10)
    if d is None:
        _DEEZER_CB.record_failure()
        return None
    _DEEZER_CB.record_success()
    if isinstance(d, dict) and d.get('error'):
        return None
    return d


def _pick_deezer_cover(obj: dict) -> str:
    if not isinstance(obj, dict):
        return ''
    for k in ('cover_xl', 'cover_big', 'cover_medium', 'cover'):
        val = str(obj.get(k) or '').strip()
        if val.startswith('http'):
            return val
    return ''


def _pick_deezer_artist_pic(obj: dict) -> str:
    if not isinstance(obj, dict):
        return ''
    for k in ('picture_xl', 'picture_big', 'picture_medium', 'picture'):
        val = str(obj.get(k) or '').strip()
        if val.startswith('http'):
            return val
    return ''


class DeezerProvider(MusicProvider):
    id = 'deezer'
    label = 'Deezer'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'discnumber'})
    supports_album_search = True
    circuit_breaker = _DEEZER_CB

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            d = _deezer_get('https://api.deezer.com/search/album', params={'q': term, 'limit': 10})
            cands = (d or {}).get('data') or [] if isinstance(d, dict) else []
            a, artist_ok = choose_album_detail(
                cands,
                album_name,
                singer,
                lambda x: x.get('title'),
                lambda x: (x.get('artist') or {}).get('name') if isinstance(x.get('artist'), dict) else '',
            )
            if a and a.get('id'):
                aid = str(a['id'])
                set_album_meta(
                    'deezer',
                    aid,
                    album_name=str(a.get('title') or ''),
                    albumartist=str((a.get('artist') or {}).get('name') or ''),
                    cover_url=_pick_deezer_cover(a),
                )
                return aid, str(a.get('title') or album_name), artist_ok
            return '', '', False

        return cached_search('deezer_alb', term, album_name, singer, _do)

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            d = _deezer_get(f'https://api.deezer.com/album/{aid}')
            if not isinstance(d, dict):
                return []
            genres_data = ((d.get('genres') or {}).get('data') or []) if isinstance(d.get('genres'), dict) else []
            genre = str((genres_data[0] or {}).get('name') or '').strip() if genres_data and isinstance(genres_data[0], dict) else ''
            art_obj = d.get('artist') if isinstance(d.get('artist'), dict) else {}
            set_album_meta(
                'deezer',
                aid,
                release_date=normalize_release_date(d.get('release_date')),
                album_name=str(d.get('title') or ''),
                albumartist=str(art_obj.get('name') or ''),
                genre=genre,
                cover_url=_pick_deezer_cover(d),
            )
            tracks_obj = d.get('tracks') if isinstance(d.get('tracks'), dict) else {}
            items = [x for x in (tracks_obj.get('data') or []) if isinstance(x, dict)]
            nb_tracks = int(d.get('nb_tracks') or 0)
            page = 0
            while (tracks_obj.get('next') or (nb_tracks and len(items) < nb_tracks)) and page < 5:
                page += 1
                more = _deezer_get(f'https://api.deezer.com/album/{aid}/tracks', params={'index': len(items), 'limit': 100})
                if not isinstance(more, dict):
                    break
                batch = [x for x in (more.get('data') or []) if isinstance(x, dict)]
                if not batch:
                    break
                items.extend(batch)
                tracks_obj = more

            items.sort(key=lambda x: (int(x.get('disk_number') or 1), int(x.get('track_position') or 0)))
            return build_tracks([
                {
                    'id': x.get('id'),
                    'name': x.get('title') or '',
                    'duration': to_seconds(x.get('duration')),
                    'disc': int(x.get('disk_number') or 1),
                    'artist': str((x.get('artist') or {}).get('name') or '') if isinstance(x.get('artist'), dict) else '',
                }
                for x in items
            ])

        return cached_album('deezer', album_id, _fetch)

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = _deezer_get('https://api.deezer.com/search/track', params={'q': term, 'limit': 10})
            rows = (d or {}).get('data') or [] if isinstance(d, dict) else []
            return [x for x in rows if isinstance(x, dict)]

        return cached_song_search('deezer_song', 'default', term, _fetch)

    def search_artist(self, artist_name: str) -> str:
        name = str(artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            d = _deezer_get('https://api.deezer.com/search/artist', params={'q': name, 'limit': 5})
            rows = (d or {}).get('data') or [] if isinstance(d, dict) else []
            if not rows:
                return ''
            sk = norm_core(name)
            for a in rows:
                if not isinstance(a, dict):
                    continue
                if sk and norm_core(a.get('name') or '') == sk:
                    pic = _pick_deezer_artist_pic(a)
                    if pic:
                        return pic
            first = rows[0]
            if isinstance(first, dict):
                return _pick_deezer_artist_pic(first)
            return ''

        return cached_artist_url('deezer', name, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            alb = s.get('album') if isinstance(s.get('album'), dict) else {}
            art = s.get('artist') if isinstance(s.get('artist'), dict) else {}
            hits.append({
                'title': str(s.get('title') or ''),
                'artist': str(art.get('name') or ''),
                'duration': to_seconds(s.get('duration')),
                'song_id': str(s.get('id') or ''),
                'album_id': str(alb.get('id') or ''),
                'album_name': str(alb.get('title') or ''),
                'release_date': '',
                'cover_url': _pick_deezer_cover(alb),
                'genre': '',
                'lrc_url': '',
            })
        return hits
