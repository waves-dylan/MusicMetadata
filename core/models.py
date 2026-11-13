"""MusicMetadata 核心领域数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.constants import STATUS_MISSING_DISK, STATUS_OK, STATUS_STALE_FOOBAR


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
    foobar_performer: str = ''
    foobar_genre: str = ''
    foobar_discnumber: str = ''
    foobar_comment: str = ''
    foobar_size: int | None = None
    foobar_mtime: float = 0.0
    foobar_stale_reasons: list[str] = field(default_factory=list)
    disk_size: int = 0
    disk_mtime: float = 0.0
    status: str = STATUS_OK
    is_full_album: bool = False
    is_temp_duplicate: bool = False
    note: str = ''
    needs_tracknumber: bool = False
    needs_foobar_sync: bool = False
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
        elif self.status == STATUS_STALE_FOOBAR and not self.needs_foobar_sync:
            self.needs_foobar_sync = True


@dataclass
class FoobarEnv:
    """foobar2000 本地宿主环境信息。"""
    fb_exe: str = ''
    fb_dir: str = ''
    profile_dir: str = ''
    library_folders: list[str] = field(default_factory=list)
    unavailable_folders: list[str] = field(default_factory=list)
    reload_command: str = '标签/从文件重新载入信息'


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
    genre: str = ''
    cover_url: str = ''
    artist_pic_url: str = ''
    lyrics: str = ''
