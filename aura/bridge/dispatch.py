"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QObject,
    Signal,
)

from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.dispatch_pending import _DispatchPending, DispatchPendingMap
from aura.bridge.dispatch_session import DispatchSession
from aura.bridge.todo_controller import DispatchTodoController
from aura.bridge.worker_completion_result import (
    _check_read_before_edit,
    _last_assistant_content,
)
from aura.bridge.worker_dispatch_runner import WorkerDispatchRunner
from aura.bridge.worker_report import (
    _build_worker_summary,
    _format_spec_as_user_message,
)
from aura.config import (
    DEFAULT_WORKER_MODEL,
    DEFAULT_WORKER_THINKING,
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.conversation import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.dispatch_plan import plan_from_request
from aura.conversation.persistence import WorkerDispatchRecord
from aura.dependency_context import build_dependency_stanza

__all__ = [
    "_DispatchProxy",
    "_DispatchPending",
    "_format_spec_as_user_message",
    "_build_worker_summary",
    "_last_assistant_content",
    "_check_read_before_edit",
]

DISPATCH_TIMEOUT = 300.0


class _DispatchProxy(QObject):
    showSpecCard = Signal(str, str, list, str, str, str, list)  # tool_id, goal, files, spec, acceptance, summary, steps
    workerStarted = Signal(str)  # tool_id
    workerFinished = Signal(str, bool, str, bool, str)  # tool_id, ok, summary, needs_followup, status
    workerCancelled = Signal(str)
    workerReasoningDelta = Signal(str, str)
    workerContentDelta = Signal(str, str)
    workerToolCallStart = Signal(str, str, str)  # parent_id, worker_tool_id, name
    workerToolCallArgs = Signal(str, str, str)
    workerToolCallEnd = Signal(str, str)
    workerToolResult = Signal(str, str, str, bool, str, dict)
    workerDiffDecided = Signal(str, str, str, str, str, str, bool)
    workerStreamDone = Signal(str, str, dict)
    workerApiError = Signal(str, int, str)
    workerUsage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    workerTodoListUpdated = Signal(str, list)  # tool_call_id, tasks
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    workerAgentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    workerAgentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code

    def __init__(
        self,
        parent_widget,
        registry_factory,
        approval_proxy: _ApprovalProxy,
        workspace_root: Path | None = None,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._parent_widget = parent_widget
        self._registry_factory = registry_factory
        self._approval_proxy = approval_proxy
        self._workspace_root = workspace_root
        self._provider = provider

        self._worker_model: ModelId = DEFAULT_WORKER_MODEL
        self._worker_thinking: ThinkingMode = DEFAULT_WORKER_THINKING
        self._worker_temperature: float = 0.7
        self._worker_system_prompt: str = ""
        self._tier1_context: str = ""
        self._max_tool_rounds: int | None = None

        # Per-call state — the pending map owns its own lock so concurrent
        # dispatches (which shouldn't happen, but be safe) don't trample.
        self._pending_map = DispatchPendingMap()
        # Records of each completed dispatch for persistence.
        self._records: list[WorkerDispatchRecord] = []
        self._result_metadata: dict[str, dict[str, Any]] = {}
        self._todo_controller: DispatchTodoController = DispatchTodoController()

    # ---- config -----------------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        self._workspace_root = root

    def set_worker_model(self, model: ModelId) -> None:
        self._worker_model = model

    def set_worker_thinking(self, thinking: ThinkingMode) -> None:
        self._worker_thinking = thinking

    def set_worker_temperature(self, temperature: float) -> None:
        self._worker_temperature = temperature

    def set_worker_system_prompt(self, prompt: str) -> None:
        self._worker_system_prompt = prompt

    def set_tier1_context(self, context: str) -> None:
        self._tier1_context = context

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)

    def set_max_tool_rounds(self, value: int | None) -> None:
        self._max_tool_rounds = value

    def records(self) -> list[WorkerDispatchRecord]:
        return list(self._records)

    def set_records(self, records: list[WorkerDispatchRecord]) -> None:
        self._records = list(records)

    def clear_records(self) -> None:
        self._records.clear()
        self._todo_controller.clear_all()

    def result_metadata(self, tool_call_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(tool_call_id, {}))

    # ---- canonical TODO controller ---------------------------------------

    def _emit_canonical_dispatch_todos(
        self, tool_call_id: str, tasks: list[dict[str, Any]]
    ) -> None:
        """Emit a canonical TODO snapshot to the GUI.

        This is the only path through which DispatchSession emits TODO updates
        during a canonical dispatch. It replaces raw Worker-local emissions.
        """
        self.workerTodoListUpdated.emit(tool_call_id, tasks)

    def _relay_worker_todo_update(self, tool_call_id: str, tasks: list) -> None:
        """Relay Worker-local TODO updates only outside canonical dispatch."""
        if self._todo_controller.has_canonical(tool_call_id):
            return
        # No canonical state: pass through as before (non-dispatch worker runs).
        self.workerTodoListUpdated.emit(tool_call_id, tasks)

    # ---- planner-thread side ---------------------------------------------

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        pending = self._pending_map.register(tool_call_id, req)

        # Tell GUI thread to render the spec card; user will call user_dispatched
        # or user_cancelled, which will set decision_event.
        self.showSpecCard.emit(
            tool_call_id,
            req.goal,
            list(req.files),
            req.spec,
            req.acceptance,
            req.summary,
            [step.to_dict() for step in req.steps],
        )

        signaled = pending.decision_event.wait(timeout=DISPATCH_TIMEOUT)
        if not signaled:
            self._pending_map.pop(tool_call_id)
            return WorkerDispatchResult(
                ok=False,
                recoverable=True,
                summary="Plan expired — click Dispatch again or Cancel",
                extras={"dispatch_not_started": True, "dispatch_approval_timeout": True},
            )

        if pending.cancelled:
            self._pending_map.pop(tool_call_id)
            return WorkerDispatchResult(
                ok=False,
                summary="Cancelled",
                cancelled=True,
                extras={"dispatch_not_started": True, "dispatch_cancelled": True},
            )

        edited = pending.edited_request or req

        # -- dependency graph: annotate downstream dependents ---------------
        if self._workspace_root is not None and edited.files:
            stanza = build_dependency_stanza(self._workspace_root, edited.files)
            if stanza:
                edited = replace(edited, spec=edited.spec + stanza)

        # Clear any prior canonical TODO state for this tool_call_id before
        # starting a new dispatch (new SpecCard → fresh checklist).
        self._todo_controller.clear(tool_call_id)

        plan = plan_from_request(edited)
        session = DispatchSession(
            tool_call_id=tool_call_id,
            original_request=edited,
            plan=plan,
            run_worker_step=self._run_worker,
            pending=pending,
            emit_todo_update=self._emit_canonical_dispatch_todos,
            emit_worker_started=self.workerStarted.emit,
            emit_worker_finished=self.workerFinished.emit,
            todo_controller=self._todo_controller,
        )
        # Run the session. The canonical TODO checklist survives after finish
        # so late Worker-local TODO events cannot repaint the rail. It is
        # cleared only on the next dispatch for this tool_call_id, cancellation,
        # or conversation reset.
        result = session.run()
        self._merge_session_result_metadata(tool_call_id, result)
        self._pending_map.pop(tool_call_id)
        return result
    # ---- GUI-thread side --------------------------------------------------

    def user_dispatched(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
    ) -> bool:
        if not self._pending_map.resolve_dispatched(
            tool_call_id,
            goal=goal,
            files=files,
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        ):
            logging.warning(
                f"user_dispatched: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        return True

    def _merge_session_result_metadata(
        self,
        tool_call_id: str,
        result: WorkerDispatchResult,
    ) -> None:
        if not isinstance(result.extras, dict) or not result.extras.get("dispatch_session"):
            return
        metadata = dict(self._result_metadata.get(tool_call_id, {}))
        extras = metadata.get("extras") if isinstance(metadata.get("extras"), dict) else {}
        metadata["extras"] = {**extras, **result.extras}
        if result.modified_files:
            metadata["modified_files"] = list(result.modified_files)
        if result.validation is not None:
            metadata["validation"] = result.validation
        self._result_metadata[tool_call_id] = metadata

    def user_cancelled(self, tool_call_id: str) -> bool:
        if not self._pending_map.resolve_cancelled(tool_call_id):
            logging.warning(
                f"user_cancelled: tool_call_id '{tool_call_id}' is not pending or has already timed out/resolved."
            )
            return False
        return True

    def cancel_all_pending(self) -> None:
        """Called when the user hits Stop. Unblocks any planner waiting for a
        dispatch decision AND signals any running worker to cancel."""
        if self._approval_proxy is not None:
            self._approval_proxy.cancel_active_dialog()
        self._pending_map.cancel_all()

    # ---- worker run ------

    def _run_worker(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        runner = self._create_worker_dispatch_runner()
        return runner.run_worker(tool_call_id, req, pending)

    def _create_worker_dispatch_runner(self) -> WorkerDispatchRunner:
        return WorkerDispatchRunner(
            approval_proxy=self._approval_proxy,
            registry_factory=self._registry_factory,
            workspace_root=self._workspace_root,
            worker_model=self._worker_model,
            worker_thinking=self._worker_thinking,
            worker_temperature=self._worker_temperature,
            worker_system_prompt=self._worker_system_prompt,
            max_tool_rounds=self._max_tool_rounds,
            dispatch_proxy=self,
            todo_relay_callback=self._relay_worker_todo_update,
            records=self._records,
            result_metadata=self._result_metadata,
            set_tier1_context=self.set_tier1_context,
        )
