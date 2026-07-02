"""Internal Worker dispatch session orchestration seam.

DispatchSession is the engine boundary between the visible GUI dispatch bridge
and the step-sized Worker execution model. It executes every WorkerDispatchPlan
step in order through the existing Worker path, stopping at the first failure,
while the same tool_call_id and visible dispatch identity are preserved throughout.

Lifecycle ownership (Phase 3D):
DispatchSession owns the outer workerStarted / workerFinished emission for the
whole campaign. _run_worker is a pure execution function that no longer emits
visible lifecycle signals, so a multi-step plan produces exactly one started and
one finished event regardless of how many internal steps run.

Execution checklist:
- DispatchSession emits checklist lifecycle facts only.
- The EventBus checklist projector owns visible row state.
- Worker-local TODO updates are ignored by the bridge.

Ownership:
- The primary checklist path is event-driven: lifecycle events flow through
  the EventBus to ExecutionChecklistController, which projects checklist
  state.

Event bus:
- Accepts an EventBus and emits campaign/step lifecycle events
  that the WorkerActivityController (or any projector) can subscribe to.
- Event emission is pure-python — no Qt dependency.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from aura.config import redact_secrets
from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.dispatch_plan import (
    StepResult,
    WorkerDispatchPlan,
    WorkerStepSpec,
    request_for_step,
)
from aura.conversation.verification_progress import fingerprint_failures
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.events import (
    AuraEvent, EventBus,
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CAMPAIGN_STARTED,
    DISPATCH_CHECKLIST_DECLARED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
)
from aura.execution_checklist import build_execution_checklist_items

RunWorkerStep = Callable[[str, WorkerDispatchRequest, Any], WorkerDispatchResult]

# Callback types for the outer campaign lifecycle signals.
# Signatures match the Qt signals on _DispatchProxy:
#   workerStarted  → (tool_call_id: str)
#   workerFinished → (tool_call_id: str, ok: bool, summary: str, needs_followup: bool, status: str)
_EmitStarted = Callable[[str], None]
_EmitFinished = Callable[[str, bool, str, bool, str], None]

MAX_WORKER_STEP_ATTEMPTS = 5
NO_PROGRESS_FINGERPRINT_THRESHOLD = 1
_log = logging.getLogger(__name__)


class DispatchSession:
    """Orchestrates one visible dispatch as one or more sequential Worker steps.

    Product invariant:
    The user expressed intent. That intent is durable until completed,
    cancelled, or stopped.

    Visible lifecycle:
    - workerStarted fires once, before the first step.
    - workerFinished fires once, after the last/blocking step, with the
      aggregate campaign outcome.
    - Internal steps execute silently inside _run_worker_step without
      re-emitting started/finished to the UI.

    Execution checklist (derived from the accepted visible checklist):
    - Visible checklist rows start as pending via EventBus declaration.
    - The active step becomes active while it runs in the EventBus projector.
    - Completed steps become done before the next step activates in the projector.
    - If a step fails, the campaign stops (no blocked TODO state).
    - One final campaign-finished event happens after the loop ends.
    - Worker-local TODO updates from inside _run_worker_step are ignored
      for canonical dispatch tool_call_ids.
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        original_request: WorkerDispatchRequest,
        plan: WorkerDispatchPlan,
        run_worker_step: RunWorkerStep,
        pending: Any,
        event_bus: EventBus,
        emit_worker_started: _EmitStarted | None = None,
        emit_worker_finished: _EmitFinished | None = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.original_request = original_request
        self.plan = plan
        self._run_worker_step = run_worker_step
        self._pending = pending
        self._emit_worker_started = emit_worker_started
        self._emit_worker_finished = emit_worker_finished
        self._event_bus = event_bus
        self.step_results: list[StepResult] = []

    # ------------------------------------------------------------------
    # Checklist lifecycle emission
    # ------------------------------------------------------------------

    def _declare_execution_checklist(self) -> None:
        """Declare canonical checklist rows from the visible checklist."""
        items = [item.to_dict() for item in build_execution_checklist_items(self.plan)]
        logging.debug(
            "DispatchSession._declare_execution_checklist tool_call_id=%s row_count=%d ids=%s",
            self.tool_call_id, len(items),
            [o["id"] for o in items],
        )
        # Emit checklist_declared on the event bus so projectors can react.
        self._emit_event(DISPATCH_CHECKLIST_DECLARED, {
            "items": items,
            "step_count": len(self.plan.steps),
        })

    # ── Event bus emission ──────────────────────────────────────────────

    def _emit_event(self, topic: str, payload: dict[str, Any]) -> None:
        """Emit a lifecycle event on the event bus."""
        self._event_bus.emit(AuraEvent(
            topic=topic,
            payload=dict(payload),
            run_id=self.tool_call_id,
            campaign_id=self.tool_call_id,
            step_id=str(payload.get("step_id", "")),
        ))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> WorkerDispatchResult:
        """Execute every plan step in order and return the aggregate result.

        workerStarted is emitted once before the first step.
        workerFinished is emitted once after the last step with the
        aggregate ok/summary/needs_followup/status — never per internal step.

        Steps run sequentially under the same tool_call_id. The first step that
        triggers _step_should_stop halts the campaign. Steps completed before
        the halt contribute their modified files to the aggregate and appear
        as done in the final TODO state.
        """
        _log.info(
            "DispatchSession.run entered tool_call_id=%s step_count=%d",
            self.tool_call_id,
            len(self.plan.steps),
        )
        if not self.plan.steps:
            result = WorkerDispatchResult(
                ok=False,
                summary="Aura stopped before completion.",
                status=WorkerOutcomeStatus.harness_error.value,
                needs_followup=True,
                recoverable=True,
                extras={
                    "dispatch_session": True,
                    "summary": "Aura stopped before completion.",
                },
            )
            self._emit_lifecycle_pair(result)
            return result

        # Declare canonical checklist state: all rows pending in the projector.
        self._declare_execution_checklist()

        # Campaign starts — one visible Worker start event for the whole run.
        if self._emit_worker_started is not None:
            _log.info("DispatchSession workerStarted emitted tool_call_id=%s", self.tool_call_id)
            self._emit_worker_started(self.tool_call_id)

        # Emit campaign_started on the event bus for activity projectors.
        self._emit_event(DISPATCH_CAMPAIGN_STARTED, {
            "goal": self.original_request.goal,
            "step_count": len(self.plan.steps),
        })

        final_worker_result: WorkerDispatchResult | None = None

        final_step_index = len(self.plan.steps) - 1
        for index, step in enumerate(self.plan.steps):
            # Emit step_started on the event bus.
            self._emit_event(DISPATCH_STEP_STARTED, {
                "step_id": step.id,
                "description": step.title or step.goal,
            })

            try:
                worker_result = self._run_one_step(step)
            except Exception as exc:
                _log.exception(
                    "DispatchSession worker step failed tool_call_id=%s step_id=%s",
                    self.tool_call_id,
                    step.id,
                )
                worker_result = _worker_step_exception_result(exc)
            final_worker_result = worker_result

            step_result = _step_result_for(step, worker_result)
            self.step_results.append(step_result)

            if _step_should_stop(
                step_result,
                worker_result,
                is_final_step=index == final_step_index,
            ):
                break

            # Emit step_completed on the event bus.
            self._emit_event(DISPATCH_STEP_COMPLETED, {
                "step_id": step.id,
                "description": step.title or step.goal,
                "ok": worker_result.ok,
            })

        if final_worker_result is None:
            # Guard — can't happen: plan.steps was verified non-empty above.
            result = WorkerDispatchResult(
                ok=False,
                summary="Aura stopped before completion.",
                status=WorkerOutcomeStatus.harness_error.value,
                needs_followup=True,
                recoverable=True,
                extras={
                    "dispatch_session": True,
                    "summary": "Aura stopped before completion.",
                },
            )
            self._emit_lifecycle_finished(result)
            return result

        aggregate = self._aggregate_from_worker_result(final_worker_result)

        # Campaign ends — one visible Worker finish event with the aggregate outcome.
        self._emit_lifecycle_finished(aggregate)

        return aggregate

    # ------------------------------------------------------------------
    # Lifecycle helpers
    # ------------------------------------------------------------------

    def _emit_lifecycle_pair(self, result: WorkerDispatchResult) -> None:
        """Emit started then finished immediately (for error/empty plan exits)."""
        if self._emit_worker_started is not None:
            _log.info("DispatchSession workerStarted emitted tool_call_id=%s", self.tool_call_id)
            self._emit_worker_started(self.tool_call_id)
        self._emit_lifecycle_finished(result)

    def _emit_lifecycle_finished(self, result: WorkerDispatchResult) -> None:
        # Emit campaign_finished on the event bus for projectors.
        self._emit_event(DISPATCH_CAMPAIGN_FINISHED, {
            "ok": result.ok,
            "summary": result.summary,
            "status": result.status or "",
            "needs_followup": result.needs_followup,
        })
        if self._emit_worker_finished is not None:
            self._emit_worker_finished(
                self.tool_call_id,
                result.ok,
                result.summary,
                result.needs_followup,
                result.status or "",
            )

    # ------------------------------------------------------------------
    # Step execution
    # ------------------------------------------------------------------

    def _run_one_step(self, step: WorkerStepSpec) -> WorkerDispatchResult:
        step_request = request_for_step(self.plan, step, self.original_request)
        attempts: list[dict[str, Any]] = []
        fingerprint_counts: dict[str, int] = {}
        modified_files: list[str] = []

        for attempt_number in range(1, MAX_WORKER_STEP_ATTEMPTS + 1):
            request = _request_with_attempt_context(
                step_request,
                attempts=attempts,
                attempt_number=attempt_number,
            )
            result = self._run_worker_step(self.tool_call_id, request, self._pending)
            modified_files = _dedupe([
                *modified_files,
                *result.modified_files,
                *_applied_write_paths(result),
            ])
            result = _with_persistence_metadata(
                result,
                attempt_number=attempt_number,
                max_attempts=MAX_WORKER_STEP_ATTEMPTS,
                attempts=attempts,
                modified_files=modified_files,
            )

            if result.ok or not _worker_step_failure_is_recoverable(result):
                return result

            if _step_has_file_progress(result):
                return _partial_progress_persistence_result(
                    result,
                    attempt_number=attempt_number,
                    max_attempts=MAX_WORKER_STEP_ATTEMPTS,
                    attempts=attempts,
                    modified_files=modified_files,
                )

            attempt = _attempt_record(attempt_number, result)
            attempts.append(attempt)
            fingerprint = str(attempt["fingerprint"])
            fingerprint_counts[fingerprint] = fingerprint_counts.get(fingerprint, 0) + 1

            if fingerprint_counts[fingerprint] >= NO_PROGRESS_FINGERPRINT_THRESHOLD:
                return _terminal_persistence_result(
                    result,
                    reason="worker_step_no_progress",
                    attempt_number=attempt_number,
                    max_attempts=MAX_WORKER_STEP_ATTEMPTS,
                    attempts=attempts,
                    modified_files=modified_files,
                    fingerprint=fingerprint,
                )

            if attempt_number >= MAX_WORKER_STEP_ATTEMPTS:
                return _terminal_persistence_result(
                    result,
                    reason="worker_step_attempt_budget_exhausted",
                    attempt_number=attempt_number,
                    max_attempts=MAX_WORKER_STEP_ATTEMPTS,
                    attempts=attempts,
                    modified_files=modified_files,
                    fingerprint=fingerprint,
                )

        return result

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def _aggregate_from_worker_result(
        self,
        worker_result: WorkerDispatchResult,
    ) -> WorkerDispatchResult:
        """Build the aggregate WorkerDispatchResult from the blocking/final step.

        modified_files is the union of files touched across all completed and
        attempted steps (first-seen order, no duplicates). All outcome fields
        — ok, status, cancelled, needs_followup, phase_boundary, followup_reason,
        recoverable, mismatch, suggested_next_spec — come from the final Worker
        result so the Planner sees the real terminal state.
        """
        worker_extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
        modified_files = _collect_modified_files(self.step_results) or _dedupe(worker_result.modified_files)
        return WorkerDispatchResult(
            ok=worker_result.ok,
            summary=worker_result.summary,
            cancelled=worker_result.cancelled,
            needs_followup=worker_result.needs_followup,
            phase_boundary=worker_result.phase_boundary,
            followup_reason=worker_result.followup_reason,
            recoverable=worker_result.recoverable,
            status=worker_result.status,
            completed=list(worker_result.completed),
            remaining=list(worker_result.remaining),
            modified_files=modified_files,
            validation=worker_result.validation,
            suggested_next_spec=worker_result.suggested_next_spec,
            extras={
                **worker_extras,
                "dispatch_session": True,
            },
            mismatch=worker_result.mismatch,
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _step_result_for(step: WorkerStepSpec, worker_result: WorkerDispatchResult) -> StepResult:
    """Convert a WorkerDispatchResult to a StepResult for the given step."""
    return StepResult.from_worker_result(step.id, worker_result)


def _worker_step_exception_result(exc: Exception) -> WorkerDispatchResult:
    """Convert a DispatchSession step exception into a visible harness error."""
    return WorkerDispatchResult(
        ok=False,
        summary="Dispatch could not continue because the worker step failed in the harness.",
        needs_followup=True,
        recoverable=True,
        status=WorkerOutcomeStatus.harness_error.value,
        extras={
            "dispatch_session": True,
            "dispatch_session_failed": True,
            "dispatch_session_step_failed": True,
            "dispatch_session_start_failed": False,
            "dispatch_internal_error": True,
            "error_type": type(exc).__name__,
            "internal_error": redact_secrets(f"{type(exc).__name__}: {exc}"),
        },
    )


def _step_should_stop(
    step_result: StepResult,
    worker_result: WorkerDispatchResult,
    *,
    is_final_step: bool = True,
) -> bool:
    """Return True if this step's outcome should halt the multi-step campaign.

    Stop conditions (any one is sufficient):
    - cancelled: user or harness stopped the Worker
    - phase_boundary: Worker hit its context/tool limit; not safe to continue
    - hard internal/user-owned failure
    - no concrete file progress

    Non-final campaign steps may leave the workspace transiently incomplete.
    If the Worker made concrete file progress, continue to the next campaign
    step instead of making the Planner schedule it. The final step's aggregate
    result decides whether the campaign succeeded.
    """
    if worker_result.cancelled:
        return True
    if worker_result.phase_boundary:
        return True
    if _step_result_is_true_blocker(worker_result):
        return True
    if not step_result.ok and not _nonfinal_step_may_continue(
        worker_result,
        is_final_step=is_final_step,
    ):
        return True
    return False


def _nonfinal_step_may_continue(
    worker_result: WorkerDispatchResult,
    *,
    is_final_step: bool,
) -> bool:
    if is_final_step:
        return False

    extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
    if not _step_made_file_progress(worker_result, extras):
        return False
    return True


def _write_record_is_explicit_file_progress(write: Any) -> bool:
    """Return True only when *write* records explicit concrete file progress.

    A write entry counts as concrete progress when ``applied`` is ``True``
    and ``path`` is a non-empty string.
    """
    if not isinstance(write, dict):
        return False
    if write.get("applied") is not True:
        return False
    path = write.get("path")
    return isinstance(path, str) and bool(path.strip())


def _step_made_file_progress(
    worker_result: WorkerDispatchResult,
    extras: dict[str, Any],
) -> bool:
    if worker_result.modified_files:
        return True
    return bool(_applied_write_paths_from_extras(extras))


def _step_has_file_progress(result: WorkerDispatchResult) -> bool:
    extras = result.extras if isinstance(result.extras, dict) else {}
    return _step_made_file_progress(result, extras)


def _applied_write_paths(result: WorkerDispatchResult) -> list[str]:
    extras = result.extras if isinstance(result.extras, dict) else {}
    return _applied_write_paths_from_extras(extras)


def _applied_write_paths_from_extras(extras: dict[str, Any]) -> list[str]:
    writes = extras.get("writes")
    if isinstance(writes, list):
        return _dedupe([
            str(write.get("path") or "")
            for write in writes
            if _write_record_is_explicit_file_progress(write)
        ])
    return []


def _step_result_is_true_blocker(worker_result: WorkerDispatchResult) -> bool:
    status = str(worker_result.status or "")
    if status in {
        WorkerOutcomeStatus.approval_rejected.value,
        WorkerOutcomeStatus.cancelled.value,
        WorkerOutcomeStatus.harness_error.value,
    }:
        return True

    extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
    return bool(
        extras.get("user_visible_blocker")
        or extras.get("user_only_blocker")
        or extras.get("terminal_environment_blocker")
        or extras.get("worker_internal_error")
        or extras.get("dispatch_internal_error")
        or worker_result.mismatch is not None
    )


def _worker_step_failure_is_recoverable(result: WorkerDispatchResult) -> bool:
    if result.cancelled or result.phase_boundary:
        return False
    status = str(result.status or "")
    if status in {
        WorkerOutcomeStatus.approval_rejected.value,
        WorkerOutcomeStatus.cancelled.value,
    }:
        return False

    extras = result.extras if isinstance(result.extras, dict) else {}
    if any(
        extras.get(key)
        for key in (
            "user_visible_blocker",
            "user_only_blocker",
            "terminal_environment_blocker",
            "worker_internal_error",
            "dispatch_internal_error",
            "environment_setup_blockers",
        )
    ):
        return False

    if status in {
        WorkerOutcomeStatus.validation_failed.value,
        WorkerOutcomeStatus.edit_mechanics_blocked.value,
    }:
        return True

    if result.recoverable or result.needs_followup:
        return True

    failure_class = str(extras.get("failure_class") or "")
    if failure_class in {
        "harness_no_progress",
        "worker_zero_work_no_progress",
        "worker_flow_zero_work_no_progress",
    }:
        return True
    if extras.get("harness_no_progress"):
        return True
    if extras.get("validation_not_run"):
        return True
    if extras.get("validation_results"):
        return True
    if extras.get("verification_progress_stop"):
        return True
    if extras.get("not_applied_writes") or extras.get("unrecovered_not_applied_writes"):
        return True
    return False


def _request_with_attempt_context(
    request: WorkerDispatchRequest,
    *,
    attempts: list[dict[str, Any]],
    attempt_number: int,
) -> WorkerDispatchRequest:
    if attempt_number <= 1 or not attempts:
        return request

    context = _failure_context_stanza(attempts[-1], attempt_number=attempt_number)
    return replace(
        request,
        spec=_append_stanza(request.spec, context),
        risk_notes=[
            *request.risk_notes,
            "Worker persistence retry: use the prior failure context; do not repeat identical edit/tool arguments.",
        ],
    )


def _failure_context_stanza(
    attempt: dict[str, Any],
    *,
    attempt_number: int,
) -> str:
    parts = [
        "",
        "Worker Persistence Context",
        f"This is attempt {attempt_number} for the same bounded step.",
        "The previous attempt failed. Treat this as an observation, not a handoff.",
        "Do not retry identical edit or validation arguments unless the previous failure context has been addressed.",
        "",
        f"Previous attempt: {attempt.get('attempt')}",
        f"Previous status: {attempt.get('status') or 'unknown'}",
        f"Previous summary: {attempt.get('summary') or '(none)'}",
    ]
    validation = str(attempt.get("validation") or "").strip()
    if validation:
        parts.extend(["Previous validation:", validation[:4000]])
    errors = attempt.get("errors")
    if isinstance(errors, list) and errors:
        parts.append("Previous errors:")
        parts.extend(f"- {str(error)[:500]}" for error in errors[:5])
    caveats = attempt.get("caveats")
    if isinstance(caveats, list) and caveats:
        parts.append("Previous caveats:")
        parts.extend(f"- {str(caveat)[:500]}" for caveat in caveats[:5])
    return "\n".join(parts)


def _append_stanza(text: str, stanza: str) -> str:
    base = str(text or "").rstrip()
    if not base:
        return stanza.strip()
    return f"{base}\n\n{stanza.strip()}"


def _attempt_record(attempt_number: int, result: WorkerDispatchResult) -> dict[str, Any]:
    extras = result.extras if isinstance(result.extras, dict) else {}
    errors = extras.get("errors") if isinstance(extras.get("errors"), list) else []
    caveats = extras.get("caveats") if isinstance(extras.get("caveats"), list) else []
    failure_text = _failure_text(result)
    return {
        "attempt": attempt_number,
        "status": result.status,
        "summary": result.summary,
        "validation": result.validation or "",
        "errors": [str(error) for error in errors[:5]],
        "caveats": [str(caveat) for caveat in caveats[:5]],
        "fingerprint": _fingerprint_key(failure_text),
    }


def _failure_text(result: WorkerDispatchResult) -> str:
    extras = result.extras if isinstance(result.extras, dict) else {}
    parts = [
        f"status={result.status or ''}",
        f"summary={result.summary or ''}",
        f"validation={result.validation or ''}",
    ]
    for key in (
        "errors",
        "caveats",
        "validation_results",
        "validation_command_issues",
        "not_applied_writes",
        "unrecovered_not_applied_writes",
        "recoverable_write_failures",
        "harness_no_progress",
        "failure_class",
    ):
        value = extras.get(key)
        if value:
            parts.append(f"{key}={value}")
    return "\n".join(parts)


def _fingerprint_key(text: str) -> str:
    return "|".join(sorted(fingerprint_failures(text)))


def _with_persistence_metadata(
    result: WorkerDispatchResult,
    *,
    attempt_number: int,
    max_attempts: int,
    attempts: list[dict[str, Any]],
    modified_files: list[str],
) -> WorkerDispatchResult:
    extras = dict(result.extras if isinstance(result.extras, dict) else {})
    extras["worker_persistence"] = {
        "attempt": attempt_number,
        "max_attempts": max_attempts,
        "prior_attempts": list(attempts),
    }
    return replace(
        result,
        modified_files=_dedupe([*modified_files, *result.modified_files]),
        extras=extras,
    )


def _partial_progress_persistence_result(
    result: WorkerDispatchResult,
    *,
    attempt_number: int,
    max_attempts: int,
    attempts: list[dict[str, Any]],
    modified_files: list[str],
) -> WorkerDispatchResult:
    extras = dict(result.extras if isinstance(result.extras, dict) else {})
    extras["worker_persistence"] = {
        "stopped_retries": True,
        "reason": "partial_progress_continuation",
        "attempt": attempt_number,
        "max_attempts": max_attempts,
        "prior_attempts": list(attempts),
        "modified_files": list(modified_files),
    }
    extras["partial_progress_continuation"] = True
    return replace(
        result,
        modified_files=_dedupe([*modified_files, *result.modified_files]),
        extras=extras,
    )


def _terminal_persistence_result(
    result: WorkerDispatchResult,
    *,
    reason: str,
    attempt_number: int,
    max_attempts: int,
    attempts: list[dict[str, Any]],
    modified_files: list[str],
    fingerprint: str,
) -> WorkerDispatchResult:
    extras = dict(result.extras if isinstance(result.extras, dict) else {})
    extras["worker_persistence"] = {
        "terminal": True,
        "reason": reason,
        "attempts": attempt_number,
        "max_attempts": max_attempts,
        "no_progress_threshold": NO_PROGRESS_FINGERPRINT_THRESHOLD,
        "fingerprint": fingerprint,
        "attempt_history": list(attempts),
    }
    summary = _terminal_persistence_summary(
        result,
        reason=reason,
        attempt_number=attempt_number,
        attempts=attempts,
        modified_files=modified_files,
    )
    return replace(
        result,
        ok=False,
        recoverable=False,
        needs_followup=False,
        phase_boundary=False,
        status=WorkerOutcomeStatus.harness_error.value,
        summary=summary,
        modified_files=_dedupe([*modified_files, *result.modified_files]),
        extras=extras,
    )


def _terminal_persistence_summary(
    result: WorkerDispatchResult,
    *,
    reason: str,
    attempt_number: int,
    attempts: list[dict[str, Any]],
    modified_files: list[str],
) -> str:
    reason_text = (
        "same failure repeated without progress"
        if reason == "worker_step_no_progress"
        else "attempt budget exhausted"
    )
    lines = [
        result.summary.strip() or "Worker could not complete this step.",
        "",
        (
            "Worker persistence stopped this step after "
            f"{attempt_number} attempt(s): {reason_text}."
        ),
    ]
    if modified_files:
        lines.append("Files changed during attempts: " + ", ".join(modified_files))
    if attempts:
        lines.append("Attempt history:")
        for attempt in attempts:
            status = attempt.get("status") or "unknown"
            summary = str(attempt.get("summary") or "").strip()
            if len(summary) > 220:
                summary = summary[:217] + "..."
            lines.append(f"- Attempt {attempt.get('attempt')}: {status} - {summary}")
    lines.append("This is a terminal harness receipt, not a request for user input.")
    return "\n".join(lines)


def _collect_modified_files(step_results: list[StepResult]) -> list[str]:
    """Dedupe modified files across all step results, preserving first-seen order."""
    seen: set[str] = set()
    files: list[str] = []
    for sr in step_results:
        for path in sr.modified_files:
            p = str(path or "").strip()
            if p and p not in seen:
                files.append(p)
                seen.add(p)
    return files


def _dedupe(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        result.append(item)
        seen.add(item)
    return result


__all__ = [
    "DispatchSession",
    "RunWorkerStep",
]
