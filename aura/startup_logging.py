"""Early startup diagnostics for packaged Aura builds.

Flight Recorder logging: session-rotated logs with faulthandler, pruning,
and lifecycle breadcrumbs for debuggability.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, TextIO

LOG_FILENAME = "aura-startup.log"
LATEST_LOG = "aura-latest.log"
PREVIOUS_LOG = "aura-previous.log"

logger = logging.getLogger(__name__)

_configured_log_path: Path | None = None
_session_log_path: Path | None = None
_original_excepthook = sys.excepthook
_original_threading_excepthook = getattr(threading, "excepthook", None)

_session_id: str = ""

_faulthandler_file: TextIO | None = None


def logs_dir() -> Path:
    """Return the logs directory path."""
    try:
        from aura.paths import data_dir

        base_dir = data_dir()
    except Exception:
        base_dir = _fallback_data_dir()
    return base_dir / "logs"


def _generate_session_id() -> str:
    now = datetime.now()
    return now.strftime("%Y%m%d-%H%M%S") + f"-p{os.getpid()}"


def configure_startup_logging() -> Path:
    """Configure flight-recorder logging with session rotation, faulthandler,
    and lifecycle breadcrumbs.

    Returns the latest log path so callers can surface it in user-facing errors.
    """
    global _configured_log_path, _session_log_path, _session_id

    if _configured_log_path is not None:
        return _configured_log_path

    log_dir = logs_dir()
    _session_id = _generate_session_id()

    latest_path = log_dir / LATEST_LOG
    previous_path = log_dir / PREVIOUS_LOG
    session_log_path = log_dir / f"aura-{_session_id}.log"

    try:
        log_dir.mkdir(parents=True, exist_ok=True)

        # Rotate the prior latest log to aura-previous.log.
        if latest_path.exists():
            latest_path.replace(previous_path)

        # Configure with two file handlers
        _configure_file_logging(latest_path, session_log_path)
        _configured_log_path = latest_path
        _session_log_path = session_log_path

        # Prune old session logs
        _prune_old_logs(log_dir)

        # Enable faulthandler to capture crashes in the session log
        _enable_faulthandler(session_log_path)

    except Exception:
        fallback_dir = Path(tempfile.gettempdir()) / "Aura" / "logs"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        fallback_path = fallback_dir / LATEST_LOG
        fallback_session = fallback_dir / f"aura-{_session_id}.log"
        _configure_file_logging(fallback_path, fallback_session)
        _configured_log_path = fallback_path
        _session_log_path = fallback_session
        logging.getLogger(__name__).exception(
            "failed to configure preferred startup log dir: %s", log_dir
        )

    _install_exception_hooks()
    _install_qt_message_handler()

    # Globally suppress Python SyntaxWarnings from dynamic workspace scans
    import warnings

    warnings.filterwarnings("ignore", category=SyntaxWarning)

    # Startup banner
    _log_startup_banner()

    logger.info("startup logging configured: %s", _configured_log_path)
    return _configured_log_path


def _configure_file_logging(latest_path: Path, session_path: Path) -> None:
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    latest_handler = logging.FileHandler(latest_path, encoding="utf-8", mode="w")
    latest_handler.setFormatter(formatter)

    session_handler = logging.FileHandler(session_path, encoding="utf-8", mode="w")
    session_handler.setFormatter(formatter)

    logging.basicConfig(
        level=logging.INFO,
        handlers=[latest_handler],
        force=True,
    )
    logging.getLogger().addHandler(session_handler)


def _prune_old_logs(log_dir: Path) -> None:
    try:
        all_logs = sorted(
            [
                p
                for p in log_dir.iterdir()
                if p.name.startswith("aura-20") and p.suffix == ".log"
            ],
            reverse=True,
        )
        to_prune = [
            p
            for p in all_logs
            if p.name not in (LATEST_LOG, PREVIOUS_LOG)
        ]
        if len(to_prune) > 20:
            for p in to_prune[20:]:
                p.unlink(missing_ok=True)
            logger.info("pruned %d old session logs", len(to_prune) - 20)
    except Exception:
        logger.exception("failed to prune old session logs")


def _enable_faulthandler(log_path: Path) -> None:
    global _faulthandler_file
    try:
        import faulthandler

        if faulthandler.is_enabled():
            faulthandler.disable()
            if _faulthandler_file is not None:
                _faulthandler_file.close()
                _faulthandler_file = None

        _faulthandler_file = log_path.open("a", encoding="utf-8")
        faulthandler.enable(_faulthandler_file, all_threads=True)
    except Exception:
        logger.exception("failed to enable faulthandler")


def _log_startup_banner() -> None:
    try:
        from aura.updater import is_packaged
        from aura.version import __version__
    except ImportError:
        __version__ = "unknown"

        def is_packaged() -> bool:
            return False

    logger.info("aura_session_id: %s", _session_id)
    logger.info("aura_version: %s", __version__)
    logger.info("packaged: %s", is_packaged())
    logger.info("exe_path: %s", sys.executable)
    logger.info("cwd: %s", os.getcwd())
    logger.info("config_path: %s", _get_config_dir())
    logger.info("data_path: %s", _get_data_dir())
    logger.info("latest_log_path: %s", _configured_log_path)
    logger.info("session_log_path: %s", _session_log_path)
    logger.info("platform: %s", sys.platform)
    logger.info("python: %s", sys.version.replace(chr(10), " "))


def _get_config_dir() -> str:
    try:
        from aura.paths import config_dir

        return str(config_dir())
    except Exception:
        logger.exception("could not resolve config dir")
        return "<unavailable>"


def _get_data_dir() -> str:
    try:
        from aura.paths import data_dir

        return str(data_dir())
    except Exception:
        logger.exception("could not resolve data dir")
        return "<unavailable>"


def startup_log_path() -> Path:
    """Return the configured startup log path (latest), or the default path."""
    return _configured_log_path or logs_dir() / LATEST_LOG


def session_log_path() -> Path | None:
    """Return the session-specific log path, or None if not yet configured."""
    return _session_log_path


def session_id() -> str:
    """Return the current session ID string, or empty if not configured."""
    return _session_id


def _startup_log_path() -> Path:
    return logs_dir() / LOG_FILENAME


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
