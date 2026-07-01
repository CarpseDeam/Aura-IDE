from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer

from aura.gui.update_dialog import UpdateWorker
from aura.updater import UpdateStatus

logger = logging.getLogger(__name__)


class MainWindowUpdateController(QObject):
    """Owns the background update-check lifecycle for MainWindow."""

    def __init__(self, toolbar, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._toolbar = toolbar
        self._worker: UpdateWorker | None = None
        self._thread: QThread | None = None

    def schedule_background_check(self, delay_ms: int = 2000) -> None:
        """Schedule a one-shot background update check."""
        QTimer.singleShot(delay_ms, self.check_for_updates)

    def check_for_updates(self) -> None:
        """Run a background update check in a worker thread."""
        if self._thread is not None:
            return
        self._worker = UpdateWorker("check")
        self._thread = QThread(self)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._on_background_update_finished)
        self._worker.finished.connect(self._thread.quit)
        self._worker.finished.connect(self._worker.deleteLater)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._clear_worker)
        self._thread.start()

    def _on_background_update_finished(self, status: UpdateStatus) -> None:
        """Handle the update worker result."""
        if status.state == "behind":
            self._toolbar.set_update_available(True)

    def _clear_worker(self) -> None:
        """Clear thread and worker references after finish."""
        self._thread = None
        self._worker = None
