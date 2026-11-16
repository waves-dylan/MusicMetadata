"""模态弹窗组件包。"""

from __future__ import annotations

from ui.dialogs.base import _ModalDialog, _msgbox, _ORIG_MSGBOX
from ui.dialogs.delete_dlg import DeleteMetadataDialog
from ui.dialogs.set_dlg import SetMetadataDialog
from ui.dialogs.convert_dlg import ConvertFormatDialog
from ui.dialogs.source_dlg import SourceConfigDialog
from ui.dialogs.folder_dlg import FolderManagerDialog
from ui.dialogs.sacd_dlg import SacdExtractDialog
from ui.dialogs.cover_dlg import CoverPickerDialog, _extract_group_clues
from ui.dialogs.artist_dlg import ArtistPickerDialog
from ui.dialogs.lyric_dlg import LyricPickerDialog
from ui.dialogs.zh_convert_dlg import ZhConvertDialog

