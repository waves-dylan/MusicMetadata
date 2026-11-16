"""多平台在线专辑与单曲元数据检索与聚合（基于模块化 providers/ 与标准库 HTTP）。"""

from __future__ import annotations

import io
import os
import pickle
import re
import time
import urllib.request
from typing import Callable

from core.constants import ARTIST_MAX_SIDE, EMBED_MAX_SIDE
from core.models import LibraryTrack
from services.art.image_ops import normalize_artist_jpeg, normalize_jpeg, sniff_image
from services.lookup.scoring import compute_album_affinity_score, rank_song_hits
from providers import (
    ONLINE_PROVIDERS,
    PRIMARY_PROVIDERS,
    PROVIDERS,
    PROVIDER_LABELS,
    TrackMatch,
    deezer_provider,
    discogs_provider,
    fanart_provider,
    get_discogs_token,
    get_fanart_keys,
    get_netease_api_base,
    get_spotify_credentials,
    get_tadb_api_key,
    has_discogs_token,
    has_spotify_credentials,
    kugou_provider,
    lrclib_provider,
    netease_provider,
    parse_source_list,
    qq_provider,
    resolve_provider_order,
    set_discogs_token,
    set_fanart_keys,
    set_netease_api_base,
    set_spotify_credentials,
    set_tadb_api_key,
    tadb_provider,
    ytmusic_provider,
)
from providers.base import (
    ARTIST_BYTES_CACHE,
    CACHE_LOCK,
    COVER_CACHE,
    PKL_CACHE,
    USER_AGENT,
    _VA_ARTISTS,
    bump_cache_gen,
    cache_resolved_album,
    extract_artist_names,
    first_artist,
    get_album_date,
    get_album_meta,
    http_get_bytes,
    match_in_tracks,
    norm_core,
    normalize_release_date,
    parse_all_lyric_credits,
    parse_lyric_credits,
    reset_all_circuit_breakers,
    song_matches,
    to_seconds,
)
from providers.musicbrainz import is_mb_circuit_open, reset_mb_circuit
from tag_io import parse_filename_clues

_QQ_MID_RE = re.compile(r'^[0-9A-Za-z]{14}$')
_NETEASE_ID_RE = re.compile(r'^\d{5,15}$')
_SPOTIFY_ID_RE = re.compile(r'^[0-9A-Za-z]{22}$')


def clear_caches() -> None:
    bump_cache_gen()
    reset_mb_circuit()
    reset_all_circuit_breakers()
    from services.art.image_ops import clear_image_caches
    clear_image_caches()


def fetch_cover_bytes(cover_url: str, timeout: int = 10) -> bytes | None:
    """下载在线封面图并规范化为 JPEG 字节；内置 URL 内存缓存（支持 Discogs 鉴权注入）。"""
    url = str(cover_url or '').strip()
    if not url or not url.startswith('http'):
        return None
    with CACHE_LOCK:
        if url in COVER_CACHE:
            return COVER_CACHE[url]
    try:
        req_headers = None
        if 'discogs.com' in url.lower():
            req_headers = {'User-Agent': 'MusicMetadata/1.0 (+https://github.com/waves-dylan/MusicMetadata)'}
            dtok = get_discogs_token()
            if dtok:
                req_headers['Authorization'] = f'Discogs token={dtok}'
        raw = http_get_bytes(url, headers=req_headers, timeout=timeout)
        if not raw or not sniff_image(raw):
            return None
        jpg = normalize_jpeg(raw, EMBED_MAX_SIDE) or raw
        with CACHE_LOCK:
            COVER_CACHE[url] = jpg
        return jpg
    except Exception:
        return None


def resolve_artist_pic_url(
    artist_name: str,
    *,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    only_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    skip_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
) -> str:
    """根据艺术家名字在线检索艺术家头像/写真 URL（遍历按偏好排序的各音乐源）。"""
    name = first_artist(artist_name) or str(artist_name or '').strip()
    if not name or norm_core(name) in _VA_ARTISTS:
        return ''
    primary_order, extended_order = resolve_provider_order(
        prefer_sources=prefer_sources,
        only_sources=only_sources,
        skip_sources=skip_sources,
    )
    for plat in primary_order + extended_order:
        prov = PROVIDERS.get(plat)
        if prov and hasattr(prov, 'search_artist'):
            try:
                url = prov.search_artist(name)
                if url:
                    return url
            except Exception:
                continue
    return ''


def fetch_artist_bytes(
    artist_name: str = '',
    *,
    artist_url: str = '',
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    only_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    skip_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    timeout: int = 15,
) -> bytes | None:
    """下载在线艺术家图片并规范化为 800px JPEG 字节；内置 URL 与艺术家名字内存缓存。"""
    url = str(artist_url or '').strip()
    name = first_artist(artist_name) or str(artist_name or '').strip()
    if not url:
        if not name or norm_core(name) in _VA_ARTISTS:
            return None
        with CACHE_LOCK:
            if name in ARTIST_BYTES_CACHE:
                return ARTIST_BYTES_CACHE[name]
        url = resolve_artist_pic_url(
            name, prefer_sources=prefer_sources, only_sources=only_sources, skip_sources=skip_sources,
        )
        if not url:
            return None
    with CACHE_LOCK:
        if url in ARTIST_BYTES_CACHE:
            return ARTIST_BYTES_CACHE[url]
    try:
        raw = http_get_bytes(url, timeout=timeout)
        if not raw or not sniff_image(raw):
            return None
        jpg = normalize_artist_jpeg(raw, ARTIST_MAX_SIDE) or raw
        with CACHE_LOCK:
            ARTIST_BYTES_CACHE[url] = jpg
            if name:
                ARTIST_BYTES_CACHE[name] = jpg
        return jpg
    except Exception:
        return None


# ==================== 本地 pkl 与文件名线索 ====================
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


def _extract_pkl_metadata(pkl: dict) -> dict:
    """从 musicdl .pkl 缓存中提取发布日期、封面、艺术家、作曲及歌词等可用字段。"""
    out = {
        'release_date': _extract_pkl_release_date(pkl),
        'cover_url': '', 'title': '', 'artist': '', 'album': '',
        'composer': '', 'lyricist': '', 'arranger': '', 'lyrics': '',
    }
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
        out['lyrics'] = lrc.strip()
        l_creds = parse_all_lyric_credits(lrc)
        out['composer'] = l_creds.get('composer', '')
        out['lyricist'] = l_creds.get('lyricist', '')
        out['arranger'] = l_creds.get('arranger', '')
    return out


def _extract_local_track_credits(track: LibraryTrack) -> dict[str, str]:
    """尝试从音频本地已有内嵌歌词或同名 .lrc 文件中解析演职人员 (作曲/作词/编曲)。"""
    if not track or not getattr(track, 'path', ''):
        return {}
    try:
        from tag_io import read_audio_info, read_lrc_file
        lrc = read_lrc_file(track.path)
        if not lrc:
            info = read_audio_info(track.path)
            lrc = str(info.get('lyrics') or '')
        if lrc:
            return parse_all_lyric_credits(lrc)
    except Exception:
        pass
    return {}


