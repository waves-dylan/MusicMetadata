"""MusicBrainz 数据源提供者（含速率限制与连续失败自动熔断器）。"""

from __future__ import annotations

import threading
import time

from providers.base import (
    MusicProvider,
    build_tracks,
    cached_album,
    cached_search,
    choose_album_detail,
    http_get_json,
    normalize_release_date,
    set_album_date,
    set_album_meta,
    to_seconds,
)

MB_UA = 'SongMetadata/1.0 (https://github.com/local/SongMetadata)'
MB_FAIL_THRESHOLD = 5

_MB_LOCK = threading.Lock()
_MB_LAST = 0.0
_MB_CONSEC_FAILS = 0
_MB_CIRCUIT_OPEN = False


def reset_mb_circuit() -> None:
    global _MB_CONSEC_FAILS, _MB_CIRCUIT_OPEN
    with _MB_LOCK:
        _MB_CONSEC_FAILS = 0
        _MB_CIRCUIT_OPEN = False


def is_mb_circuit_open() -> bool:
    with _MB_LOCK:
        return _MB_CIRCUIT_OPEN


def mb_get(url: str, params=None):
    global _MB_LAST, _MB_CONSEC_FAILS, _MB_CIRCUIT_OPEN
    with _MB_LOCK:
        if _MB_CIRCUIT_OPEN:
            return None
        wait = 1.05 - (time.time() - _MB_LAST)
        if wait > 0:
            time.sleep(wait)
        _MB_LAST = time.time()
    res = http_get_json(url, params=params, headers={'User-Agent': MB_UA}, timeout=10)
    with _MB_LOCK:
        if res is None:
            _MB_CONSEC_FAILS += 1
            if _MB_CONSEC_FAILS >= MB_FAIL_THRESHOLD:
                _MB_CIRCUIT_OPEN = True
        else:
            _MB_CONSEC_FAILS = 0
    return res


def format_mb_artist_credit(credits) -> str:
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


def extract_mb_recording_credits(rec: dict) -> tuple[str, str]:
    composers: list[str] = []
    for rel in (rec.get('relations') or []):
        if not isinstance(rel, dict):
            continue
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
    genres = sorted([g for g in (rec.get('genres') or []) if isinstance(g, dict) and g.get('name')], key=lambda g: -int(g.get('count') or 0))
    genre = str(genres[0]['name']).strip() if genres else ''
    return ', '.join(composers), genre


class MusicBrainzProvider(MusicProvider):
    id = 'mb'
    label = 'MusicBrainz'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'composer', 'discnumber'})
    supports_album_search = True

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            q = f'release:"{album_name}"' + (f' AND artist:"{singer}"' if singer else '')
            d = mb_get('https://musicbrainz.org/ws/2/release/', params={'query': q, 'limit': 8, 'fmt': 'json'})
            b, artist_ok = choose_album_detail(
                (d or {}).get('releases') or [],
                album_name, singer, lambda x: x.get('title'),
                lambda x: ' '.join(
                    (ac.get('name') or (ac.get('artist') or {}).get('name') or '')
                    for ac in (x.get('artist-credit') or [])
                    if isinstance(ac, dict)
                ),
            )
            if b and b.get('id'):
                rid = str(b['id'])
                set_album_date('mb', rid, normalize_release_date(b.get('date')))
                return rid, str(b.get('title') or album_name), artist_ok
            return '', '', False
        return cached_search('mb_alb', album_name, album_name, singer, _do)

    def get_album_tracks(self, release_id: str) -> list[dict]:
        def _fetch(rid: str) -> list[dict]:
            d = mb_get(
                f'https://musicbrainz.org/ws/2/release/{rid}',
                params={'inc': 'recordings+artist-credits+genres+recording-level-rels+work-rels+work-level-rels+artist-rels', 'fmt': 'json'},
            )
            if isinstance(d, dict):
                ev_date = next((ev.get('date') for ev in (d.get('release-events') or []) if isinstance(ev, dict) and ev.get('date')), '')
                genres = sorted([g for g in (d.get('genres') or []) if isinstance(g, dict) and g.get('name')], key=lambda g: -int(g.get('count') or 0))
                caa = d.get('cover-art-archive') if isinstance(d.get('cover-art-archive'), dict) else {}
                cov_url = f'https://coverartarchive.org/release/{rid}/front-1200' if caa.get('front') else ''
                set_album_meta(
                    'mb', rid,
                    release_date=normalize_release_date(d.get('date') or ev_date),
                    album_name=str(d.get('title') or ''),
                    albumartist=format_mb_artist_credit(d.get('artist-credit')),
                    genre=str(genres[0]['name']).strip() if genres else '',
                    cover_url=cov_url,
                )
            items = []
            for disc_idx, med in enumerate((d or {}).get('media') or [], 1):
                for tr in (med.get('tracks') or []):
                    rec = tr.get('recording') if isinstance(tr.get('recording'), dict) else {}
                    comp, rgenre = extract_mb_recording_credits(rec)
                    art = format_mb_artist_credit(tr.get('artist-credit') or rec.get('artist-credit'))
                    items.append({
                        'id': tr.get('id'),
                        'name': tr.get('title') or rec.get('title') or '',
                        'duration': to_seconds(tr.get('length')),
                        'disc': disc_idx,
                        'artist': art,
                        'composer': comp,
                        'genre': rgenre,
                    })
            return build_tracks(items)
        return cached_album('mb', release_id, _fetch)
