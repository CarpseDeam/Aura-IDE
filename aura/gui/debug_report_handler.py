from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread
from PySide6.QtWidgets import QMessageBox

from aura.config import get_provider
from aura.gui.debug_report_worker import DebugReportWorker

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class DebugReportHandler(QObject):
    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window
        self._debug_report_worker: DebugReportWorker | None = None
        self._debug_report_thread: QThread | None = None

    def on_send_debug_report(self) -> None:
        logger.info("debug_report_clicked")
        self._window._toolbar.set_send_logs_busy(True)
        self._window.statusBar().showMessage("Sending logs...")

        logger.info("debug_report_collect_start")
        log_text, metadata = self._collect_debug_report_data()
        logger.info(
            "debug_report_collect_done file_count=%d total_chars=%d",
            metadata.get("_file_count", 0),
            len(log_text),
        )

        from aura.startup_logging import session_id as get_session_id
        from aura.version import __version__

        payload = {
            "app_version": __version__,
            "session_id": get_session_id(),
            "platform": sys.platform,
            "packaged": getattr(sys, "frozen", False) or "__compiled__" in globals(),
            "workspace_root_redacted": self._redact_path(self._window._workspace_root) if self._window._workspace_root else "",
            "last_breadcrumb": "debug_report_clicked",
            "payload_json": metadata,
            "log_text": log_text,
        }

        base_url = get_provider("aura").base_url
        self._debug_report_thread = QThread(self)
        self._debug_report_worker = DebugReportWorker(base_url=base_url, payload=payload)
        self._debug_report_worker.moveToThread(self._debug_report_thread)
        self._debug_report_thread.started.connect(self._debug_report_worker.run)
        self._debug_report_worker.finished.connect(self._on_debug_report_done)
        self._debug_report_worker.finished.connect(self._debug_report_thread.quit)
        self._debug_report_thread.finished.connect(self._debug_report_thread.deleteLater)
        self._debug_report_thread.finished.connect(self._on_debug_report_thread_finished)
        self._debug_report_thread.start()
        logger.info("debug_report_thread_start")

    def _collect_debug_report_data(self) -> tuple[str, dict]:
        from aura.startup_logging import logs_dir as get_logs_dir

        ld = get_logs_dir()
        log_text_parts: list[str] = []
        total_chars = 0
        MAX_LOG_CHARS = 100000
        file_count = 0

        priority_names = ["aura-latest.log", "aura-previous.log"]
        files_to_read: list[Path] = []
        for name in priority_names:
            p = ld / name
            if p.exists():
                files_to_read.append(p)

        try:
            session_logs = sorted(
                [
                    p
                    for p in ld.iterdir()
                    if p.name.startswith("aura-") and p.name.endswith(".log") and p.name not in ("aura-latest.log", "aura-previous.log")
                ],
                key=lambda p: p.stat().st_mtime if p.exists() else 0,
                reverse=True,
            )
            files_to_read.extend(session_logs[:8])
        except Exception:
            logger.warning("debug_report: failed to list session logs", exc_info=True)

        for fp in files_to_read:
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            remaining = MAX_LOG_CHARS - total_chars
            if remaining <= 0:
                break
            header = f"--- {fp.name} ---\n"
            if len(header) + len(text) <= remaining:
                log_text_parts.append(header + text)
                total_chars += len(header) + len(text)
                file_count += 1
            else:
                truncated = text[:max(0, remaining - len(header) - 50)]
                log_text_parts.append(header + truncated + "\n... [truncated]")
                total_chars += len(header) + len(truncated) + 20
                file_count += 1
                break

        metadata: dict[str, object] = {
            "client_timestamp": datetime.utcnow().isoformat() + "Z",
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "_file_count": file_count,
        }

        try:
            from aura.config import get_provider as _get_provider
            from urllib.parse import urlparse

            provider = _get_provider("deepseek")
            metadata["deepseek_base_url_host"] = urlparse(provider.base_url).hostname or ""
        except Exception:
            logger.debug("debug_report: could not read deepseek base_url", exc_info=True)

        try:
            from aura.config import load_settings as _load_settings

            settings = _load_settings()
            metadata["provider"] = settings.provider
            metadata["planner_provider"] = settings.planner_provider
            metadata["worker_provider"] = settings.worker_provider
            metadata["model"] = settings.model
            metadata["thinking"] = settings.thinking
        except Exception:
            logger.debug("debug_report: could not load settings", exc_info=True)

        if self._window._workspace_root:
            metadata["workspace"] = self._window._workspace_root.name
        else:
            metadata["workspace"] = ""

        log_text = "\n".join(log_text_parts)
        return log_text, metadata

    @staticmethod
    def _redact_path(path: Path | None) -> str:
        if path is None:
            return ""
        try:
            return path.name
        except Exception:
            logger.debug("debug_report: redact_path failed", exc_info=True)
            return ""

    def _on_debug_report_done(self, report_id: str, error: str) -> None:
        self._window._toolbar.set_send_logs_busy(False)
        self._window.statusBar().clearMessage()
        if report_id:
            short_id = report_id[:5].upper() if len(report_id) > 5 else report_id
            logger.info("debug_report_upload_success report_id=%s", report_id)
            QMessageBox.information(
                self._window, "Debug Report",
                f"Debug report sent: AURA-{short_id}",
            )
        else:
            logger.info("debug_report_upload_failed error=%s", error)
            msg = f"Could not send debug report.\n{error}" if error else "Could not send debug report."
            QMessageBox.warning(self._window, "Debug Report", msg)

    def _on_debug_report_thread_finished(self) -> None:
        logger.info("debug_report_thread_finished")
        if self._debug_report_worker:
            self._debug_report_worker.deleteLater()
            self._debug_report_worker = None
        self._debug_report_thread = None
