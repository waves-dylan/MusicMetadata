"""多平台在线专辑与单曲元数据检索与聚合（基于模块化 providers/ 与标准库 HTTP）。"""

from __future__ import annotations

import io
import os
import pickle
import re
import time
import urllib.request

from art_repair import EMBED_MAX_SIDE, normalize_jpeg, sniff_image
from foobar_library import LibraryTrack
from providers import (
    ONLINE_PROVIDERS,
    PRIMARY_PROVIDERS,
    PROVIDERS,
    PROVIDER_LABELS,
    TrackMatch,
    deezer_provider,
    get_netease_api_base,
    get_spotify_credentials,
    has_spotify_credentials,
    kugou_provider,
    lrclib_provider,
    netease_provider,
    parse_source_list,
    qq_provider,
    resolve_provider_order,
    set_netease_api_base,
    set_spotify_credentials,
    ytmusic_provider,
)
from providers.base import (
    CACHE_LOCK,
    COVER_CACHE,
    PKL_CACHE,
    USER_AGENT,
    bump_cache_gen,
    extract_artist_names,
    first_artist,
    get_album_date,
    get_album_meta,
    match_in_tracks,
    norm_core,
    normalize_release_date,
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


def fetch_cover_bytes(cover_url: str, timeout: int = 15) -> bytes | None:
    """下载在线封面图并规范化为 JPEG 字节；内置 URL 内存缓存。"""
    url = str(cover_url or '').strip()
    if not url or not url.startswith('http'):
        return None
    with CACHE_LOCK:
        if url in COVER_CACHE:
            return COVER_CACHE[url]
    try:
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
        if not raw or not sniff_image(raw):
            return None
        jpg = normalize_jpeg(raw, EMBED_MAX_SIDE) or raw
        with CACHE_LOCK:
            COVER_CACHE[url] = jpg
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
        'cover_url': '', 'title': '', 'artist': '', 'album': '', 'composer': '', 'lyrics': '',
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
        out['composer'] = parse_lyric_credits(lrc)
    return out


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
    'composer', 'genre', 'cover_url', 'lyrics',
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
        genre=str(overrides.get('genre') or tr.get('genre') or alb_meta.get('genre') or ''),
        cover_url=str(overrides.get('cover_url') or tr.get('cover_url') or alb_meta.get('cover_url') or ''),
        lyrics=str(overrides.get('lyrics') or ''),
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
    elif plat in ('kugou', 'ytmusic', 'lrclib') and (need_comp or need_lrc) and song_id:
        ex = PROVIDERS[plat].get_song_extra(song_id, need_lyrics=need_lrc, title=title or match.title, duration=duration)
    elif plat in ('migu', 'baidu') and (need_comp or need_lrc) and lrc_url:
        ex = PROVIDERS[plat].get_lyrics_by_url(song_id, lrc_url)

    if ex:
        if plat == 'qq':
            for k in ('composer', 'genre', 'release_date'):
                if not getattr(match, k) and ex.get(k):
                    setattr(match, k, ex[k])
        elif need_comp and not match.composer and ex.get('composer'):
            match.composer = ex['composer']
        if need_lrc and not match.lyrics and ex.get('lyrics'):
            match.lyrics = ex['lyrics']
    return match


def _search_song_and_resolve_album(
    title: str, artist: str, duration: float, track: LibraryTrack | None = None,
    *, need_track: bool = True, need_date: bool = True, need_extra: bool = False, need_lyrics: bool = False,
    skip_plats: set[str] | None = None, allow_itunes_song: bool = True,
    tried_out: set[str] | None = None, plat_order: tuple[str, ...] | None = None,
) -> TrackMatch | None:
    if not title:
        return None
    skip_plats = skip_plats or set()
    order = plat_order or ('netease', 'qq', 'itunes')
    term, tc, ac = f'{title} {artist}'.strip(), norm_core(title), norm_core(artist)
    date_fallback: TrackMatch | None = None
    dummy = track or LibraryTrack(path='', filename='', folder='')

    for plat in order:
        if plat in skip_plats or (plat == 'itunes' and not allow_itunes_song):
            continue
        provider = PROVIDERS.get(plat)
        if not provider:
            continue
        label = PROVIDER_LABELS.get(plat, plat)
        seen_albs: set[str] = set()

        for hit in provider.search_song_hits(term):
            stitle, sart, sdur = hit['title'], hit['artist'], hit['duration']
            if not song_matches(stitle, sdur, sart, tc, duration, ac):
                continue
            sid, aid, alb_name = hit['song_id'], hit['album_id'], hit['album_name']
            pub_date, cov = hit['release_date'], hit['cover_url']

            if plat in ('netease', 'qq', 'itunes', 'deezer', 'ytmusic', 'spotify'):
                cov_kw = {'cover_url': cov} if cov and plat in ('netease', 'deezer', 'ytmusic', 'spotify') else {}
                if aid:
                    if aid not in seen_albs and len(seen_albs) >= 3:
                        continue
                    seen_albs.add(aid)
                    tracks = _fetch_album_tracks_for(plat, aid)
                    rdate = get_album_date(plat, aid) or pub_date
                    m = match_in_tracks(tracks, title, duration, sid)
                    if m and (need_track or not need_date or rdate):
                        cand = _make_match(plat, aid, m, len(tracks), f'{label}(单曲反查)', alb_name, release_date=rdate, **cov_kw)
                        return _enrich_song_credits(cand, plat, str(m.get('id') or sid), dummy, need_extra=need_extra, need_lyrics=need_lyrics)
                    can_fb = rdate or (plat == 'netease' and (cov or need_lyrics)) or (plat == 'qq' and need_lyrics) or (plat in ('deezer', 'ytmusic', 'spotify') and (cov or need_lyrics))
                    if not need_track and can_fb and date_fallback is None:
                        cand = _make_match(plat, aid, None, None, f'{label}(单曲反查)', alb_name, release_date=rdate, artist=sart, title=stitle, **cov_kw)
                        date_fallback = _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics)
                elif not need_track and (pub_date or (plat in ('netease', 'qq', 'ytmusic') and need_lyrics) or (plat in ('deezer', 'spotify') and cov)) and date_fallback is None:
                    extra_kw = {'genre': hit['genre'], 'cover_url': cov} if plat == 'itunes' else ({'cover_url': cov} if cov else {})
                    cand = TrackMatch(None, source=f'{label}(单曲反查)', album_name=alb_name, release_date=pub_date, artist=sart, title=stitle, **extra_kw)
                    date_fallback = _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics)
            else:
                if plat == 'kugou' and aid and aid not in seen_albs and len(seen_albs) < 3:
                    seen_albs.add(aid)
                    tracks = _fetch_album_tracks_for('kugou', aid)
                    m = match_in_tracks(tracks, title, duration, sid)
                    if m:
                        cand = _make_match('kugou', aid, m, len(tracks), '酷狗音乐(单曲反查)', alb_name, cover_url=cov)
                        return _enrich_song_credits(cand, 'kugou', sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics, title=stitle, duration=sdur)
                rdate = get_album_date('kugou', aid) if (plat == 'kugou' and aid) else ''
                cand = TrackMatch(None, source=f'{label}(单曲搜索)', album_name=alb_name, release_date=rdate, title=stitle, artist=sart, cover_url=cov)
                _enrich_song_credits(cand, plat, sid, dummy, need_extra=need_extra, need_lyrics=need_lyrics, title=stitle, duration=sdur, lrc_url=hit['lrc_url'])
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
            if plat in skip_plats or not prov or not prov.supports_album_search:
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
    song_order = tuple(p for p in order if p != 'mb')
    song_match = _search_song_and_resolve_album(
        title, artist, duration, track,
        need_track=need_track, need_date=need_date, need_extra=need_extra, need_lyrics=need_lyrics,
        skip_plats=skip_plats, allow_itunes_song=allow_itunes_song, tried_out=tried_out, plat_order=song_order,
    )
    if _match_has_requested(song_match, need_track=need_track, need_date=need_date, need_cover=need_cover, need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics):
        return song_match
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
    need_core: bool = False,
    need_extra: bool = False,
    need_lyrics: bool = False,
    need_fields: set[str] | frozenset[str] | None = None,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    only_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
    skip_sources: set[str] | frozenset[str] | tuple[str, ...] | list[str] | None = None,
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

    tried: set[str] = set()
    res = _resolve_metadata_core(
        track, clues, title, artist, album, duration,
        need_track=need_track, need_date=need_date, need_cover=need_cover,
        need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics,
        allow_mb=('mb' in primary_order), tried_out=tried, plat_order=primary_order,
    )
    primary_matched = bool(res.source)

    pkl = _find_pkl_item(track, ident)
    if pkl:
        pm = _extract_pkl_metadata(pkl)
        _merge_match(res, TrackMatch(
            None, source=res.source or 'musicdl缓存', album_name=pm['album'], release_date=pm['release_date'],
            title=pm['title'], artist=pm['artist'], composer=pm['composer'], cover_url=pm['cover_url'],
            lyrics=pm['lyrics'],
        ))

    # 若主平台未命中且仍缺少除纯粹 composer 外的必要字段（或缺少歌词），继续尝试扩展中文源（酷狗/咪咕/千千）
    if not primary_matched and extended_order and (need_track or need_date or need_cover or need_core or need_lyrics):
        ext_res = _resolve_metadata_core(
            track, clues, title, artist, album, duration,
            need_track=need_track, need_date=need_date, need_cover=need_cover,
            need_core=need_core, need_extra=need_extra, need_lyrics=need_lyrics,
            allow_mb=False, tried_out=tried, plat_order=extended_order,
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

        enrich_order = primary_order + extended_order
        all_enabled = set(enrich_order)
        for plat in enrich_order:
            if plat in tried:
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
            )
            _merge_match(res, extra_res)

    return res


def resolve_release_date(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑发布日期。"""
    return resolve_track_metadata(track, need_track=False, need_date=True)


def resolve_track_number(track: LibraryTrack) -> TrackMatch:
    """为单首歌曲联网解析专辑内音轨号（同时尽可能附带发布日期）。"""
    return resolve_track_metadata(track, need_track=True, need_date=True)
