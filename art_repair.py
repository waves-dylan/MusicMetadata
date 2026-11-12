"""专辑封面规范化修复、分组标签补全与统一原子写入模块。"""

from __future__ import annotations

import hashlib
import io
import os
import re
from base64 import b64decode, b64encode
from collections import Counter, OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass, field

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TXXX, USLT
from mutagen.mp4 import MP4, MP4Cover

from tag_io import (
    LOADERS,
    PREFER_EXTERNAL_LRC_CONTAINERS,
    SUPPORTS_EMBEDDED_LYRICS,
    _as_text,
    _id3_text,
    _remove_file_with_retry,
    _tag_text,
    apply_track_to_audio,
    atomic_mutate_audio,
    cleanup_stale_musictmp_for,
    container_family,
    convert_audio_file,
    delete_keys_ci,
    delete_lrc_file,
    ensure_tags,
    iter_tag_values,
    parse_filename_clues,
    read_audio_info,
    sniff_container,
    write_lrc_file,
)

IMG_EXT = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
COVER_NAMES = {'cover', 'folder', 'front', 'album', 'albumart'}
MAX_SIDE = 1000
EMBED_MAX_SIDE = 1400
JPEG_QUALITY = 90
HUGE_BYTES = 2 * 1024 * 1024
HUGE_PIXELS = 4_000_000
COVER_FRONT_TYPE = 3
SAME_ART_DISTANCE = 12
SUPPORTS_COVER_WRITE = {'flac', 'mp4', 'mp3', 'wav', 'aiff', 'ogg', 'opus'}
LYRICS_MODES = ('auto', 'embed', 'lrc', 'both')

_DATE_DIR_PREFIX_RE = re.compile(r'^\d{2,4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\s*')
_TRAILING_ID_RE = re.compile(r'\s*-\s*[0-9A-Za-z]{6,}$')

_TAG_WRITE_TABLE = {
    'vorbis': {
        'album': 'ALBUM', 'albumartist': 'ALBUMARTIST', 'artist': 'ARTIST', 'title': 'TITLE',
        'date': 'DATE', 'composer': 'COMPOSER', 'genre': 'GENRE', 'comment': 'COMMENT',
    },
    'mp4': {
        'album': '\xa9alb', 'albumartist': 'aART', 'artist': '\xa9ART', 'title': '\xa9nam',
        'date': '\xa9day', 'composer': '\xa9wrt', 'genre': '\xa9gen', 'comment': '\xa9cmt',
    },
    'asf': {
        'album': 'WM/AlbumTitle', 'albumartist': 'WM/AlbumArtist', 'artist': 'Author', 'title': 'Title',
        'date': 'WM/Year', 'composer': 'WM/Composer', 'genre': 'WM/Genre', 'comment': 'Description',
    },
    'ape': {
        'album': 'Album', 'albumartist': 'Album Artist', 'artist': 'Artist', 'title': 'Title',
        'date': 'Year', 'composer': 'Composer', 'genre': 'Genre', 'comment': 'Comment',
    },
}
_ID3_WRITE_TABLE = {
    'album': ('TALB', TALB), 'albumartist': ('TPE2', TPE2), 'artist': ('TPE1', TPE1),
    'title': ('TIT2', TIT2), 'date': ('TDRC', TDRC), 'composer': ('TCOM', TCOM), 'genre': ('TCON', TCON),
}

DELETABLE_FIELDS: tuple[str, ...] = (
    'cover', 'title', 'artist', 'albumartist', 'album', 'date',
    'composer', 'performer', 'genre', 'tracknumber', 'discnumber', 'comment', 'lyrics',
)
SETTABLE_FIELDS: tuple[str, ...] = (
    'title', 'artist', 'albumartist', 'album', 'date',
    'composer', 'performer', 'genre', 'tracknumber', 'discnumber', 'comment', 'lyrics',
)

_TAG_DELETE_KEYS = {
    'vorbis': {
        'title': ('TITLE',), 'artist': ('ARTIST',), 'albumartist': ('ALBUMARTIST', 'ALBUM ARTIST'),
        'album': ('ALBUM',), 'date': ('DATE', 'YEAR'),
        'composer': ('COMPOSER',), 'performer': ('PERFORMER',), 'genre': ('GENRE',),
        'tracknumber': ('TRACKNUMBER', 'TRACK'), 'discnumber': ('DISCNUMBER', 'DISC'),
        'comment': ('COMMENT', 'DESCRIPTION'),
        'lyrics': ('LYRICS', 'UNSYNCEDLYRICS', 'UNSYNCED LYRICS'),
    },
    'mp4': {
        'title': ('\xa9nam',), 'artist': ('\xa9ART',), 'albumartist': ('aART',),
        'album': ('\xa9alb',), 'date': ('\xa9day',),
        'composer': ('\xa9wrt',), 'performer': ('----:com.apple.iTunes:PERFORMER',), 'genre': ('\xa9gen',),
        'tracknumber': ('trkn',), 'discnumber': ('disk',), 'comment': ('\xa9cmt',),
        'lyrics': ('\xa9lyr',),
    },
    'asf': {
        'title': ('Title',), 'artist': ('Author',), 'albumartist': ('WM/AlbumArtist',),
        'album': ('WM/AlbumTitle',), 'date': ('WM/Year',),
        'composer': ('WM/Composer',), 'performer': ('WM/Performer', 'Performer'), 'genre': ('WM/Genre',),
        'tracknumber': ('WM/TrackNumber',), 'discnumber': ('WM/PartOfSet',), 'comment': ('Description',),
        'lyrics': ('WM/Lyrics',),
    },
    'ape': {
        'title': ('Title', 'TITLE'), 'artist': ('Artist', 'ARTIST'),
        'albumartist': ('Album Artist', 'ALBUM ARTIST', 'ALBUMARTIST'), 'album': ('Album', 'ALBUM'),
        'date': ('Year', 'DATE'), 'composer': ('Composer', 'COMPOSER'),
        'performer': ('Performer', 'PERFORMER'), 'genre': ('Genre', 'GENRE'),
        'tracknumber': ('Track', 'TRACK', 'TRACKNUMBER'),
        'discnumber': ('Disc', 'DISC', 'DISCNUMBER'), 'comment': ('Comment', 'COMMENT'),
        'lyrics': ('Lyrics', 'LYRICS'),
    },
}
_ID3_DELETE_KEYS = {
    'title': ('TIT2',), 'artist': ('TPE1',), 'albumartist': ('TPE2',), 'album': ('TALB',),
    'date': ('TDRC', 'TYER', 'TDAT'), 'composer': ('TCOM',), 'genre': ('TCON',),
    'tracknumber': ('TRCK',), 'discnumber': ('TPOS',), 'lyrics': ('USLT',),
}


