"""目录外置图片、内嵌封面/艺术家写真与缺失分组标签的规划分析服务。"""

from __future__ import annotations

import hashlib
import os
import re
from collections import Counter, OrderedDict

from core.constants import (
    ARTIST_MAX_SIDE,
    ARTIST_NAMES,
    ARTIST_PIC_TYPE,
    COVER_FRONT_TYPE,
    COVER_NAMES,
    EMBED_MAX_SIDE,
    HUGE_BYTES,
    HUGE_PIXELS,
    IMG_EXT,
    MAX_SIDE,
    SAME_ART_DISTANCE,
    SUPPORTS_ARTIST_WRITE,
    SUPPORTS_COVER_WRITE,
)
from core.models import FolderArtPlan, TrackArtTagPlan
from services.art.image_ops import (
    ahash,
    ham,
    normalize_artist_jpeg,
    normalize_jpeg,
    same_art,
    sniff_image,
)
from tag_io import LOADERS, parse_filename_clues, read_audio_info, read_pictures

_DATE_DIR_PREFIX_RE = re.compile(r'^\d{2,4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\s*')
_TRAILING_ID_RE = re.compile(r'\s*-\s*[0-9A-Za-z]{6,}$')


def plan_picture_fix(pics: list[dict], album_jpeg: bytes | None) -> tuple[bool, list[str], bytes | None, str]:
    """只读分析曲目是否包含有效的内嵌封面图片（Type 3），以及是否需要规范化或补齐。"""
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
    if len(fronts) > 1:
        reasons.append(f'multi-front({len(fronts)})')

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


def plan_artist_picture_fix(pics: list[dict], artist_jpeg: bytes | None) -> tuple[bool, list[str], bytes | None, str]:
    """只读分析曲目是否包含有效的内嵌艺术家图片（Type 8），以及是否需要规范化或补齐。"""
    reasons: list[str] = []
    decodable: list[tuple[dict, tuple[str, int, int]]] = []
    for p in pics:
        info = sniff_image(p['data'])
        if info:
            decodable.append((p, info))
    artists = [(p, i) for p, i in decodable if p['type'] in (ARTIST_PIC_TYPE, 7)]
    if not artists:
        return (False, ['no-artist-art-source'], None, 'image/jpeg') if artist_jpeg is None else (True, ['no-artist-art'], artist_jpeg, 'image/jpeg')
    if len(artists) > 1:
        reasons.append(f'multi-artist({len(artists)})')

    base, (fmt, w, h) = artists[0]
    data = base['data']
    mime = 'image/png' if fmt == 'PNG' else ('image/jpeg' if fmt in ('JPEG', 'MPO') else f'image/{fmt.lower()}')
    if (base['mime'] or '').lower() != mime:
        reasons.append(f"mime-mismatch({base['mime'] or '空'}!={mime})")
    if max(w, h) > ARTIST_MAX_SIDE or len(data) > 1024 * 1024:
        reasons.append(f'oversize({w}x{h},{len(data) / 1048576.0:.1f}MB)')
        shrunk = normalize_artist_jpeg(data, ARTIST_MAX_SIDE)
        if shrunk:
            data, mime = shrunk, 'image/jpeg'
    if not reasons:
        return False, [], None, 'image/jpeg'
    if base['type'] != ARTIST_PIC_TYPE:
        reasons.append(f"type-fixed({base['type']}->8)")
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


