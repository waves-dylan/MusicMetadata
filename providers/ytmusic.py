"""YouTube Music 数据源提供者（免登录 InnerTube WEB_REMIX API，支持专辑/单曲搜索、封面与歌词）。"""

from __future__ import annotations

import re

from providers.base import (
    USER_AGENT,
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    cached_album,
    cached_search,
    cached_song_meta,
    cached_song_search,
    choose_album_detail,
    http_post_json,
    normalize_release_date,
    parse_lyric_credits,
    set_album_meta,
    to_seconds,
)

_YTM_CB = ProviderCircuitBreaker(threshold=5)
_YTM_CONTEXT = {
    'client': {
        'clientName': 'WEB_REMIX',
        'clientVersion': '1.20250101.01.00',
        'hl': 'en',
        'gl': 'US',
    },
}
_YTM_HEADERS = {
    'User-Agent': USER_AGENT,
    'Origin': 'https://music.youtube.com',
    'Referer': 'https://music.youtube.com/',
}
_YTM_SONG_PARAMS = 'EgWKAQIIAWoKEAMQBBAJEAoQBQ%3D%3D'
_YTM_ALBUM_PARAMS = 'EgWKAQIYAWoKEAMQBBAJEAoQBQ%3D%3D'
_DUR_RE = re.compile(r'^\d{1,2}:\d{2}(?::\d{2})?$')
_YEAR_RE = re.compile(r'^(19|20)\d{2}$')
_RELEASE_TYPES = {'album', 'ep', 'single', 'song', 'video', '专辑', '单曲'}


def _ytm_post(endpoint: str, payload: dict):
    if _YTM_CB.is_open():
        return None
    d = http_post_json(
        f'https://music.youtube.com/youtubei/v1/{endpoint}?prettyPrint=false',
        {'context': _YTM_CONTEXT, **payload},
        headers=_YTM_HEADERS,
        timeout=10,
    )
    if d is None:
        _YTM_CB.record_failure()
        return None
    _YTM_CB.record_success()
    return d if isinstance(d, dict) else None


def _runs_text(obj) -> str:
    if not isinstance(obj, dict):
        return ''
    return ''.join(str(r.get('text') or '') for r in (obj.get('runs') or []) if isinstance(r, dict)).strip()


def _upgrade_thumb(thumbs: list | None, size: int = 1200) -> str:
    if not isinstance(thumbs, list) or not thumbs:
        return ''
    last = thumbs[-1] if isinstance(thumbs[-1], dict) else {}
    url = str(last.get('url') or '').strip()
    if not url:
        return ''
    if url.startswith('//'):
        url = 'https:' + url
    return re.sub(r'=w\d+-h\d+[^/?]*', f'=w{size}-h{size}-l90-rj', url)


def _item_thumb(item: dict) -> str:
    thumbs = (((item.get('thumbnail') or {}).get('musicThumbnailRenderer') or {}).get('thumbnail') or {}).get('thumbnails')
    return _upgrade_thumb(thumbs)


def _flex_runs(item: dict, col_idx: int) -> list[dict]:
    cols = item.get('flexColumns') or []
    if col_idx >= len(cols) or not isinstance(cols[col_idx], dict):
        return []
    text_obj = (cols[col_idx].get('musicResponsiveListItemFlexColumnRenderer') or {}).get('text') or {}
    return [r for r in (text_obj.get('runs') or []) if isinstance(r, dict)]


def _iter_shelf_items(resp: dict) -> list[dict]:
    items: list[dict] = []

    def _walk(node):
        if isinstance(node, dict):
            if 'musicResponsiveListItemRenderer' in node and isinstance(node['musicResponsiveListItemRenderer'], dict):
                items.append(node['musicResponsiveListItemRenderer'])
                return
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for el in node:
                _walk(el)

    _walk(resp)
    return items


def _find_first(node, key: str):
    if isinstance(node, dict):
        if key in node and isinstance(node[key], dict):
            return node[key]
        for v in node.values():
            found = _find_first(v, key)
            if found is not None:
                return found
    elif isinstance(node, list):
        for el in node:
            found = _find_first(el, key)
            if found is not None:
                return found
    return None