def _extract_local_track_composer(track: LibraryTrack) -> str:
    """尝试从音频本地已有内嵌歌词或同名 .lrc 文件中解析作曲者。"""
    return _extract_local_track_credits(track).get('composer', '')


def _load_pkl_index(directory: str) -> dict:
    with CACHE_LOCK:
        if directory in PKL_CACHE:
            return PKL_CACHE[directory]
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
    with CACHE_LOCK:
        PKL_CACHE[directory] = idx
    return idx


def _parse_filename_clues(track: LibraryTrack) -> dict:
    return parse_filename_clues(track.filename or track.path, track.folder)


def _find_pkl_item(track: LibraryTrack, ident: str) -> dict | None:
    idx = _load_pkl_index(track.folder)
    if not idx:
        return None
    if ident and ident in idx:
        return idx[ident]
    stem = os.path.splitext(os.path.basename(track.filename))[0]
    if ' - ' in stem and idx.get('name:' + stem.rsplit(' - ', 1)[0]):
        return idx['name:' + stem.rsplit(' - ', 1)[0]]
    return idx.get('name:' + track.title) if track.title else None


# ==================== 匹配构造与跨平台聚合 ====================
_TEXT_MERGE_FIELDS = (
    'album_name', 'release_date', 'title', 'artist', 'albumartist',
    'composer', 'lyricist', 'arranger', 'genre', 'cover_url', 'artist_pic_url', 'lyrics',
)

_PLAT_CAPABILITIES: dict[str, set[str]] = {
    pid: set(p.capabilities - {'track'}) for pid, p in PROVIDERS.items()
}

_SOURCE_TAG_PREFIXES: tuple[tuple[str, str], ...] = (
    ('netease', '网易云'),
    ('qq', 'QQ'),
    ('itunes', 'Apple'),
    ('mb', 'MusicBrainz'),
    ('kugou', '酷狗'),
    ('migu', '咪咕'),
    ('baidu', '千千'),
    ('deezer', 'Deezer'),
    ('ytmusic', 'YouTube Music'),
    ('lrclib', 'LRCLIB'),
    ('spotify', 'Spotify'),
)


def _fetch_album_tracks_for(plat: str, album_id: str) -> list[dict]:
    if plat == 'kugou':
        kugou_provider.get_album_info(album_id)
    return PROVIDERS[plat].get_album_tracks(album_id)


def _make_match(
    plat: str, album_id: str, m: dict | None, total: int | None, source: str, album_name: str = '', **overrides,
) -> TrackMatch:
    alb_meta = get_album_meta(plat, album_id) if (plat and album_id) else {}
    tr = m or {}
    return TrackMatch(
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
        lyricist=str(overrides.get('lyricist') or tr.get('lyricist') or ''),
        arranger=str(overrides.get('arranger') or tr.get('arranger') or ''),
        genre=str(overrides.get('genre') or tr.get('genre') or alb_meta.get('genre') or ''),
        cover_url=str(overrides.get('cover_url') or tr.get('cover_url') or alb_meta.get('cover_url') or ''),
        artist_pic_url=str(overrides.get('artist_pic_url') or tr.get('artist_pic_url') or alb_meta.get('artist_pic_url') or ''),
        lyrics=str(overrides.get('lyrics') or ''),
        affinity_score=float(overrides.get('affinity_score', 0.0) or 0.0),
    )


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


def _enrich_song_credits(
    match: TrackMatch, plat: str, song_id: str, track: LibraryTrack, *,
    need_extra: bool = False, need_lyrics: bool = False,
    title: str = '', duration: float = 0.0, lrc_url: str = '',
) -> TrackMatch:
    if (not need_extra and not need_lyrics) or (not song_id and not lrc_url):
        return match
    need_comp = bool(need_extra and not (track.composer or match.composer))
    need_genre = bool(need_extra and not (track.genre or match.genre))
    need_lrc = bool(need_lyrics and not (track.has_lyrics or match.lyrics))
    if not (need_comp or need_genre or need_lrc):
        return match

    ex: dict = {}
    if plat == 'netease' and (need_comp or need_lrc) and song_id:
        ex = netease_provider.get_song_extra(song_id)
    elif plat == 'qq' and (need_comp or need_genre or need_lrc) and song_id:
        ex = qq_provider.get_song_extra(song_id)
    elif plat in ('kugou', 'ytmusic', 'lrclib', 'tadb') and (need_comp or need_lrc) and song_id:
        ex = PROVIDERS[plat].get_song_extra(song_id, need_lyrics=need_lrc, title=title or match.title, duration=duration)
    elif plat in ('migu', 'baidu') and (need_comp or need_lrc) and lrc_url:
        ex = PROVIDERS[plat].get_lyrics_by_url(song_id, lrc_url)

    if ex:
        if plat == 'qq':
            for k in ('composer', 'lyricist', 'arranger', 'genre', 'release_date'):
                if not getattr(match, k) and ex.get(k):
                    setattr(match, k, ex[k])
        for rk in ('composer', 'lyricist', 'arranger'):
            if not getattr(match, rk) and ex.get(rk):
                setattr(match, rk, ex[rk])
        if not match.genre and ex.get('genre'):
            match.genre = ex['genre']
        if need_lrc and not match.lyrics and ex.get('lyrics'):
            match.lyrics = ex['lyrics']
            lrc_c = parse_all_lyric_credits(match.lyrics)
            for rk in ('composer', 'lyricist', 'arranger'):
                if not getattr(match, rk) and lrc_c.get(rk):
                    setattr(match, rk, lrc_c[rk])
    return match


