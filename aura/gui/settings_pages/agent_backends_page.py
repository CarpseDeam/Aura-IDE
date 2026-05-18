from __future__ import annotations

import logging
import subprocess
import sys

from PySide6.QtCore import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.gui.theme import FG_DIM, SUCCESS, WARN

logger = logging.getLogger(__name__)


class _AuthCheckWorker(QObject):
    finished = Signal(str, bool, str)  # backend_id, ok, detail

    def __init__(self, backend_id: str, check_cmd: list[str]):
        super().__init__()
        self.backend_id = backend_id
        self.check_cmd = check_cmd

    def run(self):
        try:
            proc = subprocess.run(
                self.check_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=15,
                **({} if sys.platform != "win32" else {"creationflags": subprocess.CREATE_NO_WINDOW}),
            )
            ok = proc.returncode == 0
            detail = (proc.stdout + proc.stderr).strip()[:200]
            self.finished.emit(self.backend_id, ok, detail)
        except FileNotFoundError:
            self.finished.emit(self.backend_id, False, "CLI tool not found on PATH")
        except Exception as exc:
            self.finished.emit(self.backend_id, False, str(exc))


class AgentBackendsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._threads: list[QThread] = []
        self._workers: list[_AuthCheckWorker] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        title = QLabel("CLI Agent Backend Authentication")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        self._backend_rows: dict[str, dict[str, object]] = {}

        backends = [
            ("gemini", "Gemini CLI", ["gemini", "--version"]),
            ("claude_code", "Claude Code", ["claude", "auth", "status"]),
            ("codex", "Codex", ["codex", "login", "status"]),
        ]

        for bid, label, check_cmd in backends:
            header = QLabel(label)
            header.setStyleSheet(f"color: {FG_DIM}; font-weight: 600;")
            form.addRow("", header)

            status_label = QLabel("Checking...")
            status_label.setWordWrap(True)
            form.addRow("Status:", status_label)

            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)

            recheck_btn = QPushButton("Recheck Status")
            recheck_btn.clicked.connect(lambda checked=False, b=bid, c=check_cmd: self._start_check(b, c))
            btn_row.addWidget(recheck_btn)

            login_btn = QPushButton("Login")
            login_btn.clicked.connect(lambda checked=False, b=bid: self._launch_login(b))
            btn_row.addWidget(login_btn)

            btn_widget = QWidget()
            btn_widget.setLayout(btn_row)
            form.addRow("", btn_widget)

            self._backend_rows[bid] = {"status": status_label}

            self._start_check(bid, check_cmd)

        layout.addLayout(form)
        layout.addStretch()

    def _start_check(self, backend_id: str, check_cmd: list[str]) -> None:
        row = self._backend_rows[backend_id]
        status_label: QLabel = row["status"]  # type: ignore[assignment]
        status_label.setText("Checking...")
        status_label.setStyleSheet(f"color: {FG_DIM};")

        thread = QThread(self)
        worker = _AuthCheckWorker(backend_id, check_cmd)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_check_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()

        self._threads.append(thread)
        self._workers.append(worker)

    def _on_check_finished(self, backend_id: str, ok: bool, detail: str) -> None:
        if backend_id not in self._backend_rows:
            return
        row = self._backend_rows[backend_id]
        status_label: QLabel = row["status"]  # type: ignore[assignment]
        if ok:
            status_label.setText("Authenticated ✓")
            status_label.setStyleSheet(f"color: {SUCCESS};")
        else:
            status_label.setText(f"Not authenticated: {detail}")
            status_label.setStyleSheet(f"color: {WARN};")

    def _launch_login(self, backend_id: str) -> None:
        from aura.sandbox import SandboxExecutor
        from pathlib import Path

        ws = Path.cwd()

        if backend_id == "gemini":
            SandboxExecutor._launch_interactive_terminal("gemini auth login", ws)
        elif backend_id == "claude_code":
            SandboxExecutor._launch_interactive_terminal("claude auth login", ws)
        elif backend_id == "codex":
            SandboxExecutor._launch_interactive_terminal("codex login", ws)

    def cleanup_threads(self) -> None:
        for thread in self._threads:
            try:
                if thread.isRunning():
                    thread.quit()
                    if not thread.wait(10000):
                        logger.warning("Agent backends check thread did not stop cleanly; waiting...")
                        thread.wait()
            except RuntimeError:
                pass
        self._threads.clear()
        self._workers.clear()

    def collect_settings(self, settings: AppSettings) -> None:
        pass  # No settings to collect — purely informational
