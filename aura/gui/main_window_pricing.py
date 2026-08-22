"""Startup pricing hydration — one off-thread refresh per launch.

``aura.config`` restores the last-known-good pricing cache at import time,
but a fresh profile (or a cache rejected by a schema change) starts with no
rates at all, and the only other refresh runs inside Settings → Models
discovery. Without this controller a normal launch would keep recording
usage events with unknown cost until the user opened Settings.

This owns exactly that one refresh: after the window exists, ask the shared
pricing boundary to refresh the selected provider once, in a worker thread,
and never again for the life of the process. It is provider-neutral — any
provider that registers a pricing source hydrates the same way, and a
provider without one is a no-op. Failures only log: the pricing store keeps
whatever valid in-memory or cached result it already had.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from aura.providers.pricing import has_pricing_source, refresh_provider_pricing

logger = logging.getLogger(__name__)

#: Threads that outlived their shutdown wait. Held so a QThread object is
#: never destroyed while its worker is still inside a blocking request.
_LINGERING_THREADS: list[QThread] = []


class PricingRefreshWorker(QObject):
    """Runs one provider pricing refresh; emits whether rates are in force."""

    finished = Signal(str, bool)  # provider_id, priced

    def __init__(self, provider_id: str) -> None:
        super().__init__()
        self._provider_id = provider_id

    def run(self) -> None:
        priced = False
        try:
            priced = refresh_provider_pricing(self._provider_id) is not None
        except Exception:
            logger.warning(
                "Startup pricing refresh raised for %s", self._provider_id, exc_info=True
            )
        self.finished.emit(self._provider_id, priced)


class MainWindowPricingController(QObject):
    """Owns the one-shot startup pricing refresh lifecycle for MainWindow."""

    refreshFinished = Signal(str, bool)  # provider_id, priced

    def __init__(self, provider_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._provider_id = provider_id
        self._worker: PricingRefreshWorker | None = None
        self._thread: QThread | None = None
        self._scheduled = False

    def schedule_startup_refresh(self, delay_ms: int = 0) -> bool:
        """Schedule this launch's single pricing refresh.

        Returns True when a refresh was scheduled — False when this provider
        has no pricing source, or one was already scheduled. The timer is
        parented here so a window torn down before it fires cancels it.
        """
        if self._scheduled:
            return False
        if not self._provider_id or not has_pricing_source(self._provider_id):
            logger.debug("No pricing source for %s — skipping startup refresh.", self._provider_id)
            return False
        self._scheduled = True
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(self._start_refresh)
        timer.start(delay_ms)
        return True

    def _start_refresh(self) -> None:
        if self._thread is not None:
            return
        worker = PricingRefreshWorker(self._provider_id)
        thread = QThread()  # unparented: outlives this controller if it must
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_refresh_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_worker)
        self._worker = worker
        self._thread = thread
        thread.start()

    def _on_refresh_finished(self, provider_id: str, priced: bool) -> None:
        if priced:
            logger.info("Startup pricing refresh hydrated %s.", provider_id)
        else:
            logger.warning(
                "Startup pricing refresh produced no rates for %s; "
                "any previous valid result is unchanged.",
                provider_id,
            )
        self.refreshFinished.emit(provider_id, priced)

    def _clear_worker(self) -> None:
        self._thread = None
        self._worker = None

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Stop the refresh thread before the window goes away."""
        thread = self._thread
        self._thread = None
        self._worker = None
        if thread is None:
            return
        thread.quit()
        if not thread.wait(timeout_ms):
            # A blocking request can't be interrupted safely; keep the thread
            # object alive so its destructor never runs while it is running.
            logger.warning("Startup pricing refresh still running at shutdown; detaching.")
            _LINGERING_THREADS.append(thread)
