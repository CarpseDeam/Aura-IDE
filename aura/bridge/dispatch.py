"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.

Uses WorkArtifactController instead of the old DispatchSession campaign
orchestration. Every Worker run is visible, reviewable, and recorded.
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
from aura.bridge.dispatch_pending import DispatchPendingMap, _DispatchPending
from aura.bridge.worker_activity import WorkerActivityController
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
    redact_secrets,
)
from aura.conversation import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerOutcomeStatus,
)
from aura.conversation.persistence import WorkerDispatchRecord
from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS
from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.dependency_context import build_dependency_stanza
from aura.events import EventBus
from aura.lifecycle import HookContext, HookMatcher, LifecycleHooks, attach_lifecycle_notify
from aura.lifecycle.builtin_worker_gates import register_builtin_worker_gates
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.receipt import worker_result_to_receipt
from aura.worker_todo import WorkerTodoProjector

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
    showSpecCard = Signal(str, str, list, str, str, str, list)  # tool_id, goal, files, spec, acceptance, summary, steps (legacy, always [])
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
    workerTerminalOutput = Signal(str, str, str)  # parent_tool_id, worker_tool_id, text
    workerAgentProcessStarted = Signal(str, str, str, str)  # parent_tool_id, process_id, label, command
    workerAgentProcessOutput = Signal(str, str, str)  # parent_tool_id, process_id, text
    workerAgentProcessFinished = Signal(str, str, object)  # parent_tool_id, process_id, exit_code
    workflowStateChanged = Signal(object)  # WorkflowState snapshot
    workerActivityUpdated = Signal(str, list)  # tool_call_id, activity snapshot entries
    workerTodoUpdated = Signal(str, list)  # tool_call_id, full Worker TODO snapshot

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

        # Work Artifact controller — owns active artifacts for the session.
        self._artifact_controller = WorkArtifactController()

        # Event bus — owned by the dispatch proxy.
        self._event_bus = EventBus()

        # Lifecycle hooks — observation bridge attached to the event bus.
        self._lifecycle = LifecycleHooks()
        self._detach_builtin_worker_gates = register_builtin_worker_gates(
            self._lifecycle
        )
        self._detach_lifecycle_notify = attach_lifecycle_notify(
            self._event_bus, self._lifecycle
        )

        # Activity controller projects from worker tool/command events on the bus.
        self._activity_controller = WorkerActivityController(self._event_bus)
        self._activity_controller.set_on_change(self._on_activity_changed)
        self._todo_projector = WorkerTodoProjector(self._event_bus)
        self._todo_projector.set_on_change(self._on_todo_changed)

        # Listen for artifact projection updates.
        self._artifact_controller.set_on_projection_updated(
            self._on_artifact_projection_updated
        )

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

    def lifecycle_hooks(self) -> LifecycleHooks:
        """Return the lifecycle hooks bridge for test inspection."""
        return self._lifecycle

    def records(self) -> list[WorkerDispatchRecord]:
        return list(self._records)

    def set_records(self, records: list[WorkerDispatchRecord]) -> None:
        self._records = list(records)

    def clear_records(self) -> None:
        self._records.clear()

    def result_metadata(self, tool_call_id: str) -> dict[str, Any]:
        return dict(self._result_metadata.get(tool_call_id, {}))

    def artifact_controller(self) -> WorkArtifactController:
        """Return the WorkArtifactController for test inspection."""
        return self._artifact_controller

    # ---- Worker Activity (projected from event bus) -----------------------

    def _on_activity_changed(self, entries: list) -> None:
        """Emit the latest activity snapshot whenever the controller appends."""
        self.workerActivityUpdated.emit(
            entries[0].artifact_id if entries else "",
            [e.to_dict() for e in entries],
        )

    def _on_todo_changed(self, tool_call_id: str, items: list[dict[str, str]]) -> None:
        """Emit the latest Worker TODO snapshot whenever the projector updates."""
        self.workerTodoUpdated.emit(tool_call_id, items)

    def clear_activity(self) -> None:
        """Clear activity entries (conversation reset / teardown)."""
        self._activity_controller.clear()
        self._todo_projector.clear()
        self._artifact_controller.clear()

    # ---- artifact projection updates ------------------------------------

    def _on_artifact_projection_updated(self, projection: WorkArtifactProjection) -> None:
        """React to WorkArtifact projection changes from the controller."""
        _log.debug(
            "WorkArtifact projection updated artifact_id=%s",
            projection.artifact_id,
        )
        # This is where GUI updates would be triggered — e.g. update
        # an artifact card in the chat view.

    # ---- planner-thread side ---------------------------------------------

    def _register_artifact_from_request(self, tool_call_id: str, req: WorkerDispatchRequest) -> None:
        """Register a WorkArtifact for this dispatch request.

        If the request already has an artifact_id, it means the ToolRunner
        already created the artifact. Otherwise, create a one-item
        compatibility artifact.
        """
        if self._artifact_controller.get_artifact(tool_call_id) is not None:
            return  # Already registered.
        self._artifact_controller.create_one_item_artifact(tool_call_id, req)

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        # Register the artifact (idempotent — tool_runner may have created it).
        self._register_artifact_from_request(tool_call_id, req)

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
            [],  # legacy steps — always empty
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

        edited = pending.edited_request or req

        # -- dependency graph: annotate downstream dependents ---------------
        if self._workspace_root is not None and edited.files:
            stanza = build_dependency_stanza(self._workspace_root, edited.files)
            if stanza:
                edited = replace(edited, spec=edited.spec + stanza)

        # --- Mark artifact item active ---
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        if artifact is not None and artifact.current_item_id:
            self._artifact_controller.mark_item_active(
                tool_call_id, artifact.current_item_id
            )

        # --- Emit dispatched snapshot ---
        self._transition_workflow_state(
            tool_call_id,
            WorkflowStatus.dispatched,
            pending_user_action="",
        )

        # --- Emit workerStarted ---
        _log.info("workerStarted emitted tool_call_id=%s", tool_call_id)
        self.workerStarted.emit(tool_call_id)

        # --- Run Worker exactly once ---
        try:
            result = self._run_worker(tool_call_id, edited, pending)
        except Exception as exc:
            _log.exception(
                "request_dispatch _run_worker failed tool_call_id=%s",
                tool_call_id,
            )
            result = WorkerDispatchResult(
                ok=False,
                summary=f"Harness error: {type(exc).__name__}",
                cancelled=False,
                recoverable=False,
                status=WorkerOutcomeStatus.harness_error.value,
                extras={
                    "worker_internal_error": True,
                    "error_type": type(exc).__name__,
                    "internal_error": redact_secrets(f"{type(exc).__name__}: {exc}"),
                },
            )

        # --- Convert result to receipt and attach to artifact ---
        self._artifact_controller.attach_receipt(tool_call_id, result)

        # --- Emit artifact projection update ---
        projection = WorkArtifactProjection.from_artifact(artifact) if artifact else None
        if projection is not None:
            self._on_artifact_projection_updated(projection)

        # --- Emit workerFinished ---
        _log.info(
            "workerFinished emitted tool_call_id=%s ok=%s",
            tool_call_id, result.ok,
        )
        self.workerFinished.emit(
            tool_call_id,
            result.ok,
            result.summary,
            result.needs_followup,
            result.status or "",
        )

        # --- Emit finished WorkflowState snapshot ---
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

        # --- Advance to next item if available (review only, no auto-dispatch) ---
        has_next = self._artifact_controller.advance_to_next_item(tool_call_id)
        if has_next:
            _log.info(
                "WorkArtifact has next pending item tool_call_id=%s — ready for review",
                tool_call_id,
            )

        self._store_result_metadata(tool_call_id, result)
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
        """Callback suitable for threading through to ToolRunner."""
        if self._active_workflow is None or self._active_workflow.tool_call_id != tool_call_id:
            self._init_workflow_state(tool_call_id, goal, summary)
        self._transition_workflow_state(tool_call_id, status)

    def _workflow_tool_started(
        self, tool_call_id: str, worker_tool_id: str, name: str
    ) -> None:
        """Transition workflow state when a Worker tool starts."""
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
        """Absorb a Worker tool result into the canonical WorkflowState."""
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

    def _create_worker_dispatch_runner(
        self,
        *,
        suppress_final_report_activity: bool = False,
        suppress_workflow_state_updates: bool = False,
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
            suppress_final_report_activity=suppress_final_report_activity,
            suppress_workflow_state_updates=suppress_workflow_state_updates,
            records=self._records,
            result_metadata=self._result_metadata,
            set_tier1_context=self.set_tier1_context,
            event_bus=self._event_bus,
            lifecycle=self._lifecycle,
        )