@dataclass
class TrackArtTagPlan:
    path: str
    cover_status: str = '正常'
    cover_reasons: list[str] = field(default_factory=list)
    needs_cover_fix: bool = False
    target_pic_bytes: bytes | None = None
    target_pic_mime: str = 'image/jpeg'
    tag_updates: dict[str, str] = field(default_factory=dict)
    needs_tag_fix: bool = False


@dataclass
class FolderArtPlan:
    folder: str
    album_count: int = 0
    cover_action: str = 'ok'
    cover_path: str = ''
    backup_path: str = ''
    cover_jpeg: bytes | None = None
    detail: str = ''
    track_plans: dict[str, TrackArtTagPlan] = field(default_factory=dict)

    @property
    def has_folder_cover_work(self) -> bool:
        return self.cover_action in ('create', 'rewrite', 'remove')


def _lanczos():
    return getattr(getattr(Image, 'Resampling', Image), 'LANCZOS', getattr(Image, 'LANCZOS', 1))


def sniff_image(data: bytes) -> tuple[str, int, int] | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        return str(im.format or ''), int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def normalize_jpeg(data: bytes, max_side: int = MAX_SIDE) -> bytes | None:
    if not data:
        return None
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
        if im.mode not in ('RGB', 'L'):
            im = im.convert('RGB')
        if max(im.size) > max_side:
            im = im.copy()
            im.thumbnail((max_side, max_side), _lanczos())
        buf = io.BytesIO()
        im.save(buf, 'JPEG', quality=JPEG_QUALITY, optimize=True, progressive=False)
        return buf.getvalue()
    except Exception:
        return None


def ahash(data: bytes) -> int:
    px = Image.open(io.BytesIO(data)).convert('L').resize((9, 8), _lanczos()).tobytes()
    bits = 0
    for r in range(8):
        row = px[r * 9:(r + 1) * 9]
        for c in range(8):
            bits = (bits << 1) | (1 if row[c] > row[c + 1] else 0)
    return bits


def ham(a: int, b: int) -> int:
    return bin(a ^ b).count('1')


def same_art(data_a: bytes, data_b: bytes) -> bool:
    try:
        return ham(ahash(data_a), ahash(data_b)) <= SAME_ART_DISTANCE
    except Exception:
        return False


def read_pictures(audio, container: str | None = None) -> list[dict]:
    out: list[dict] = []
    try:
        if isinstance(audio, FLAC) or container == 'flac':
            for p in getattr(audio, 'pictures', []) or []:
                out.append({'mime': p.mime or '', 'type': int(p.type), 'data': p.data})
        elif isinstance(audio, MP4) or container == 'mp4':
            for c in (getattr(audio, 'tags', None) or {}).get('covr', []):
                mime = 'image/png' if getattr(c, 'imageformat', None) == MP4Cover.FORMAT_PNG else 'image/jpeg'
                out.append({'mime': mime, 'type': COVER_FRONT_TYPE, 'data': bytes(c)})
        elif isinstance(getattr(audio, 'tags', None), ID3):
            for k in audio.tags.getall('APIC'):
                out.append({'mime': k.mime or '', 'type': int(k.type), 'data': k.data})
        elif getattr(audio, 'tags', None) is not None and 'metadata_block_picture' in audio.tags:
            for v in audio.tags['metadata_block_picture']:
                pic = Picture(b64decode(v))
                out.append({'mime': pic.mime or '', 'type': int(pic.type), 'data': pic.data})
    except Exception:
        pass
    return out


def apply_single_front_cover(audio, container: str, data: bytes, mime: str = 'image/jpeg') -> bool:
    if container not in SUPPORTS_COVER_WRITE:
        return False
    if getattr(audio, 'tags', None) is None and hasattr(audio, 'add_tags'):
        audio.add_tags()
    if container == 'flac' or isinstance(audio, FLAC):
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = COVER_FRONT_TYPE, mime, 'Cover (front)', data
        audio.clear_pictures()
        audio.add_picture(pic)
        return True
    if container == 'mp4' or isinstance(audio, MP4):
        audio.tags['covr'] = [MP4Cover(data, imageformat=MP4Cover.FORMAT_PNG if mime == 'image/png' else MP4Cover.FORMAT_JPEG)]
        return True
    if container in ('mp3', 'wav', 'aiff') or isinstance(getattr(audio, 'tags', None), ID3):
        audio.tags.delall('APIC')
        audio.tags.add(APIC(encoding=3, mime=mime, type=COVER_FRONT_TYPE, desc='Cover (front)', data=data))
        return True
    if container in ('ogg', 'opus') and audio.tags is not None:
        pic = Picture()
        pic.type, pic.mime, pic.desc, pic.data = COVER_FRONT_TYPE, mime, 'Cover (front)', data
        audio.tags['metadata_block_picture'] = [b64encode(pic.write()).decode('ascii')]
        return True
    return False


_LYRICS_WRITE_KEYS = {
    'vorbis': 'LYRICS',
    'mp4': '\xa9lyr',
    'asf': 'WM/Lyrics',
    'ape': 'Lyrics',
}


def apply_tag_field(audio, container: str, field_name: str, value: str) -> bool:
    if not value:
        return False
    tags = ensure_tags(audio)
    if tags is None:
        return False
    fam = container_family(audio, container)
    if fam == 'id3':
        if field_name == 'comment':
            if '163 key' in _id3_text(tags, 'COMM'):
                return False
            tags['COMM::eng'] = COMM(encoding=3, lang='eng', desc='', text=[value])
            return True
        entry = _ID3_WRITE_TABLE.get(field_name)
        if not entry:
            return False
        fid, cls = entry
        tags.setall(fid, [cls(encoding=3, text=[value])])
        return True
    key = _TAG_WRITE_TABLE.get(fam, {}).get(field_name)
    if not key:
        return False
    if field_name == 'comment' and '163 key' in _as_text(tags.get(key)):
        return False
    tags[key] = [value]
    return True


def apply_lyrics_to_audio(audio, container: str, lyrics: str) -> bool:
    """将歌词写入已打开的音频对象（清理旧歌词帧避免重复堆积）。"""
    txt = str(lyrics or '').replace('\r\n', '\n').strip()
    if not txt or container not in SUPPORTS_EMBEDDED_LYRICS:
        return False
    tags = ensure_tags(audio)
    if tags is None:
        return False
    fam = container_family(audio, container)
    if fam == 'id3':
        tags.delall('USLT')
        tags.add(USLT(encoding=3, lang='eng', desc='', text=txt))
        return True
    if fam == 'vorbis':
        delete_keys_ci(tags, 'UNSYNCEDLYRICS', 'UNSYNCED LYRICS')
    key = _LYRICS_WRITE_KEYS.get(fam)
    if key:
        tags[key] = [txt]
        return True
    return False


