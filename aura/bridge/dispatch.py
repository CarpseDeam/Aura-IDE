"""Dispatch proxy, pending state, and worker result helpers.

Routes dispatch_to_worker calls through the GUI (SpecCard) and runs
the worker manager when the user clicks Dispatch.

Uses WorkArtifactController instead of the old DispatchSession campaign
orchestration. Every Worker run is visible, reviewable, and recorded.
"""

from __future__ import annotations

import logging
import threading
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
from aura.lifecycle import LifecycleHooks, attach_lifecycle_notify
from aura.lifecycle.builtin_worker_gates import register_builtin_worker_gates
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.model import WorkItemStatus
from aura.work_artifact.projection import WorkArtifactProjection
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


def _failure_signature(result: "WorkerDispatchResult") -> tuple[str, str]:
    """Stable failure signature for stall detection.

    Returns (failure_class, summary_core) where the summary is trimmed
    to its first 200 characters to remove variable detail such as
    timestamps or path formatting.  When the signature and the set of
    modified files both match the previous attempt, the attempt counts
    toward the stall limit.
    """
    extras = result.extras if isinstance(result.extras, dict) else {}
    fc = str(extras.get("failure_class", "") or "")
    stable = (result.summary or "")[:200]
    return (fc, stable)


class _DispatchProxy(QObject):
    showSpecCard = Signal(str, str, list, str, str, str)  # tool_id, goal, files, spec, acceptance, summary
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
    artifactProjectionUpdated = Signal(object)  # WorkArtifactProjection

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

        # Cancel event for internal artifact item jobs.
        self._artifact_job_cancel_event = threading.Event()

        # Approved artifact requests — retains job ownership across item
        # failures and infrastructure pauses. Entry removed only on
        # completion, user cancellation, recovery exhaustion, or teardown.
        self._approved_artifact_requests: dict[str, WorkerDispatchRequest] = {}

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
        self._approved_artifact_requests.clear()

    # ---- artifact projection updates ------------------------------------

    def _on_artifact_projection_updated(self, projection: WorkArtifactProjection) -> None:
        """React to WorkArtifact projection changes from the controller."""
        _log.debug(
            "WorkArtifact projection updated artifact_id=%s",
            projection.artifact_id,
        )
        self.artifactProjectionUpdated.emit(projection)

    # ---- planner-thread side ---------------------------------------------

    def _register_artifact_from_request(self, tool_call_id: str, req: WorkerDispatchRequest) -> None:
        """Register a WorkArtifact for this dispatch request.

        Priority:
        1. If the controller already has an artifact for this ID, skip.
        2. If the request carries a ``work_artifact_payload`` (multi-item
           artifact from the Planner), create the full artifact from it.
        3. Otherwise, create a one-item compatibility artifact.
        """
        if self._artifact_controller.get_artifact(tool_call_id) is not None:
            return  # Already registered.
        if req.work_artifact_payload is not None:
            self._artifact_controller.create_artifact_from_payload(
                tool_call_id, req.work_artifact_payload,
            )
            _log.info(
                "Full WorkArtifact created from payload tool_call_id=%s",
                tool_call_id,
            )
        else:
            self._artifact_controller.create_one_item_artifact(tool_call_id, req)

    def request_dispatch(
        self, tool_call_id: str, req: WorkerDispatchRequest
    ) -> WorkerDispatchResult:
        """Called from the planner's worker thread. Blocks."""
        # ── Binding guard: resume existing approved artifact job ────────────
        # If any approved artifact request still has pending items, the
        # incoming dispatch continues that job. No SpecCard, no per-item
        # review — the original approval is still in effect.
        resume_original_id: str | None = None
        for existing_id in list(self._approved_artifact_requests.keys()):
            if self._artifact_controller.pending_items(existing_id):
                resume_original_id = existing_id
                break

        if resume_original_id is not None:
            return self._run_resumed_artifact_job(
                tool_call_id, resume_original_id, req,
            )

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

        # --- Determine if this is a WorkArtifact job ---
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        is_artifact_job = (
            artifact is not None
            and req.work_artifact_payload is not None
        )

        # --- Mark artifact item active (flat only; artifact job marks internally) ---
        if not is_artifact_job and artifact is not None and artifact.current_item_id:
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

        # --- Store approved request (artifact job retains ownership) ---
        if is_artifact_job:
            self._approved_artifact_requests[tool_call_id] = edited

        # --- Run Worker ---
        try:
            if is_artifact_job:
                # WorkArtifact: run all items internally as bounded requests
                self._artifact_job_cancel_event.clear()
                result = self._run_approved_artifact_job(
                    tool_call_id, edited, pending,
                )
            else:
                # Flat dispatch: single Worker run on the edited request
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

        # --- Remove approved artifact request on terminal outcomes ---
        # Infrastructure-paused jobs retain ownership so they can be
        # resumed. Completion, cancellation, and exhaustion are terminal.
        if is_artifact_job and not result.extras.get("work_artifact_unfinished"):
            self._approved_artifact_requests.pop(tool_call_id, None)

        # --- Attach receipt for flat dispatch (artifact job handles its own receipts) ---
        if not is_artifact_job:
            self._artifact_controller.attach_receipt(tool_call_id, result)

        # --- Emit artifact projection update ---
        projection = WorkArtifactProjection.from_artifact(artifact) if artifact else None
        if projection is not None:
            self._on_artifact_projection_updated(projection)

        # --- Emit workerFinished (once for the whole job) ---
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
        self._artifact_job_cancel_event.set()

    # ---- worker run ------

    # Stall limit per artifact item — consecutive identical-failure attempts
    # that produce no new information (same failure signature, same modified
    # files). Exceeded → recovery exhaustion (terminal). Any change in
    # signature or modified files resets the counter to zero.
    _ARTIFACT_ITEM_STALL_LIMIT = 3

    def _run_resumed_artifact_job(
        self,
        incoming_tool_call_id: str,
        original_id: str,
        req: WorkerDispatchRequest,
    ) -> WorkerDispatchResult:
        """Resume an infrastructure-paused artifact job under the original approval.

        No SpecCard is shown. The pending map is registered under
        *original_id* so ``_run_worker`` has its pending object.
        Emissions (workerStarted, projection, workerFinished, workflow
        state) use the original tool_call_id. The Planner receives the
        aggregate with ``resumed_artifact_id`` in extras.
        """
        # Remove any artifact created for the incoming call.
        self._artifact_controller.remove_artifact(incoming_tool_call_id)

        # Register pending under the original id and auto-resolve so the
        # dispatch proceeds without GUI interaction.
        pending = self._pending_map.register(original_id, req)
        pending.edited_request = self._approved_artifact_requests[original_id]
        pending.decision_event.set()

        _log.info(
            "Resuming approved artifact job tool_call_id=%s (incoming=%s)",
            original_id, incoming_tool_call_id,
        )

        # Skip showSpecCard — resume uses the original approval.
        self._transition_workflow_state(
            original_id,
            WorkflowStatus.dispatched,
            pending_user_action="",
        )
        self.workerStarted.emit(original_id)

        try:
            self._artifact_job_cancel_event.clear()
            edited = self._approved_artifact_requests[original_id]
            result = self._run_approved_artifact_job(original_id, edited, pending)
        except Exception as exc:
            _log.exception(
                "Resumed artifact job failed tool_call_id=%s",
                original_id,
            )
            result = WorkerDispatchResult(
                ok=False,
                summary=f"Harness error on resume: {type(exc).__name__}",
                cancelled=False,
                recoverable=False,
                status=WorkerOutcomeStatus.harness_error.value,
                extras={
                    "worker_internal_error": True,
                    "error_type": type(exc).__name__,
                    "internal_error": redact_secrets(f"{type(exc).__name__}: {exc}"),
                },
            )

        # Remove from approved on terminal outcomes.
        if not result.extras.get("work_artifact_unfinished"):
            self._approved_artifact_requests.pop(original_id, None)

        # Emit projection.
        artifact = self._artifact_controller.get_artifact(original_id)
        if artifact is not None:
            projection = WorkArtifactProjection.from_artifact(artifact)
            self._on_artifact_projection_updated(projection)

        # Emit workerFinished.
        self.workerFinished.emit(
            original_id,
            result.ok,
            result.summary,
            result.needs_followup,
            result.status or "",
        )

        # Emit finished WorkflowState.
        if self._active_workflow is not None and self._active_workflow.tool_call_id == original_id:
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

        self._store_result_metadata(original_id, result)
        self._pending_map.pop(original_id)

        # Tag with the resumed artifact id so the Planner can correlate.
        extras_out = dict(result.extras) if isinstance(result.extras, dict) else {}
        extras_out["resumed_artifact_id"] = original_id
        result = replace(result, extras=extras_out)

        return result

    @staticmethod
    def _is_infrastructure_failure(result: WorkerDispatchResult) -> bool:
        """True for harness/provider/auth/network failures.

        These pause the job rather than retrying the item, and the job
        can be resumed later when the infrastructure is healthy.
        """
        if result.status == WorkerOutcomeStatus.harness_error.value:
            return True
        extras = result.extras if isinstance(result.extras, dict) else {}
        if extras.get("api_errors"):
            return True
        fc = str(extras.get("failure_class", "") or "")
        if any(marker in fc for marker in ("provider", "network", "auth", "api_error", "unavailable")):
            return True
        return False

    def _run_approved_artifact_job(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        """Run all artifact items internally as bounded Worker requests.

        Recovery: Every non-cancelled, non-infrastructure failure retries
        the same item with accumulated failure context appended to the
        spec. An attempt counts toward **stall** only when its failure
        signature AND modified-files set both match the previous attempt
        — any difference resets the stall counter.

        Outcomes
        --------
        - **completed** — all items ok.
        - **cancelled** — user cancelled the job.
        - **infrastructure-paused** — harness/provider/auth/network failure;
          resumable later (``work_artifact_unfinished: true``).
        - **exhausted** — stall limit reached (terminal).
        """
        item_results: list[tuple[str, WorkerDispatchResult]] = []
        recovered_item_ids: list[str] = []
        failed_attempts: dict[str, int] = {}

        pending_items = self._artifact_controller.pending_items(tool_call_id)
        total = len(pending_items)

        for idx, item in enumerate(pending_items):
            item_index = idx + 1  # 1-based index

            # ── Check for external cancellation between items ────────────────
            if self._artifact_job_cancel_event.is_set():
                return self._aggregate_artifact_results(
                    tool_call_id, approved_req, item_results,
                    recovered_item_ids, failed_attempts, total,
                    terminal_override=WorkerDispatchResult(
                        ok=False,
                        summary="WorkArtifact job cancelled during internal items.",
                        cancelled=True,
                        extras={"work_artifact_job": True, "work_artifact_cancelled": True},
                    ),
                )

            _log.info(
                "WorkArtifact internal item %d/%d tool_call_id=%s item=%s",
                item_index, total, tool_call_id, item.id,
            )

            # Mark this exact item active.
            self._artifact_controller.mark_item_active(tool_call_id, item.id)
            self._emit_projection(tool_call_id)

            # Build a bounded WorkerDispatchRequest for this item.
            item_req = self._build_artifact_item_request(
                tool_call_id, approved_req, item, item_index, total,
            )

            # ── Item retry loop with stall detection ─────────────────────────
            attempt = 0
            stall_count = 0
            last_sig: tuple[str, str] | None = None
            last_files: frozenset[str] = frozenset()
            item_result = None

            while True:
                item_result = self._run_worker(tool_call_id, item_req, pending)
                attempt += 1

                if item_result.ok:
                    _log.info(
                        "WorkArtifact item %s ok (attempt %d)",
                        item.id, attempt,
                    )
                    break

                if item_result.cancelled:
                    _log.info(
                        "WorkArtifact item %s cancelled (attempt %d)",
                        item.id, attempt,
                    )
                    break

                # ── Infrastructure failure → pause the entire job ──────────
                if self._is_infrastructure_failure(item_result):
                    _log.info(
                        "WorkArtifact item %s infrastructure failure — "
                        "pausing job tool_call_id=%s",
                        item.id, tool_call_id,
                    )
                    self._artifact_controller.attach_receipt(
                        tool_call_id, item_result, item_id=item.id,
                    )
                    item_results.append((item.id, item_result))
                    return self._aggregate_artifact_results(
                        tool_call_id, approved_req, item_results,
                        recovered_item_ids, failed_attempts, total,
                        terminal_override=item_result,
                        infrastructure_pause=True,
                    )

                # ── Non-infrastructure failure → retry with recovery note ──
                sig = _failure_signature(item_result)
                mod = frozenset(item_result.modified_files or [])

                if sig == last_sig and mod == last_files:
                    stall_count += 1
                    _log.info(
                        "WorkArtifact item %s stall %d/%d (same sig, same files)",
                        item.id, stall_count, self._ARTIFACT_ITEM_STALL_LIMIT,
                    )
                else:
                    stall_count = 0
                    _log.info(
                        "WorkArtifact item %s stall reset (sig or files changed)",
                        item.id,
                    )

                last_sig = sig
                last_files = mod

                if stall_count >= self._ARTIFACT_ITEM_STALL_LIMIT:
                    _log.info(
                        "WorkArtifact item %s stall limit — recovery exhausted",
                        item.id,
                    )
                    failed_attempts[item.id] = attempt
                    break

                # Build recovery context and retry.
                recovered_item_ids.append(item.id)
                extras = item_result.extras if isinstance(item_result.extras, dict) else {}
                recovery_note = (
                    f"\n\n--- Recovery attempt {attempt} for this item ---\n"
                    f"Previous result: {item_result.summary}\n"
                    f"Failure class: {extras.get('failure_class', 'unknown')}\n"
                    f"Suggested next action: {extras.get('suggested_next_action', '')}\n"
                    "Complete only this item. Aura will continue the approved job after this item succeeds."
                )
                item_req = replace(item_req, spec=item_req.spec + recovery_note)
                _log.info(
                    "WorkArtifact item %s retry attempt %d (stall_count=%d)",
                    item.id, attempt, stall_count,
                )

            if item_result is None:
                item_result = WorkerDispatchResult(
                    ok=False,
                    summary="WorkArtifact internal item was not started.",
                    extras={"work_artifact_item_not_started": True},
                )

            # Attach receipt to this exact item.
            self._artifact_controller.attach_receipt(
                tool_call_id, item_result, item_id=item.id,
            )
            item_results.append((item.id, item_result))

            # Stop on cancellation or exhaustion — NOT on ordinary failure.
            if item_result.cancelled:
                _log.info(
                    "WorkArtifact job stopping after cancelled item tool_call_id=%s",
                    tool_call_id,
                )
                break
            if stall_count >= self._ARTIFACT_ITEM_STALL_LIMIT:
                _log.info(
                    "WorkArtifact job recovery exhausted tool_call_id=%s item=%s",
                    tool_call_id, item.id,
                )
                break

        # Build and return aggregate result.
        return self._aggregate_artifact_results(
            tool_call_id, approved_req, item_results,
            recovered_item_ids, failed_attempts, total,
        )

    def _build_artifact_item_request(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        item: Any,
        index: int,
        total: int,
    ) -> WorkerDispatchRequest:
        """Build a bounded WorkerDispatchRequest for one artifact item.

        Preserves the approved top-level context while scoping to the item.
        If the item carries a non-ok, non-continuing receipt (from a prior
        failed attempt), appends that receipt's status and summary to the
        spec so resumed items have context.
        """
        spec_parts = [
            f"WorkArtifact Item {index}/{total}: {item.title}",
            "",
            f"Approved job goal: {approved_req.goal}",
            f"Top-level constraints: {approved_req.spec}",
            "",
            f"Item intent: {item.intent}",
            "",
            "This is one bounded item inside an already approved WorkArtifact job.",
            "Complete only this item. Do not execute other artifact items.",
            "Other items of this approved job may have already modified files "
            "in the workspace. Those changes are NOT yours. Do not inspect, "
            "verify, revert, re-implement, or report on them. They are approved "
            "background, identical to any other pre-existing code. When this "
            "item's acceptance criteria are met and its validation commands "
            "pass, report done immediately. Do not continue checking other "
            "items or the overall job goal — the harness owns job-level "
            "completion, not you.",
            "Aura will continue the approved job after this item succeeds.",
        ]
        spec = "\n".join(spec_parts)

        # Append prior-attempt receipt context if the item carries one.
        if item.receipt is not None and item.receipt.status not in ("ok", "continuing"):
            receipt = item.receipt
            spec += (
                f"\n\n--- Previous attempt on this item ---\n"
                f"Status: {receipt.status}\n"
                f"Summary: {receipt.summary}\n"
                f"Modified files: {', '.join(receipt.modified_files) if receipt.modified_files else '(none)'}"
            )

        # Append manifest of prior done items so each worker knows what already
        # changed in the workspace and why — derived from verified receipts, not
        # from planner narration.
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        if artifact is not None:
            done_items = [
                it for it in artifact.work_items
                if it.status == WorkItemStatus.done and it.receipt is not None
            ]
            if done_items:
                manifest_parts = [
                    "",
                    "--- Changes already made by prior items of this job ---",
                ]
                for done_item in done_items:
                    files_str = (
                        ", ".join(done_item.receipt.modified_files)
                        if done_item.receipt.modified_files
                        else "(none recorded)"
                    )
                    manifest_parts.append(
                        f"Item: {done_item.title}\n"
                        f"  Modified files: {files_str}\n"
                        f"  Summary: {done_item.receipt.summary}"
                    )
                manifest_parts.append(
                    "These changes are complete, verified, and expected in the working tree "
                    "and in git status/diff output. Treat them as existing code. Do not revert, "
                    "re-verify, or re-implement them."
                )
                spec += "\n".join(manifest_parts)

        return WorkerDispatchRequest(
            goal=item.intent or approved_req.goal,
            files=list(item.target_files) if item.target_files else list(approved_req.files),
            spec=spec,
            acceptance=item.acceptance or approved_req.acceptance,
            summary=item.title or approved_req.summary,
            artifact_id=tool_call_id,
            artifact_item_id=item.id,
            validation_commands=list(approved_req.validation_commands),
        )

    def _aggregate_artifact_results(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        item_results: list[tuple[str, WorkerDispatchResult]],
        recovered_item_ids: list[str],
        failed_attempts: dict[str, int],
        total_items: int,
        terminal_override: WorkerDispatchResult | None = None,
        infrastructure_pause: bool = False,
    ) -> WorkerDispatchResult:
        """Aggregate per-item results into one outcome.

        Four outcomes
        -------------
        1. **completed** — every item succeeded.
        2. **cancelled** — user cancelled the job.
        3. **infrastructure-paused** — harness/provider/auth/network failure;
           ``work_artifact_unfinished: true`` with ``pending_item_ids`` so
           the caller can resume.
        4. **exhausted** — stall limit reached; terminal with per-item summaries.

        There is no "failed at item X, awaiting instructions" outcome.
        """
        if terminal_override is not None and infrastructure_pause:
            # ── Infrastructure pause: resumable ──
            completed_ids = [item_id for item_id, r in item_results if r.ok]
            pending_ids = [
                it.id
                for it in self._artifact_controller.pending_items(tool_call_id)
            ]
            paused_extras: dict[str, Any] = dict(terminal_override.extras or {})
            paused_extras.update({
                "work_artifact_job": True,
                "work_artifact_unfinished": True,
                "completed_items": completed_ids,
                "pending_item_ids": pending_ids,
                "total_items": total_items,
                "current_item_id": item_results[-1][0] if item_results else "",
            })
            return WorkerDispatchResult(
                ok=False,
                summary=(
                    f"WorkArtifact job paused: "
                    f"{terminal_override.summary or 'Infrastructure issue'}. "
                    f"Job will resume when the provider is reachable. "
                    f"{len(completed_ids)}/{total_items} items completed."
                ),
                cancelled=False,
                modified_files=list(terminal_override.modified_files)
                if terminal_override.modified_files else [],
                recoverable=True,
                status=terminal_override.status or WorkerOutcomeStatus.harness_error.value,
                extras=paused_extras,
            )

        if terminal_override is not None:
            # ── Cancellation: passed through directly ──
            return terminal_override

        all_ok = all(r.ok for _item_id, r in item_results)
        all_cancelled = any(r.cancelled for _item_id, r in item_results)
        first_not_ok = next(
            ((item_id, r) for item_id, r in item_results if not r.ok and not r.cancelled),
            None,
        )
        cancelled_item_id = next(
            (item_id for item_id, r in item_results if r.cancelled),
            None,
        )

        # Collect modified files (ordered union).
        modified_files: list[str] = []
        seen_files: set[str] = set()
        for _item_id, r in item_results:
            for f in (r.modified_files or []):
                if f not in seen_files:
                    seen_files.add(f)
                    modified_files.append(f)

        # Collect validation summaries.
        validation_parts: list[str] = []
        for _item_id, r in item_results:
            if r.validation:
                validation_parts.append(r.validation)
        validation = "\n".join(validation_parts) if validation_parts else None

        item_summaries = {
            item_id: r.summary for item_id, r in item_results
        }
        completed_items = [
            item_id for item_id, r in item_results if r.ok
        ]
        failed_item_id = first_not_ok[0] if first_not_ok else None
        cancel_item_id = cancelled_item_id if all_cancelled else None
        current_item_id = failed_item_id or cancel_item_id or (item_results[-1][0] if item_results else "")

        extras: dict[str, Any] = {
            "work_artifact_job": True,
            "completed_items": completed_items,
            "total_items": total_items,
            "current_item_id": current_item_id,
            "recovered_item_ids": list(recovered_item_ids),
            "failed_attempts": dict(failed_attempts),
            "item_summaries": item_summaries,
        }
        if failed_item_id:
            extras["failed_item_id"] = failed_item_id

        if all_ok:
            return WorkerDispatchResult(
                ok=True,
                summary=f"WorkArtifact job completed: {total_items} item(s) done.",
                modified_files=modified_files,
                validation=validation,
                status=WorkerOutcomeStatus.completed.value,
                extras=extras,
            )
        elif all_cancelled:
            return WorkerDispatchResult(
                ok=False,
                summary="WorkArtifact job cancelled during internal items.",
                cancelled=True,
                modified_files=modified_files,
                status=WorkerOutcomeStatus.cancelled.value,
                extras=extras,
            )
        else:
            # ── Exhausted: terminal, with precise per-item summaries ──
            extras["recovery_exhausted"] = True
            summary_parts = ["WorkArtifact job recovery exhausted."]
            summary_parts.append(
                f"Completed: {len(completed_items)}/{total_items} items."
            )
            for item_id, r in item_results:
                if r.ok:
                    summary_parts.append(f"✓ {item_id}: {r.summary}")
                else:
                    summary_parts.append(f"✗ {item_id}: {r.summary}")
            status = first_not_ok[1].status if first_not_ok else WorkerOutcomeStatus.harness_error.value
            return WorkerDispatchResult(
                ok=False,
                summary=" ".join(summary_parts),
                modified_files=modified_files,
                validation=validation,
                status=status or WorkerOutcomeStatus.harness_error.value,
                extras=extras,
            )

    def _emit_projection(self, tool_call_id: str) -> None:
        """Helper to emit artifact projection from the current state."""
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        if artifact is None:
            return
        projection = WorkArtifactProjection.from_artifact(artifact)
        self._on_artifact_projection_updated(projection)

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
