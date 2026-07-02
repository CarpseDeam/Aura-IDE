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
from aura.bridge.dispatch_todo_controller import DispatchTodoController
from aura.bridge.worker_activity import WorkerActivityController
from aura.bridge.worker_recording import record_dispatch_campaign_completion
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
    redact_secrets,
    ThinkingMode,
)
from aura.conversation import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerOutcomeStatus,
)
from aura.conversation.dispatch_plan import plan_from_request
from aura.conversation.dispatch_todo_manifest import ensure_dispatch_todo_checklist
from aura.conversation.persistence import WorkerDispatchRecord
from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS
from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.dependency_context import build_dependency_stanza
from aura.events import EventBus

__all__ = [
    "_DispatchProxy",
    "_DispatchPending",
    "_format_spec_as_user_message",
    "_build_worker_summary",
    "_last_assistant_content",
    "_check_read_before_edit",
]

DISPATCH_TIMEOUT = 300.0
_log = logging.getLogger(__name__)


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
    workerTodoListUpdated = Signal(str, list)  # tool_call_id, tasks (Worker-local only)
    dispatchTodoListUpdated = Signal(str, list)  # tool_call_id, tasks (canonical snapshots from DispatchSession)
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    workerAgentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    workerAgentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code
    workflowStateChanged = Signal(object)  # WorkflowState snapshot
    workerActivityUpdated = Signal(str, list)  # tool_call_id, activity snapshot entries

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
        self._active_workflow: WorkflowState | None = None

        # Event bus — owned by the dispatch proxy; shared with DispatchSession
        # and WorkerDispatchRunner for activity projection.
        self._event_bus = EventBus()

        # TODO controller projects from dispatch lifecycle events on the bus.
        self._todo_controller = DispatchTodoController(event_bus=self._event_bus)
        self._todo_controller.set_on_change(self._on_todo_controller_changed)

        # Activity controller projects from worker tool/command events on the bus.
        self._activity_controller = WorkerActivityController(self._event_bus)
        self._activity_controller.set_on_change(self._on_activity_changed)

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

    def result_metadata(self, tool_call_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(tool_call_id, {}))

    # ---- canonical TODO (owned by DispatchTodoController) ------------------

    def _emit_dispatch_todo_snapshot(self, tool_call_id: str, tasks: list[dict[str, Any]]) -> None:
        """Emit a canonical dispatch TODO snapshot."""
        logging.debug(
            "_emit_dispatch_todo_snapshot tool_call_id=%s task_count=%d statuses=%s",
            tool_call_id, len(tasks),
            [t.get("status", "?") for t in tasks if isinstance(t, dict)],
        )
        self.dispatchTodoListUpdated.emit(tool_call_id, tasks)

    def _relay_worker_todo_update(self, tool_call_id: str, tasks: list) -> None:
        """Relay Worker-local TODO updates only outside canonical dispatch.

        During canonical dispatch the Worker's update_todo_list tool calls
        and progress-TODO emissions are suppressed — the visible dispatch
        TODO rail is derived from DispatchTodoController state.
        """
        if self._has_canonical_todo(tool_call_id):
            logging.debug(
                "_relay_worker_todo_update tool_call_id=%s suppressed (canonical dispatch TODO active)",
                tool_call_id,
            )
            return
        logging.debug(
            "_relay_worker_todo_update tool_call_id=%s task_count=%d",
            tool_call_id, len(tasks),
        )
        self.workerTodoListUpdated.emit(tool_call_id, tasks)

    # ---- Worker Activity (projected from event bus) -----------------------

    def _on_activity_changed(self, entries: list) -> None:
        """Emit the latest activity snapshot whenever the controller appends."""
        # Forward as a dict snapshot for bridge relay & GUI consumption.
        self.workerActivityUpdated.emit(
            entries[0].campaign_id if entries else "",
            [e.to_dict() for e in entries],
        )

    def clear_activity(self) -> None:
        """Clear activity entries (conversation reset / teardown)."""
        self._activity_controller.clear()

    # ---- planner-thread side ---------------------------------------------

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        pending = self._pending_map.register(tool_call_id, req)
        _log.info("request_dispatch registered pending tool_call_id=%s", tool_call_id)

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
        _log.info("request_dispatch showSpecCard emitted tool_call_id=%s", tool_call_id)

        # --- Emit plan_ready snapshot ---
        self._set_workflow_state(
            WorkflowState.intent_captured(
                tool_call_id, req.goal, summary=req.summary,
            ).with_status(
                WorkflowStatus.plan_ready,
                pending_user_action="Dispatch, edit, or cancel the plan.",
            )
        )
        _log.info("request_dispatch workflow plan_ready emitted tool_call_id=%s", tool_call_id)

        _log.info("request_dispatch waiting for dispatch decision tool_call_id=%s", tool_call_id)
        signaled = pending.decision_event.wait(timeout=DISPATCH_TIMEOUT)
        _log.info(
            "request_dispatch decision_event wait returned tool_call_id=%s signaled=%s cancelled=%s failure=%s",
            tool_call_id,
            signaled,
            pending.cancelled,
            pending.failure_result is not None,
        )
        if not signaled:
            self._pending_map.pop(tool_call_id)
            self._transition_workflow_state(
                tool_call_id,
                WorkflowStatus.blocked,
                blocker_reason="Plan expired — click Dispatch again or Cancel",
                follow_up_required=True,
            )
            return WorkerDispatchResult(
                ok=False,
                recoverable=True,
                summary="Plan expired — click Dispatch again or Cancel",
                extras={"dispatch_not_started": True, "dispatch_approval_timeout": True},
            )

        if pending.failure_result is not None:
            result = pending.failure_result
            self._pending_map.pop(tool_call_id)
            self._transition_workflow_state(
                tool_call_id,
                WorkflowStatus.failed_nonrecoverable,
                pending_user_action="",
                failure_reason=result.summary,
                follow_up_required=True,
            )
            self._store_result_metadata(tool_call_id, result)
            self.workerFinished.emit(
                tool_call_id,
                result.ok,
                result.summary,
                result.needs_followup,
                result.status or "",
            )
            return result

        if pending.cancelled:
            self._pending_map.pop(tool_call_id)
            self._transition_workflow_state(
                tool_call_id,
                WorkflowStatus.cancelled,
                pending_user_action="",
            )
            return WorkerDispatchResult(
                ok=False,
                summary="Cancelled",
                cancelled=True,
                extras={"dispatch_not_started": True, "dispatch_cancelled": True},
            )

        edited = ensure_dispatch_todo_checklist(pending.edited_request or req)

        # -- dependency graph: annotate downstream dependents ---------------
        if self._workspace_root is not None and edited.files:
            stanza = build_dependency_stanza(self._workspace_root, edited.files)
            if stanza:
                edited = ensure_dispatch_todo_checklist(replace(edited, spec=edited.spec + stanza))

        # --- Emit dispatched snapshot (fresh WorkflowState — no steps yet) ---
        self._transition_workflow_state(
            tool_call_id,
            WorkflowStatus.dispatched,
            pending_user_action="",
        )

        plan = plan_from_request(edited)
        _log.info(
            "request_dispatch DispatchSession constructed tool_call_id=%s step_count=%d",
            tool_call_id,
            len(plan.steps),
        )
        worker_started_emitted = False

        def emit_worker_started(started_tool_call_id: str) -> None:
            nonlocal worker_started_emitted
            worker_started_emitted = True
            _log.info("workerStarted emitted tool_call_id=%s", started_tool_call_id)
            self.workerStarted.emit(started_tool_call_id)

        session = DispatchSession(
            tool_call_id=tool_call_id,
            original_request=edited,
            plan=plan,
            run_worker_step=self._run_worker_internal,
            pending=pending,
            emit_worker_started=emit_worker_started,
            emit_worker_finished=self.workerFinished.emit,
            event_bus=self._event_bus,
        )
        # Run the session. The canonical TODO checklist survives after finish
        # so late Worker-local TODO events cannot repaint the rail. It is
        # cleared only on the next dispatch for this tool_call_id, cancellation,
        # or conversation reset.
        try:
            result = session.run()
        except Exception as exc:
            _log.exception(
                "request_dispatch DispatchSession.run failed tool_call_id=%s worker_started=%s",
                tool_call_id,
                worker_started_emitted,
            )
            result = self._dispatch_session_failure_result(
                tool_call_id=tool_call_id,
                exc=exc,
                worker_started=worker_started_emitted,
            )
            self._store_result_metadata(tool_call_id, result)
            self.workerFinished.emit(
                tool_call_id,
                result.ok,
                result.summary,
                result.needs_followup,
                result.status or "",
            )

        # Create one aggregate WorkerDispatchRecord for the whole dispatch
        # campaign so that conversation replay shows one user-facing
        # WorkerSummaryCard rather than one card per internal step.
        if isinstance(result.extras, dict) and result.extras.get("dispatch_session"):
            record_dispatch_campaign_completion(
                records=self._records,
                workspace_root=self._workspace_root,
                tool_call_id=tool_call_id,
                edited_request=edited,
                result=result,
            )

        # Emit the finished WorkflowState snapshot for the terminal outcome.
        if self._active_workflow is not None and self._active_workflow.tool_call_id == tool_call_id:
            extras = result.extras if isinstance(result.extras, dict) else {}
            self._set_workflow_state(
                self._active_workflow.finish(
                    ok=result.ok,
                    summary=result.summary,
                    needs_followup=bool(result.needs_followup),
                    status=result.status,
                    modified_files=list(result.modified_files) if result.modified_files else None,
                    validation=result.validation,
                    extras=extras,
                )
            )
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
        _log.info("user_dispatched called tool_call_id=%s", tool_call_id)
        if not self._pending_map.resolve_dispatched(
            tool_call_id,
            goal=goal,
            files=files,
            spec=spec,
            acceptance=acceptance,
            summary=summary,
        ):
            active_pending_ids = self._pending_map.active_ids()
            pending_existed = tool_call_id in active_pending_ids
            result = self._pending_resolution_failure_result(
                tool_call_id=tool_call_id,
                pending_existed=pending_existed,
                active_pending_ids=active_pending_ids,
            )
            failed_ids = self._pending_map.fail_unresolved(result)
            _log.warning(
                "user_dispatched resolve failed tool_call_id=%s pending_existed=%s active_pending_ids=%s failed_pending_ids=%s",
                tool_call_id,
                pending_existed,
                active_pending_ids,
                failed_ids,
            )
            if not failed_ids:
                self._store_result_metadata(tool_call_id, result)
                self.workerApiError.emit(tool_call_id, -1, result.summary)
                self.workerFinished.emit(
                    tool_call_id,
                    result.ok,
                    result.summary,
                    result.needs_followup,
                    result.status or "",
                )
            return False
        _log.info("user_dispatched resolve succeeded tool_call_id=%s", tool_call_id)
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

    def _store_result_metadata(
        self,
        tool_call_id: str,
        result: WorkerDispatchResult,
    ) -> None:
        metadata = dict(self._result_metadata.get(tool_call_id, {}))
        if result.modified_files:
            metadata["modified_files"] = list(result.modified_files)
        if result.validation is not None:
            metadata["validation"] = result.validation
        metadata["extras"] = dict(result.extras if isinstance(result.extras, dict) else {})
        self._result_metadata[tool_call_id] = metadata

    # ---- canonical WorkflowState ownership --------------------------------

    def _set_workflow_state(self, state: WorkflowState) -> None:
        """Store a WorkflowState snapshot and emit it to the GUI."""
        self._active_workflow = state
        self.workflowStateChanged.emit(state)

    def _get_workflow_state(self) -> WorkflowState | None:
        return self._active_workflow

    def _transition_workflow_state(
        self,
        tool_call_id: str,
        status: WorkflowStatus,
        *,
        pending_user_action: str | None = None,
        blocker_reason: str | None = None,
        failure_reason: str | None = None,
        follow_up_required: bool | None = None,
    ) -> None:
        """Transition the active workflow to *status* if it matches *tool_call_id*."""
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            return
        self._set_workflow_state(
            self._active_workflow.with_status(
                status,
                pending_user_action=pending_user_action,
                blocker_reason=blocker_reason,
                failure_reason=failure_reason,
                follow_up_required=follow_up_required,
            )
        )

    def _init_workflow_state(
        self,
        tool_call_id: str,
        goal: str,
        summary: str = "",
    ) -> None:
        """Initialize a new WorkflowState for a dispatch attempt."""
        self._set_workflow_state(
            WorkflowState.intent_captured(tool_call_id, goal, summary=summary)
        )

    def _workflow_state_callback(
        self,
        tool_call_id: str,
        goal: str,
        summary: str,
        status: WorkflowStatus,
    ) -> None:
        """Callback suitable for threading through to ToolRunner.

        Creates a WorkflowState if none exists for this tool_call_id, then
        transitions to *status*.  Used by the campaign/quality reject paths
        that fire before ``request_dispatch`` is ever called.
        """
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            self._init_workflow_state(tool_call_id, goal, summary)
        self._transition_workflow_state(tool_call_id, status)

    # ---- dispatch TODO helpers --------------------------------------------

    def _on_todo_controller_changed(self, tool_call_id: str, tasks: list[dict[str, Any]]) -> None:
        """Relay a canonical TODO snapshot from the controller to the GUI.

        Only relays when the active workflow matches — stale emissions after
        cancellation or reset are silently dropped.
        """
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            logging.debug(
                "_on_todo_controller_changed tool_call_id=%s suppressed — workflow mismatch",
                tool_call_id,
            )
            return
        self._emit_dispatch_todo_snapshot(tool_call_id, tasks)

    # ── canonical TODO guard ────────────────────────────────────────────

    def _has_canonical_todo(self, tool_call_id: str) -> bool:
        return self._todo_controller.has_active_tool_call(tool_call_id)

    def _workflow_tool_started(
        self, tool_call_id: str, worker_tool_id: str, name: str
    ) -> None:
        """Transition workflow state when a Worker tool starts.

        Called synchronously on the planner thread via DirectConnection
        from WorkerEventRelay.toolCallStart.
        """
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            return
        if name in WRITE_TOOLS:
            self._transition_workflow_state(tool_call_id, WorkflowStatus.editing)
        elif name in TERMINAL_TOOLS:
            self._transition_workflow_state(tool_call_id, WorkflowStatus.validating)

    def _workflow_tool_result(
        self,
        parent_tool_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        """Absorb a Worker tool result into the canonical WorkflowState.

        Called synchronously on the planner thread via DirectConnection
        from WorkerEventRelay.toolResult.
        """
        if self._active_workflow is None or self._active_workflow.tool_call_id != parent_tool_id:
            return
        new_state = self._active_workflow.absorb_worker_tool_result(name, ok, result, extras)
        self._set_workflow_state(new_state)

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

    def _run_worker_internal(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        """Run a single dispatch step without creating a replayable record.

        Internal steps (part of a multi-step DispatchSession campaign) must
        not create durable WorkerDispatchRecord entries, because the
        aggregate campaign result is recorded once after session.run()
        returns.
        """
        _log.info("_run_worker_internal entered tool_call_id=%s", tool_call_id)
        runner = self._create_worker_dispatch_runner(
            suppress_worker_todo_updates=True,
        )
        return runner.run_worker(tool_call_id, req, pending, record_replayable=False)

    def _pending_resolution_failure_result(
        self,
        *,
        tool_call_id: str,
        pending_existed: bool,
        active_pending_ids: list[str],
    ) -> WorkerDispatchResult:
        active_text = ", ".join(active_pending_ids) if active_pending_ids else "(none)"
        summary = (
            "Dispatch could not start Worker because the pending dispatch was "
            "not found/resolved. "
            f"tool_call_id={tool_call_id}; pending_existed={pending_existed}; "
            f"active_pending_ids={active_text}"
        )
        return WorkerDispatchResult(
            ok=False,
            summary=summary,
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.harness_error.value,
            extras={
                "dispatch_not_started": True,
                "dispatch_handoff_failed": True,
                "dispatch_pending_resolution_failed": True,
                "dispatch_internal_error": True,
                "requested_tool_call_id": tool_call_id,
                "pending_existed": pending_existed,
                "active_pending_ids": list(active_pending_ids),
            },
        )

    def _dispatch_session_failure_result(
        self,
        *,
        tool_call_id: str,
        exc: Exception,
        worker_started: bool,
    ) -> WorkerDispatchResult:
        error = redact_secrets(f"{type(exc).__name__}: {exc}")
        return WorkerDispatchResult(
            ok=False,
            summary="Dispatch could not start Worker because the dispatch session failed.",
            needs_followup=True,
            recoverable=True,
            status=WorkerOutcomeStatus.harness_error.value,
            extras={
                "dispatch_session": True,
                "dispatch_session_failed": True,
                "dispatch_session_start_failed": not worker_started,
                "dispatch_internal_error": True,
                "tool_call_id": tool_call_id,
                "error_type": type(exc).__name__,
                "internal_error": error,
            },
        )

    def _create_worker_dispatch_runner(
        self,
        *,
        suppress_worker_todo_updates: bool = False,
    ) -> WorkerDispatchRunner:
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
            suppress_worker_todo_updates=suppress_worker_todo_updates,
            records=self._records,
            result_metadata=self._result_metadata,
            set_tier1_context=self.set_tier1_context,
            event_bus=self._event_bus,
        )