def _search_song_and_resolve_album(
    title: str, artist: str, duration: float, track: LibraryTrack | None = None,
    *, need_track: bool = True, need_date: bool = True, need_extra: bool = False, need_lyrics: bool = False,
    skip_plats: set[str] | None = None, allow_itunes_song: bool = True,
    tried_out: set[str] | None = None, plat_order: tuple[str, ...] | None = None,
    prefer_rich_lyrics: bool = False,
) -> TrackMatch | None:
    if not title:
        return None
    skip_plats = skip_plats or set()
    order = plat_order or ('netease', 'qq', 'itunes')
    term, tc, ac = f'{title} {artist}'.strip(), norm_core(title), norm_core(artist)
    date_fallback: TrackMatch | None = None
    dummy = track or LibraryTrack(path='', filename='', folder='')
    target_alb_for_scoring = (dummy.album or (track.album if track else '') or '').strip()

    for plat in order:
        if plat in skip_plats or (plat == 'itunes' and not allow_itunes_song):
            continue
        provider = PROVIDERS.get(plat)
        if not provider:
            continue
        label = PROVIDER_LABELS.get(plat, plat)
        seen_albs: set[str] = set()

        try:
            song_hits = provider.search_song_hits(term, title=title, artist=artist)
        except TypeError:
            song_hits = provider.search_song_hits(term)

        # 智能亲和度排序：基于专辑名相似度、首版年代、发行类型(精选/Live降权)进行智能加权重排
        ranked_hits = rank_song_hits(
            song_hits,
            target_title=title,
            target_artist=artist,
            target_album=target_alb_for_scoring,
            duration=duration,
            prefer_rich_lyrics=prefer_rich_lyrics,
        )

        for hit in ranked_hits:
            if hit.get('affinity_score', 0.0) <= -9000.0:
                continue
            stitle, sart, sdur = hit['title'], hit['artist'], hit['duration']
            if not song_matches(stitle, sdur, sart, tc, duration, ac):
                continue
            sid, aid, alb_name = hit['song_id'], hit['album_id'], hit['album_name']
            pub_date, cov = hit['release_date'], hit['cover_url']
            aff_score = float(hit.get('affinity_score', 0.0) or 0.0)

            if plat in ('netease', 'qq', 'itunes', 'deezer', 'ytmusic', 'spotify', 'tadb'):
                cov_kw = {'cover_url': cov} if cov else {}
                extra_cand_kw = {}
                if hit.get('composer'):
                    extra_cand_kw['composer'] = hit['composer']
                if aid:
                    if aid not in seen_albs and len(seen_albs) >= 3:
                        continue
                    seen_albs.add(aid)
                    tracks = _fetch_album_tracks_for(plat, aid)
                    rdate = get_album_date(plat, aid) or pub_date
                    m = match_in_tracks(tracks, title, duration, sid)
                    if m and (need_track or not need_date or rdate):
                        cand = _make_match(plat, aid, m, len(tracks), f'{label}(单曲反查)', alb_name, release_date=rdate, **cov_kw, **extra_cand_kw)
                        cand.affinity_score = aff_score
                        if dummy.album or alb_name:
                            cache_resolved_album(plat, dummy.album or alb_name, dummy.artist or sart, aid, alb_name)
                        return _enrich_song_credits(cand, plat, str(m.get('id') or sid), dummy, need_extra=need_extra, need_lyrics=need_lyrics)
                    can_fb = rdate or (plat in ('netease', 'qq') and (cov or need_lyrics)) or (plat in ('deezer', 'ytmusic', 'spotify', 'tadb') and (cov or need_lyrics or hit.get('composer')))
                    if not need_track and can_fb and date_fallback is None:
                        cand = _make_match(plat, aid, None, None, f'{label}(单曲反查)', alb_name, release_date=rdate, artist=sart, title=stitle, **cov_kw, **extra_cand_kw)
                        cand.affinity_score = aff_score
                        date_fallback = _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics)
                elif not need_track and (pub_date or (plat in ('netease', 'qq', 'ytmusic') and (cov or need_lyrics)) or (plat in ('deezer', 'spotify', 'tadb') and (cov or hit.get('composer')))) and date_fallback is None:
                    cand_kws = {'cover_url': cov} if cov else {}
                    if hit.get('genre'):
                        cand_kws['genre'] = hit['genre']
                    if hit.get('composer'):
                        cand_kws['composer'] = hit['composer']
                    cand = TrackMatch(None, source=f'{label}(单曲反查)', album_name=alb_name, release_date=pub_date, artist=sart, title=stitle, **cand_kws)
                    cand.affinity_score = aff_score
                    date_fallback = _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics)
            else:
                if plat == 'kugou' and aid and aid not in seen_albs and len(seen_albs) < 3:
                    seen_albs.add(aid)
                    tracks = _fetch_album_tracks_for('kugou', aid)
                    m = match_in_tracks(tracks, title, duration, sid)
                    if m:
                        cand = _make_match('kugou', aid, m, len(tracks), '酷狗音乐(单曲反查)', alb_name, cover_url=cov)
                        cand.affinity_score = aff_score
                        if dummy.album or alb_name:
                            cache_resolved_album('kugou', dummy.album or alb_name, dummy.artist or sart, aid, alb_name)
                        return _enrich_song_credits(cand, 'kugou', sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics, title=stitle, duration=sdur)
                rdate = get_album_date('kugou', aid) if (plat == 'kugou' and aid) else ''
                cand = TrackMatch(None, source=f'{label}(单曲搜索)', album_name=alb_name, release_date=rdate, title=stitle, artist=sart, cover_url=cov)
                cand.affinity_score = aff_score
                _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics, title=stitle, duration=sdur, lrc_url=hit.get('lrc_url'))
                if plat == 'lrclib' and need_lyrics and not cand.lyrics:
                    continue
                if not need_track:
                    return cand
                if date_fallback is None:
                    date_fallback = cand

        if tried_out is not None:
            tried_out.add(plat)

    return date_fallback


def _match_has_requested(
    match: TrackMatch | None, *, need_track: bool, need_date: bool, need_cover: bool, need_core: bool, need_extra: bool, need_lyrics: bool = False,
) -> bool:
    if match is None:
        return False
    if need_track:
        return match.tracknumber is not None
    if need_date and match.release_date:
        return True
    if need_cover and match.cover_url:
        return True
    if need_lyrics and match.lyrics:
        return True
    if need_core and any((match.title, match.artist, match.album_name, match.albumartist, match.release_date)):
        return True
    if need_extra and any((match.composer, match.genre, match.discnumber)):
        return True
    return False


def _resolve_by_song_id(
    plat: str, ident: str, track: LibraryTrack, title: str, album: str, duration: float,
    *, need_track: bool, need_date: bool, need_cover: bool, need_core: bool, need_extra: bool, need_lyrics: bool,
) -> TrackMatch | None:
    provider = PROVIDERS[plat]
    det = provider.get_song_detail(ident)
    if not isinstance(det, dict):
        return None
    alb = det.get('album') or {}
    if plat == 'qq':
        aid = str(alb.get('mid') or '')
        song_date = normalize_release_date(det.get('time_public') or alb.get('time_public') or det.get('pubtime'))
        song_art = extract_artist_names(det.get('singer'))
        song_title = str(det.get('title') or det.get('name') or '')
        cover_url, fallback_tn = '', det.get('index_album')
    elif plat == 'spotify':
        aid = str(alb.get('id') or '')
        song_date = normalize_release_date(alb.get('release_date'))
        song_art = extract_artist_names(det.get('artists'))
        song_title = str(det.get('name') or '')
        imgs = alb.get('images') or []
        cover_url = str((imgs[0] if imgs and isinstance(imgs[0], dict) else {}).get('url') or '')
        fallback_tn = det.get('track_number')
    else:
        aid = str(alb.get('id') or '')
        song_date = normalize_release_date(alb.get('publishTime') or det.get('publishTime'), is_ms=True)
        song_art = extract_artist_names(det.get('artists') or det.get('ar'))
        song_title = str(det.get('name') or '')
        cover_url, fallback_tn = str(alb.get('picUrl') or ''), None
    source_label = f'{provider.label}(ID直查)'
    alb_name = alb.get('name') or album
    req_kw = dict(need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics)
    cov_kw = {'cover_url': cover_url} if cover_url else {}

    if aid:
        tracks = _fetch_album_tracks_for(plat, aid)
        m = match_in_tracks(tracks, title, duration, ident)
        cand = _make_match(
            plat, aid, m, len(tracks) if m else None, source_label, alb_name,
            release_date=get_album_date(plat, aid) or song_date, artist=song_art, title=song_title, **cov_kw,
        )
        _enrich_song_credits(cand, plat, ident, track, need_extra=need_extra, need_lyrics=need_lyrics)
        if _match_has_requested(cand, need_track=need_track, **req_kw):
            return cand
    if need_track and fallback_tn:
        cand = TrackMatch(int(fallback_tn), None, None, source_label, alb_name, release_date=song_date, artist=song_art, title=song_title, **cov_kw)
        return _enrich_song_credits(cand, plat, ident, track, need_extra=need_extra, need_lyrics=need_lyrics)
    if not need_track:
        cand = TrackMatch(None, source=source_label, album_name=alb_name, release_date=song_date, artist=song_art, title=song_title, **cov_kw)
        _enrich_song_credits(cand, plat, ident, track, need_extra=need_extra, need_lyrics=need_lyrics)
        if _match_has_requested(cand, need_track=False, **req_kw):
            return cand
    return None


