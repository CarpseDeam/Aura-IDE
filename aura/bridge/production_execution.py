"""Role-neutral owner of one direct production execution turn.

Normal Aura coding runs one continuous production model.  This module owns the
*projection* of that run into Aura's existing polished workspace: reasoning,
activity, live TODO, tool cards, writes and diffs, terminal output, external
process output, validation evidence, the completion receipt, cancellation, and
the return to idle.

It owns execution activity only.  The assistant's conversational prose belongs
to the chat transcript and is routed there by ``ConversationBridge``; this
session never re-emits it as Worker activity.

It deliberately reuses the existing execution machinery rather than growing a
second workflow engine:

* ``WorkerEventRelay`` is the single authoritative execution ledger.
* ``WorkerActivityController`` projects the activity heartbeat.
* ``WorkerTodoProjector`` projects the live TODO snapshot.
* ``create_worker_relay`` performs the signal wiring.

The ``worker*`` signal names are retained as compatibility aliases so the
existing ``WorkerEventHandler`` / ``AuraPlayground`` projection binds unchanged.

This session never invokes a model, generates a plan, creates a SpecCard,
constructs a Worker capsule, parses prose to infer state, or depends on
``_DispatchProxy`` lifecycle state.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from PySide6.QtCore import QObject, Signal

from aura.bridge.production_receipt import (
    ProductionReceipt,
    ProductionRunEvidence,
    build_production_receipt,
)
from aura.bridge.worker_activity import WorkerActivityController
from aura.bridge.worker_relay_factory import create_worker_relay
from aura.client import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    Event,
)
from aura.events import EventBus
from aura.worker_todo import WorkerTodoProjector

_log = logging.getLogger(__name__)


def new_production_run_id() -> str:
    """Return a stable execution identity for one direct production turn."""
    return f"prod-{uuid.uuid4().hex[:12]}"


class ProductionExecutionSession(QObject):
    """Owns the execution identity, ledger, and projection for one production run."""

    # Lifecycle
    workerStarted = Signal(str)                        # run_id
    workerFinished = Signal(str, bool, str, bool, str)  # run_id, ok, summary, needs_followup, status
    workerCancelled = Signal(str)                      # run_id

    # Stream / tool projection (names match the historical Worker family so the
    # existing workspace projection binds without change).
    workerReasoningDelta = Signal(str, str)
    workerContentDelta = Signal(str, str)
    workerToolCallStart = Signal(str, str, str)
    workerToolCallArgs = Signal(str, str, str)
    workerToolCallEnd = Signal(str, str)
    workerToolResult = Signal(str, str, str, bool, str, dict)
    workerDiffDecided = Signal(str, str, str, str, str, str, bool)
    workerStreamDone = Signal(str, str, dict)
    workerApiError = Signal(str, int, str)
    workerUsage = Signal(str, str, int, int, int, int)
    workerTerminalOutput = Signal(str, str, str)
    workerAgentProcessStarted = Signal(str, str, str, str)
    workerAgentProcessOutput = Signal(str, str, str)
    workerAgentProcessFinished = Signal(str, str, object)

    # Projected snapshots
    workerActivityUpdated = Signal(str, list)
    workerTodoUpdated = Signal(str, list)

    def __init__(self, approval_proxy: Any, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._approval_proxy = approval_proxy
        self._event_bus = EventBus()
        self._activity_controller = WorkerActivityController(self._event_bus)
        self._activity_controller.set_on_change(self._on_activity_changed)
        self._todo_projector = WorkerTodoProjector(self._event_bus)
        self._todo_projector.set_on_change(self._on_todo_changed)

        # One authoritative execution ledger for the active production run.
        #
        # Assistant prose is deliberately NOT projected here: conversation
        # content is chat-owned and reaches ChatView through the bridge's
        # canonical ``contentDelta`` path.  Projecting it again would put the
        # answer in the ephemeral Worker Log, where it is cleared on reload.
        self._relay = create_worker_relay(
            approval_proxy=approval_proxy,
            worker_model="",
            dispatch_proxy=self,
            event_bus=self._event_bus,
            suppress_workflow_state_updates=True,
            suppress_content_projection=True,
        )

        self._run_id: str = ""
        self._model: str = ""
        self._cancelled: bool = False
        self._finished: bool = False
        self._process_results: list[dict[str, Any]] = []
        self._result_metadata: dict[str, dict[str, Any]] = {}

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
        self._process_results.clear()
        self._relay.reset()
        self._relay.set_model(self._model)
        self._activity_controller.clear()
        self._todo_projector.clear()
        _log.info(
            "production_run_started run_id=%s model=%s", self._run_id, self._model
        )
        self.workerStarted.emit(self._run_id)
        return self._run_id

    def handle_event(self, ev: Event) -> None:
        """Feed one model/tool event into the ledger and workspace projection."""
        if not self._run_id:
            return
        self._track_process_event(ev)
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
        self.workerCancelled.emit(self._run_id)

    def finish(
        self,
        blocked_reason: str = "",
        provider_contract_failure: bool = False,
        already_satisfied: bool = False,
        bears_production_action: bool = False,
        runaway_stopped: bool = False,
    ) -> ProductionReceipt | None:
        """Build exactly one completion receipt and emit the finish lifecycle.

        ``already_satisfied``, ``bears_production_action``, and
        ``runaway_stopped`` are structured receipt inputs that the completion
        contract reads (see :mod:`aura.bridge.production_receipt`): a
        production-action turn may only be reported completed when one truthful
        terminal outcome occurred, and a runaway stop is reported as explicitly
        incomplete.
        """
        if not self._run_id or self._finished:
            return None
        self._finished = True
        if self._cancelled:
            _log.info("production_run_cancelled run_id=%s", self._run_id)
            self.workerCancelled.emit(self._run_id)
            return None

        receipt = build_production_receipt(
            self.evidence(
                blocked_reason=blocked_reason,
                provider_contract_failure=provider_contract_failure,
                already_satisfied=already_satisfied,
                bears_production_action=bears_production_action,
                runaway_stopped=runaway_stopped,
            )
        )
        self._result_metadata[self._run_id] = dict(receipt.metadata)
        _log.info(
            "production_run_finished run_id=%s ok=%s status=%s",
            self._run_id, receipt.ok, receipt.status,
        )
        self.workerFinished.emit(
            self._run_id,
            receipt.ok,
            receipt.text,
            receipt.needs_followup,
            receipt.status,
        )
        return receipt

    # ---- evidence --------------------------------------------------------

    def evidence(
        self,
        blocked_reason: str = "",
        provider_contract_failure: bool = False,
        already_satisfied: bool = False,
        bears_production_action: bool = False,
        runaway_stopped: bool = False,
    ) -> ProductionRunEvidence:
        """Return the structured execution evidence for the active run."""
        relay = self._relay
        return ProductionRunEvidence(
            run_id=self._run_id,
            model=self._model,
            write_results=list(relay.write_results),
            not_applied_writes=list(relay.not_applied_writes),
            terminal_results=list(relay.terminal_results),
            validation_results=list(relay.validation_results),
            process_results=list(self._process_results),
            api_errors=list(relay.api_errors),
            failed_tool_results=list(relay.failed_tool_results),
            final_response=relay.final_report_text,
            cancelled=self._cancelled,
            blocked_reason=blocked_reason,
            provider_contract_failure=provider_contract_failure,
            bears_production_action=bears_production_action,
            already_satisfied=already_satisfied,
            runaway_stopped=runaway_stopped,
        )

    def result_metadata(self, run_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(run_id, {}))

    def clear(self) -> None:
        """Clear projected state (conversation reset / teardown)."""
        self._activity_controller.clear()
        self._todo_projector.clear()
        self._relay.reset()
        self._process_results.clear()
        self._result_metadata.clear()
        self._run_id = ""
        self._finished = False
        self._cancelled = False

    # ---- internals -------------------------------------------------------

    def _track_process_event(self, ev: Event) -> None:
        """Record external process start/finish so the receipt can cite exit status."""
        if isinstance(ev, AgentProcessStarted):
            self._process_results.append({
                "process_id": ev.process_id,
                "label": ev.label,
                "command": ev.command,
                "exit_code": None,
            })
        elif isinstance(ev, AgentProcessFinished):
            for record in reversed(self._process_results):
                if record.get("process_id") == ev.process_id:
                    record["exit_code"] = ev.exit_code
                    return
            self._process_results.append({
                "process_id": ev.process_id,
                "label": "",
                "command": "",
                "exit_code": ev.exit_code,
            })
        elif isinstance(ev, AgentProcessOutput):
            return

    def _on_activity_changed(self, entries: list) -> None:
        self.workerActivityUpdated.emit(
            self._run_id,
            [entry.to_dict() for entry in entries],
        )

    def _on_todo_changed(self, run_id: str, items: list[dict[str, str]]) -> None:
        self.workerTodoUpdated.emit(run_id, items)


__all__ = [
    "ProductionExecutionSession",
    "new_production_run_id",
]
