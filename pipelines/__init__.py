"""业务处理流水线包：包含选项模型、并发调度与各类批量操作管线。"""

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
    _run_pool_cancellable,
    _sync_foobar_targets,
    resolve_hook,
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
)

__all__ = [
    'ConvertOptions',
    'DeleteOptions',
    'FixOptions',
    'SetOptions',
    'default_checked',
    'parse_delete_fields',
    '_run_pool_cancellable',
    '_sync_foobar_targets',
    'resolve_hook',
    '_count_deletable_targets',
    'execute_deletions',
    'execute_set_tags',
    'execute_convert',
    'FOLDER_COVER_LABELS',
    '_album_date_group_key',
    '_download_and_apply_online_artist_pics',
    '_download_and_apply_online_covers',
    '_propagate_album_group_metadata',
    'execute_repairs',
]