def _resolve_metadata_core(
    track: LibraryTrack, clues: dict, title: str, artist: str, album: str, duration: float,
    *, need_track: bool, need_date: bool, need_cover: bool = False, need_core: bool = False, need_extra: bool = False, need_lyrics: bool = False,
    skip_plats: set[str] | None = None, allow_mb: bool = True,
    allow_itunes_song: bool = True, tried_out: set[str] | None = None,
    plat_order: tuple[str, ...] | None = None,
    prefer_rich_lyrics: bool = False,
) -> TrackMatch:
    ident = clues['identifier']
    skip_plats = skip_plats or set()
    order = plat_order or PRIMARY_PROVIDERS

    # 1. 本地 .pkl 缓存线索
    pkl = _find_pkl_item(track, ident) if not skip_plats else None
    if pkl:
        src = str(pkl.get('source') or '')
        raw = pkl.get('raw_data') or {}
        sd = raw.get('search') if isinstance(raw, dict) and isinstance(raw.get('search'), dict) else {}
        pid, pt, pd = str(pkl.get('identifier') or ident or ''), str(pkl.get('song_name') or title), to_seconds(pkl.get('duration_s')) or duration
        pkl_meta = _extract_pkl_metadata(pkl)
        pkl_date = pkl_meta['release_date']
        alb_id, alb_name, plat, fallback_tn = None, album, '', None
        if 'QQ' in src:
            alb = sd.get('album') or {}
            alb_id, alb_name, plat = alb.get('mid') or alb.get('albumMid') or sd.get('albummid'), alb.get('name') or album, 'qq'
            fallback_tn = sd.get('index_album')
        elif 'Netease' in src:
            alb = sd.get('al') or sd.get('album') or {}
            alb_id, alb_name, plat = alb.get('id') or sd.get('albumId'), alb.get('name') or album, 'netease'
        elif 'Kugou' in src:
            alb_id, plat = sd.get('AlbumID') or sd.get('album_id') or sd.get('albumid'), 'kugou'
        elif 'Kuwo' in src:
            alb_id, plat = sd.get('ALBUMID') or sd.get('albumId') or sd.get('albumid'), 'kuwo'
        elif 'Spotify' in src:
            alb = ((sd.get('item') or {}).get('data') or (sd.get('itemV2') or {}).get('data') or {}).get('albumOfTrack') or {}
            alb_id, alb_name, plat = alb.get('id') or (alb.get('uri') or '').rsplit(':', 1)[-1], alb.get('name') or album, 'spotify'
        elif 'Apple' in src:
            attrs = sd.get('attributes') or {}
            if attrs.get('trackNumber'):
                return TrackMatch(
                    int(attrs['trackNumber']), int(attrs['discNumber']) if attrs.get('discNumber') else None, None,
                    'Apple Music(本地缓存)', album, release_date=pkl_date,
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], lyrics=pkl_meta['lyrics'],
                )
        label = PROVIDERS[plat].label if plat in PROVIDERS else ''
        if not need_track and not need_extra and not (need_cover and not pkl_meta['cover_url']) and not (need_lyrics and not pkl_meta['lyrics']) and pkl_date:
            return TrackMatch(
                None, source=f'{label or "musicdl"}(本地缓存)', album_name=alb_name,
                release_date=pkl_date, cover_url=pkl_meta['cover_url'],
                title=pkl_meta['title'], artist=pkl_meta['artist'],
                composer=pkl_meta['composer'], lyrics=pkl_meta['lyrics'],
            )
        if alb_id and plat in PROVIDERS:
            tracks = _fetch_album_tracks_for(plat, str(alb_id))
            m = match_in_tracks(tracks, pt, pd, pid)
            if m:
                cand = _make_match(
                    plat, str(alb_id), m, len(tracks), f'{label}(本地缓存)', alb_name,
                    release_date=pkl_date or get_album_date(plat, str(alb_id)),
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], lyrics=pkl_meta['lyrics'],
                )
                return _enrich_song_credits(cand, plat, str(m.get('id') or pid), track, need_extra=need_extra, need_lyrics=need_lyrics)
            if not need_track and (get_album_meta(plat, str(alb_id)) or pkl_date or pkl_meta['cover_url'] or pkl_meta['lyrics']):
                cand = _make_match(
                    plat, str(alb_id), None, None, f'{label}(本地缓存)', alb_name,
                    release_date=pkl_date or get_album_date(plat, str(alb_id)),
                    cover_url=pkl_meta['cover_url'], title=pkl_meta['title'], artist=pkl_meta['artist'],
                    composer=pkl_meta['composer'], lyrics=pkl_meta['lyrics'],
                )
                return _enrich_song_credits(cand, plat, pid, track, need_extra=need_extra, need_lyrics=need_lyrics)
        if need_track and fallback_tn:
            return TrackMatch(
                int(fallback_tn), None, None, f'{label}(index_album)', alb_name,
                release_date=pkl_date, cover_url=pkl_meta['cover_url'],
                title=pkl_meta['title'], artist=pkl_meta['artist'],
                composer=pkl_meta['composer'], lyrics=pkl_meta['lyrics'],
            )

    # 2. 内嵌网易云 163 key
    if 'netease' not in skip_plats and track.netease_meta and track.netease_meta.get('albumId'):
        aid = str(track.netease_meta['albumId'])
        tracks = netease_provider.get_album_tracks(aid)
        mid = str(track.netease_meta.get('musicId') or '')
        m = match_in_tracks(tracks, title, duration, mid)
        cand = _make_match('netease', aid, m, len(tracks) if m else None, '网易云音乐(163 key)', str(track.netease_meta.get('album') or album))
        _enrich_song_credits(cand, 'netease', str((m or {}).get('id') or mid), track, need_extra=need_extra, need_lyrics=need_lyrics)
        if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
            return cand

    # 3. 文件名自带歌曲 ID 直查
    id_plat = (
        'qq' if (ident and _QQ_MID_RE.match(ident))
        else ('netease' if (ident and _NETEASE_ID_RE.match(ident))
              else ('spotify' if (ident and has_spotify_credentials() and _SPOTIFY_ID_RE.match(ident)) else ''))
    )
    if id_plat and id_plat not in skip_plats:
        id_cand = _resolve_by_song_id(
            id_plat, ident, track, title, album, duration,
            need_track=need_track, need_date=need_date, need_cover=need_cover,
            need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics,
        )
        if id_cand is not None:
            return id_cand

    # 4. 专辑名+歌手依次检索（支持偏好顺序）
    track_fallback: TrackMatch | None = None
    album_fallback: TrackMatch | None = None
    if album:
        terms = [f'{album} {artist}'.strip(), album] if artist else [album]
        for plat in order:
            prov = PROVIDERS.get(plat)
            if plat in skip_plats or not prov or not prov.supports_album_search or not prov.is_available:
                continue
            if plat == 'mb' and not allow_mb:
                continue
            label = prov.label
            query_terms = [album] if plat == 'mb' else terms
            for term in query_terms:
                aid, aname, artist_ok = prov.search_album(term, album, artist)
                if not aid:
                    continue
                tracks = prov.get_album_tracks(aid)
                m = match_in_tracks(tracks, title, duration) if title else None
                if (
                    m and not artist_ok and len(tracks) == 1
                    and duration > 0 and m.get('duration') and abs(float(m['duration']) - duration) > 25.0
                ):
                    m = None
                if m:
                    cand = _make_match(plat, aid, m, len(tracks), label, aname)
                    _enrich_song_credits(cand, plat, str(m.get('id') or ''), track, need_extra=need_extra, need_lyrics=need_lyrics)
                    if _match_has_requested(cand, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
                        if artist_ok:
                            return cand
                        if track_fallback is None:
                            track_fallback = cand
                elif not need_track and artist_ok:
                    cand = _make_match(plat, aid, None, None, label, aname)
                    if _match_has_requested(cand, need_track=False, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
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

    # 5. 单曲反查兜底（支持按偏好顺序查找单曲及歌词/封面）
    # 若调用仅是为了补充可选附加字段（如 composer/genre/discnumber），在专辑未命中时无需进行全平台单曲搜索
    if not need_track and not need_date and not need_cover and not need_core and not need_lyrics:
        if album_fallback is not None:
            return album_fallback
        if track_fallback is not None:
            return track_fallback
        return TrackMatch(None, reason='未在在线平台匹配到附加元数据')

    song_order = tuple(p for p in order if p != 'mb')
    song_match = _search_song_and_resolve_album(
        title, artist, duration, track,
        need_track=need_track, need_date=need_date, need_extra=need_extra, need_lyrics=need_lyrics,
        skip_plats=skip_plats, allow_itunes_song=allow_itunes_song, tried_out=tried_out, plat_order=song_order,
        prefer_rich_lyrics=prefer_rich_lyrics,
    )
    if _match_has_requested(song_match, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
        return song_match

    # 若未命中且无艺术家，但有从目录推断出的专辑名（单级目录形如 邓丽君/01 - 小城故事.dff，目录名实为歌手名）
    if not artist and album and album != title:
        cand_art_match = _search_song_and_resolve_album(
            title, album, duration, track,
            need_track=need_track, need_date=need_date, need_extra=need_extra, need_lyrics=need_lyrics,
            skip_plats=skip_plats, allow_itunes_song=allow_itunes_song, tried_out=tried_out, plat_order=song_order,
            prefer_rich_lyrics=prefer_rich_lyrics,
        )
        if _match_has_requested(cand_art_match, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
            return cand_art_match
        if cand_art_match and (song_match is None or not song_match.source or (cand_art_match.cover_url and not song_match.cover_url)):
            song_match = cand_art_match
    if not need_track and album_fallback is not None:
        return album_fallback
    if not need_track and track_fallback is not None:
        return track_fallback
    return TrackMatch(
        None,
        reason='未在在线平台唯一匹配到专辑曲目' if need_track else '未在在线平台匹配到元数据',
    )


def _remaining_needs(
    res: TrackMatch, track: LibraryTrack, *,
    need_date: bool, need_cover: bool, need_core: bool, need_extra: bool, need_lyrics: bool = False,
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
        'discnumber': bool(need_extra and _want('discnumber') and not (track.disk_disc_int or (track.disk_discnumber or '').strip()) and res.discnumber is None),
        'lyrics': bool(need_lyrics and _want('lyrics') and not (track.has_lyrics or res.lyrics)),
    }


def _needs_further_enrichment(
    res: TrackMatch, track: LibraryTrack, *,
    need_date: bool, need_cover: bool, need_core: bool, need_extra: bool, need_lyrics: bool = False,
    need_fields: set[str] | frozenset[str] | None = None,
) -> tuple[bool, bool, bool, bool, bool]:
    rem = _remaining_needs(
        res, track, need_date=need_date, need_cover=need_cover, need_core=need_core,
        need_extra=need_extra, need_lyrics=need_lyrics, need_fields=need_fields,
    )
    rem_date = rem['date']
    rem_cover = rem['cover']
    rem_core = any(rem[k] for k in ('title', 'artist', 'album', 'albumartist'))
    rem_extra = any(rem[k] for k in ('genre', 'composer', 'discnumber'))
    rem_lyrics = rem['lyrics']
    return rem_date, rem_cover, rem_core, rem_extra, rem_lyrics


def resolve_track_metadata(
    track: LibraryTrack, *,
    need_track: bool = True,
    need_date: bool = True,
    need_cover: bool = False,
    need_artist_art: bool = False,
    need_core: bool = False,
    need_extra: bool = False,
    need_lyrics: bool = False,
    need_fields: set[str] | frozenset[str] | None = None,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    only_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    skip_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    prefer_rich_lyrics: bool = False,
) -> TrackMatch:
    """为单首歌曲联网解析并合并多平台元数据（音轨/碟号/日期/封面/艺术家/作曲/流派/歌词）。"""
    clues = _parse_filename_clues(track)
    ident = clues['identifier']
    title = (track.title or clues['inferred_title']).strip()
    artist = first_artist(track.artist or track.albumartist or clues['inferred_artist'])
    album = (track.album or clues['inferred_album']).strip()
    duration = float(track.duration or 0.0)
    if not title and not (not need_track and (album or ident or track.netease_meta or _find_pkl_item(track, ident))):
        return TrackMatch(None, reason='缺少标题与文件名线索')

    primary_order, extended_order = resolve_provider_order(
        prefer_sources=prefer_sources,
        only_sources=only_sources,
        skip_sources=skip_sources,
    )
    if not primary_order and not extended_order:
        return TrackMatch(None, reason='所有在线数据源均被跳过')

    # 若仅缺少制作人员(词/曲/编)且本地已有内嵌歌词/LRC写明，直接秒级返回，免去跨国网络查询
    want_credits = bool(
        (need_extra or (need_fields and any(f in need_fields for f in ('composer', 'lyricist', 'arranger'))))
        and not (track.composer and track.lyricist and track.arranger)
    )
    if want_credits and not any((need_track, need_date, need_cover, need_artist_art, need_core, need_lyrics)):
        local_creds = _extract_local_track_credits(track)
        if any(local_creds.values()):
            return TrackMatch(
                None, source='本地歌词',
                composer=local_creds.get('composer', ''),
                lyricist=local_creds.get('lyricist', ''),
                arranger=local_creds.get('arranger', ''),
            )

    tried: set[str] = set()
    res = _resolve_metadata_core(
        track, clues, title, artist, album, duration,
        need_track=need_track, need_date=need_date, need_cover=need_cover,
        need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics,
        allow_mb=('mb' in primary_order), tried_out=tried, plat_order=primary_order,
        prefer_rich_lyrics=prefer_rich_lyrics,
    )
    primary_matched = bool(res.source)

    pkl = _find_pkl_item(track, ident)
    if pkl:
        pm = _extract_pkl_metadata(pkl)
        _merge_match(res, TrackMatch(
            None, source=res.source or 'musicdl缓存', album_name=pm['album'], release_date=pm['release_date'],
            title=pm['title'], artist=pm['artist'], composer=pm.get('composer', ''),
            lyricist=pm.get('lyricist', ''), arranger=pm.get('arranger', ''),
            cover_url=pm['cover_url'], lyrics=pm['lyrics'],
        ))

    if want_credits and not (res.composer and res.lyricist and res.arranger):
        local_creds = _extract_local_track_credits(track)
        for rk in ('composer', 'lyricist', 'arranger'):
            if not getattr(res, rk) and local_creds.get(rk):
                setattr(res, rk, local_creds[rk])
                if not res.source:
                    res.source = '本地歌词'

    # 若主平台未命中且仍缺少除纯粹 composer 外的必要字段（或缺少歌词），继续尝试扩展中文源（酷狗/咪咕/千千）
    if not primary_matched and extended_order and (need_track or need_date or need_cover or need_core or need_lyrics):
        ext_res = _resolve_metadata_core(
            track, clues, title, artist, album, duration,
            need_track=need_track, need_date=need_date, need_cover=need_cover,
            need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics,
            allow_mb=False, tried_out=tried, plat_order=extended_order,
            prefer_rich_lyrics=prefer_rich_lyrics,
        )
        _merge_match(res, ext_res)
        primary_matched = bool(res.source)

    # 若主平台缺少部分字段，仅针对有能力补齐缺失字段的剩余平台做补充合并
    rem_date, rem_cover, rem_core, rem_extra, rem_lyrics = _needs_further_enrichment(
        res, track, need_date=need_date, need_cover=need_cover, need_core=need_core,
        need_extra=need_extra, need_lyrics=need_lyrics, need_fields=need_fields,
    )
    if primary_matched and any((rem_date, rem_cover, rem_core, rem_extra, rem_lyrics)) and (album or title):
        src_l = res.source
        for pid, prefix in _SOURCE_TAG_PREFIXES:
            if prefix in src_l:
                tried.add(pid)
                break

        # 仅缺失附加标签（如 composer/genre）时，只尝试主平台（避免轮询全部国际扩展源造成网络阻塞）
        if rem_extra and not (rem_date or rem_cover or rem_core or rem_lyrics) and not prefer_sources:
            enrich_order = tuple(primary_order)
        elif rem_lyrics and not (rem_date or rem_cover or rem_core or rem_extra) and not prefer_sources:
            enrich_order = tuple(p for p in (primary_order + extended_order) if 'lyrics' in _PLAT_CAPABILITIES.get(p, set()))
        else:
            enrich_order = primary_order + extended_order

        all_enabled = set(enrich_order)
        for plat in enrich_order:
            if plat in tried:
                continue
            prov = PROVIDERS.get(plat)
            if not prov or not prov.is_available:
                continue
            rem = _remaining_needs(
                res, track, need_date=need_date, need_cover=need_cover, need_core=need_core,
                need_extra=need_extra, need_lyrics=need_lyrics, need_fields=need_fields,
            )
            missing_keys = {k for k, v in rem.items() if v}
            if not missing_keys:
                break
            if not (missing_keys & _PLAT_CAPABILITIES.get(plat, set())):
                continue
            if plat == 'mb' and (not album or is_mb_circuit_open()):
                continue
            rem_date = rem['date']
            rem_cover = rem['cover']
            rem_core = any(rem[k] for k in ('title', 'artist', 'album', 'albumartist'))
            rem_extra = any(rem[k] for k in ('genre', 'composer', 'discnumber'))
            rem_lyrics = rem['lyrics']
            allow_itunes_song = any(rem[k] for k in ('date', 'cover', 'title', 'artist', 'album', 'albumartist', 'genre'))
            skip = all_enabled - {plat}
            extra_res = _resolve_metadata_core(
                track, clues, title, artist, album, duration,
                need_track=False, need_date=rem_date, need_cover=rem_cover,
                need_core=rem_core, need_extra=rem_extra, need_lyrics=rem_lyrics,
                skip_plats=skip, allow_mb=(plat == 'mb'), allow_itunes_song=allow_itunes_song,
                plat_order=(plat,),
                prefer_rich_lyrics=prefer_rich_lyrics,
            )
            _merge_match(res, extra_res)
            # 若全部所需字段均已补齐，立即退出补充轮询
            now_rem = _remaining_needs(
                res, track, need_date=need_date, need_cover=need_cover, need_core=need_core,
                need_extra=need_extra, need_lyrics=need_lyrics, need_fields=need_fields,
            )
            if not any(now_rem.values()):
                break

    if (need_artist_art or (need_fields and 'artist_cover' in need_fields)) and not res.artist_pic_url:
        art_target = res.albumartist or res.artist or track.albumartist or track.artist or clues.get('inferred_artist') or ''
        if art_target:
            res.artist_pic_url = resolve_artist_pic_url(
                art_target, prefer_sources=prefer_sources, only_sources=only_sources, skip_sources=skip_sources,
            )

    return res


def resolve_release_date(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑发布日期。"""
    return resolve_track_metadata(track, need_track=False, need_date=True)


def resolve_track_number(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑内音轨号（同时尽可能附带发布日期）。"""
    return resolve_track_metadata(track, need_track=True, need_date=True)


def search_candidate_album_covers(
    title: str = '',
    artist: str = '',
    *,
    album: str = '',
    limit: int = 14,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """为曲目在线检索多个候选专辑封面，严格遵循三级梯队策略并根据原版/首版大碟智能加权打分：
    1. 第一优先：“专辑名 + 艺术家”（直接命中真实大碟，杜绝被演唱会/Live/精选集带偏）
    2. 第二优先：“歌曲名 + 艺术家”（单曲反查母碟版本，回退补充）
    3. 第三优先：“歌曲名”（或单一关键词独立单搜，应对歌手名缺失或外语翻译不一致）
    """
    def _notify(evt: dict):
        if on_progress:
            try:
                on_progress(evt)
            except Exception:
                pass

    t_clean = str(title or '').strip()
    a_clean = first_artist(artist) or str(artist or '').strip()
    alb_clean = str(album or '').strip()

    # 若未显式传入 artist，但 title 或 album 中包含空格，尝试推断可能的歌手后缀
    if not a_clean and ' ' in t_clean:
        p_t, p_a = t_clean.rsplit(' ', 1)
        if len(p_a) >= 2 and not p_a.isdigit():
            t_clean, a_clean = p_t.strip(), p_a.strip()

    # 如果推断出的专辑名与歌手名相同（如把歌手目录误当成专辑名），清空专辑名
    if alb_clean and a_clean and alb_clean.lower() == a_clean.lower():
        alb_clean = ''

    # 构建三级梯队搜索词列表
    query_chain: list[dict] = []

    # 1. 第一优先：“专辑名 + 艺术家”
    if alb_clean and a_clean:
        term1 = f'{alb_clean} {a_clean}'.strip()
        query_chain.append({'term': term1, 'kind': 'album+artist', 'target_alb': alb_clean})

    # 2. 第二优先：“歌曲名 + 艺术家”
    if t_clean and a_clean:
        term2 = f'{t_clean} {a_clean}'.strip()
        if not any(q['term'] == term2 for q in query_chain):
            query_chain.append({'term': term2, 'kind': 'title+artist', 'target_alb': alb_clean or t_clean})

    # 3. 第三优先：“歌曲名”（或单一核心词独立检索）
    if t_clean and not any(q['term'] == t_clean for q in query_chain):
        query_chain.append({'term': t_clean, 'kind': 'title_only', 'target_alb': alb_clean or t_clean})

    # 4. 补充兜底：独立“专辑名”
    if alb_clean and not any(q['term'] == alb_clean for q in query_chain):
        query_chain.append({'term': alb_clean, 'kind': 'album_only', 'target_alb': alb_clean})

    if not query_chain:
        raw_fallback = t_clean or alb_clean or a_clean
        if not raw_fallback:
            return []
        query_chain.append({'term': raw_fallback, 'kind': 'raw', 'target_alb': raw_fallback})

    candidates: list[dict] = []
    seen_covers: set[str] = set()

    sources = list(prefer_sources) if prefer_sources else ['qq', 'netease', 'itunes']
    for plat in ('qq', 'netease', 'itunes'):
        if plat not in sources:
            sources.append(plat)

    active_sources = []
    for plat in sources:
        prov = PROVIDERS.get(plat)
        if prov and prov.is_available:
            active_sources.append({'platform': plat, 'label': prov.label})

    _notify({
        'type': 'start',
        'sources': active_sources,
        'title': t_clean,
        'artist': a_clean,
        'album': alb_clean,
        'total_tiers': len(query_chain),
    })

    for tier_idx, q_item in enumerate(query_chain, 1):
        term = q_item['term']
        is_album_level = 'album' in q_item['kind']
        target_name = q_item['target_alb']
        kind = q_item['kind']

        tier_name = {
            'album+artist': '第一优先：专辑名 + 艺术家（精确定位大碟）',
            'title+artist': '第二优先：歌曲名 + 艺术家（单曲反查母碟）',
            'title_only': '第三优先：歌曲名独立检索（兜底容错）',
            'album_only': '补充梯队：专辑名独立检索',
            'raw': '关键词检索',
        }.get(kind, kind)

        _notify({
            'type': 'tier_start',
            'tier_idx': tier_idx,
            'total_tiers': len(query_chain),
            'tier_name': tier_name,
            'kind': kind,
            'term': term,
            'found_count': len(candidates),
        })

        for plat in sources:
            prov = PROVIDERS.get(plat)
            if not prov or not prov.is_available:
                continue

            prev_cnt = len(candidates)
            _notify({
                'type': 'platform_start',
                'platform': plat,
                'platform_label': prov.label,
                'term': term,
                'kind': kind,
                'found_count': prev_cnt,
            })

            # 若当前为专辑级检索，且音源支持直搜专辑，优先直查该大碟
            if is_album_level and prov.supports_album_search and hasattr(prov, 'search_album'):
                try:
                    aid, aname, artist_ok = prov.search_album(term, alb_clean or term, a_clean)
                    if aid:
                        meta = get_album_meta(plat, aid)
                        cov = meta.get('cover_url') or ''
                        if cov and cov not in seen_covers:
                            seen_covers.add(cov)
                            rdate = meta.get('release_date') or ''
                            sart = a_clean or meta.get('artist') or ''

                            # 计算匹配得分
                            score = 0
                            an_norm = norm_core(aname or target_name)
                            t_norm = norm_core(target_name)

                            clean_an = re.sub(r'^(复黑王|环球复黑王|btb|首版|经典|lp|cd)\s*', '', an_norm).strip()
                            if a_clean:
                                clean_an = re.sub(rf'\s*{re.escape(norm_core(a_clean))}$', '', clean_an).strip()
                            if not clean_an:
                                clean_an = an_norm

                            if t_norm in an_norm:
                                score += 50
                                if an_norm == t_norm or clean_an == t_norm:
                                    score += 30
                            if any(k in an_norm for k in ('精选', '选辑', '演唱会', 'live', 'collection', '纪念', '经典金曲')):
                                score -= 40
                            if any(k in an_norm for k in ('复黑王', '首版', 'btb', '原声', 'lp', '黑胶')):
                                score += 25
                            if rdate:
                                try:
                                    yr = int(rdate[:4])
                                    if yr <= 1995:
                                        score += max(0, (2000 - yr))
                                except Exception:
                                    pass

                            candidates.append({
                                'source': prov.label,
                                'platform': plat,
                                'album_id': aid,
                                'album_name': aname or target_name,
                                'artist': sart,
                                'release_date': rdate,
                                'cover_url': cov,
                                'score': score + 10,  # 官方大碟直查额外加 10 分
                            })
                except Exception:
                    pass

            # 统一执行单曲命中反查候选专辑
            try:
                hits = prov.search_song_hits(term)
            except Exception:
                hits = []

            for h in hits:
                aid = h.get('album_id')
                aname = (h.get('album_name') or '').strip()
                if not aid or not aname:
                    continue

                try:
                    prov.get_album_tracks(aid)
                except Exception:
                    pass
                meta = get_album_meta(plat, aid)
                cov = meta.get('cover_url') or h.get('cover_url') or ''
                if not cov or cov in seen_covers:
                    continue
                seen_covers.add(cov)

                rdate = meta.get('release_date') or h.get('release_date') or ''
                sart = h.get('artist') or a_clean

                # 计算品质与原版匹配度得分
                score = 0
                an_norm = norm_core(aname)
                t_norm = norm_core(target_name or t_clean or alb_clean)

                # 剥离版本前缀与歌手后缀，提取核心专辑名对比
                clean_an = re.sub(r'^(复黑王|环球复黑王|btb|首版|经典|lp|cd)\s*', '', an_norm).strip()
                if a_clean:
                    clean_an = re.sub(rf'\s*{re.escape(norm_core(a_clean))}$', '', clean_an).strip()
                if not clean_an:
                    clean_an = an_norm

                if t_norm in an_norm:
                    score += 50
                    if an_norm == t_norm or clean_an == t_norm:
                        score += 30
                # 常见精选、现场、合辑降权
                if any(k in an_norm for k in ('精选', '选辑', '演唱会', 'live', 'collection', '纪念', '经典金曲')):
                    score -= 40
                # 复黑、首版、原声带、黑胶等加权
                if any(k in an_norm for k in ('复黑王', '首版', 'btb', '原声', 'lp', '黑胶')):
                    score += 25
                if rdate:
                    try:
                        yr = int(rdate[:4])
                        if yr <= 1995:
                            score += max(0, (2000 - yr))
                    except Exception:
                        pass

                candidates.append({
                    'source': prov.label,
                    'platform': plat,
                    'album_id': aid,
                    'album_name': aname,
                    'artist': sart,
                    'release_date': rdate,
                    'cover_url': cov,
                    'score': score,
                })

            added_cnt = len(candidates) - prev_cnt
            _notify({
                'type': 'platform_done',
                'platform': plat,
                'platform_label': prov.label,
                'added': added_cnt,
                'found_count': len(candidates),
                'term': term,
            })

        # 检查是否已捕获足够丰富的优质候选（若第一/第二阶段已经命中高分大碟且数量充足，无需进行下一级模糊单搜）
        if len(candidates) >= 6 and any(c['score'] >= 80 for c in candidates):
            _notify({
                'type': 'early_stop',
                'found_count': len(candidates),
                'message': '已命中足够丰富的高品质首版大碟，提前完成检索',
            })
            break

    _notify({
        'type': 'scoring',
        'found_count': len(candidates),
        'message': f'正在对 {len(candidates)} 个候选版本进行首版与品质加权排序...',
    })

    def _cand_sort_key(c):
        sc = c['score']
        rd = c.get('release_date') or ''
        digits = re.sub(r'\D', '', rd)[:8]
        date_num = int(digits.ljust(8, '0')) if len(digits) >= 4 else 99999999
        return (sc, -date_num)

    candidates.sort(key=_cand_sort_key, reverse=True)

    _notify({
        'type': 'done',
        'found_count': len(candidates),
        'final_count': len(candidates[:limit]),
        'candidates': candidates[:limit],
    })

    return candidates[:limit]


def search_candidate_artist_pics(
    artist_name: str,
    *,
    track_hint: Any = None,
    limit: int = 14,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    on_progress: Callable[[dict], None] | None = None,
) -> list[dict]:
    """为艺术家在线跨国并发检索多个候选头像与宽幅舞台写真壁纸：
    1. 国内主流官方源（QQ音乐、网易云音乐、酷狗）：获取大头高清肖像（800x800~1000x1000）、曲目与专辑数及别名
    2. 海外专业开放库（Fanart.tv、TheAudioDB）：获取 1920x1080 社区精选无水印宽屏舞台壁纸与艺术背景
    3. 全流程支持实时事件反馈与首选/品质加权排序
    """
    def _notify(evt: dict):
        if on_progress:
            try:
                on_progress(evt)
            except Exception:
                pass

    a_clean = first_artist(artist_name) or str(artist_name or '').strip()
    if not a_clean or norm_core(a_clean) in _VA_ARTISTS:
        return []

    hint_title = ''
    if track_hint is not None:
        if hasattr(track_hint, 'title'):
            hint_title = str(track_hint.title or '').strip()
        else:
            hint_title = str(track_hint or '').strip()

    # 音源梯队
    sources = list(prefer_sources) if prefer_sources else ['qq', 'netease', 'fanart', 'tadb', 'kugou']
    for plat in ('qq', 'netease', 'fanart', 'tadb', 'kugou'):
        if plat not in sources:
            sources.append(plat)

    active_sources = []
    for plat in sources:
        prov = PROVIDERS.get(plat)
        if prov and prov.is_available:
            active_sources.append({'platform': plat, 'label': prov.label})

    _notify({
        'type': 'start',
        'sources': active_sources,
        'artist': a_clean,
        'hint': hint_title,
        'total_sources': len(active_sources),
    })

    candidates: list[dict] = []
    seen_urls: set[str] = set()

    for item in active_sources:
        plat = item['platform']
        plabel = item['label']
        prov = PROVIDERS.get(plat)
        if not prov:
            continue

        prev_cnt = len(candidates)
        _notify({
            'type': 'platform_start',
            'platform': plat,
            'platform_label': plabel,
            'artist': a_clean,
            'found_count': prev_cnt,
        })

        raw_cands: list[dict] = []
        try:
            if hasattr(prov, 'search_artist_candidates'):
                raw_cands = prov.search_artist_candidates(a_clean, track_hint=hint_title)
            elif hasattr(prov, 'search_artist'):
                url = prov.search_artist(a_clean)
                if url:
                    raw_cands = [{
                        'source': prov.label,
                        'platform': plat,
                        'artist_name': a_clean,
                        'pic_url': url,
                        'aspect_type': 'square',
                        'resolution': '800×800',
                        'width': 800,
                        'height': 800,
                        'clues': '官方歌手头像',
                        'raw_meta': {},
                    }]
        except Exception:
            raw_cands = []

        for cand in raw_cands:
            pic_url = str(cand.get('pic_url') or '').strip()
            if not pic_url or pic_url in seen_urls:
                continue
            seen_urls.add(pic_url)

            # 打分模型
            score = 0
            c_name = norm_core(cand.get('artist_name') or '')
            t_name = norm_core(a_clean)

            if c_name == t_name:
                score += 50
            elif t_name in c_name or c_name in t_name:
                score += 25

            # 同名线索加分
            clues = str(cand.get('clues') or '')
            if hint_title and norm_core(hint_title) in norm_core(clues):
                score += 30

            # 画幅与分辨率加权
            w = int(cand.get('width') or 0)
            aspect = cand.get('aspect_type') or 'square'
            if aspect == 'landscape':
                if w >= 1920:
                    score += 25
                elif w >= 1200:
                    score += 15
            else:
                if w >= 1000:
                    score += 20
                elif w >= 800:
                    score += 15
                elif 0 < w < 500:
                    score -= 20

            # 平台权威度
            if plat in ('fanart', 'tadb') and aspect == 'landscape':
                score += 10
            if plat in ('qq', 'netease'):
                score += 10

            cand_entry = dict(cand)
            cand_entry['score'] = score
            candidates.append(cand_entry)

        added = len(candidates) - prev_cnt
        _notify({
            'type': 'platform_done',
            'platform': plat,
            'platform_label': plabel,
            'added': added,
            'found_count': len(candidates),
        })

    _notify({
        'type': 'scoring',
        'found_count': len(candidates),
        'message': f'正在对 {len(candidates)} 个候选写真进行品质与构图加权排序...',
    })

    # 排序：得分降序，宽度降序
    def _cand_sort(c):
        return (c.get('score', 0), int(c.get('width', 0)))

    candidates.sort(key=_cand_sort, reverse=True)

    # 赋予推荐徽章
    for idx, c in enumerate(candidates):
        if idx == 0 and c.get('score', 0) >= 60:
            c['badge'] = '★首选'
        elif c.get('score', 0) >= 50:
            c['badge'] = '推荐'
        else:
            c['badge'] = ''

    _notify({
        'type': 'done',
        'found_count': len(candidates),
        'final_count': len(candidates[:limit]),
        'candidates': candidates[:limit],
    })

    return candidates[:limit]