def plan_picture_fix(pics: list[dict], album_jpeg: bytes | None) -> tuple[bool, list[str], bytes | None, str]:
    reasons: list[str] = []
    decodable: list[tuple[dict, tuple[str, int, int]]] = []
    for p in pics:
        info = sniff_image(p['data'])
        if info:
            decodable.append((p, info))
        else:
            reasons.append('undecodable')
    fronts = [(p, i) for p, i in decodable if p['type'] == COVER_FRONT_TYPE]
    if not decodable:
        return (False, ['no-cover-source'], None, 'image/jpeg') if album_jpeg is None else (True, ['no-cover'], album_jpeg, 'image/jpeg')
    if not fronts:
        reasons.append('no-front(type!=3)')
    if len(pics) > 1:
        reasons.append(f'multi-pic({len(pics)})')

    base, (fmt, w, h) = fronts[0] if fronts else decodable[0]
    data = base['data']
    mime = 'image/png' if fmt == 'PNG' else ('image/jpeg' if fmt in ('JPEG', 'MPO') else f'image/{fmt.lower()}')
    if (base['mime'] or '').lower() != mime:
        reasons.append(f"mime-mismatch({base['mime'] or '空'}!={mime})")
    if w * h > HUGE_PIXELS or len(data) > HUGE_BYTES:
        reasons.append(f'oversize({w}x{h},{len(data) / 1048576.0:.1f}MB)')
        shrunk = normalize_jpeg(data, EMBED_MAX_SIDE)
        if shrunk:
            data, mime = shrunk, 'image/jpeg'
    if not reasons:
        return False, [], None, 'image/jpeg'
    if base['type'] != COVER_FRONT_TYPE:
        reasons.append(f"type-fixed({base['type']}->3)")
    return True, reasons, data, mime


def _pick_folder_image(folder: str) -> bytes | None:
    best = None
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    for name in names:
        if os.path.splitext(name)[1].lower() not in IMG_EXT:
            continue
        p = os.path.join(folder, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, 'rb') as fp:
                data = fp.read()
        except OSError:
            continue
        info = sniff_image(data)
        if not info:
            continue
        rank = (3 if os.path.splitext(name)[0].lower() in COVER_NAMES else 1, info[1] * info[2])
        if best is None or rank > best[0]:
            best = (rank, data)
    return best[1] if best else None


def _date_year_and_specificity(date_str: str) -> tuple[str, int]:
    s = str(date_str or '').strip()
    m_full = re.match(r'^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:\b|[T\s])', s)
    if m_full:
        yr, mo, dy = m_full.group(1), int(m_full.group(2)), int(m_full.group(3))
        return yr, (1 if (mo == 1 and dy == 1) else 3)
    m_ym = re.match(r'^(\d{4})[-/.](\d{1,2})$', s)
    if m_ym:
        return m_ym.group(1), 2
    m_yr = re.match(r'^(\d{4})\b', s)
    return (m_yr.group(1), 1) if m_yr else ('', 0)


def pick_canonical_album_date(dates: list[str] | tuple[str, ...]) -> str:
    """从同专辑多个日期中选出规范主发行日（优先主年份与非 YYYY-01-01 精确日期）。"""
    cleaned = [str(d or '').strip() for d in dates if str(d or '').strip()]
    if not cleaned:
        return ''
    if len(set(cleaned)) == 1:
        return cleaned[0]
    date_cnt = Counter(cleaned)
    year_cnt: Counter[str] = Counter()
    parsed: dict[str, tuple[str, int]] = {}
    for d in date_cnt:
        yr, spec = _date_year_and_specificity(d)
        parsed[d] = (yr, spec)
        if yr:
            year_cnt[yr] += date_cnt[d]
    return max(
        date_cnt,
        key=lambda d: (
            year_cnt.get(parsed[d][0], date_cnt[d]) if parsed[d][0] else date_cnt[d],
            parsed[d][1],
            date_cnt[d],
            d,
        ),
    )


def _compute_majority_tags(records: list[dict]) -> dict[str, str]:
    maj: dict[str, str] = {}
    for f in ('album', 'albumartist', 'artist', 'date', 'genre'):
        vals = [str(r['info'].get(f) or '').strip() for r in records if str(r['info'].get(f) or '').strip()]
        if f == 'date':
            maj[f] = pick_canonical_album_date(vals)
        else:
            maj[f] = Counter(vals).most_common(1)[0][0] if vals else ''
    if not maj['albumartist']:
        maj['albumartist'] = maj['artist']
    if '/' in maj['albumartist'] and len(maj['albumartist']) > 20:
        maj['albumartist'] = ''
    return maj


