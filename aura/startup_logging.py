"""Early startup diagnostics for packaged Aura builds."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from types import TracebackType
from typing import Any

LOG_FILENAME = "aura-startup.log"

logger = logging.getLogger(__name__)

_configured_log_path: Path | None = None
_original_excepthook = sys.excepthook
_original_threading_excepthook = getattr(threading, "excepthook", None)


def configure_startup_logging() -> Path:
    """Configure early file logging and global exception hooks.

    Returns the log path so callers can surface it in user-facing errors.
    """
    global _configured_log_path

    if _configured_log_path is not None:
        return _configured_log_path

    preferred_log_path = _startup_log_path()
    try:
        _configure_file_logging(preferred_log_path)
        log_path = preferred_log_path
    except Exception:
        log_path = Path(tempfile.gettempdir()) / "Aura" / "logs" / LOG_FILENAME
        _configure_file_logging(log_path)
        logging.getLogger(__name__).exception(
            "failed to configure preferred startup log: %s",
            preferred_log_path,
        )

    _configured_log_path = log_path
    _install_exception_hooks()
    _install_qt_message_handler()

    # Globally suppress Python SyntaxWarnings from dynamic workspace scans
    import warnings
    warnings.filterwarnings("ignore", category=SyntaxWarning)

    logger.info("startup logging configured: %s", log_path)
    return log_path


def _configure_file_logging(log_path: Path) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8", mode="w"),
        ],
        force=True,
    )


def startup_log_path() -> Path:
    """Return the configured startup log path, or the default path."""
    return _configured_log_path or _startup_log_path()


def _startup_log_path() -> Path:
    try:
        from aura.paths import data_dir

        base_dir = data_dir()
    except Exception:
        base_dir = _fallback_data_dir()
    return base_dir / "logs" / LOG_FILENAME


def _fallback_data_dir() -> Path:
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Aura"
    return Path.home() / ".aura"


def _install_exception_hooks() -> None:
    sys.excepthook = _log_uncaught_exception
    if hasattr(threading, "excepthook"):
        threading.excepthook = _log_uncaught_thread_exception


def _log_uncaught_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        _original_excepthook(exc_type, exc_value, exc_traceback)
        return

    logging.getLogger("aura.startup.uncaught").critical(
        "uncaught exception",
        exc_info=(exc_type, exc_value, exc_traceback),
    )


def _log_uncaught_thread_exception(args: threading.ExceptHookArgs) -> None:
    if args.exc_type is not None and issubclass(args.exc_type, KeyboardInterrupt):
        if _original_threading_excepthook is not None:
            _original_threading_excepthook(args)
        return

    logging.getLogger("aura.startup.threading").critical(
        "uncaught thread exception in %s",
        getattr(args.thread, "name", "<unknown>"),
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
    )


def _install_qt_message_handler() -> None:
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except Exception:
        logger.debug("Qt message handler not installed", exc_info=True)
        return

    def qt_message_handler(
        mode: QtMsgType,
        context: Any,
        message: str,
    ) -> None:
        qt_logger = logging.getLogger("aura.qt")
        level = _qt_log_level(mode)
        file_name = getattr(context, "file", None)
        line = getattr(context, "line", None)
        function = getattr(context, "function", None)
        qt_logger.log(
            level,
            "%s (file=%s line=%s function=%s)",
            message,
            file_name or "<unknown>",
            line or "<unknown>",
            function or "<unknown>",
        )

    qInstallMessageHandler(qt_message_handler)
    logger.info("Qt message handler installed")


def _qt_log_level(mode: Any) -> int:
    try:
        from PySide6.QtCore import QtMsgType

        debug_msg = getattr(QtMsgType, "QtDebugMsg", None)
        info_msg = getattr(QtMsgType, "QtInfoMsg", None)
        warning_msg = getattr(QtMsgType, "QtWarningMsg", None)
        critical_msg = getattr(QtMsgType, "QtCriticalMsg", None)
        fatal_msg = getattr(QtMsgType, "QtFatalMsg", None)

        if mode in (debug_msg, info_msg):
            return logging.INFO
        if mode == warning_msg:
            return logging.WARNING
        if mode == critical_msg:
            return logging.ERROR
        if mode == fatal_msg:
            return logging.CRITICAL
    except Exception:
        pass
    return logging.ERROR
