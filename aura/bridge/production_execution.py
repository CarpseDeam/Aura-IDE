"""Role-neutral owner of one direct production execution turn.

Normal Aura coding runs one continuous production model.  This module owns the
*projection* of that run into Aura's existing polished workspace: reasoning,
live TODO, tool cards, writes and diffs, terminal output, external process
output, validation evidence, cancellation, and the return to idle.

It owns execution activity only.  The assistant's conversational prose belongs
to the chat transcript and is routed there by ``ConversationBridge``; this
session never re-emits it as Execution activity.

Its focused collaborators are:

* ``ExecutionEventRelay`` is the single authoritative execution ledger.
* ``TaskChecklistProjector`` projects the live task checklist snapshot.
* ``create_execution_relay`` performs the signal wiring.

This session projects an existing model run; it does not invoke the model or
parse prose to infer state. The turn's terminal outcome is reported as a bare
``(ok, status)`` pair — a lifecycle fact, never a summary or report string.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.bridge.execution_relay_factory import create_execution_relay
from aura.client import Event
from aura.conversation.execution_outcome import ExecutionOutcomeStatus
from aura.conversation.validation_truth import summarize_validation
from aura.events import EventBus
from aura.task_checklist import TaskChecklistProjector

_log = logging.getLogger(__name__)


def new_production_run_id() -> str:
    """Return a stable execution identity for one direct production turn."""
    return f"prod-{uuid.uuid4().hex[:12]}"


def _resolve_outcome_status(
    *,
    cancelled: bool,
    api_errors: list[str],
    blocked_reason: str,
    validation_results: list[dict[str, Any]],
) -> str:
    """Project the turn's terminal status from its structured execution evidence.

    Precedence is fixed: cancellation, harness error, and an explicit blocker
    all outrank success; failing validation outranks a plain completion.
    Nothing here judges whether the turn was *allowed* to end the way it did.
    """
    if cancelled:
        return ExecutionOutcomeStatus.cancelled.value
    if api_errors:
        return ExecutionOutcomeStatus.harness_error.value
    if blocked_reason:
        return ExecutionOutcomeStatus.harness_error.value
    outcomes = summarize_validation(validation_results)
    if any(not outcome.passed for outcome in outcomes):
        return ExecutionOutcomeStatus.validation_failed.value
    return ExecutionOutcomeStatus.completed.value


class ProductionExecutionSession(QObject):
    """Owns the execution identity, ledger, and projection for one production run."""

    # Lifecycle
    executionStarted = Signal(str)                   # run_id
    executionFinished = Signal(str, bool, str)       # run_id, ok, status
    executionCancelled = Signal(str)                 # run_id

    # Stream and tool projection for the active execution.
    executionReasoningDelta = Signal(str, str)
    executionContentDelta = Signal(str, str)
    executionToolCallStart = Signal(str, str, str)
    executionToolCallArgs = Signal(str, str, str)
    executionToolCallEnd = Signal(str, str)
    executionToolResult = Signal(str, str, str, bool, str, dict)
    executionDiffDecided = Signal(str, str, str, str, str, str, bool)
    executionFileEditLifecycle = Signal(str, str, str, str, list, str)
    executionWorkspaceReconcileRequested = Signal(str, str)
    executionTerminalCommandStarted = Signal(str, str, str, str)
    executionStreamDone = Signal(str, str, dict)
    executionApiError = Signal(str, int, str)
    executionUsage = Signal(str, str, int, int, int, int)
    executionDelegationUsage = Signal(str, str, str, int, int, int, int)
    executionTerminalOutput = Signal(str, str, str)
    executionAgentProcessStarted = Signal(str, str, str, str)
    executionAgentProcessOutput = Signal(str, str, str)
    executionAgentProcessFinished = Signal(str, str, object)

    # Projected snapshots
    taskChecklistUpdated = Signal(str, list)

    def __init__(self, approval_proxy: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._event_bus = EventBus()
        self._checklist_projector = TaskChecklistProjector(self._event_bus)
        self._checklist_projector.set_on_change(self._on_checklist_changed)

        # One authoritative execution ledger for the active production run.
        #
        # Assistant prose is deliberately NOT projected here: conversation
        # content is chat-owned and reaches ChatView through the bridge's
        # canonical ``contentDelta`` path.  Projecting it again would put the
        # answer in the ephemeral Execution Log, where it is cleared on reload.
        self._relay = create_execution_relay(
            approval_proxy=approval_proxy,
            execution_model="",
            projection_target=self,
            event_bus=self._event_bus,
            suppress_content_projection=True,
        )

        self._run_id: str = ""
        self._model: str = ""
        self._cancelled: bool = False
        self._finished: bool = False

    # ---- identity --------------------------------------------------------

    @property
    def run_id(self) -> str:
        """Execution identity of the active (or last) production run."""
        return self._run_id

    @property
    def event_bus(self) -> EventBus:
        return self._event_bus

    @property
    def relay(self):
        """The authoritative execution ledger for the active run."""
        return self._relay

    def is_active(self) -> bool:
        return bool(self._run_id) and not self._finished

    # ---- lifecycle -------------------------------------------------------

    def begin(self, model: str = "", run_id: str | None = None) -> str:
        """Start a new production turn and emit the workspace start lifecycle."""
        self._run_id = run_id or new_production_run_id()
        self._model = str(model or "")
        self._cancelled = False
        self._finished = False
        self._relay.reset()
        self._relay.set_model(self._model)
        self._checklist_projector.clear()
        _log.info(
            "production_run_started run_id=%s model=%s", self._run_id, self._model
        )
        self.executionStarted.emit(self._run_id)
        return self._run_id

    def handle_event(self, ev: Event) -> None:
        """Feed one model/tool event into the ledger and workspace projection."""
        if not self._run_id:
            return
        self._relay.relay(self._run_id, ev)

    def note_cancelled(self) -> None:
        """Record that the user stopped this run."""
        self._cancelled = True

    def cancel(self) -> None:
        """Emit the cancelled lifecycle and return the workspace to idle."""
        if not self._run_id or self._finished:
            return
        self._cancelled = True
        self._finished = True
        _log.info("production_run_cancelled run_id=%s", self._run_id)
        self.executionCancelled.emit(self._run_id)

    def finish(self, blocked_reason: str = "") -> None:
        """Resolve the turn's terminal status and emit the finish lifecycle.

        ``blocked_reason`` is a passive summary of an optional report tool the
        model chose to call. It describes what was observed; it never decides
        whether the model was allowed to finish.
        """
        if not self._run_id or self._finished:
            return
        self._finished = True
        if self._cancelled:
            _log.info("production_run_cancelled run_id=%s", self._run_id)
            self.executionCancelled.emit(self._run_id)
            return

        status = _resolve_outcome_status(
            cancelled=self._cancelled,
            api_errors=self._relay.api_errors,
            blocked_reason=blocked_reason,
            validation_results=self._relay.validation_results,
        )
        ok = status == ExecutionOutcomeStatus.completed.value
        _log.info(
            "production_run_finished run_id=%s ok=%s status=%s",
            self._run_id, ok, status,
        )
        self.executionFinished.emit(self._run_id, ok, status)

    def clear(self) -> None:
        """Clear projected state (conversation reset / teardown)."""
        self._checklist_projector.clear()
        self._relay.reset()
        self._run_id = ""
        self._finished = False
        self._cancelled = False

    # ---- internals -------------------------------------------------------

    def _on_checklist_changed(self, run_id: str, items: list[dict[str, str]]) -> None:
        self.taskChecklistUpdated.emit(run_id, items)


__all__ = [
    "ProductionExecutionSession",
    "new_production_run_id",
]