def _pick_folder_artist_image(folder: str) -> bytes | None:
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
        stem = os.path.splitext(name)[0].lower()
        if stem not in ARTIST_NAMES:
            continue
        rank = (3 if stem == 'artist' else 1, info[1] * info[2])
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
        artist_cover_path=os.path.join(folder, 'artist.jpg'),
        backup_artist_path=os.path.join(folder, 'disabled-artist.jpg.bak'),
    )
    records: list[dict] = []
    for p in audio_paths:
        info = pre_read_infos.get(p) if pre_read_infos else None
        if info is None:
            info = read_audio_info(p)
        pics = info.get('pics')
        if pics is None:
            loader = LOADERS.get(info.get('container')) if not info.get('error') else None
            if loader is None:
                continue
            try:
                pics = read_pictures(loader(p), info.get('container'))
            except Exception:
                pics = []
        records.append({
            'path': p,
            'norm': os.path.normcase(os.path.abspath(p)),
            'info': info,
            'container': info.get('container'),
            'pics': pics,
        })
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
        jpeg = (
            normalize_jpeg(sorted(votes.values(), key=lambda x: (x[0], x[1], x[2]), reverse=True)[0][3], MAX_SIDE)
            if votes else None
        )
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

    # 统计目录内的艺术家图片（内嵌或外置）
    artist_votes: OrderedDict[str, list] = OrderedDict()
    for rec in records:
        for pic in rec['pics']:
            if pic['type'] in (ARTIST_PIC_TYPE, 7):
                pinfo = sniff_image(pic['data'])
                if not pinfo:
                    continue
                h = hashlib.md5(pic['data']).hexdigest()
                type_rank = 1 if pic['type'] == ARTIST_PIC_TYPE else 0
                if h not in artist_votes:
                    artist_votes[h] = [type_rank, 1, pinfo[1] * pinfo[2], pic['data']]
                else:
                    artist_votes[h][0] = max(artist_votes[h][0], type_rank)
                    artist_votes[h][1] += 1
    best_artist_jpeg = (
        normalize_artist_jpeg(sorted(artist_votes.values(), key=lambda x: (x[0], x[1], x[2]), reverse=True)[0][3], ARTIST_MAX_SIDE)
        if artist_votes else None
    )
    if best_artist_jpeg is None:
        raw_art_dir = _pick_folder_artist_image(folder)
        if raw_art_dir:
            best_artist_jpeg = normalize_artist_jpeg(raw_art_dir, ARTIST_MAX_SIDE)

    distinct_artists = {
        (rec['info'].get('albumartist') or rec['info'].get('artist') or '').strip().lower()
        for rec in records if (rec['info'].get('albumartist') or rec['info'].get('artist') or '').strip()
    }
    same_artist_in_folder = len(distinct_artists) <= 1

    if single or same_artist_in_folder:
        plan.artist_cover_jpeg = best_artist_jpeg
        if best_artist_jpeg is None:
            plan.artist_cover_action = 'nodata'
        elif not os.path.exists(plan.artist_cover_path):
            plan.artist_cover_action, plan.artist_detail = 'create', '目录缺少外置 artist.jpg'
        else:
            try:
                with open(plan.artist_cover_path, 'rb') as fp:
                    old_art = fp.read()
                if old_art == best_artist_jpeg or same_art(old_art, best_artist_jpeg):
                    plan.artist_cover_action = 'ok'
                else:
                    plan.artist_cover_action, plan.artist_detail = 'rewrite', '外置 artist.jpg 与代表艺术家图不一致，需重写'
            except OSError:
                plan.artist_cover_action, plan.artist_detail = 'rewrite', '外置 artist.jpg 读取异常，需重写'
    else:
        if os.path.exists(plan.artist_cover_path):
            plan.artist_cover_action, plan.artist_detail = 'remove', '目录含多位不同艺术家，需撤回误放的 artist.jpg'
        else:
            plan.artist_cover_action, plan.artist_detail = 'mixed-skip', '目录含多位不同艺术家，跳过外置 artist.jpg'

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

            need_art_pic, art_reasons, tgt_art_bytes, tgt_art_mime = plan_artist_picture_fix(pics, plan.artist_cover_jpeg)
            if need_art_pic and container not in SUPPORTS_ARTIST_WRITE:
                need_art_pic = False
                art_reasons.append(f'{container}暂不支持写艺术家图')
            has_art_pic = any(p['type'] in (ARTIST_PIC_TYPE, 7) for p in pics)
            art_status = ('待修:' + ','.join(art_reasons)) if need_art_pic else ('缺艺术家图(无可用图源)' if 'no-artist-art-source' in art_reasons else '正常')

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
                path=rec['path'],
                cover_status=cov_status,
                cover_reasons=reasons,
                needs_cover_fix=need_pic,
                target_pic_bytes=tgt_bytes,
                target_pic_mime=tgt_mime,
                artist_status=art_status,
                artist_reasons=art_reasons,
                needs_artist_fix=need_art_pic,
                has_artist_art=has_art_pic,
                target_artist_bytes=tgt_art_bytes,
                target_artist_mime=tgt_art_mime,
                tag_updates=tag_updates,
                needs_tag_fix=bool(tag_updates),
            )
    return plan


def apply_folder_cover_file(plan: FolderArtPlan) -> tuple[bool, str]:
    """为单专辑目录写入或重写外置 cover.jpg（多专辑混放目录撤回备份）。"""
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


def apply_folder_artist_file(plan: FolderArtPlan) -> tuple[bool, str]:
    """为单专辑目录写入或重写外置 artist.jpg（多艺术家混放目录撤回备份）。"""
    if plan.artist_cover_action in ('create', 'rewrite'):
        if not plan.artist_cover_jpeg:
            return False, '无可用艺术家图片数据'
        try:
            with open(plan.artist_cover_path, 'wb') as fp:
                fp.write(plan.artist_cover_jpeg)
            return True, '已生成 artist.jpg' if plan.artist_cover_action == 'create' else '已重写 artist.jpg'
        except OSError as err:
            return False, f'写入 artist.jpg 失败: {err}'
    if plan.artist_cover_action == 'remove' and os.path.exists(plan.artist_cover_path):
        try:
            os.replace(plan.artist_cover_path, plan.backup_artist_path)
            return True, '已撤回多艺术家目录 artist.jpg -> disabled-artist.jpg.bak'
        except OSError as err:
            return False, f'撤回 artist.jpg 失败: {err}'
    return True, '无需处理外置艺术家图片'


def delete_folder_cover_file(folder: str) -> tuple[bool, str]:
    """删除目录中的外置 cover.jpg。"""
    p = os.path.join(folder, 'cover.jpg')
    if os.path.isfile(p):
        try:
            os.remove(p)
            return True, '已删除目录 cover.jpg'
        except OSError as err:
            return False, f'删除 cover.jpg 失败: {err}'
    return True, '目录不存在 cover.jpg'


def delete_folder_artist_file(folder: str) -> tuple[bool, str]:
    """删除目录中的外置 artist.jpg。"""
    p = os.path.join(folder, 'artist.jpg')
    if os.path.isfile(p):
        try:
            os.remove(p)
            return True, '已删除目录 artist.jpg'
        except OSError as err:
            return False, f'删除 artist.jpg 失败: {err}'
    return True, '目录不存在 artist.jpg'
