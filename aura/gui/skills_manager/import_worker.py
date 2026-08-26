"""Off-GUI-thread execution for one import job at a time.

Copying a folder, extracting an archive, downloading a repository, and
installing a validated preview are all blocking filesystem or network work.
They run here, on a worker moved into its own ``QThread``, following the
same convention as the rest of Aura's background work: an unparented thread
that outlives its owner if it must, and a lingering list so a ``QThread``
object is never destroyed while its worker is still inside a blocking call.

The worker interprets nothing. It runs the callable it was handed and emits
the result or the exception exactly as it came back; deciding what either
means — and redacting anything user-facing — belongs to the import
controller.
"""
from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)

#: Threads that outlived their shutdown wait. Held so a QThread object is
#: never destroyed while its worker is still running.
_LINGERING_THREADS: list[QThread] = []


class ImportJobWorker(QObject):
    """Runs one blocking import job and reports how it went."""

    #: (result, error) — exactly one is meaningful; error is an Exception.
    finished = Signal(object, object)

    def __init__(self, job: Callable[[], object]) -> None:
        super().__init__()
        self._job = job

    def run(self) -> None:
        try:
            result = self._job()
        except Exception as exc:  # reported, never raised into the event loop
            logger.debug("skills import: job failed", exc_info=True)
            self.finished.emit(None, exc)
            return
        self.finished.emit(result, None)


class ImportJobRunner(QObject):
    """Owns the one running import thread and its shutdown."""

    #: (token, result, error) — the token identifies the session that started
    #: this job, so a late result can be recognised and discarded.
    finished = Signal(int, object, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: ImportJobWorker | None = None
        #: Every thread this runner started that has not reported ``finished``
        #: yet. A QThread must never be destroyed while it is running, and in
        #: PySide the last Python reference is what keeps it alive.
        self._running: list[QThread] = []

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def start(self, token: int, job: Callable[[], object]) -> bool:
        """Run *job* off the GUI thread. False when one is already running."""
        if self._thread is not None:
            return False
        worker = ImportJobWorker(job)
        thread = QThread()  # unparented: outlives this runner if it must
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(
            lambda result, error: self._on_worker_finished(thread, token, result, error)
        )
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: self._release(thread))
        self._running.append(thread)
        self._worker = worker
        self._thread = thread
        thread.start()
        return True

    def _on_worker_finished(self, thread: QThread, token: int, result, error) -> None:
        """Free the slot before reporting, so the next job can start at once.

        The job is over and the thread has been asked to quit, but it is not
        finished yet — it stays in ``_running`` until it says so. Releasing
        only the slot is what lets an install follow its own preview without
        waiting for the preview's thread to wind down.
        """
        if self._thread is thread:
            self._thread = None
            self._worker = None
        self.finished.emit(token, result, error)

    def _release(self, thread: QThread) -> None:
        if thread in self._running:
            self._running.remove(thread)

    def shutdown(self, timeout_ms: int = 3000) -> None:
        """Stop every import thread before their owner goes away."""
        self._thread = None
        self._worker = None
        for thread in list(self._running):
            thread.quit()
            if not thread.wait(timeout_ms):
                # A download or a large copy cannot be interrupted safely;
                # keep the thread object alive so its destructor never runs
                # while the worker is still inside it.
                logger.warning("Skill import still running at shutdown; detaching.")
                _LINGERING_THREADS.append(thread)
            self._release(thread)


__all__ = ["ImportJobRunner", "ImportJobWorker"]
