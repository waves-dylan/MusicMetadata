"""专辑封面规范化修复、分组标签补全与统一原子写入模块。"""

from __future__ import annotations

import hashlib
import io
import os
import re
from base64 import b64decode, b64encode
from collections import Counter, OrderedDict
from dataclasses import dataclass, field

from PIL import Image
from mutagen.flac import FLAC, Picture
from mutagen.id3 import APIC, COMM, ID3, TALB, TCOM, TCON, TDRC, TIT2, TPE1, TPE2, TXXX
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

from tag_io import (
    LOADERS,
    _as_text,
    _id3_text,
    _tag_text,
    apply_track_to_audio,
    atomic_mutate_audio,
    cleanup_stale_musictmp_for,
    read_audio_info,
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

_DATE_DIR_PREFIX_RE = re.compile(r'^\d{2,4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\s*')
_TRAILING_ID_RE = re.compile(r'\s*-\s*[0-9A-Za-z]{6,}$')

_TAG_WRITE_TABLE = {
    'vorbis': {
        'album': 'ALBUM', 'albumartist': 'ALBUMARTIST', 'artist': 'ARTIST', 'title': 'TITLE',
        'date': 'DATE', 'composer': 'COMPOSER', 'performer': 'PERFORMER', 'genre': 'GENRE', 'comment': 'COMMENT',
    },
    'mp4': {
        'album': '\xa9alb', 'albumartist': 'aART', 'artist': '\xa9ART', 'title': '\xa9nam',
        'date': '\xa9day', 'composer': '\xa9wrt', 'genre': '\xa9gen', 'comment': '\xa9cmt',
    },
    'asf': {
        'album': 'WM/AlbumTitle', 'albumartist': 'WM/AlbumArtist', 'artist': 'Author', 'title': 'Title',
        'date': 'WM/Year', 'composer': 'WM/Composer', 'performer': 'WM/Performer', 'genre': 'WM/Genre', 'comment': 'Description',
    },
    'ape': {
        'album': 'Album', 'albumartist': 'Album Artist', 'artist': 'Artist', 'title': 'Title',
        'date': 'Year', 'composer': 'Composer', 'performer': 'Performer', 'genre': 'Genre', 'comment': 'Comment',
    },
}
_ID3_WRITE_TABLE = {
    'album': ('TALB', TALB), 'albumartist': ('TPE2', TPE2), 'artist': ('TPE1', TPE1),
    'title': ('TIT2', TIT2), 'date': ('TDRC', TDRC), 'composer': ('TCOM', TCOM), 'genre': ('TCON', TCON),
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


def apply_tag_field(audio, container: str, field_name: str, value: str) -> bool:
    if not value:
        return False
    if getattr(audio, 'tags', None) is None and hasattr(audio, 'add_tags'):
        audio.add_tags()
    tags = getattr(audio, 'tags', None)
    if tags is None:
        return False
    if container in ('mp3', 'wav', 'aiff') or isinstance(tags, ID3):
        if field_name == 'performer':
            tags['TXXX:PERFORMER'] = TXXX(encoding=3, desc='PERFORMER', text=[value])
            return True
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
    fam = 'vorbis' if container in ('flac', 'ogg', 'opus') else ('mp4' if (container == 'mp4' or isinstance(audio, MP4)) else ('asf' if container == 'asf' else ('ape' if container in ('ape', 'wv') else '')))
    if fam == 'mp4' and field_name == 'performer':
        tags['----:com.apple.iTunes:PERFORMER'] = [MP4FreeForm(str(value).encode('utf-8'))]
        return True
    key = _TAG_WRITE_TABLE.get(fam, {}).get(field_name)
    if not key:
        return False
    if field_name == 'comment' and fam != 'vorbis' and '163 key' in _as_text(tags.get(key)):
        return False
    tags[key] = [value]
    return True


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

            if not (info.get('title') or '').strip():
                inferred = _TRAILING_ID_RE.sub('', os.path.splitext(os.path.basename(rec['path']))[0]).strip()
                if inferred:
                    tag_updates['title'] = inferred

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
) -> tuple[bool, str]:
    """在单次原子临时副本上合并执行标签补全、封面规范与音轨/碟号写入。"""
    tag_updates = dict(tag_updates or {})
    if not tag_updates and cover_bytes is None and tracknumber is None and discnumber is None:
        return True, '无需写入'
    state = read_audio_info(path)
    if state.get('error'):
        return False, str(state['error'])

    write_tn = tracknumber
    if write_tn is not None and not overwrite_track:
        cur_tn = state.get('track_int')
        if cur_tn is not None and cur_tn > 0:
            if not tag_updates and cover_bytes is None and discnumber is None:
                return (True, '已有相同音轨号') if cur_tn == int(write_tn) else (False, f"已有音轨号 ({state.get('tracknumber')})，跳过覆盖")
            write_tn = None
    write_dn = int(discnumber) if (discnumber is not None and int(discnumber) > 0 and not state.get('disc_int')) else None
    if not tag_updates and cover_bytes is None and write_tn is None and write_dn is None:
        return True, '无需写入'

    applied_fields: set[str] = set()

    def _mutate(audio, container: str, _tmp: str):
        applied_fields.clear()
        did: list[str] = []
        for fn, val in tag_updates.items():
            if apply_tag_field(audio, container, fn, val):
                did.append(f'{fn}={val}')
                applied_fields.add(fn)
        if cover_bytes is not None:
            if not apply_single_front_cover(audio, container, cover_bytes, cover_mime or 'image/jpeg'):
                raise RuntimeError(f'{container} 容器暂不支持写入封面')
            did.append('封面已规范')
        if write_tn is not None or write_dn is not None:
            apply_track_to_audio(audio, container, write_tn, discnumber=write_dn, write_disc_if_missing=(write_dn is not None))
            if write_tn is not None:
                did.append(f'音轨号={int(write_tn)}')
            if write_dn is not None:
                did.append(f'碟号={int(write_dn)}')
        return (bool(did), '，'.join(did) if did else '无需改写')

    def _verify(chk_path: str, container: str):
        chk_info = read_audio_info(chk_path) if (write_tn is not None or write_dn is not None or applied_fields) else {}
        if write_tn is not None and chk_info.get('track_int') != int(write_tn):
            return False, '音轨号校验失败'
        if write_dn is not None and chk_info.get('disc_int') != int(write_dn):
            return False, '碟片编号校验失败'
        for fn in applied_fields:
            got_val = (chk_info.get(fn) or '').strip()
            if not got_val or (fn == 'date' and got_val != str(tag_updates.get('date') or '').strip()):
                return False, f'{fn}标签写回校验失败'
        if cover_bytes is not None and container in SUPPORTS_COVER_WRITE:
            pics = read_pictures(LOADERS[container](chk_path), container)
            if not pics or not sniff_image(pics[0]['data']):
                return False, '封面写回校验失败'
        return True, ''

    ok, msg = atomic_mutate_audio(path, _mutate, verify_fn=_verify)
    if ok:
        cleanup_stale_musictmp_for(path)
    return ok, msg
