"""本地媒体库扫描与全专一致性校对包。"""

from __future__ import annotations

from core.models import LibraryTrack
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
