"""QQ 音乐数据源提供者（支持桌面端 musicu.fcg 搜索备选与双语 Base64 歌词合并）。"""

from __future__ import annotations

import base64
import re
import uuid

from providers.base import (
    MusicProvider,
    ProviderCircuitBreaker,
    build_tracks,
    cached_album,
    cached_artist_url,
    cached_search,
    cached_song_meta,
    cached_song_search,
    choose_album_detail,
    extract_artist_names,
    http_get_json,
    http_post_json,
    merge_lyrics,
    norm_core,
    normalize_release_date,
    parse_lyric_credits,
    set_album_date,
    set_album_meta,
    to_seconds,
)


_QQ_CB = ProviderCircuitBreaker(threshold=5)


class QQMusicProvider(MusicProvider):
    id = 'qq'
    label = 'QQ音乐'
    capabilities = frozenset({'track', 'date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre', 'composer', 'discnumber', 'lyrics'})
    supports_album_search = True
    circuit_breaker = _QQ_CB

    def search_album(self, term: str, album_name: str, singer: str) -> tuple[str, str, bool]:
        def _do():
            d = http_get_json(
                'https://c.y.qq.com/soso/fcgi-bin/client_search_cp',
                params={'w': term, 'format': 'json', 'p': 1, 'n': 10, 't': 8},
                headers={'Referer': 'https://y.qq.com/'},
            )
            a, artist_ok = choose_album_detail(
                (((d or {}).get('data') or {}).get('album') or {}).get('list') or [],
                album_name, singer, lambda x: x.get('albumName'), lambda x: x.get('singerName'),
            )
            if a and a.get('albumMID'):
                amid = str(a['albumMID'])
                set_album_date('qq', amid, normalize_release_date(a.get('publicTime') or a.get('time_public')))
                return amid, str(a.get('albumName') or album_name), artist_ok
            return '', '', False
        return cached_search('qq_alb', term, album_name, singer, _do)

    def get_album_tracks(self, album_mid: str) -> list[dict]:
        def _fetch(amid: str) -> list[dict]:
            d = http_get_json(
                'https://c.y.qq.com/v8/fcg-bin/fcg_v8_album_info_cp.fcg',
                params={
                    'albummid': amid, 'g_tk': '5381', 'format': 'json',
                    'inCharset': 'utf8', 'outCharset': 'utf-8', 'notice': '0',
                    'platform': 'yqq.json', 'needNewCode': '0',
                },
                headers={'Referer': 'https://y.qq.com/'},
            )
            data = (d or {}).get('data') or {}
            if isinstance(data, dict):
                set_album_meta(
                    'qq', amid,
                    release_date=normalize_release_date(data.get('aDate') or data.get('publicTime') or data.get('time_public')),
                    album_name=str(data.get('name') or ''),
                    albumartist=str(data.get('singername') or ''),
                    genre=str(data.get('genre') or ''),
                    cover_url=f'https://y.gtimg.cn/music/photo_new/T002R800x800M000{amid}.jpg',
                )
            items = data.get('list') or [] if isinstance(data, dict) else []
            return build_tracks([
                {
                    'id': x.get('songmid'),
                    'name': x.get('songname'),
                    'duration': to_seconds(x.get('interval')),
                    'disc': int(x.get('cdIdx') or 0) + 1,
                    'artist': ', '.join(
                        str(a.get('name') or '').strip()
                        for a in (x.get('singer') or [])
                        if isinstance(a, dict) and a.get('name')
                    ),
                }
                for x in items if isinstance(x, dict)
            ])
        return cached_album('qq', album_mid, _fetch)

    def _search_desktop_musicu(self, term: str) -> list[dict]:
        payload = {
            'comm': {
                'wid': '', 'tmeAppID': 'qqmusic', 'authst': '', 'uid': '', 'gray': '0',
                'OpenUDID': '2d484d3157d4ed482e406e6c5fdcf8c3d3275deb',
                'ct': '6', 'patch': '2', 'psrf_qqopenid': '', 'sid': '',
                'psrf_access_token_expiresAt': '', 'cv': '80600', 'gzip': '0',
                'qq': '', 'nettype': '2', 'psrf_qqunionid': '', 'psrf_qqaccess_token': '',
                'tmeLoginType': '2',
            },
            'music.search.SearchCgiService.DoSearchForQQMusicDesktop': {
                'module': 'music.search.SearchCgiService',
                'method': 'DoSearchForQQMusicDesktop',
                'param': {
                    'num_per_page': 15, 'page_num': 1, 'remoteplace': 'txt.mac.search',
                    'search_type': 0, 'query': term, 'grp': 1,
                    'searchid': str(uuid.uuid4()), 'nqc_flag': 0,
                },
            },
        }
        d = http_post_json(
            'https://u.y.qq.com/cgi-bin/musicu.fcg',
            payload,
            headers={
                'Referer': 'https://y.qq.com/portal/profile.html',
                'User-Agent': 'QQ%E9%9F%B3%E4%B9%90/73222 CFNetwork/1406.0.3 Darwin/22.4.0',
            },
        )
        raw_list = ((((d or {}).get('music.search.SearchCgiService.DoSearchForQQMusicDesktop') or {}).get('data') or {}).get('body') or {}).get('song') or {}
        items = raw_list.get('list') or [] if isinstance(raw_list, dict) else []
        out: list[dict] = []
        for it in items:
            if not isinstance(it, dict):
                continue
            alb = it.get('album') or {}
            out.append({
                'songmid': it.get('mid') or '',
                'songname': it.get('title') or it.get('name') or '',
                'singer': it.get('singer') or [],
                'albummid': alb.get('mid') or '',
                'albumname': alb.get('title') or alb.get('name') or '',
                'interval': it.get('interval') or 0,
                'pubtime': it.get('time_public') or '',
            })
        return out

    def search_songs(self, term: str) -> list[dict]:
        def _fetch():
            d = http_get_json(
                'https://c.y.qq.com/soso/fcgi-bin/client_search_cp',
                params={'w': term, 'format': 'json', 'p': 1, 'n': 10, 't': 0},
                headers={'Referer': 'https://y.qq.com/'},
            )
            songs = (((d or {}).get('data') or {}).get('song') or {}).get('list') or []
            if songs:
                return songs
            return self._search_desktop_musicu(term)
        return cached_song_search('qq_song', 'default', term, _fetch)

    def search_artist(self, artist_name: str) -> str:
        name = str(artist_name or '').strip()
        if not name:
            return ''

        def _fetch() -> str:
            payload = {
                'comm': {'ct': '6', 'cv': '80600'},
                'music.search.SearchCgiService.DoSearchForQQMusicDesktop': {
                    'module': 'music.search.SearchCgiService',
                    'method': 'DoSearchForQQMusicDesktop',
                    'param': {
                        'num_per_page': 5,
                        'page_num': 1,
                        'search_type': 1,
                        'query': name,
                        'searchid': str(uuid.uuid4()),
                    },
                },
            }
            d = http_post_json('https://u.y.qq.com/cgi-bin/musicu.fcg', payload, headers={'Referer': 'https://y.qq.com/'})
            res = (
                ((d or {}).get('music.search.SearchCgiService.DoSearchForQQMusicDesktop') or {})
                .get('data', {})
                .get('body', {})
                .get('singer', {})
                .get('list')
                or []
            )
            if not isinstance(res, list) or not res:
                return ''
            sk = norm_core(name)
            for s in res:
                if not isinstance(s, dict):
                    continue
                if sk and norm_core(s.get('singerName') or '') == sk:
                    smid = str(s.get('singerMID') or '').strip()
                    if smid:
                        return f'https://y.gtimg.cn/music/photo_new/T001R800x800M000{smid}.jpg'
            first = res[0]
            if isinstance(first, dict):
                smid = str(first.get('singerMID') or '').strip()
                if smid:
                    return f'https://y.gtimg.cn/music/photo_new/T001R800x800M000{smid}.jpg'
            return ''

        return cached_artist_url('qq', name, _fetch)

    def search_song_hits(self, term: str) -> list[dict]:
        hits: list[dict] = []
        for s in self.search_songs(term):
            if not isinstance(s, dict):
                continue
            hits.append({
                'title': str(s.get('songname') or ''),
                'artist': extract_artist_names(s.get('singer')),
                'duration': to_seconds(s.get('interval')),
                'song_id': str(s.get('songmid') or ''),
                'album_id': str(s.get('albummid') or ''),
                'album_name': str(s.get('albumname') or ''),
                'release_date': normalize_release_date(s.get('pubtime')),
                'cover_url': '',
                'genre': '',
                'lrc_url': '',
            })
        return hits

    def get_song_detail(self, song_mid: str) -> dict | None:
        smid = str(song_mid or '').strip()
        if not smid:
            return None
        d = http_get_json(
            'https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg',
            params={'songmid': smid, 'tpl': 'yqq_song_detail', 'format': 'json'},
            headers={'Referer': 'https://y.qq.com/'},
        )
        det = ((d or {}).get('data') or [None])[0]
        return det if isinstance(det, dict) else None

    @staticmethod
    def _decode_b64_text(raw: str) -> str:
        if not raw:
            return ''
        try:
            return base64.b64decode(raw).decode('utf-8', errors='replace')
        except Exception:
            return ''

    def get_song_extra(self, song_mid: str, *, need_lyrics: bool = False, title: str = '', duration: float = 0.0) -> dict:
        def _load(smid: str) -> dict:
            comp, genre, pub_date, merged_lrc = '', '', '', ''
            ql = http_get_json(
                'https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg',
                params={'songmid': smid, 'g_tk': '5381', 'format': 'json', 'inCharset': 'utf8', 'outCharset': 'utf-8'},
                headers={'Referer': 'https://y.qq.com/portal/player.html'},
            )
            if isinstance(ql, dict):
                orig_lrc = self._decode_b64_text(str(ql.get('lyric') or ''))
                trans_lrc = self._decode_b64_text(str(ql.get('trans') or ''))
                if orig_lrc:
                    comp = parse_lyric_credits(orig_lrc)
                    merged_lrc = merge_lyrics(orig_lrc, trans_lrc)

            qd = http_post_json(
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

            return {'composer': comp, 'genre': genre, 'release_date': pub_date, 'lyrics': merged_lrc}

        return cached_song_meta('qq', song_mid, _load)