def analyze_folder_art_and_tags(
    folder: str,
    audio_paths: list[str],
    pre_read_infos: dict[str, dict] | None = None,
) -> FolderArtPlan:
    """只读分析目录外置封面、曲目内嵌封面及缺失分组标签。"""
    plan = FolderArtPlan(
        folder=folder,
        cover_path=os.path.join(folder, 'cover.jpg'),
        backup_path=os.path.join(folder, 'disabled-cover.jpg.bak'),
    )
    records: list[dict] = []
    for p in audio_paths:
        info = pre_read_infos.get(p) if pre_read_infos else None
        if info is None:
            info = read_audio_info(p)
        loader = LOADERS.get(info.get('container')) if not info.get('error') else None
        if loader is None:
            continue
        try:
            pics = read_pictures(loader(p), info['container'])
        except Exception:
            pics = []
        records.append({'path': p, 'norm': os.path.normcase(os.path.abspath(p)), 'info': info, 'container': info['container'], 'pics': pics})
    if not records:
        return plan

    folder_album = _DATE_DIR_PREFIX_RE.sub('', os.path.basename(folder)).strip()
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for rec in records:
        groups.setdefault((rec['info'].get('album') or '').strip() or folder_album, []).append(rec)
    plan.album_count = len(groups)
    single = len(groups) == 1

    album_targets: dict[str, bytes | None] = {}
    album_hashes: dict[str, int | None] = {}
    for alb_key, grp_recs in groups.items():
        votes: OrderedDict[str, list] = OrderedDict()
        for rec in grp_recs:
            for pic in rec['pics']:
                pinfo = sniff_image(pic['data'])
                if not pinfo:
                    continue
                h = hashlib.md5(pic['data']).hexdigest()
                front = 1 if pic['type'] == COVER_FRONT_TYPE else 0
                if h not in votes:
                    votes[h] = [front, 1, pinfo[1] * pinfo[2], pic['data']]
                else:
                    votes[h][0] = max(votes[h][0], front)
                    votes[h][1] += 1
        jpeg = normalize_jpeg(sorted(votes.values(), key=lambda x: (x[0], x[1], x[2]), reverse=True)[0][3], MAX_SIDE) if votes else None
        album_targets[alb_key] = jpeg
        try:
            album_hashes[alb_key] = ahash(jpeg) if jpeg else None
        except Exception:
            album_hashes[alb_key] = None

    if single:
        only_key = next(iter(groups))
        if album_targets.get(only_key) is None:
            raw_dir = _pick_folder_image(folder)
            if raw_dir:
                album_targets[only_key] = normalize_jpeg(raw_dir, MAX_SIDE)
                try:
                    album_hashes[only_key] = ahash(album_targets[only_key]) if album_targets[only_key] else None
                except Exception:
                    album_hashes[only_key] = None
        cover_jpeg = album_targets.get(only_key)
        plan.cover_jpeg = cover_jpeg
        if cover_jpeg is None:
            plan.cover_action = 'nodata'
        elif not os.path.exists(plan.cover_path):
            plan.cover_action, plan.detail = 'create', '单专辑目录缺少外置 cover.jpg'
        else:
            try:
                with open(plan.cover_path, 'rb') as fp:
                    old = fp.read()
                if old == cover_jpeg or same_art(old, cover_jpeg):
                    plan.cover_action = 'ok'
                else:
                    plan.cover_action, plan.detail = 'rewrite', '外置 cover.jpg 与代表封面不一致，需重写'
            except OSError:
                plan.cover_action, plan.detail = 'rewrite', '外置 cover.jpg 读取异常，需重写'
    else:
        names = '、'.join(k or '(空专辑)' for k in groups)
        if os.path.exists(plan.cover_path):
            plan.cover_action, plan.detail = 'remove', f'目录含 {len(groups)} 张专辑（{names}），需撤回误放的 cover.jpg'
        else:
            plan.cover_action, plan.detail = 'mixed-skip', f'目录含 {len(groups)} 张专辑（{names}），跳过外置封面'

    folder_maj = _compute_majority_tags(records) if single else None
    for alb_key, grp_recs in groups.items():
        tgt_jpeg, tgt_hash = album_targets.get(alb_key), album_hashes.get(alb_key)
        grp_maj = folder_maj if single else _compute_majority_tags(grp_recs)
        for rec in grp_recs:
            info, container, pics = rec['info'], rec['container'], rec['pics']
            need_pic, reasons, tgt_bytes, tgt_mime = plan_picture_fix(pics, tgt_jpeg)
            if not need_pic and tgt_jpeg and tgt_hash is not None and 'no-cover-source' not in reasons:
                fronts = [pc for pc in pics if pc['type'] == COVER_FRONT_TYPE] or pics
                try:
                    if fronts and ham(ahash(fronts[0]['data']), tgt_hash) > SAME_ART_DISTANCE:
                        need_pic, reasons, tgt_bytes, tgt_mime = True, ['art-mismatch(与专辑代表封面不同)'], tgt_jpeg, 'image/jpeg'
                except Exception:
                    pass
            if need_pic and container not in SUPPORTS_COVER_WRITE:
                need_pic = False
                reasons.append(f'{container}暂不支持写封面')

            cov_status = ('待修:' + ','.join(reasons)) if need_pic else ('缺封面(无可用图源)' if 'no-cover-source' in reasons else '正常')
            tag_updates: dict[str, str] = {}
            if single or (info.get('album') or '').strip():
                for f in ('album', 'albumartist', 'date', 'genre'):
                    if not (info.get(f) or '').strip():
                        val = grp_maj.get(f, '') or (folder_album if (f == 'album' and single) else '')
                        if val:
                            tag_updates[f] = val
            elif not (info.get('albumartist') or '').strip():
                own_artist = (info.get('artist') or '').strip()
                if own_artist and not ('/' in own_artist and len(own_artist) > 20):
                    tag_updates['albumartist'] = own_artist

            if not (info.get('title') or '').strip() or not (info.get('artist') or '').strip():
                clues = parse_filename_clues(rec['path'], folder)
                if not (info.get('title') or '').strip():
                    inferred_title = clues.get('inferred_title') or _TRAILING_ID_RE.sub('', os.path.splitext(os.path.basename(rec['path']))[0]).strip()
                    if inferred_title:
                        tag_updates['title'] = inferred_title
                if not (info.get('artist') or '').strip() and clues.get('inferred_artist'):
                    tag_updates['artist'] = clues['inferred_artist']

            plan.track_plans[rec['norm']] = TrackArtTagPlan(
                path=rec['path'], cover_status=cov_status, cover_reasons=reasons, needs_cover_fix=need_pic,
                target_pic_bytes=tgt_bytes, target_pic_mime=tgt_mime, tag_updates=tag_updates, needs_tag_fix=bool(tag_updates),
            )
    return plan


def apply_folder_cover_file(plan: FolderArtPlan) -> tuple[bool, str]:
    if plan.cover_action in ('create', 'rewrite'):
        if not plan.cover_jpeg:
            return False, '无可用封面数据'
        try:
            with open(plan.cover_path, 'wb') as fp:
                fp.write(plan.cover_jpeg)
            return True, '已生成 cover.jpg' if plan.cover_action == 'create' else '已重写 cover.jpg'
        except OSError as err:
            return False, f'写入 cover.jpg 失败: {err}'
    if plan.cover_action == 'remove' and os.path.exists(plan.cover_path):
        try:
            os.replace(plan.cover_path, plan.backup_path)
            return True, '已撤回多专辑目录 cover.jpg -> disabled-cover.jpg.bak'
        except OSError as err:
            return False, f'撤回 cover.jpg 失败: {err}'
    return True, '无需处理外置封面'


