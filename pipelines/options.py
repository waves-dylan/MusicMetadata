"""批处理流水线参数模型与选项解析器。"""

from __future__ import annotations

from dataclasses import dataclass

from core.constants import DELETABLE_FIELDS, FIELD_LABELS, FOLDER_COVER_LABELS
from core.models import LibraryTrack, TrackMatch
from infra.scanner import track_has_pending_work


@dataclass
class FixOptions:
    """元数据与封面自动/联网修复配置选项。"""
    enable_track: bool = True
    enable_art: bool = True
    enable_artist_art: bool = False
    enable_tags: bool = True
    enable_extra_tags: bool = True
    enable_date: bool = True
    enable_albumartist: bool = True
    enable_artist: bool = True
    enable_composer: bool = True
    enable_genre: bool = True
    enable_disc: bool = True
    enable_lyrics: bool = False
    lyrics_mode: str = 'auto'
    prefer_sources: tuple[str, ...] = ()
    only_sources: tuple[str, ...] = ()
    skip_sources: tuple[str, ...] = ()
    netease_api: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None
    tadb_api_key: str | None = None
    fanart_api_key: str | None = None
    fanart_client_key: str | None = None
    enable_sync: bool = True
    sync_only: bool = False
    dry_run: bool = False

    def active_tag_fields(self) -> frozenset[str]:
        if not self.enable_tags:
            return frozenset()
        fields: set[str] = set()
        if self.enable_date and self.enable_albumartist and self.enable_artist:
            fields.update(('title', 'album'))
        if self.enable_date:
            fields.add('date')
        if self.enable_albumartist:
            fields.add('albumartist')
        if self.enable_artist:
            fields.add('artist')
        if self.enable_extra_tags:
            if self.enable_composer:
                fields.add('composer')
            if self.enable_genre:
                fields.add('genre')
            if self.enable_disc:
                fields.add('discnumber')
        return frozenset(fields)


def default_checked(track: LibraryTrack, skip_full_album: bool = True, options: FixOptions | None = None) -> bool:
    """根据选项配置判断某条轨道是否默认勾选待处理。"""
    opts = options or FixOptions()
    return track_has_pending_work(
        track,
        enable_track=opts.enable_track,
        enable_art=opts.enable_art,
        enable_tags=opts.enable_tags,
        enable_sync=opts.enable_sync,
        skip_full_album=skip_full_album,
        enable_extra_tags=(opts.enable_tags and opts.enable_extra_tags),
        enabled_tag_fields=opts.active_tag_fields(),
        enable_lyrics=opts.enable_lyrics,
        enable_artist_art=opts.enable_artist_art,
    )


@dataclass
class DeleteOptions:
    """批量局部删除元数据配置选项。"""
    fields: frozenset[str] = frozenset()
    delete_folder_cover: bool = False
    delete_folder_artist: bool = False
    delete_lrc_file: bool = True
    protect_163_key: bool = True
    enable_sync: bool = True
    dry_run: bool = False


@dataclass
class SetOptions:
    """批量手工设置标签配置选项。"""
    tag_values: dict[str, str]
    protect_163_key: bool = True
    enable_sync: bool = True
    dry_run: bool = False


@dataclass
class ConvertOptions:
    """音频格式批量转换配置选项。"""
    target_format: str = 'flac'
    overwrite: bool = False
    delete_source: bool = False
    enable_sync: bool = True
    dry_run: bool = False


_DELETE_FIELD_ALIASES = {
    'art': 'cover',
    'album_artist': 'albumartist',
    'track': 'tracknumber',
    'disc': 'discnumber',
    'year': 'date',
    'comments': 'comment',
    'lrc': 'lyrics',
    'lyric': 'lyrics',
    **{v: k for k, v in FIELD_LABELS.items()},
}


def parse_delete_fields(raw_items: list[str] | tuple[str, ...] | str) -> tuple[list[str], list[str]]:
    """解析命令行/外部传入的待删除元数据类别名称，返回 (有效字段列表, 无效标记列表)。"""
    items = [raw_items] if isinstance(raw_items, str) else list(raw_items or ())
    requested: set[str] = set()
    invalid: list[str] = []
    valid_set = set(DELETABLE_FIELDS)
    for item in items:
        for raw_tok in str(item or '').replace('，', ',').replace(';', ',').split(','):
            tok = raw_tok.strip()
            if not tok:
                continue
            low = tok.lower()
            mapped = low if low in valid_set else _DELETE_FIELD_ALIASES.get(low) or _DELETE_FIELD_ALIASES.get(tok)
            if mapped in valid_set:
                requested.add(mapped)
            elif tok not in invalid:
                invalid.append(tok)
    ordered = [f for f in DELETABLE_FIELDS if f in requested]
    return ordered, invalid
