"""批量修复、删除、手工设置标签与格式转换的执行管线兼容门面（重定向至 pipelines）。"""

from __future__ import annotations

from pipelines.options import (
    ConvertOptions,
    DeleteOptions,
    FixOptions,
    SetOptions,
    default_checked,
    parse_delete_fields,
)
from pipelines.executor import (
    _ORIG_HOOKS,
    _run_pool_cancellable,
    _sync_foobar_targets,
)
from pipelines.delete import _count_deletable_targets, execute_deletions
from pipelines.set_tags import execute_set_tags
from pipelines.convert import execute_convert
from pipelines.repair import (
    FOLDER_COVER_LABELS,
    _album_date_group_key,
    _download_and_apply_online_artist_pics,
    _download_and_apply_online_covers,
    _propagate_album_group_metadata,
    execute_repairs,
    resolve_hook,
)

__all__ = [
    'FOLDER_COVER_LABELS',
    'ConvertOptions',
    'DeleteOptions',
    'FixOptions',
    'SetOptions',
    '_album_date_group_key',
    '_count_deletable_targets',
    '_download_and_apply_online_artist_pics',
    '_download_and_apply_online_covers',
    '_propagate_album_group_metadata',
    '_run_pool_cancellable',
    '_sync_foobar_targets',
    'default_checked',
    'execute_convert',
    'execute_deletions',
    'execute_repairs',
    'execute_set_tags',
    'parse_delete_fields',
    'resolve_hook',
]
