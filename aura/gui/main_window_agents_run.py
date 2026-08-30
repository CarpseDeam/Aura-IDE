"""Running a workflow from the Agents window without freezing it.

Everything about *what* a run does lives in
:class:`aura.agents.workflow_runner.WorkflowRunner`, which is not Qt-aware and
is the same runner Aura uses when it invokes a workflow itself. This module is
only the adapter between that and a window: a worker on its own thread, the
node reports relayed back to the GUI thread as signals, and a cancel event the
Stop button can set without waiting for anything.

The plan is frozen before the thread starts and never re-read, so the canvas
stays fully editable during a run: whatever the user draws now belongs to the
next run, not this one.
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QThread, Signal, Slot

from aura.agents.workflow_plan import WorkflowRunPlan
from aura.agents.workflow_runner import WorkflowRunner, WorkflowStepState

logger = logging.getLogger(__name__)


class _WorkflowRunWorker(QObject):
    """Runs one frozen plan on a worker thread and reports as it goes."""

    stepChanged = Signal(str, str)  # solid Step or invoked helper node, state
    completed = Signal(object)  # WorkflowRunResult

    def __init__(
        self,
        runner: WorkflowRunner,
        plan: WorkflowRunPlan,
        task: str,
        cancel: threading.Event,
    ) -> None:
        super().__init__()
        self._runner = runner
        self._plan = plan
        self._task = task
        self._cancel = cancel

    @Slot()
    def run(self) -> None:
        try:
            result = self._runner.run(
                self._plan,
                self._task,
                cancel_event=self._cancel,
                on_step=self._on_step,
            )
        except Exception:  # pragma: no cover - the runner reports its own failures
            logger.exception("agents: manual workflow run raised")
            result = None
        self.completed.emit(result)

    def _on_step(self, node_id: str, state: WorkflowStepState) -> None:
        # Called on the worker thread; a queued signal is what gets it onto
        # the GUI thread, so nothing here touches a widget.
        self.stepChanged.emit(str(node_id), state.value)


class WorkflowRunController(QObject):
    """Owns the thread, the cancel event, and the states the canvas shows."""

    runningChanged = Signal(bool)
    statesChanged = Signal(dict)
    finished = Signal(object)  # WorkflowRunResult | None

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: _WorkflowRunWorker | None = None
        self._cancel = threading.Event()
        self._states: dict[str, str] = {}

    @property
    def running(self) -> bool:
        thread = self._thread
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._thread = None
            self._worker = None
            return False

    @property
    def states(self) -> dict[str, str]:
        """What each run node is doing, as the canvas currently draws it."""
        return dict(self._states)

    def start(self, runner: WorkflowRunner, plan: WorkflowRunPlan, task: str) -> bool:
        """Begin one run. Returns False when one is already in flight."""
        if self.running or runner is None or plan is None:
            return False
        self._cancel = threading.Event()
        # Every step starts unmarked rather than pre-coloured: a step that has
        # not begun has nothing true to say about itself yet.
        self._states = {}
        self.statesChanged.emit(self.states)

        self._thread = QThread()
        self._worker = _WorkflowRunWorker(runner, plan, task, self._cancel)
        self._worker.moveToThread(self._thread)
        self._worker.stepChanged.connect(self._on_step_changed)
        self._worker.completed.connect(self._on_completed)
        self._thread.started.connect(self._worker.run)
        self.runningChanged.emit(True)
        self._thread.start()
        return True

    def stop(self) -> None:
        """Ask the run to stop, and return immediately.

        Setting the event is the whole gesture. The runner checks it between
        steps and hands it to the child loop, so the current step ends on its
        own terms and the worktree is still checkpointed — nothing here waits,
        kills, or reaches into the run.
        """
        self._cancel.set()

    def clear_states(self) -> None:
        """Drop the marks from the last run — used when the workflow changes."""
        if not self._states:
            return
        self._states = {}
        self.statesChanged.emit(self.states)

    @Slot(str, str)
    def _on_step_changed(self, node_id: str, state: str) -> None:
        self._states[node_id] = state
        self.statesChanged.emit(self.states)

    @Slot(object)
    def _on_completed(self, result: object) -> None:
        thread, self._thread = self._thread, None
        self._worker = None
        if thread is not None:
            thread.quit()
            thread.wait(2000)
            thread.deleteLater()
        self.runningChanged.emit(False)
        self.finished.emit(result)


__all__ = ["WorkflowRunController"]
