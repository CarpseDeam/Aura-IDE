from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal

from aura.config import AppSettings, get_api_key, get_provider
from aura.gui.balance_fetcher import BalanceWorker

logger = logging.getLogger(__name__)


class MainWindowBalanceController(QObject):
    balance_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._balance_micros: int | None = None
        self._inflight: bool = False
        self._thread: QThread | None = None
        self._worker: BalanceWorker | None = None

    @property
    def balance_micros(self) -> int | None:
        return self._balance_micros

    def refresh(self, settings: AppSettings) -> None:
        if self._inflight:
            return
        if settings.planner_provider != "aura" and settings.worker_provider != "aura":
            self._balance_micros = None
            self.balance_changed.emit()
            return
        api_key = get_api_key("aura")
        if not api_key:
            self._balance_micros = None
            self.balance_changed.emit()
            return
        self._inflight = True
        provider = get_provider("aura")
        base_url = provider.base_url

        thread = QThread(self)
        worker = BalanceWorker(base_url=base_url, api_key=api_key)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_balance_fetched)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)

        def _cleanup():
            if self._thread is thread:
                self._thread = None
            if self._worker is worker:
                self._worker = None
            self._inflight = False

        thread.finished.connect(_cleanup)
        thread.finished.connect(thread.deleteLater)
        thread.start()

    def _on_balance_fetched(self, balance_micros: int, error: str) -> None:
        self._inflight = False
        if balance_micros >= 0:
            self._balance_micros = balance_micros
        if error:
            logger.warning("Balance fetch error: %s", error)
        self.balance_changed.emit()
