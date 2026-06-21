from __future__ import annotations

import logging

import httpx
from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class DebugReportWorker(QObject):
    finished = Signal(str, str)  # report_id, error_msg

    def __init__(self, base_url: str, payload: dict):
        super().__init__()
        self._base_url = base_url
        self._payload = payload

    def run(self):
        try:
            url = self._base_url.rstrip("/") + "/diagnostics/report"
            resp = httpx.post(url, json=self._payload, timeout=30.0)
            if resp.status_code == 200:
                data = resp.json()
                report_id = data.get("report_id", "")
                self.finished.emit(report_id, "")
            else:
                self.finished.emit("", f"HTTP {resp.status_code}")
        except Exception as exc:
            logger.warning("Debug report upload failed: %s", exc)
            self.finished.emit("", str(exc))
