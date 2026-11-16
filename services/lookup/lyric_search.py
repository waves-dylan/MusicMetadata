"""在线多源歌词并发检索与候选聚合服务（支持网易云、QQ音乐、酷狗、LRCLIB等）。"""

from __future__ import annotations

import concurrent.futures
from dataclasses import dataclass
import re
from typing import Any, Callable

from providers import PROVIDERS, PROVIDER_LABELS
from providers.base import first_artist, norm_core, parse_all_lyric_credits, to_seconds
from services.lookup.scoring import (
    COMPILATION_KEYWORDS,
    DISQUALIFY_KEYWORDS,
    LIVE_KEYWORDS,
    clean_album_noise,
    compute_album_affinity_score,
)

_LRC_TIMESTAMP_RE = re.compile(r'\[(\d{1,2}):(\d{2})(?:\.(\d{2,3}))?\]')


@dataclass
class LyricCandidate:
    """单个歌词候选版本载体。"""
    platform: str
    platform_label: str
    song_id: str
    title: str
    artist: str
    album_name: str
    release_date: str
    duration: float
    duration_diff: float
    lyrics: str
    has_translation: bool
    composer: str
    lyricist: str
    arranger: str
    score: float
    is_live: bool
    is_compilation: bool
    cover_url: str = ''


def detect_has_translation(lyrics_text: str) -> bool:
    """检测 LRC 文本中是否包含双语翻译（如相同时间戳重复行或中外双语对照）。"""
    if not lyrics_text:
        return False
    ts_counts: dict[str, int] = {}
    lines = lyrics_text.splitlines()
    has_cjk = False
    has_latin = False

    for line in lines:
        s = line.strip()
        if not s or s.startswith(('[ti:', '[ar:', '[al:', '[by:', '[offset:')):
            continue
        m = _LRC_TIMESTAMP_RE.match(s)
        if m:
            ts = m.group(0)
            ts_counts[ts] = ts_counts.get(ts, 0) + 1
            if ts_counts[ts] >= 2:
                return True
            content = _LRC_TIMESTAMP_RE.sub('', s).strip()
            if any('\u4e00' <= ch <= '\u9fff' for ch in content):
                has_cjk = True
            if any(('a' <= ch <= 'z' or 'A' <= ch <= 'Z') for ch in content):
                has_latin = True

    # 若超过 5 行相同时间戳，确为双语
    dups = sum(1 for c in ts_counts.values() if c >= 2)
    return dups >= 3 or (has_cjk and has_latin and dups >= 1)


def apply_lrc_offset(lyrics_text: str, offset_ms: int) -> str:
    """为 LRC 歌词中的所有时间戳统一平移 offset_ms 毫秒（支持提前或延后）。"""
    if not lyrics_text or offset_ms == 0:
        return lyrics_text

    def _shift(match: re.Match) -> str:
        mm = int(match.group(1))
        ss = int(match.group(2))
        ms_str = match.group(3) or '0'
        if len(ms_str) == 2:
            ms = int(ms_str) * 10
        else:
            ms = int(ms_str[:3])
        total_ms = (mm * 60 + ss) * 1000 + ms + offset_ms
        if total_ms < 0:
            total_ms = 0
        new_mm = total_ms // 60000
        rem_ms = total_ms % 60000
        new_ss = rem_ms // 1000
        new_cs = (rem_ms % 1000) // 10
        return f'[{new_mm:02d}:{new_ss:02d}.{new_cs:02d}]'

    return _LRC_TIMESTAMP_RE.sub(_shift, lyrics_text)