def apply_track_repairs(
    path: str,
    tag_updates: dict[str, str] | None = None,
    cover_bytes: bytes | None = None,
    cover_mime: str = 'image/jpeg',
    tracknumber: int | None = None,
    discnumber: int | None = None,
    overwrite_track: bool = False,
    lyrics: str | None = None,
    lyrics_mode: str = 'auto',
) -> tuple[bool, str]:
    """在单次原子临时副本上合并执行标签补全、封面规范、歌词写入与音轨/碟号写入。"""
    tag_updates = dict(tag_updates or {})
    lyrics_txt = str(lyrics or '').replace('\r\n', '\n').strip()
    if not tag_updates and cover_bytes is None and tracknumber is None and discnumber is None and not lyrics_txt:
        return True, '无需写入'
    state = read_audio_info(path)
    if state.get('error'):
        return False, str(state['error'])

    write_tn = tracknumber
    if write_tn is not None and not overwrite_track:
        cur_tn = state.get('track_int')
        if cur_tn is not None and cur_tn > 0:
            if not tag_updates and cover_bytes is None and discnumber is None and not lyrics_txt:
                return (True, '已有相同音轨号') if cur_tn == int(write_tn) else (False, f"已有音轨号 ({state.get('tracknumber')})，跳过覆盖")
            write_tn = None
    write_dn = int(discnumber) if (discnumber is not None and int(discnumber) > 0 and not state.get('disc_int')) else None

    lmode = str(lyrics_mode or 'auto').strip().lower()
    if lmode not in LYRICS_MODES:
        lmode = 'auto'
    container = state.get('container') or ''
    want_embed_lyrics = False
    want_lrc_file = False
    if lyrics_txt:
        if lmode == 'auto':
            if container in PREFER_EXTERNAL_LRC_CONTAINERS:
                want_lrc_file = True
            else:
                want_embed_lyrics = True
        elif lmode == 'embed':
            want_embed_lyrics = True
        elif lmode == 'lrc':
            want_lrc_file = True
        elif lmode == 'both':
            want_embed_lyrics = True
            want_lrc_file = True

    if not tag_updates and cover_bytes is None and write_tn is None and write_dn is None and not want_embed_lyrics and not want_lrc_file:
        return True, '无需写入'

    applied_fields: set[str] = set()
    wrote_embedded_lyrics = False

    def _mutate(audio, c_type: str, _tmp: str):
        nonlocal wrote_embedded_lyrics
        applied_fields.clear()
        wrote_embedded_lyrics = False
        did: list[str] = []
        for fn, val in tag_updates.items():
            if apply_tag_field(audio, c_type, fn, val):
                did.append(f'{fn}={val}')
                applied_fields.add(fn)
        if cover_bytes is not None:
            if not apply_single_front_cover(audio, c_type, cover_bytes, cover_mime or 'image/jpeg'):
                raise RuntimeError(f'{c_type} 容器暂不支持写入封面')
            did.append('封面已规范')
        if want_embed_lyrics:
            if apply_lyrics_to_audio(audio, c_type, lyrics_txt):
                wrote_embedded_lyrics = True
                did.append('已嵌入歌词')
        if write_tn is not None or write_dn is not None:
            apply_track_to_audio(audio, c_type, write_tn, discnumber=write_dn, write_disc_if_missing=(write_dn is not None))
            if write_tn is not None:
                did.append(f'音轨号={int(write_tn)}')
            if write_dn is not None:
                did.append(f'碟号={int(write_dn)}')
        return (bool(did), '，'.join(did) if did else '无需改写')

    def _verify(chk_path: str, c_type: str):
        chk_info = read_audio_info(chk_path) if (write_tn is not None or write_dn is not None or applied_fields or wrote_embedded_lyrics) else {}
        if write_tn is not None and chk_info.get('track_int') != int(write_tn):
            return False, '音轨号校验失败'
        if write_dn is not None and chk_info.get('disc_int') != int(write_dn):
            return False, '碟片编号校验失败'
        for fn in applied_fields:
            got_val = (chk_info.get(fn) or '').strip()
            if not got_val or (fn == 'date' and got_val != str(tag_updates.get('date') or '').strip()):
                return False, f'{fn}标签写回校验失败'
        if wrote_embedded_lyrics and not chk_info.get('has_embedded_lyrics'):
            return False, '歌词写回校验失败'
        if cover_bytes is not None and c_type in SUPPORTS_COVER_WRITE:
            pics = read_pictures(LOADERS[c_type](chk_path), c_type)
            if not pics or not sniff_image(pics[0]['data']):
                return False, '封面写回校验失败'
        return True, ''

    parts: list[str] = []
    if tag_updates or cover_bytes is not None or write_tn is not None or write_dn is not None or want_embed_lyrics:
        ok, msg = atomic_mutate_audio(path, _mutate, verify_fn=_verify)
        if not ok:
            return False, msg
        cleanup_stale_musictmp_for(path)
        if msg and msg != '无需改写':
            parts.append(msg)

    if want_lrc_file:
        ok_lrc, msg_lrc = write_lrc_file(
            path,
            lyrics_txt,
            title=tag_updates.get('title') or state.get('title') or '',
            artist=tag_updates.get('artist') or state.get('artist') or '',
            album=tag_updates.get('album') or state.get('album') or '',
            overwrite=True,
        )
        if not ok_lrc:
            return False, msg_lrc
        parts.append(msg_lrc)

    return True, '，'.join(parts) if parts else '已写入'


_PIC_TAG_KEYS = {
    'vorbis': ('metadata_block_picture',),
    'mp4': ('covr',),
    'asf': ('wm/picture',),
    'ape': ('cover art (front)', 'cover art (back)'),
}


def _container_family(audio, container: str) -> str:
    fam = container_family(audio, container)
    return '' if fam == 'id3' else fam


def _audio_has_pictures(audio, container: str) -> bool:
    if read_pictures(audio, container):
        return True
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return False
    fam = container_family(audio, container)
    pic_keys = _PIC_TAG_KEYS.get(fam, ())
    if not pic_keys:
        return False
    if fam == 'vorbis' and container != 'flac':
        return False
    targets = {k.lower() for k in pic_keys}
    return any(str(k).lower() in targets for k in tags.keys())


def _has_comment_entries(audio, container: str) -> tuple[bool, bool]:
    """返回音频中 (是否存在非163key注释, 是否存在网易云163key)。"""
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return False, False
    has_unprot = False
    has_163 = False
    fam = container_family(audio, container)
    if fam == 'id3':
        for f in tags.getall('COMM'):
            txt = _as_text(getattr(f, 'text', None) or str(f))
            if '163 key' in txt:
                has_163 = True
            elif txt:
                has_unprot = True
        return has_unprot, has_163

    target_lower = {k.lower() for k in _TAG_DELETE_KEYS.get(fam, {}).get('comment', ())}
    for k in list(tags.keys()):
        if str(k).lower() not in target_lower:
            continue
        for item in iter_tag_values(tags.get(k)):
            txt = str(item).strip()
            if '163 key' in txt:
                has_163 = True
            elif txt:
                has_unprot = True
    return has_unprot, has_163


def delete_pictures_from_audio(audio, container: str) -> bool:
    """移除音频中的全部内嵌封面图片或封面标签。"""
    removed = False
    if container == 'flac' or isinstance(audio, FLAC):
        if getattr(audio, 'pictures', None):
            audio.clear_pictures()
            removed = True
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return removed
    fam = container_family(audio, container)
    if fam == 'id3':
        if tags.getall('APIC'):
            tags.delall('APIC')
            return True
        return removed
    pic_keys = _PIC_TAG_KEYS.get(fam, ())
    if pic_keys and delete_keys_ci(tags, *pic_keys):
        removed = True
    return removed


