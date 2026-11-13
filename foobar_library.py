"""foobar2000 媒体库集成兼容门面（重定向至 core 与 infra.integrations）。"""

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
from core.models import FoobarEnv, LibraryTrack
from infra.integrations.foobar import (
    _detect_reload_command,
    _open_sqlite_ro,
    _parse_metadb_info,
    _resolve_fb_uri,
    detect_foobar_env,
    find_foobar_cache_diffs,
    get_running_foobar_exes,
    load_metadb_index,
    parse_library_folders,
    scan_library,
    sync_files_to_foobar,
)
from infra.scanner import (
    _append_note,
    _is_full_album_file,
    _is_temp_duplicate,
    _norm_cmp,
    is_under_folders,
    reconcile_album_consistency,
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
    'FoobarEnv',
    'LibraryTrack',
    '_append_note',
    '_detect_reload_command',
    '_is_full_album_file',
    '_is_temp_duplicate',
    '_norm_cmp',
    '_open_sqlite_ro',
    '_parse_metadb_info',
    '_resolve_fb_uri',
    'detect_foobar_env',
    'find_foobar_cache_diffs',
    'get_running_foobar_exes',
    'is_under_folders',
    'load_metadb_index',
    'parse_library_folders',
    'reconcile_album_consistency',
    'scan_library',
    'sync_files_to_foobar',
    'touch_files_mtime',
    'track_has_pending_work',
]
