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
from aura.work_artifact.model import ValidationCommandSpec, WorkItemStatus
from aura.work_artifact.projection import WorkArtifactProjection
from aura.work_artifact.runner import WorkArtifactRunner
from aura.work_artifact.validation_baseline import capture_baseline
from aura.work_artifact.verification import (
    WorkArtifactAttemptOutcome,
    add_retry_context as _add_retry_context,
    classify_item_attempt as _classify_item_attempt,
    declared_validation_commands as _declared_validation_commands,
    ensure_item_verification_source as _ensure_item_verification_source,
    is_infrastructure_failure as _is_infrastructure_failure,
    validation_satisfied as _validation_satisfied,
)
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

        Only creates an artifact when the request carries a real
        ``work_artifact_payload`` (multi-item artifact from the Planner).
        Flat dispatch requests without a payload do NOT create an artifact.
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
        # Flat dispatch: no artifact created.  No projection, no receipt.

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
            if self._artifact_controller.unfinished_items(existing_id):
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
        is_artifact_job = req.work_artifact_payload is not None

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

            # Capture validation baseline once before any item runs.
            # This collects fingerprints of all declared item validation
            # commands so we can later distinguish pre-existing failures
            # from novel ones introduced by each item.
            self._capture_artifact_baseline(tool_call_id)

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
        # Infrastructure-paused and retry-cap-reached jobs retain
        # ownership so they can be resumed. Completion and cancellation
        # are terminal.
        if is_artifact_job and not result.extras.get("work_artifact_unfinished"):
            self._approved_artifact_requests.pop(tool_call_id, None)

        # --- Emit artifact projection update ---
        if is_artifact_job:
            artifact = self._artifact_controller.get_artifact(tool_call_id)
            if artifact is not None:
                projection = WorkArtifactProjection.from_artifact(artifact)
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

        Delegates to ``aura.work_artifact.verification.is_infrastructure_failure``.
        """
        return _is_infrastructure_failure(result)

    @staticmethod
    def _decide_artifact_item_outcome(
        item_req: WorkerDispatchRequest,
        item_result: WorkerDispatchResult,
        item: Any,
    ) -> str:
        """Decide the outcome of a single WorkArtifact item attempt.

        Delegates to ``aura.work_artifact.verification.classify_item_attempt``
        and maps the ``WorkArtifactAttemptOutcome`` enum back to the legacy
        string values for backward compatibility.

        Returns one of:
        - ``"cancelled"`` — user cancelled the item.
        - ``"external_pause"`` — infrastructure failure (provider/API/harness).
        - ``"done"`` — validation evidence shows the item passed.
        - ``"continue_same_item"`` — anything else (failed/missing validation).
        """
        outcome = _classify_item_attempt(item_req, item_result)
        if outcome == WorkArtifactAttemptOutcome.cancelled:
            return "cancelled"
        if outcome == WorkArtifactAttemptOutcome.pause:
            return "external_pause"
        if outcome == WorkArtifactAttemptOutcome.done:
            return "done"
        return "continue_same_item"

    def _create_work_artifact_runner(
        self,
        pending: "_DispatchPending",
    ) -> WorkArtifactRunner:
        """Create a ``WorkArtifactRunner`` configured with this dispatch's callbacks."""
        return WorkArtifactRunner(
            controller=self._artifact_controller,
            run_worker=lambda tid, ireq: self._run_worker(tid, ireq, pending),
            emit_projection=lambda tid: self._emit_projection(tid),
            workspace_root=self._workspace_root,
            capture_baseline=lambda tid: self._capture_artifact_baseline(tid),
        )

    def _run_approved_artifact_job(
        self,
        tool_call_id: str,
        approved_req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        """Run all artifact items via ``WorkArtifactRunner``.

        This is a thin delegation wrapper.  The item loop, request
        construction, classification, and aggregation all live in
        ``aura.work_artifact.runner.WorkArtifactRunner`` and
        ``aura.work_artifact.verification``.
        """
        runner = self._create_work_artifact_runner(pending)
        return runner.run(
            artifact_id=tool_call_id,
            approved_req=approved_req,
            cancel_event=self._artifact_job_cancel_event,
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

        Delegates to ``WorkArtifactRunner._build_artifact_item_request``.
        """
        runner = WorkArtifactRunner(
            controller=self._artifact_controller,
            run_worker=lambda tid, ireq: None,  # not used for request construction
            emit_projection=lambda tid: None,
        )
        return runner._build_artifact_item_request(
            tool_call_id, approved_req, item, index, total,
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

        Delegates to ``WorkArtifactRunner._aggregate_artifact_results``.
        """
        runner = WorkArtifactRunner(
            controller=self._artifact_controller,
            run_worker=lambda tid, ireq: None,  # not used for aggregation
            emit_projection=lambda tid: None,
        )
        return runner._aggregate_artifact_results(
            tool_call_id, approved_req, item_results,
            recovered_item_ids, failed_attempts, total_items,
            terminal_override=terminal_override,
            infrastructure_pause=infrastructure_pause,
        )

    def _emit_projection(self, tool_call_id: str) -> None:
        """Helper to emit artifact projection from the current state."""
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        if artifact is None:
            return
        projection = WorkArtifactProjection.from_artifact(artifact)
        self._on_artifact_projection_updated(projection)

    def _capture_artifact_baseline(self, tool_call_id: str) -> None:
        """Capture validation baseline fingerprints for the active artifact.

        Runs once after approval and before any item Worker launches.
        Stores fingerprints on the ``WorkArtifact.baseline_validation_fingerprints``
        field.  Does not recapture if baseline already exists (idempotent
        across resume).
        """
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        if artifact is None:
            _log.warning("_capture_artifact_baseline: no artifact for %s", tool_call_id)
            return

        if artifact.baseline_validation_fingerprints:
            _log.info("Baseline already exists for artifact %s — skipping", tool_call_id)
            return

        if self._workspace_root is None:
            _log.warning("_capture_artifact_baseline: no workspace root — skipping")
            return

        # Collect the union of all per-item declared validation commands.
        all_commands: list[ValidationCommandSpec] = []
        seen_commands: set[str] = set()
        for item in artifact.work_items:
            for vc in (item.validation_commands or []):
                if vc.command and vc.command not in seen_commands:
                    seen_commands.add(vc.command)
                    all_commands.append(vc)

        if not all_commands:
            _log.info("No item-level validation commands to baseline for %s", tool_call_id)
            return

        _log.info(
            "Capturing baseline for artifact %s with %d command(s)",
            tool_call_id, len(all_commands),
        )
        try:
            baseline = capture_baseline(all_commands, self._workspace_root)
            artifact.baseline_validation_fingerprints = baseline
            _log.info(
                "Baseline captured for artifact %s: %d command(s) fingerprinted",
                tool_call_id, len(baseline),
            )
        except Exception as exc:
            _log.exception(
                "Baseline capture failed for artifact %s: %s",
                tool_call_id, exc,
            )
            # Missing baseline degrades to strict gating — each item will
            # treat all failures as novel.

    def _run_worker(
        self,
        tool_call_id: str,
        req: WorkerDispatchRequest,
        pending: "_DispatchPending",
    ) -> WorkerDispatchResult:
        # For artifact items, thread the captured validation baseline through
        # to the Worker finalization gate so pre-existing failures on declared
        # item commands are attributed correctly.  Flat dispatches yield either
        # a compatibility artifact with no baseline or None — both produce None.
        artifact = self._artifact_controller.get_artifact(tool_call_id)
        baseline_validation_fingerprints = (
            dict(artifact.baseline_validation_fingerprints)
            if artifact is not None
            else None
        )
        runner = self._create_worker_dispatch_runner()
        return runner.run_worker(
            tool_call_id, req, pending,
            baseline_validation_fingerprints=baseline_validation_fingerprints,
        )

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