def delete_tag_field_from_audio(
    audio, container: str, field_name: str, *, protect_163_key: bool = True,
) -> bool:
    """从已打开的音频对象中删除指定类别元数据标签，返回是否发生实际删除。"""
    if field_name == 'cover':
        return delete_pictures_from_audio(audio, container)
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return False

    if container in ('mp3', 'wav', 'aiff') or isinstance(tags, ID3):
        if field_name == 'performer':
            removed = False
            kept_txxx = []
            for f in tags.getall('TXXX'):
                if str(getattr(f, 'desc', '') or '').strip().upper() == 'PERFORMER':
                    removed = True
                else:
                    kept_txxx.append(f)
            if removed:
                tags.setall('TXXX', kept_txxx)
            for fid in ('TMCL', 'TIPL', 'IPLS'):
                if tags.getall(fid):
                    tags.delall(fid)
                    removed = True
            return removed
        if field_name == 'comment':
            frames = tags.getall('COMM')
            if not frames:
                return False
            if protect_163_key:
                kept = [f for f in frames if '163 key' in _as_text(getattr(f, 'text', None) or str(f))]
                if len(kept) == len(frames):
                    return False
                tags.setall('COMM', kept)
                return True
            tags.delall('COMM')
            return True
        fids = _ID3_DELETE_KEYS.get(field_name, ())
        removed = False
        for fid in fids:
            if tags.getall(fid):
                tags.delall(fid)
                removed = True
        return removed

    fam = _container_family(audio, container)
    keys = _TAG_DELETE_KEYS.get(fam, {}).get(field_name, ())
    if not keys:
        return False
    target_lower = {k.lower() for k in keys}
    matched_keys = [k for k in list(tags.keys()) if str(k).lower() in target_lower]
    if not matched_keys:
        return False

    if field_name == 'comment' and protect_163_key:
        removed = False
        for k in matched_keys:
            val = tags.get(k)
            items = list(val) if (hasattr(val, '__iter__') and not isinstance(val, (str, bytes, bytearray))) else [val]
            kept = [x for x in items if '163 key' in str(x)]
            if len(kept) == len(items):
                continue
            if not kept:
                del tags[k]
            else:
                tags[k] = [str(x) for x in kept] if fam == 'ape' else kept
            removed = True
        return removed

    for k in matched_keys:
        del tags[k]
    return True


def inspect_deletable_fields(
    path: str, fields: Iterable[str], *, protect_163_key: bool = True, include_lrc_file: bool = True,
) -> tuple[dict, list[str], list[str]]:
    """只读检查音频文件实际包含哪些可删除的指定元数据类别。"""
    req = [f for f in DELETABLE_FIELDS if f in set(fields)]
    info = read_audio_info(path)
    if info.get('error') or not req:
        return info, [], []

    container = info.get('container')
    loader = LOADERS.get(container) if container else None
    audio = None
    if loader is not None and ('cover' in req or ('comment' in req and info.get('has_163_key'))):
        try:
            audio = loader(path)
        except Exception:
            audio = None

    deletable: list[str] = []
    protected_notes: list[str] = []
    for fn in req:
        if fn == 'cover':
            if audio is not None and _audio_has_pictures(audio, container):
                deletable.append('cover')
        elif fn == 'comment':
            if audio is not None:
                has_unprot, has_163 = _has_comment_entries(audio, container)
                if has_163 and protect_163_key:
                    protected_notes.append('保留网易云 163 key')
                if has_unprot or (has_163 and not protect_163_key):
                    deletable.append('comment')
            else:
                if (info.get('comment') or '').strip() or (not protect_163_key and (info.get('raw_comment') or '').strip()):
                    deletable.append('comment')
                elif protect_163_key and info.get('has_163_key'):
                    protected_notes.append('保留网易云 163 key')
        elif fn == 'lyrics':
            if info.get('has_embedded_lyrics') or (include_lrc_file and info.get('has_lrc_file')):
                deletable.append('lyrics')
        else:
            if (info.get(fn) or '').strip():
                deletable.append(fn)
    return info, deletable, protected_notes


def apply_track_deletions(
    path: str,
    fields: Iterable[str],
    *,
    protect_163_key: bool = True,
    delete_lrc: bool = True,
    out_deleted: list[str] | None = None,
) -> tuple[bool, str]:
    """在单次原子临时副本上批量删除指定元数据类别并回读校验（支持同时清理同名 .lrc）。"""
    state, deletable, protected_notes = inspect_deletable_fields(
        path, fields, protect_163_key=protect_163_key, include_lrc_file=delete_lrc,
    )
    if state.get('error'):
        return False, str(state['error'])
    if not deletable:
        return True, '；'.join(protected_notes) if protected_notes else '无所选元数据可删'

    has_emb_lyrics = bool(state.get('has_embedded_lyrics'))
    has_ext_lrc = bool(delete_lrc and state.get('has_lrc_file'))
    embed_deletable = [f for f in deletable if not (f == 'lyrics' and not has_emb_lyrics)]
    had_163_before = bool(state.get('has_163_key'))
    applied_fields: list[str] = []

    def _mutate(audio, container: str, _tmp: str):
        applied_fields.clear()
        for fn in embed_deletable:
            if delete_tag_field_from_audio(audio, container, fn, protect_163_key=protect_163_key):
                applied_fields.append(fn)
        if not applied_fields:
            return False, '；'.join(protected_notes) if protected_notes else '无所选元数据可删'
        msg = '已删除: ' + ', '.join(applied_fields)
        if protected_notes:
            msg += f"（{'；'.join(protected_notes)}）"
        return True, msg

    def _verify(chk_path: str, container: str):
        text_fields = [f for f in applied_fields if f != 'cover']
        chk_info = read_audio_info(chk_path) if text_fields else {}
        if 'cover' in applied_fields:
            chk_audio = LOADERS[container](chk_path)
            if _audio_has_pictures(chk_audio, container):
                return False, '封面删除校验失败'
        for fn in text_fields:
            if fn == 'comment':
                if protect_163_key:
                    chk_audio = LOADERS[container](chk_path)
                    has_unprot, has_163 = _has_comment_entries(chk_audio, container)
                    if has_unprot:
                        return False, 'comment 标签删除校验失败'
                    if had_163_before and not has_163:
                        return False, '网易云 163 key 保护校验失败'
                else:
                    if (chk_info.get('comment') or '').strip() or (chk_info.get('raw_comment') or '').strip():
                        return False, 'comment 标签删除校验失败'
            elif fn == 'lyrics':
                if chk_info.get('has_embedded_lyrics'):
                    return False, 'lyrics 标签删除校验失败'
            elif (chk_info.get(fn) or '').strip():
                return False, f'{fn} 标签删除校验失败'
        return True, ''

    msg_parts: list[str] = []
    if embed_deletable:
        ok, msg = atomic_mutate_audio(path, _mutate, verify_fn=_verify)
        if not ok:
            return False, msg
        cleanup_stale_musictmp_for(path)
        if applied_fields and msg:
            msg_parts.append(msg)

    if 'lyrics' in deletable and has_ext_lrc:
        ok_lrc, msg_lrc = delete_lrc_file(path)
        if not ok_lrc:
            return False, msg_lrc
        if 'lyrics' not in applied_fields:
            applied_fields.append('lyrics')
        msg_parts.append(msg_lrc)

    if not applied_fields:
        return True, '；'.join(protected_notes) if protected_notes else '无所选元数据可删'
    if out_deleted is not None:
        out_deleted.extend(applied_fields)
    return True, '；'.join(msg_parts)