def _parse_song_meta_runs(runs: list[dict]) -> tuple[str, str, str, float]:
    artists: list[str] = []
    album_id, album_name, dur_sec = '', '', 0.0
    plain_parts: list[str] = []
    for r in runs:
        txt = str(r.get('text') or '').strip()
        if not txt or txt in ('•', '·', ','):
            continue
        if _DUR_RE.match(txt):
            dur_sec = to_seconds(txt)
            continue
        nav = (r.get('navigationEndpoint') or {}).get('browseEndpoint') or {}
        bid = str(nav.get('browseId') or '')
        ptype = str((((nav.get('browseEndpointContextSupportedConfigs') or {}).get('browseEndpointContextMusicConfig') or {}).get('pageType')) or '')
        if ptype == 'MUSIC_PAGE_TYPE_ALBUM' or bid.startswith('MPREb_'):
            album_id = bid
            album_name = txt
        elif ptype == 'MUSIC_PAGE_TYPE_ARTIST' or bid.startswith('UC'):
            artists.append(txt)
        elif txt.lower() not in _RELEASE_TYPES:
            plain_parts.append(txt)
    if not artists and plain_parts:
        artists.append(plain_parts[0])
    if not album_name and len(plain_parts) >= 2 and artists and plain_parts[0] == artists[0]:
        album_name = plain_parts[1]
    return ', '.join(dict.fromkeys(artists)), album_id, album_name, dur_sec


def _parse_album_meta_runs(runs: list[dict]) -> tuple[str, str]:
    artists: list[str] = []
    year = ''
    plain: list[str] = []
    for r in runs:
        txt = str(r.get('text') or '').strip()
        if not txt or txt in ('•', '·', ','):
            continue
        if _YEAR_RE.match(txt):
            year = txt
            continue
        if txt.lower() in _RELEASE_TYPES:
            continue
        nav = (r.get('navigationEndpoint') or {}).get('browseEndpoint') or {}
        bid = str(nav.get('browseId') or '')
        ptype = str((((nav.get('browseEndpointContextSupportedConfigs') or {}).get('browseEndpointContextMusicConfig') or {}).get('pageType')) or '')
        if ptype == 'MUSIC_PAGE_TYPE_ARTIST' or bid.startswith('UC'):
            artists.append(txt)
        else:
            plain.append(txt)
    if not artists and plain:
        artists.append(plain[0])
    return ', '.join(dict.fromkeys(artists)), year


