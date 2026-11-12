"""Apple Music / iTunes 数据源提供者。"""

from __future__ import annotations

import re

from providers.base import (
    MusicProvider,
    build_tracks,
    cached_album,
    cached_search,
    cached_song_search,
    choose_album_detail,
    http_get_json,
    normalize_release_date,
    set_album_date,
    set_album_meta,
    to_seconds,
)


def itunes_cover_url(url: str, size: int = 1200) -> str:
    s = str(url or '').strip()
    if not s:
        return ''
    return re.sub(r'/\d+x\d+bb\.(jpg|png)$', f'/{size}x{size}bb.jpg', s)


class ITunesProvider(MusicProvider):
    id = 'itunes'
    label = 'Apple Music/iTunes'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'discnumber'})
    supports_album_search = True

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            for country in ('CN', 'US'):
                d = http_get_json('https://itunes.apple.com/search', params={'term': term, 'entity': 'album', 'limit': 10, 'country': country})
                a, artist_ok = choose_album_detail(
                    (d or {}).get('results') or [],
                    album_name, singer, lambda x: x.get('collectionName'), lambda x: x.get('artistName'),
                )
                if a and a.get('collectionId'):
                    cid = str(a['collectionId'])
                    set_album_date('itunes', cid, normalize_release_date(a.get('releaseDate')))
                    return cid, str(a.get('collectionName') or album_name), artist_ok
            return '', '', False
        return cached_search('itunes_alb', term, album_name, singer, _do)

    def get_album_tracks(self, cid: str) -> list[dict]:
        def _fetch(col_id: str) -> list[dict]:
            d = http_get_json('https://itunes.apple.com/lookup', params={'id': col_id, 'entity': 'song', 'limit': 250})
            all_res = [x for x in ((d or {}).get('results') or []) if isinstance(x, dict)]
            col = next((x for x in all_res if x.get('wrapperType') == 'collection'), None)
            res = [x for x in all_res if x.get('wrapperType') == 'track']
            set_album_meta(
                'itunes', col_id,
                release_date=normalize_release_date((col or {}).get('releaseDate') or (res[0].get('releaseDate') if res else '')),
                album_name=str((col or {}).get('collectionName') or (res[0].get('collectionName') if res else '')),
                albumartist=str((col or {}).get('collectionArtistName') or (col or {}).get('artistName') or ''),
                genre=str((col or {}).get('primaryGenreName') or (res[0].get('primaryGenreName') if res else '')),
                cover_url=itunes_cover_url((col or {}).get('artworkUrl100') or (res[0].get('artworkUrl100') if res else '')),
            )
            res.sort(key=lambda x: (int(x.get('discNumber') or 1), int(x.get('trackNumber') or 0)))
            return build_tracks([
                {
                    'id': x.get('trackId'),
                    'name': x.get('trackName'),
                    'duration': to_seconds(x.get('trackTimeMillis')),
                    'disc': int(x.get('discNumber') or 1),
                    'artist': str(x.get('artistName') or ''),
                    'genre': str(x.get('primaryGenreName') or ''),
                }
                for x in res
            ])
        return cached_album('itunes', cid, _fetch)

    def search_songs_by_country(self, term: str, country: str) -> list[dict]:
        return cached_song_search(
            'itunes_song', country, term,
            lambda c=country: (http_get_json('https://itunes.apple.com/search', params={'term': term, 'entity': 'song', 'limit': 10, 'country': c}) or {}).get('results') or [],
        )

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for country in ('CN', 'US'):
            for it in self.search_songs_by_country(term, country):
                if not isinstance(it, dict):
                    continue
                hits.append({
                    'title': str(it.get('trackName') or ''),
                    'artist': str(it.get('artistName') or ''),
                    'duration': to_seconds(it.get('trackTimeMillis')),
                    'song_id': str(it.get('trackId') or ''),
                    'album_id': str(it['collectionId']) if it.get('collectionId') else '',
                    'album_name': str(it.get('collectionName') or ''),
                    'release_date': normalize_release_date(it.get('releaseDate')),
                    'cover_url': itunes_cover_url(it.get('artworkUrl100') or ''),
                    'genre': str(it.get('primaryGenreName') or ''),
                    'lrc_url': '',
                })
        return hits