def delete_folder_cover_file(folder: str) -> tuple[bool, str]:
    """删除目录下外置 cover.jpg 文件。"""
    if not folder:
        return True, '无需删除外置封面'
    cover_path = os.path.join(folder, 'cover.jpg')
    if not os.path.isfile(cover_path):
        return True, '目录无外置 cover.jpg'
    if _remove_file_with_retry(cover_path, attempts=4, base_delay=0.15):
        return True, '已删除目录 cover.jpg'
    return False, '删除目录 cover.jpg 失败'


# ==================== 手工标签覆写 & 格式转换 ====================
_SET_FIELD_ALIASES = {
    'album_artist': 'albumartist',
    'track': 'tracknumber',
    'disc': 'discnumber',
    'year': 'date',
    'lyric': 'lyrics',
    'lrc': 'lyrics',
    'comments': 'comment',
    '标题': 'title',
    '歌手': 'artist',
    '艺术家': 'artist',
    '专辑艺术家': 'albumartist',
    '专辑': 'album',
    '日期': 'date',
    '年份': 'date',
    '作曲': 'composer',
    '表演者': 'performer',
    '流派': 'genre',
    '音轨号': 'tracknumber',
    '碟片编号': 'discnumber',
    '碟号': 'discnumber',
    '注释': 'comment',
    '歌词': 'lyrics',
}


def normalize_set_field_name(raw_key: str) -> str:
    tok = str(raw_key or '').strip()
    low = tok.lower()
    if low in SETTABLE_FIELDS:
        return low
    return _SET_FIELD_ALIASES.get(low) or _SET_FIELD_ALIASES.get(tok) or tok


def parse_set_tags(raw_items: Iterable[str] | str) -> tuple[dict[str, str], str]:
    """解析 --set key=value 参数列表，返回 (有序字典, 错误消息)。"""
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items or ())
    parsed: dict[str, str] = {}
    for item in items:
        raw = str(item or '').strip()
        if not raw:
            continue
        if '=' not in raw:
            return {}, f'无效的 --set 参数（应为 key=value）: {raw}'
        k_raw, v_raw = raw.split('=', 1)
        key = normalize_set_field_name(k_raw)
        val = v_raw.strip()
        if not key:
            return {}, f'标签名不能为空: {raw}'
        if not val:
            return {}, f'标签 {key} 的值不能为空（若需删除请使用 --delete）'
        parsed[key] = val
    return parsed, ''


def _parse_num_pair(raw: str) -> tuple[int | None, int]:
    parts = [p.strip() for p in str(raw or '').split('/', 1)]
    if not parts or not parts[0].isdigit():
        return None, 0
    num = int(parts[0])
    tot = int(parts[1]) if (len(parts) > 1 and parts[1].isdigit()) else 0
    return num, tot


def _set_id3_txxx(tags, desc: str, val: str) -> None:
    kept = [f for f in tags.getall('TXXX') if str(getattr(f, 'desc', '') or '').strip().lower() != desc.lower()]
    kept.append(TXXX(encoding=3, desc=desc, text=[val]))
    tags.setall('TXXX', kept)


def _set_num_pair_tag(tags, fam: str, field_name: str, val: str) -> tuple[bool, str]:
    num, tot = _parse_num_pair(val)
    if num is None or num < 0:
        return False, f'{field_name} 必须是正整数（如 1 或 1/12）'
    key_map = {
        'vorbis': ('TRACKNUMBER', 'DISCNUMBER'),
        'asf': ('WM/TrackNumber', 'WM/PartOfSet'),
        'ape': ('Track', 'Disc'),
    }
    if fam in key_map:
        tags[key_map[fam][0 if field_name == 'tracknumber' else 1]] = [val]
        return True, ''
    if fam == 'id3':
        from mutagen.id3 import TPOS, TRCK
        fid, cls = ('TRCK', TRCK) if field_name == 'tracknumber' else ('TPOS', TPOS)
        tags.setall(fid, [cls(encoding=3, text=[val])])
        return True, ''
    if fam == 'mp4':
        tags['trkn' if field_name == 'tracknumber' else 'disk'] = [(num, tot)]
        return True, ''
    return False, f'暂不支持写入 {field_name}'


def _set_custom_tag(tags, fam: str, key: str, val: str) -> tuple[bool, str]:
    clean = key.strip()
    if fam == 'id3':
        _set_id3_txxx(tags, clean, val)
        return True, ''
    if fam == 'vorbis':
        ckey = clean.upper()
        if ckey == 'METADATA_BLOCK_PICTURE':
            return False, '禁止通过自定义标签直接覆盖封面块'
        tags[ckey] = [val]
        return True, ''
    if fam == 'mp4':
        tags[f'----:com.apple.iTunes:{clean}'] = [val.encode('utf-8')]
        return True, ''
    if fam in ('asf', 'ape'):
        tags[clean] = [val]
        return True, ''
    return False, '容器暂不支持写入自定义标签'


def apply_set_tag_to_audio(
    audio, container: str, field_name: str, value: str, *, protect_163_key: bool = True,
) -> tuple[bool, str]:
    """手工设置标准字段或自定义标签（支持全格式与 163 key 保护）。"""
    val = str(value or '').strip()
    if not val:
        return False, '值不能为空'
    tags = ensure_tags(audio)
    if tags is None:
        return False, '容器不支持标签'
    fam = container_family(audio, container)

    if field_name == 'lyrics':
        ok = apply_lyrics_to_audio(audio, container, val)
        return (ok, '') if ok else (False, f'{container} 暂不支持写入歌词')

    if field_name in ('tracknumber', 'discnumber'):
        return _set_num_pair_tag(tags, fam, field_name, val)

    if field_name == 'performer':
        key_map = {'vorbis': 'PERFORMER', 'asf': 'WM/Performer', 'ape': 'Performer'}
        if fam == 'id3':
            _set_id3_txxx(tags, 'PERFORMER', val)
            return True, ''
        if fam == 'mp4':
            tags['----:com.apple.iTunes:PERFORMER'] = [val.encode('utf-8')]
            return True, ''
        if fam in key_map:
            tags[key_map[fam]] = [val]
            return True, ''
        return False, f'{container} 暂不支持写入 performer'

    if field_name == 'comment' and not protect_163_key:
        if fam == 'id3':
            tags.delall('COMM')
            tags['COMM::eng'] = COMM(encoding=3, lang='eng', desc='', text=[val])
            return True, ''
        if fam == 'vorbis':
            if '163 key' in _tag_text(tags, 'DESCRIPTION'):
                delete_keys_ci(tags, 'DESCRIPTION')
            tags['COMMENT'] = [val]
            return True, ''
        key = _TAG_WRITE_TABLE.get(fam, {}).get('comment')
        if key:
            tags[key] = [val]
            return True, ''

    if field_name in SETTABLE_FIELDS:
        if apply_tag_field(audio, container, field_name, val):
            return True, ''
        if field_name == 'comment' and protect_163_key:
            return False, '存在网易云 163 key 保护，跳过覆写 comment'
        return False, f'{container} 暂不支持写入 {field_name}'

    return _set_custom_tag(tags, fam, field_name, val)


