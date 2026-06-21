from __future__ import annotations

import logging
from urllib.parse import urlparse

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
        logger.info("debug_report_worker_run")
        try:
            url = self._base_url.rstrip("/") + "/diagnostics/report"
            parsed = urlparse(url)
            logger.info("debug_report_upload_prepare host=%s path=%s payload_chars=%d", parsed.hostname, parsed.path, len(str(self._payload)))
            resp = httpx.post(url, json=self._payload, timeout=30.0)
            logger.info("debug_report_http_response status=%d", resp.status_code)
            if 200 <= resp.status_code <= 299:
                data = resp.json()
                report_id = data.get("report_id", "")
                if report_id:
                    self.finished.emit(report_id, "")
                else:
                    self.finished.emit("", f"HTTP {resp.status_code}: missing report_id — {resp.text[:300]}")
            else:
                self.finished.emit("", f"HTTP {resp.status_code} — {resp.text[:300]}")
        except Exception as exc:
            logger.warning("Debug report upload failed: %s", exc)
            self.finished.emit("", str(exc))
