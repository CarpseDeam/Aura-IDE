"""Windows shell identity helpers."""

from __future__ import annotations

import ctypes
import logging
import sys

APP_USER_MODEL_ID = "CarpseDeam.AuraIDE"

logger = logging.getLogger(__name__)


def set_current_process_app_user_model_id() -> None:
    """Set Aura's Windows AppUserModelID for taskbar grouping and icons."""
    if sys.platform != "win32":
        return

    try:
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id(APP_USER_MODEL_ID)
        if result:
            logger.debug(
                "failed to set Windows AppUserModelID: HRESULT 0x%08X",
                result & 0xFFFFFFFF,
            )
    except Exception:
        logger.debug("failed to set Windows AppUserModelID", exc_info=True)
