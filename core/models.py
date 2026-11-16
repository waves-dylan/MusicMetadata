"""MusicMetadata 核心领域数据模型。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from core.constants import (
    PLAYER_TYPE_FOOBAR,
    PLAYER_TYPE_GENERIC,
    PLAYER_TYPE_MUSICBEE,
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_PLAYER,
)


@dataclass
class TrackArtTagPlan:
    """音轨级封面、艺术家写真与标签预修复计划。"""
    path: str
    cover_status: str = '正常'
    cover_reasons: list[str] = field(default_factory=list)
    needs_cover_fix: bool = False
    target_pic_bytes: bytes | None = None
    target_pic_mime: str = 'image/jpeg'
    artist_status: str = '正常'
    artist_reasons: list[str] = field(default_factory=list)
    needs_artist_fix: bool = False
    has_artist_art: bool = False
    target_artist_bytes: bytes | None = None
    target_artist_mime: str = 'image/jpeg'
    tag_updates: dict[str, str] = field(default_factory=dict)
    needs_tag_fix: bool = False


@dataclass
class FolderArtPlan:
    """目录级 cover.jpg 与 artist.jpg 生成/改写/撤回计划。"""
    folder: str
    album_count: int = 0
    cover_action: str = 'ok'
    cover_path: str = ''
    backup_path: str = ''
    cover_jpeg: bytes | None = None
    detail: str = ''
    artist_cover_action: str = 'ok'
    artist_cover_path: str = ''
    backup_artist_path: str = ''
    artist_cover_jpeg: bytes | None = None
    artist_detail: str = ''
    track_plans: dict[str, TrackArtTagPlan] = field(default_factory=dict)

    @property
    def has_folder_cover_work(self) -> bool:
        return self.cover_action in ('create', 'rewrite', 'remove')

    @property
    def has_folder_artist_work(self) -> bool:
        return self.artist_cover_action in ('create', 'rewrite', 'remove')

    @property
    def artist_action(self) -> str:
        return self.artist_cover_action

    @artist_action.setter
    def artist_action(self, val: str):
        self.artist_cover_action = val

    @property
    def artist_path(self) -> str:
        return self.artist_cover_path

    @artist_path.setter
    def artist_path(self, val: str):
        self.artist_cover_path = val


@dataclass
class LibraryTrack:
    """媒体库音频轨道完整状态模型。"""
    path: str
    filename: str
    folder: str
    title: str = ''
    artist: str = ''
    albumartist: str = ''
    album: str = ''
    date: str = ''
    composer: str = ''
    lyricist: str = ''
    arranger: str = ''
    performer: str = ''
    genre: str = ''
    duration: float = 0.0
    disk_tracknumber: str = ''
    disk_track_int: int | None = None
    disk_discnumber: str = ''
    disk_disc_int: int | None = None
    comment: str = ''
    has_163_key: bool = False
    has_lyrics: bool = False
    has_embedded_lyrics: bool = False
    has_lrc_file: bool = False
    needs_lyrics: bool = False
    netease_meta: dict | None = None
    container: str | None = None
    extension_mismatch: bool = False
    error: str | None = None
    foobar_in_db: bool = False
    foobar_tracknumber: str = ''
    foobar_title: str = ''
    foobar_artist: str = ''
    foobar_albumartist: str = ''
    foobar_album: str = ''
    foobar_date: str = ''
    foobar_composer: str = ''
    foobar_lyricist: str = ''
    foobar_arranger: str = ''
    foobar_performer: str = ''
    foobar_genre: str = ''
    foobar_discnumber: str = ''
    foobar_comment: str = ''
    cached_size: int | None = None
    cached_mtime: float = 0.0
    player_stale_reasons: list[str] = field(default_factory=list)
    disk_size: int = 0
    disk_mtime: float = 0.0
    status: str = STATUS_OK
    is_full_album: bool = False
    is_temp_duplicate: bool = False
    note: str = ''
    needs_tracknumber: bool = False
    needs_player_sync: bool = False
    needs_cover_fix: bool = False
    needs_cover_online: bool = False
    cover_status: str = '正常'
    cover_reasons: list[str] = field(default_factory=list)
    target_pic_bytes: bytes | None = None
    target_pic_mime: str = 'image/jpeg'
    has_artist_art: bool = False
    needs_artist_art: bool = False
    artist_status: str = '正常'
    artist_reasons: list[str] = field(default_factory=list)
    target_artist_bytes: bytes | None = None
    target_artist_mime: str = 'image/jpeg'
    needs_tag_fix: bool = False
    tag_updates: dict[str, str] = field(default_factory=dict)
    target_disc: int | None = None
    needs_date: bool = False
    missing_core_fields: list[str] = field(default_factory=list)
    missing_extra_fields: list[str] = field(default_factory=list)
    needs_extra_tags: bool = False
    folder_cover_action: str = 'ok'
    folder_artist_action: str = 'ok'
    folder_plan: FolderArtPlan | None = None
    matched_track: int | None = None
    matched_disc: int | None = None
    matched_total: int | None = None
    matched_source: str = ''
    matched_album: str = ''
    match_note: str = ''
    write_status: str = ''

    def __post_init__(self):
        if self.status == STATUS_MISSING_DISK and not self.needs_tracknumber:
            self.needs_tracknumber = True
        elif self.status == STATUS_STALE_PLAYER and not self.needs_player_sync:
            self.needs_player_sync = True


@dataclass
class PlayerEnv:
    """播放器宿主环境通用数据实体（适配 foobar2000、MusicBee 与通用媒体库）。"""
    player_type: str = PLAYER_TYPE_GENERIC
    player_name: str = '通用本地媒体库'
    exe_path: str = ''
    app_dir: str = ''
    config_dir: str = ''
    library_folders: list[str] = field(default_factory=list)
    unavailable_folders: list[str] = field(default_factory=list)
    reload_command: str = ''
    artist_pictures_dir: str = ''
    extra_info: dict = field(default_factory=dict)


@dataclass
class FoobarEnv(PlayerEnv):
    """foobar2000 本地宿主环境信息（兼容 PlayerEnv）。"""
    def __init__(
        self,
        exe_path: str = '',
        app_dir: str = '',
        config_dir: str = '',
        library_folders: list[str] | None = None,
        unavailable_folders: list[str] | None = None,
        reload_command: str = '标签/从文件重新载入信息',
        **kwargs,
    ):
        super().__init__(
            player_type=PLAYER_TYPE_FOOBAR,
            player_name='foobar2000',
            exe_path=exe_path,
            app_dir=app_dir,
            config_dir=config_dir,
            library_folders=library_folders or [],
            unavailable_folders=unavailable_folders or [],
            reload_command=reload_command,
            extra_info=kwargs,
        )


@dataclass
class TrackMatch:
    """在线检索音轨匹配结果。"""
    tracknumber: int | None
    discnumber: int | None = None
    totaltracks: int | None = None
    source: str = ''
    album_name: str = ''
    reason: str = ''
    release_date: str = ''
    title: str = ''
    artist: str = ''
    albumartist: str = ''
    composer: str = ''
    lyricist: str = ''
    arranger: str = ''
    genre: str = ''
    cover_url: str = ''
    artist_pic_url: str = ''
    lyrics: str = ''
    affinity_score: float = 0.0


def natural_sort_key(s: str) -> tuple:
    """提取支持数字自然排序且大小写不敏感的类型安全元组。"""
    if not s:
        return ()
    return tuple((0, int(t)) if t.isdigit() else (1, t.lower()) for t in re.split(r'(\d+)', str(s)))


def default_track_sort_key(tr: LibraryTrack) -> tuple:
    """默认排序规则：[专辑艺术家 - 艺术家 - 时间 - 专辑 - 音轨号 - 歌曲名]。"""
    album_artist = tr.albumartist or tr.artist or ''
    artist = tr.artist or ''
    date = tr.date or ''
    album = tr.album or ''
    d_num = tr.disk_disc_int if (tr.disk_disc_int is not None and tr.disk_disc_int > 0) else 1
    t_num = tr.disk_track_int if (tr.disk_track_int is not None and tr.disk_track_int > 0) else 999999
    title = tr.title or os.path.splitext(tr.filename)[0]
    return (
        natural_sort_key(album_artist),
        natural_sort_key(artist),
        natural_sort_key(date),
        natural_sort_key(album),
        d_num,
        t_num,
        natural_sort_key(tr.disk_tracknumber or ''),
        natural_sort_key(title),
        tr.path,
    )
