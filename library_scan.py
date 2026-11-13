"""通用本地音乐库目录扫描、轨道状态模型与专辑校对（向下兼容门面）。"""

from __future__ import annotations

from core.constants import (
    CORE_TEXT_FIELDS,
    EXTRA_TEXT_FIELDS,
    FIELD_LABELS,
    STATUS_ERROR,
    STATUS_MISSING_DISK,
    STATUS_OK,
    STATUS_STALE_FOOBAR,
)
from core.models import FolderArtPlan, LibraryTrack
from infra.scanner.local_scanner import (
    _FULL_ALBUM_RE,
    _TEMP_COPY_RE,
    _append_note,
    _is_full_album_file,
    _is_temp_duplicate,
    _norm_cmp,
    is_under_folders,
    reconcile_album_consistency,
    scan_library,
    touch_files_mtime,
    track_has_pending_work,
)

__all__ = [
    'CORE_TEXT_FIELDS',
    'EXTRA_TEXT_FIELDS',
    'FIELD_LABELS',
    'STATUS_ERROR',
    'STATUS_MISSING_DISK',
    'STATUS_OK',
    'STATUS_STALE_FOOBAR',
    'FolderArtPlan',
    'LibraryTrack',
    '_FULL_ALBUM_RE',
    '_TEMP_COPY_RE',
    '_append_note',
    '_is_full_album_file',
    '_is_temp_duplicate',
    '_norm_cmp',
    'is_under_folders',
    'reconcile_album_consistency',
    'scan_library',
    'touch_files_mtime',
    'track_has_pending_work',
]