def apply_track_set_tags(
    path: str,
    tag_values: dict[str, str],
    *,
    protect_163_key: bool = True,
) -> tuple[bool, str]:
    """在单次原子临时副本上批量覆写指定标签或自定义标签，并回读校验。"""
    if not tag_values:
        return True, '无需设置'
    state = read_audio_info(path)
    if state.get('error'):
        return False, str(state['error'])

    applied: dict[str, str] = {}
    skipped_notes: list[str] = []

    def _mutate(audio, container: str, _tmp: str):
        applied.clear()
        skipped_notes.clear()
        for k, v in tag_values.items():
            ok_set, err_note = apply_set_tag_to_audio(audio, container, k, v, protect_163_key=protect_163_key)
            if ok_set:
                applied[k] = str(v).strip()
            elif err_note:
                skipped_notes.append(err_note)
        if not applied:
            return False, '；'.join(skipped_notes) if skipped_notes else '无标签被修改'
        msg = '已设置: ' + ', '.join(f'{k}={v}' for k, v in applied.items())
        if skipped_notes:
            msg += f"（{'；'.join(skipped_notes)}）"
        return True, msg

    def _verify(chk_path: str, container: str):
        chk_info = read_audio_info(chk_path)
        for k, v in applied.items():
            if k == 'lyrics':
                if not chk_info.get('has_embedded_lyrics'):
                    return False, 'lyrics 写回校验失败'
            elif k == 'tracknumber':
                num, _ = _parse_num_pair(v)
                if num is not None and chk_info.get('track_int') != num:
                    return False, 'tracknumber 写回校验失败'
            elif k == 'discnumber':
                num, _ = _parse_num_pair(v)
                if num is not None and chk_info.get('disc_int') != num:
                    return False, 'discnumber 写回校验失败'
            elif k in SETTABLE_FIELDS:
                if not (chk_info.get(k) or '').strip():
                    return False, f'{k} 写回校验失败'
        return True, ''

    ok, msg = atomic_mutate_audio(path, _mutate, verify_fn=_verify)
    if ok:
        cleanup_stale_musictmp_for(path)
    return ok, msg


def _extract_cover_for_conversion(path: str, container: str) -> tuple[bytes | None, str]:
    loader = LOADERS.get(container)
    if loader is None:
        return None, 'image/jpeg'
    try:
        audio = loader(path)
    except Exception:
        return None, 'image/jpeg'
    pics = read_pictures(audio, container)
    if pics:
        fronts = [p for p in pics if p['type'] == COVER_FRONT_TYPE] or pics
        return fronts[0]['data'], fronts[0]['mime'] or 'image/jpeg'
    if container in ('ape', 'wv') and getattr(audio, 'tags', None):
        for k in ('Cover Art (front)', 'Cover Art (Front)', 'Cover Art (back)'):
            raw = audio.tags.get(k)
            if raw is None:
                continue
            blob = bytes(raw) if not isinstance(raw, bytes) else raw
            idx = blob.find(b'\x00')
            img = blob[idx + 1:] if idx != -1 else blob
            sinfo = sniff_image(img)
            if sinfo:
                fmt = sinfo[0]
                mime = 'image/png' if fmt == 'PNG' else ('image/jpeg' if fmt in ('JPEG', 'MPO') else f'image/{fmt.lower()}')
                return img, mime
    return None, 'image/jpeg'


def convert_track_format(
    input_path: str,
    target_format: str,
    *,
    overwrite: bool = False,
    delete_source: bool = False,
) -> tuple[bool, str, str]:
    """转换音频格式，并通过 mutagen 补全 ffmpeg 可能丢弃的封面与歌词/扩展标签。"""
    src_info = read_audio_info(input_path)
    if src_info.get('error'):
        return False, str(src_info['error']), ''

    src_container = src_info.get('container') or ''
    cov_data, cov_mime = _extract_cover_for_conversion(input_path, src_container)

    def _post_hook(_src: str, tmp_out: str, _fmt: str) -> tuple[bool, str]:
        out_container = sniff_container(tmp_out) or ''
        loader = LOADERS.get(out_container)
        if loader is None:
            return True, ''
        dst_info = read_audio_info(tmp_out)
        audio = loader(tmp_out)
        changed = False
        if cov_data and out_container in SUPPORTS_COVER_WRITE and not read_pictures(audio, out_container):
            if apply_single_front_cover(audio, out_container, cov_data, cov_mime):
                changed = True
        if src_info.get('has_embedded_lyrics') and src_info.get('lyrics') and not dst_info.get('has_embedded_lyrics'):
            if apply_lyrics_to_audio(audio, out_container, src_info['lyrics']):
                changed = True
        for fld in ('title', 'artist', 'albumartist', 'album', 'date', 'composer', 'performer', 'genre', 'comment'):
            val = (src_info.get(fld) or '').strip()
            if val and not (dst_info.get(fld) or '').strip():
                ok_s, _ = apply_set_tag_to_audio(audio, out_container, fld, val, protect_163_key=False)
                if ok_s:
                    changed = True
        if (src_info.get('tracknumber') or '').strip() and not (dst_info.get('tracknumber') or '').strip():
            ok_s, _ = apply_set_tag_to_audio(audio, out_container, 'tracknumber', src_info['tracknumber'])
            if ok_s:
                changed = True
        if (src_info.get('discnumber') or '').strip() and not (dst_info.get('discnumber') or '').strip():
            ok_s, _ = apply_set_tag_to_audio(audio, out_container, 'discnumber', src_info['discnumber'])
            if ok_s:
                changed = True
        if changed:
            audio.save(v2_version=3) if out_container == 'mp3' else audio.save()
        return True, ''

    return convert_audio_file(
        input_path,
        target_format,
        overwrite=overwrite,
        delete_source=delete_source,
        copy_sidecar_lrc=True,
        post_hook=_post_hook,
    )
