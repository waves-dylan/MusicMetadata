"""元数据与歌词亲和度智能加权引擎（打分、文本降噪与大碟识别）。"""

from __future__ import annotations

import difflib
import re
from typing import Any

from providers.base import norm_core

# 降权关键词：精选集、拼盘大碟、合辑
COMPILATION_KEYWORDS: tuple[str, ...] = (
    '精选', '选辑', '大碟精选', '全集', '新歌+精选', '新歌加精选', 'best of', 'greatest hits',
    'collection', 'anthology', 'the ultimate', 'complete', 'golden hits', '经典金曲',
    '极品珍藏', '纪念盘', '世纪巨星', '世纪金选', '国语金曲', '白金唱片', '至尊精选',
    '黄金典藏', '珍藏集', '典藏集', 'hits collection', 'the best',
)

# 降权关键词：演唱会、现场实况、Live 版
LIVE_KEYWORDS: tuple[str, ...] = (
    '演唱会', 'live', '音乐会', '现场', 'live session', 'tour', '实况录音', '不插电',
    'mtv live', 'in concert', '巡回演唱会', '现场录音', 'acoustic live',
)

# 加权关键词：首版、复黑王、黑胶大碟、发烧母带
BONUS_EDITION_KEYWORDS: tuple[str, ...] = (
    '首版', '复黑', '环球复黑王', 'btb', '黑胶', 'lp', 'sacd', 'xrcd', 'dsd', '母带',
    'master', 'original soundtrack', 'ost', '原声大碟', '原声带',
)

# 排除关键词：纯音乐、伴奏、卡拉OK、DJ混音（非原唱原版曲目）
DISQUALIFY_KEYWORDS: tuple[str, ...] = (
    '伴奏', '纯音乐', 'instrumental', 'karaoke', 'ktv版', '伴唱', '铃声', 'bgm', 'off vocal',
    'dj版', 'dj默涵', 'dj', 'remix', '电音版', '加快版', '减速版',
)

_BRACKET_NOISE_RE = re.compile(
    r'[\(\[\{（【《][^()\[\]{}（）【】《》]*(?:remaster|deluxe|version|edition|bonus|24bit|96khz|192khz|hi-res|flac|dsd|sacd|cd|lp|首版|复黑|独家|无损)[^()\[\]{}（）【】《》]*[\)\]\}）】》]',
    re.IGNORECASE,
)

_VERSION_PREFIX_RE = re.compile(
    r'^(?:复黑王|环球复黑王|btb|首版|经典|lp|cd|dsd|sacd)\s*',
    re.IGNORECASE,
)


def clean_album_noise(text: str) -> str:
    """去除专辑名中的常见版本噪声修饰词（如 Remastered, Deluxe, 首版, 复黑王等）。"""
    s = str(text or '').strip()
    if not s:
        return ''
    # 移除修饰性括号
    s = _BRACKET_NOISE_RE.sub('', s)
    s = _VERSION_PREFIX_RE.sub('', s)
    # 移除多余连续空格
    return re.sub(r'\s+', ' ', s).strip()


def clean_title_noise(text: str) -> str:
    """去除歌曲名中的修饰词（如 Live, 2020 Remaster, Single Version 等）。"""
    s = str(text or '').strip()
    if not s:
        return ''
    s = _BRACKET_NOISE_RE.sub('', s)
    s = re.sub(r'[\(\[\{（【《][^()\[\]{}（）【】《》]*(?:live|现场|remix|version)[^()\[\]{}（）【】《》]*[\)\]\}）】》]', '', s, flags=re.IGNORECASE)
    return re.sub(r'\s+', ' ', s).strip()


def text_similarity(a: str, b: str) -> float:
    """计算两个字符串的归一化核心词相似度比率 [0.0, 1.0]。"""
    na = norm_core(a)
    nb = norm_core(b)
    if not na and not nb:
        return 1.0
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return max(len(na), len(nb)) / (len(na) + len(nb)) * 1.5
    return difflib.SequenceMatcher(None, na, nb).ratio()


def is_disqualified(title: str, album: str, artist: str = '') -> bool:
    """检查候选是否属于纯音乐、伴奏或卡拉OK等无效歌词候选。"""
    t_low = title.lower()
    alb_low = album.lower()
    for kw in DISQUALIFY_KEYWORDS:
        if kw in t_low or kw in alb_low:
            return True
    return False