class YouTubeMusicProvider(MusicProvider):
    id = 'ytmusic'
    label = 'YouTube Music'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'lyrics'})
    supports_album_search = True
    circuit_breaker = _YTM_CB

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            d = _ytm_post('search', {'query': term, 'params': _YTM_ALBUM_PARAMS})
            cands: list[dict] = []
            for it in _iter_shelf_items(d or {}):
                bid = str(((it.get('navigationEndpoint') or {}).get('browseEndpoint') or {}).get('browseId') or '')
                r0 = _flex_runs(it, 0)
                if not bid and r0:
                    bid = str(((r0[0].get('navigationEndpoint') or {}).get('browseEndpoint') or {}).get('browseId') or '')
                title = ''.join(str(r.get('text') or '') for r in r0).strip()
                if not bid or not title:
                    continue
                artist, year = _parse_album_meta_runs(_flex_runs(it, 1))
                cands.append({
                    'id': bid,
                    'title': title,
                    'artist': artist,
                    'year': year,
                    'cover_url': _item_thumb(it),
                })
            a, artist_ok = choose_album_detail(cands, album_name, singer, lambda x: x.get('title'), lambda x: x.get('artist'))
            if a and a.get('id'):
                aid = str(a['id'])
                set_album_meta(
                    'ytmusic',
                    aid,
                    release_date=normalize_release_date(a.get('year')),
                    album_name=str(a.get('title') or ''),
                    albumartist=str(a.get('artist') or ''),
                    cover_url=str(a.get('cover_url') or ''),
                )
                return aid, str(a.get('title') or album_name), artist_ok
            return '', '', False

        return cached_search('ytmusic_alb', term, album_name, singer, _do)

    def get_album_tracks(self, album_id: str) -> list[dict]:
        def _fetch(aid: str) -> list[dict]:
            d = _ytm_post('browse', {'browseId': aid})
            if not isinstance(d, dict):
                return []
            hdr = _find_first(d, 'musicResponsiveHeaderRenderer') or _find_first(d, 'musicDetailHeaderRenderer') or {}
            alb_title = _runs_text(hdr.get('title'))
            sub_runs = [r for r in ((hdr.get('subtitle') or {}).get('runs') or []) if isinstance(r, dict)]
            strap_runs = [r for r in (((hdr.get('straplineTextOne') or {}).get('runs')) or []) if isinstance(r, dict)]
            alb_artist, alb_year = _parse_album_meta_runs(strap_runs + sub_runs)
            thumbs = (((hdr.get('thumbnail') or {}).get('musicThumbnailRenderer') or {}).get('thumbnail') or {}).get('thumbnails')
            cov_url = _upgrade_thumb(thumbs)
            set_album_meta(
                'ytmusic',
                aid,
                release_date=normalize_release_date(alb_year),
                album_name=alb_title,
                albumartist=alb_artist,
                cover_url=cov_url,
            )

            out: list[dict] = []
            for it in _iter_shelf_items(d):
                r0 = _flex_runs(it, 0)
                t_name = ''.join(str(r.get('text') or '') for r in r0).strip()
                if not t_name:
                    continue
                vid = str((it.get('playlistItemData') or {}).get('videoId') or '')
                if not vid and r0:
                    vid = str(((r0[0].get('navigationEndpoint') or {}).get('watchEndpoint') or {}).get('videoId') or '')
                if not vid:
                    play_nav = _find_first(it.get('overlay') or {}, 'watchEndpoint') or {}
                    vid = str(play_nav.get('videoId') or '')

                dur = 0.0
                for fc in (it.get('fixedColumns') or []):
                    ftxt = _runs_text((fc.get('musicResponsiveListItemFixedColumnRenderer') or {}).get('text') if isinstance(fc, dict) else None)
                    if _DUR_RE.match(ftxt):
                        dur = to_seconds(ftxt)
                        break
                t_art, _, _, dur2 = _parse_song_meta_runs(_flex_runs(it, 1))
                out.append({
                    'id': vid,
                    'name': t_name,
                    'duration': dur or dur2,
                    'disc': 1,
                    'artist': t_art or alb_artist,
                    'cover_url': cov_url,
                })
            return build_tracks(out)

        return cached_album('ytmusic', album_id, _fetch)

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = _ytm_post('search', {'query': term, 'params': _YTM_SONG_PARAMS})
            return _iter_shelf_items(d or {})

        return cached_song_search('ytmusic_song', 'default', term, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for it in self.search_songs(term):
            r0 = _flex_runs(it, 0)
            title = ''.join(str(r.get('text') or '') for r in r0).strip()
            vid = str((it.get('playlistItemData') or {}).get('videoId') or '')
            if not vid and r0:
                vid = str(((r0[0].get('navigationEndpoint') or {}).get('watchEndpoint') or {}).get('videoId') or '')
            if not title or not vid:
                continue
            artist, album_id, album_name, dur = _parse_song_meta_runs(_flex_runs(it, 1))
            hits.append({
                'title': title,
                'artist': artist,
                'duration': dur,
                'song_id': vid,
                'album_id': album_id,
                'album_name': album_name,
                'release_date': '',
                'cover_url': _item_thumb(it),
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_extra(self, song_id: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        if not need_lyrics or not song_id:
            return {}

        def _load(vid: str) -> dict:
            nxt = _ytm_post('next', {'videoId': vid})
            if not isinstance(nxt, dict):
                return {}
            lyr_bid = ''
            tabs = (((((nxt.get('contents') or {}).get('singleColumnMusicWatchNextResultsRenderer') or {}).get('tabbedRenderer') or {}).get('watchNextTabbedResultsRenderer') or {}).get('tabs')) or []
            for tb in tabs:
                tr = (tb or {}).get('tabRenderer') if isinstance(tb, dict) else None
                if not isinstance(tr, dict):
                    continue
                bid = str(((tr.get('endpoint') or {}).get('browseEndpoint') or {}).get('browseId') or '')
                if bid.startswith('MPLYt_') or str(tr.get('title') or '').lower() in ('lyrics', '歌词'):
                    lyr_bid = bid
                    break
            if not lyr_bid:
                return {}
            br = _ytm_post('browse', {'browseId': lyr_bid})
            shelf = _find_first(br or {}, 'musicDescriptionShelfRenderer') or {}
            lyrics = _runs_text(shelf.get('description'))
            return {'lyrics': lyrics, 'composer': parse_lyric_credits(lyrics) if lyrics else ''}

        return cached_song_meta('ytmusic', song_id, _load)