def search_candidate_lyrics(
    title: str,
    artist: str,
    *,
    album: str = '',
    duration: float = 0.0,
    prefer_sources: tuple[str, ...] | list[str] | None = None,
    prefer_rich_lyrics: bool = False,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> list[LyricCandidate]:
    """为单曲跨平台检索多版本候选歌词，返回按专辑亲和度打分倒序排列的候选列表。"""
    def _notify(evt: dict[str, Any]):
        if on_progress:
            try:
                on_progress(evt)
            except Exception:
                pass

    t_clean = str(title or '').strip()
    a_clean = first_artist(artist) or str(artist or '').strip()
    alb_clean = str(album or '').strip()

    if not t_clean:
        return []

    # 1. 梯队式检索词：兼顾歌曲名+歌手，以及用户指定的特定专辑/版本修饰词 (如复黑王、首版、BTB等)
    terms: list[str] = [f'{t_clean} {a_clean}'.strip() if a_clean else t_clean]
    if alb_clean:
        c_alb = clean_album_noise(alb_clean)
        if c_alb and c_alb not in terms[0]:
            terms.append(f'{t_clean} {a_clean} {c_alb}'.strip())
        for kw in ('复黑王', '首版', 'btb', '原声', '黑胶', '电影原声'):
            if kw in alb_clean.lower():
                t_kw = f'{t_clean} {a_clean} {kw}'.strip()
                if t_kw not in terms:
                    terms.append(t_kw)

    if prefer_sources:
        sources = list(prefer_sources)
    else:
        sources = ['netease', 'qq', 'kugou', 'lrclib']

    active_sources = [p for p in sources if p in PROVIDERS and PROVIDERS[p].is_available]
    _notify({'type': 'start', 'term': terms[0], 'sources': active_sources})

    candidates: list[LyricCandidate] = []
    seen_keys: set[tuple[str, str]] = set()

    def _fetch_from_platform(plat: str) -> list[LyricCandidate]:
        prov = PROVIDERS.get(plat)
        if not prov or not prov.is_available:
            return []
        plat_label = PROVIDER_LABELS.get(plat, plat)
        hits: list[dict[str, Any]] = []

        # 遍历检索词链获取候选
        for query_term in terms:
            try:
                try:
                    cur_hits = prov.search_song_hits(query_term, title=t_clean, artist=a_clean)
                except TypeError:
                    cur_hits = prov.search_song_hits(query_term)
                if cur_hits:
                    for h in cur_hits:
                        if not any(x.get('song_id') == h.get('song_id') for x in hits):
                            hits.append(h)
            except Exception:
                pass
            if len(hits) >= 12:
                break

        plat_cands: list[LyricCandidate] = []
        # 每平台评估前 8 个命中
        for h in (hits or [])[:8]:
            sid = str(h.get('song_id') or '').strip()
            stitle = str(h.get('title') or '').strip()
            sart = str(h.get('artist') or '').strip()
            salb = str(h.get('album_name') or '').strip()
            sdur = float(h.get('duration') or 0.0)
            rdate = str(h.get('release_date') or '')

            if not sid or not stitle:
                continue

            dur_diff = abs(sdur - duration) if (duration > 0 and sdur > 0) else 0.0
            if dur_diff > 8.0:
                continue

            lyrics_text = ''
            extra_dict: dict = {}
            try:
                if plat == 'lrclib' and h.get('lrc_url'):
                    l_res = prov.get_lyrics_by_url(sid, h['lrc_url'])
                    extra_dict = l_res if isinstance(l_res, dict) else {}
                    lyrics_text = str(extra_dict.get('lyrics') or '').strip()
                elif hasattr(prov, 'get_song_extra'):
                    extra = prov.get_song_extra(sid, need_lyrics=True, title=stitle, duration=sdur)
                    extra_dict = extra if isinstance(extra, dict) else {}
                    lyrics_text = str(extra_dict.get('lyrics') or '').strip()
            except Exception:
                lyrics_text = ''

            if not lyrics_text:
                continue

            key = (plat, sid)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            has_trans = detect_has_translation(lyrics_text)
            creds = parse_all_lyric_credits(lyrics_text)
            comp = creds.get('composer') or str(extra_dict.get('composer') or h.get('composer') or '')
            lyr = creds.get('lyricist') or str(extra_dict.get('lyricist') or h.get('lyricist') or '')
            arr = creds.get('arranger') or str(extra_dict.get('arranger') or h.get('arranger') or '')

            is_live = any(k in salb.lower() for k in LIVE_KEYWORDS) or any(k in stitle.lower() for k in LIVE_KEYWORDS)
            is_comp = any(k in salb.lower() for k in COMPILATION_KEYWORDS)

            score = compute_album_affinity_score(
                cand_album=salb,
                target_album=alb_clean,
                target_title=t_clean,
                target_artist=a_clean,
                cand_artist=sart,
                cand_title=stitle,
                release_date=rdate,
                duration_diff=dur_diff,
                has_translation=has_trans,
                has_credits=bool(comp or lyr),
                prefer_rich_lyrics=prefer_rich_lyrics,
            )

            cand = LyricCandidate(
                platform=plat,
                platform_label=plat_label,
                song_id=sid,
                title=stitle,
                artist=sart,
                album_name=salb,
                release_date=rdate,
                duration=sdur,
                duration_diff=dur_diff,
                lyrics=lyrics_text,
                has_translation=has_trans,
                composer=comp,
                lyricist=lyr,
                arranger=arr,
                score=score,
                is_live=is_live,
                is_compilation=is_comp,
                cover_url=str(h.get('cover_url') or ''),
            )
            plat_cands.append(cand)
            _notify({'type': 'candidate_found', 'candidate': cand})

        return plat_cands

    # 多音源并发拉取 (并发度 4)
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(active_sources) or 1)) as executor:
        futs = [executor.submit(_fetch_from_platform, p) for p in active_sources]
        for fut in concurrent.futures.as_completed(futs):
            try:
                res = fut.result()
                candidates.extend(res)
            except Exception:
                pass

    candidates.sort(key=lambda c: c.score, reverse=True)
    _notify({'type': 'done', 'total': len(candidates)})
    return candidates


__all__ = [
    'LyricCandidate',
    'apply_lrc_offset',
    'detect_has_translation',
    'search_candidate_lyrics',
]
