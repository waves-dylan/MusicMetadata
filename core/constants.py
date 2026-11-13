"""MusicMetadata 核心常量与枚举定义。"""

from __future__ import annotations

# 音频文件与封面 Picture 类型
COVER_FRONT_TYPE = 3
ARTIST_PIC_TYPE = 8

# 支持的音频扩展名与容器
AUDIO_EXTS: set[str] = {
    '.flac', '.mp3', '.mp2', '.m4a', '.mp4', '.m4b', '.alac',
    '.ogg', '.oga', '.opus', '.wav', '.wave', '.aiff', '.aif', '.aifc',
    '.wma', '.asf', '.ape', '.wv',
}
SUPPORTS_EMBEDDED_LYRICS: set[str] = {
    'flac', 'mp3', 'mp4', 'ogg', 'opus', 'asf', 'aiff', 'wav', 'ape', 'wv',
}
PREFER_EXTERNAL_LRC_CONTAINERS: set[str] = {'wav', 'ape', 'wv'}
CONVERT_TARGET_FORMATS: tuple[str, ...] = ('flac', 'mp3', 'm4a', 'ogg', 'opus', 'wav', 'aiff')

# 图片规范与尺寸限制
IMG_EXT: set[str] = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
COVER_NAMES: set[str] = {'cover', 'folder', 'front', 'album', 'albumart'}
ARTIST_NAMES: set[str] = {'artist', 'artistart', 'author', 'singer', 'band'}
MAX_SIDE = 1000
EMBED_MAX_SIDE = 1400
ARTIST_MAX_SIDE = 1000
JPEG_QUALITY = 90
ARTIST_JPEG_QUALITY = 100
ARTIST_MAX_BYTES = 200 * 1024
HUGE_BYTES = 2 * 1024 * 1024
HUGE_PIXELS = 4_000_000
SAME_ART_DISTANCE = 12

SUPPORTS_COVER_WRITE: set[str] = {'flac', 'mp4', 'mp3', 'wav', 'aiff', 'ogg', 'opus'}
SUPPORTS_ARTIST_WRITE: set[str] = {'flac', 'mp3', 'wav', 'aiff', 'ogg', 'opus'}
LYRICS_MODES: tuple[str, ...] = ('auto', 'embed', 'lrc', 'both')

# 轨道扫描状态
STATUS_MISSING_DISK = 'missing_disk'
STATUS_STALE_FOOBAR = 'stale_foobar'
STATUS_OK = 'ok'
STATUS_ERROR = 'error'

# 标签字段分组与展示名称
CORE_TEXT_FIELDS: tuple[str, ...] = ('title', 'artist', 'albumartist', 'album', 'date')
EXTRA_TEXT_FIELDS: tuple[str, ...] = ('composer', 'genre', 'discnumber')
FIELD_LABELS: dict[str, str] = {
    'title': '标题',
    'artist': '艺术家',
    'albumartist': '专辑艺术家',
    'album': '专辑',
    'date': '日期',
    'composer': '作曲',
    'performer': '表演者',
    'genre': '流派',
    'tracknumber': '音轨号',
    'discnumber': '碟片编号',
    'comment': '注释',
    'lyrics': '歌词',
    'cover': '封面',
    'artist_cover': '封面-艺术家',
}

DELETABLE_FIELDS: tuple[str, ...] = (
    'cover', 'artist_cover', 'title', 'artist', 'albumartist', 'album', 'date',
    'composer', 'performer', 'genre', 'tracknumber', 'discnumber', 'comment', 'lyrics',
)
SETTABLE_FIELDS: tuple[str, ...] = (
    'title', 'artist', 'albumartist', 'album', 'date',
    'composer', 'performer', 'genre', 'tracknumber', 'discnumber', 'comment', 'lyrics',
)

FOLDER_COVER_LABELS: dict[str, str] = {
    'create': '目录缺cover.jpg',
    'rewrite': '目录cover.jpg需重写',
    'remove': '多专辑需撤回cover.jpg',
}