def compute_album_affinity_score(
    cand_album: str,
    target_album: str,
    target_title: str,
    target_artist: str,
    *,
    cand_artist: str = '',
    cand_title: str = '',
    release_date: str = '',
    duration_diff: float = 0.0,
    has_translation: bool = False,
    has_credits: bool = False,
    prefer_rich_lyrics: bool = False,
) -> float:
    """根据专辑名亲和度、发行版本类型、年代与歌词丰富度计算多维综合评分。

    总分基准 100 分，典型优良录音室大碟得分 200~300+ 分，严重偏差或精选集得分通常 < 150 分，
    致命不匹配返回 -9999.0 分。
    """
    # 1. 致命公差硬拦截 (严格锁定 8.0 秒标准歌词时间轴公差，防止错误混搭不同剪辑/现场版)
    if duration_diff > 8.0:
        return -9999.0

    # 2. 伴奏/纯音乐一票否决
    if is_disqualified(cand_title, cand_album, cand_artist):
        return -9999.0

    score = 100.0

    # 3. 时长惩罚：平滑渐进衰减 (8.0 秒内微小误差平滑扣分，避免断崖式淘汰)
    if duration_diff <= 3.0:
        score -= (duration_diff ** 2) * 1.0
    else:
        score -= 9.0 + (duration_diff - 3.0) * 3.0

    # 4. 歌曲标题契合度 (0 ~ 50 分)
    clean_tt = clean_title_noise(target_title)
    clean_ct = clean_title_noise(cand_title)
    if clean_tt and clean_ct:
        t_sim = text_similarity(clean_tt, clean_ct)
        score += min(50.0, t_sim * 50.0)

    # 5. 艺术家契合度 (0 ~ 40 分)
    clean_ta = norm_core(target_artist)
    clean_ca = norm_core(cand_artist)
    if clean_ta and clean_ca:
        if clean_ta in clean_ca or clean_ca in clean_ta:
            score += 40.0
        else:
            a_sim = text_similarity(clean_ta, clean_ca)
            score += min(40.0, a_sim * 40.0)
    elif clean_ta:
        score += 15.0

    # 6. 专辑名亲和度 (-60 ~ +80 分)
    c_alb_clean = clean_album_noise(cand_album)
    t_alb_clean = clean_album_noise(target_album)

    if t_alb_clean:
        # 分支 A：本地已有明确目标专辑名
        sim = text_similarity(t_alb_clean, c_alb_clean)
        norm_ta = norm_core(t_alb_clean)
        norm_ca = norm_core(c_alb_clean)

        if norm_ta == norm_ca:
            score += 80.0
        elif norm_ta in norm_ca or norm_ca in norm_ta:
            score += 55.0
        elif sim >= 0.75:
            score += 40.0
        elif sim >= 0.5:
            score += 15.0
        else:
            score -= 35.0
    else:
        # 分支 B：本地无专辑名（单曲推断模式，偏好同名专辑或标准录音室命名）
        norm_ct = norm_core(clean_tt or target_title)
        norm_ca = norm_core(c_alb_clean)
        norm_art = norm_core(clean_ta or target_artist)

        # 歌曲同名主打大碟 (Title Track Album) 加分
        if norm_ct and norm_ct in norm_ca:
            score += 50.0
        # 歌手同名大碟 (Self-titled Album) 加分
        if norm_art and norm_art in norm_ca:
            score += 35.0
        # 专辑名不为空且长度适中加基础分
        if 2 <= len(norm_ca) <= 20:
            score += 10.0

    # 7. 发行类型打分（精选集与 Live 降权，首版与母带加权）
    c_alb_lower = (cand_album or '').lower()
    c_tit_lower = (cand_title or '').lower()

    if any(k in c_alb_lower for k in COMPILATION_KEYWORDS):
        score -= 45.0
    elif any(k in c_alb_lower for k in LIVE_KEYWORDS) or any(k in c_tit_lower for k in LIVE_KEYWORDS):
        score -= 40.0
    elif any(k in c_alb_lower for k in BONUS_EDITION_KEYWORDS):
        score += 25.0
    else:
        # 标准录音室大碟默认正向加分
        score += 15.0

    # 8. 首版年代加权 (0 ~ 30 分)
    if release_date:
        try:
            yr_str = str(release_date)[:4]
            if yr_str.isdigit():
                yr = int(yr_str)
                if 1950 <= yr <= 2010:
                    score += min(30.0, max(0.0, (2010 - yr) * 0.8))
        except Exception:
            pass

    # 9. 歌词丰富度加权（响应用户需求：可配置“优先选择含双语翻译/词曲齐全版本”）
    if prefer_rich_lyrics:
        if has_translation:
            score += 20.0
        if has_credits:
            score += 15.0
    else:
        # 未开启时仅给予微小辅助加分（同专辑同版本时的 tie-breaker）
        if has_translation:
            score += 3.0
        if has_credits:
            score += 2.0

    return round(score, 2)


def rank_song_hits(
    hits: list[dict[str, Any]],
    target_title: str,
    target_artist: str,
    target_album: str,
    duration: float,
    *,
    prefer_rich_lyrics: bool = False,
) -> list[dict[str, Any]]:
    """对单曲搜索返回的候选列表进行专辑亲和度打分并倒序排列。"""
    ranked: list[dict[str, Any]] = []
    for h in hits:
        stitle = str(h.get('title') or '')
        sart = str(h.get('artist') or '')
        sdur = float(h.get('duration') or 0.0)
        alb_name = str(h.get('album_name') or '')
        rdate = str(h.get('release_date') or '')

        dur_diff = abs(sdur - duration) if (duration > 0 and sdur > 0) else 0.0

        has_trans = bool(h.get('has_translation'))
        has_cred = bool(h.get('composer') or h.get('lyricist'))

        score = compute_album_affinity_score(
            cand_album=alb_name,
            target_album=target_album,
            target_title=target_title,
            target_artist=target_artist,
            cand_artist=sart,
            cand_title=stitle,
            release_date=rdate,
            duration_diff=dur_diff,
            has_translation=has_trans,
            has_credits=has_cred,
            prefer_rich_lyrics=prefer_rich_lyrics,
        )

        item = dict(h)
        item['affinity_score'] = score
        item['duration_diff'] = dur_diff
        ranked.append(item)

    ranked.sort(key=lambda x: x['affinity_score'], reverse=True)
    return ranked


__all__ = [
    'BONUS_EDITION_KEYWORDS',
    'COMPILATION_KEYWORDS',
    'DISQUALIFY_KEYWORDS',
    'LIVE_KEYWORDS',
    'clean_album_noise',
    'clean_title_noise',
    'compute_album_affinity_score',
    'is_disqualified',
    'rank_song_hits',
    'text_similarity',
]
