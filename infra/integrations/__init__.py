"""外部播放器与宿主环境集成包（foobar2000 等）。"""

from __future__ import annotations

from infra.integrations.foobar import (
    detect_foobar_env,
    find_foobar_cache_diffs,
    get_running_foobar_exes,
    load_metadb_index,
    parse_library_folders,
    scan_library,
    sync_files_to_foobar,
)

__all__ = [
    'detect_foobar_env',
    'find_foobar_cache_diffs',
    'get_running_foobar_exes',
    'load_metadb_index',
    'parse_library_folders',
    'scan_library',
    'sync_files_to_foobar',
]
