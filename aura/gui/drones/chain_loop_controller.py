"""Owns chain execution and the metabolism loop in a background thread."""

from __future__ import annotations

import datetime as dt
import threading
from dataclasses import asdict
from typing import Callable

from PySide6.QtCore import QObject, QThread, Signal

from aura.drones.chain_runner import run_chain


class ChainLoopController(QObject):
    """Owns chain execution and the metabolism loop.

    chain_provider is a callable returning (workspace_root, chain, drone_lookup)
    for the current Mission Control workflow. The controller never resolves
    the chain itself.
    """

    lap_finished = Signal(dict)
    lap_error = Signal(str)
    loop_finished = Signal()

    def __init__(
        self,
        chain_provider: Callable[[], tuple],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._chain_provider = chain_provider
        self._stop = threading.Event()
        self._thread: QThread | None = None
        self._cooldown_seconds: float = 5.0
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        """Start the execution loop. Ignore if already running."""
        if self._running:
            return
        self._stop.clear()
        self._running = True
        self._thread = QThread(self)
        worker = _LoopWorker(
            self._chain_provider, self._stop, self._cooldown_seconds
        )
        worker.moveToThread(self._thread)

        worker.lap_finished.connect(self.lap_finished)
        worker.lap_error.connect(self.lap_error)
        worker.finished.connect(self._on_worker_finished)
        worker.finished.connect(worker.deleteLater)

        self._thread.started.connect(worker.run)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop after the current lap finishes."""
        self._stop.set()

    def set_cooldown(self, seconds: float) -> None:
        self._cooldown_seconds = seconds

    def _on_worker_finished(self) -> None:
        """Clean up thread when the worker loop exits."""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
        self._running = False
        self.loop_finished.emit()


class _LoopWorker(QObject):
    """Runs laps in a loop on a background thread. Emits signals per lap."""

    lap_finished = Signal(dict)
    lap_error = Signal(str)
    finished = Signal()

    def __init__(
        self,
        chain_provider: Callable,
        stop_event: threading.Event,
        cooldown_seconds: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._chain_provider = chain_provider
        self._stop = stop_event
        self._cooldown = cooldown_seconds

    def run(self) -> None:
        """Main loop — called from the worker thread via thread.started."""
        try:
            while not self._stop.is_set():
                ws, chain, drone_lookup = self._chain_provider()
                if ws is None or chain is None:
                    self.lap_error.emit("No active workflow chain.")
                    break

                try:
                    result = run_chain(
                        ws, chain,
                        drone_lookup=drone_lookup,
                        approval_callback=lambda nodes: True,
                    )
                    d = asdict(result)
                    d["chain_name"] = chain.name

                    # Compute elapsed time
                    started = result.started_at
                    ended = result.ended_at
                    if started and ended:
                        try:
                            s = dt.datetime.fromisoformat(started)
                            e = dt.datetime.fromisoformat(ended)
                            delta = (e - s).total_seconds()
                            d["elapsed"] = f"{delta:.1f}s"
                        except Exception:
                            d["elapsed"] = ""
                    else:
                        d["elapsed"] = ""

                    # Find failed node
                    failed_at = ""
                    for node_id, nr in d.get("node_runs", {}).items():
                        if nr.get("status") == "failed":
                            failed_at = node_id
                            break
                    d["failed_at"] = failed_at

                    self.lap_finished.emit(d)
                except Exception as exc:
                    self.lap_error.emit(str(exc))

                # Single-run mode — stop after one lap
                if not chain.loop:
                    break

                # Cooldown between laps (cancellable via stop event)
                self._stop.wait(self._cooldown)
        finally:
            self.finished.emit()
