"""在线音乐平台数据源注册表与优先级编排工具。"""

from __future__ import annotations

from collections.abc import Iterable

from providers.baidu import BaiduProvider
from providers.base import MusicProvider, TrackMatch
from providers.itunes import ITunesProvider
from providers.kugou import KugouProvider
from providers.kuwo import KuwoProvider
from providers.migu import MiGuProvider
from providers.musicbrainz import MusicBrainzProvider
from providers.netease import NetEaseProvider, get_netease_api_base, set_netease_api_base
from providers.qqmusic import QQMusicProvider
from providers.spotify import SpotifyProvider

PRIMARY_PROVIDERS: tuple[str, ...] = ('netease', 'qq', 'itunes', 'mb')
EXTENDED_CN_PROVIDERS: tuple[str, ...] = ('kugou', 'migu', 'baidu')
ONLINE_PROVIDERS: tuple[str, ...] = PRIMARY_PROVIDERS + EXTENDED_CN_PROVIDERS

netease_provider = NetEaseProvider()
qq_provider = QQMusicProvider()
itunes_provider = ITunesProvider()
mb_provider = MusicBrainzProvider()
kugou_provider = KugouProvider()
migu_provider = MiGuProvider()
baidu_provider = BaiduProvider()
kuwo_provider = KuwoProvider()
spotify_provider = SpotifyProvider()

PROVIDERS: dict[str, MusicProvider] = {
    'netease': netease_provider,
    'qq': qq_provider,
    'itunes': itunes_provider,
    'mb': mb_provider,
    'kugou': kugou_provider,
    'migu': migu_provider,
    'baidu': baidu_provider,
    'kuwo': kuwo_provider,
    'spotify': spotify_provider,
}

PROVIDER_LABELS: dict[str, str] = {pid: p.label for pid, p in PROVIDERS.items()}

_SOURCE_ALIASES: dict[str, str] = {
    'netease': 'netease', '163': 'netease', 'wy': 'netease', '网易': 'netease', '网易云': 'netease', '网易云音乐': 'netease',
    'qq': 'qq', 'qqmusic': 'qq', 'tencent': 'qq', 'qq音乐': 'qq',
    'itunes': 'itunes', 'apple': 'itunes', 'applemusic': 'itunes', 'apple music': 'itunes',
    'mb': 'mb', 'musicbrainz': 'mb',
    'kugou': 'kugou', 'kg': 'kugou', '酷狗': 'kugou', '酷狗音乐': 'kugou',
    'migu': 'migu', 'mg': 'migu', '咪咕': 'migu', '咪咕音乐': 'migu',
    'baidu': 'baidu', 'qianqian': 'baidu', '91q': 'baidu', '百度': 'baidu', '百度音乐': 'baidu', '千千': 'baidu', '千千音乐': 'baidu',
    'kuwo': 'kuwo', 'kw': 'kuwo', '酷我': 'kuwo', '酷我音乐': 'kuwo',
    'spotify': 'spotify',
}


def normalize_provider_name(raw: str) -> str | None:
    tok = str(raw or '').strip()
    if not tok:
        return None
    low = tok.lower()
    return _SOURCE_ALIASES.get(low) or _SOURCE_ALIASES.get(tok)


def parse_source_list(raw_items: Iterable[str] | str | None) -> tuple[tuple[str, ...], list[str]]:
    """解析逗号分隔或列表形式的音源名称，返回 (标准化ID元组, 无效标记列表)。"""
    if raw_items is None:
        return (), []
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items)
    out: list[str] = []
    invalid: list[str] = []
    for item in items:
        for tok in str(item or '').replace('，', ',').replace(';', ',').split(','):
            s = tok.strip()
            if not s:
                continue
            pid = normalize_provider_name(s)
            if pid and pid in ONLINE_PROVIDERS:
                if pid not in out:
                    out.append(pid)
            elif s not in invalid:
                invalid.append(s)
    return tuple(out), invalid


def resolve_provider_order(
    *,
    prefer_sources: Iterable[str] | None = None,
    only_sources: Iterable[str] | None = None,
    skip_sources: Iterable[str] | None = None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """根据用户偏好、限定和跳过规则计算 (主专辑/单曲源顺序, 扩展兜底源顺序)。"""
    skip = {p for raw in (skip_sources or ()) if (p := normalize_provider_name(raw))}
    only_list = [p for raw in (only_sources or ()) if (p := normalize_provider_name(raw)) and p in ONLINE_PROVIDERS]
    prefer_list = [p for raw in (prefer_sources or ()) if (p := normalize_provider_name(raw)) and p in ONLINE_PROVIDERS]

    if only_list:
        ordered = [p for p in only_list if p not in skip]
        return tuple(ordered), ()

    ordered_all: list[str] = []
    for p in prefer_list:
        if p not in skip and p not in ordered_all:
            ordered_all.append(p)
    for p in ONLINE_PROVIDERS:
        if p not in skip and p not in ordered_all:
            ordered_all.append(p)

    # 若用户在 prefer 中明确指定了酷狗/咪咕/千千，则将其纳入主顺序，否则作为扩展兜底顺序
    explicit_ext = set(prefer_list) & set(EXTENDED_CN_PROVIDERS)
    primary = [p for p in ordered_all if p in PRIMARY_PROVIDERS or p in explicit_ext]
    extended = [p for p in ordered_all if p not in primary]
    return tuple(primary), tuple(extended)


__all__ = [
    'EXTENDED_CN_PROVIDERS', 'ONLINE_PROVIDERS', 'PRIMARY_PROVIDERS',
    'PROVIDERS', 'PROVIDER_LABELS', 'MusicProvider', 'TrackMatch',
    'baidu_provider', 'get_netease_api_base', 'itunes_provider', 'kugou_provider',
    'kuwo_provider', 'mb_provider', 'migu_provider', 'netease_provider',
    'normalize_provider_name', 'parse_source_list', 'qq_provider',
    'resolve_provider_order', 'set_netease_api_base', 'spotify_provider',
]
