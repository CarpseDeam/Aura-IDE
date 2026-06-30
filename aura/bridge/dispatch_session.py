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

TODO rail:
- All steps start as pending via todo_controller.begin().
- The active step becomes active while it runs (todo_controller.set_active).
- Completed steps become done before the next step activates.
- A blocked/failed step shows as active+blocked in the final emission.
- One final TODO emission happens after the loop ends.
- Worker-local TODO updates are blocked by the controller.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerOutcomeStatus,
)
from aura.conversation.dispatch_plan import (
    StepResult,
    WorkerDispatchPlan,
    WorkerStepSpec,
    compact_todo_label,
    request_for_campaign_validation,
    request_for_step,
)

if TYPE_CHECKING:
    from aura.bridge.todo_controller import DispatchTodoController

RunWorkerStep = Callable[[str, WorkerDispatchRequest, Any], WorkerDispatchResult]
CAMPAIGN_RECOVERY_BUDGET_PER_STEP = 1

INTERNAL_RECOVERABLE_ERROR = "internal_recoverable_error"
PLANNER_RESOLUTION_NEEDED = "planner_resolution_needed"
USER_VISIBLE_BLOCKER = "user_visible_blocker"
USER_ONLY_BLOCKER = "user_only_blocker"
TERMINAL_ENVIRONMENT_BLOCKER = "terminal_environment_blocker"
NO_RECOVERY_NEEDED = "none"

# Callback types for the outer campaign lifecycle signals.
# Signatures match the Qt signals on _DispatchProxy:
#   workerStarted  → (tool_call_id: str)
#   workerFinished → (tool_call_id: str, ok: bool, summary: str, needs_followup: bool, status: str)
_EmitStarted = Callable[[str], None]
_EmitFinished = Callable[[str, bool, str, bool, str], None]


@dataclass
class DispatchStepCursor:
    """Mutable cursor tracking progress across one visible dispatch campaign."""

    index: int = 0
    completed_step_ids: list[str] = field(default_factory=list)
    blocked_step_id: str | None = None

    @property
    def completed_set(self) -> set[str]:
        return set(self.completed_step_ids)


class DispatchSession:
    """Orchestrates one visible dispatch as one or more sequential Worker steps.

    Product invariant:
    The user expressed intent. That intent is durable until completed,
    cancelled, or truly blocked by a user-only decision.

    Visible lifecycle:
    - workerStarted fires once, before the first step.
    - workerFinished fires once, after the last/blocking step, with the
      aggregate campaign outcome.
    - Internal steps execute silently inside _run_worker_step without
      re-emitting started/finished to the UI.

    TODO rail:
    - All steps start as pending.
    - The active step becomes active while it runs.
    - Completed steps become done before the next step activates.
    - A blocked/failed step shows as active+blocked in the final emission.
    - One final TODO emission happens after the loop ends.
    - Worker-local TODO updates from inside _run_worker_step are blocked for
      canonical dispatch tool_call_ids by _DispatchProxy._relay_worker_todo_update.
    """

    def __init__(
        self,
        *,
        tool_call_id: str,
        original_request: WorkerDispatchRequest,
        plan: WorkerDispatchPlan,
        run_worker_step: RunWorkerStep,
        pending: Any,
        emit_todo_update: Callable[[str, list[dict[str, Any]]], None] | None = None,
        emit_worker_started: _EmitStarted | None = None,
        emit_worker_finished: _EmitFinished | None = None,
        todo_controller: DispatchTodoController | None = None,
    ) -> None:
        self.tool_call_id = tool_call_id
        self.original_request = original_request
        self.plan = plan
        self._run_worker_step = run_worker_step
        self._pending = pending
        self._emit_todo_update = emit_todo_update
        self._emit_worker_started = emit_worker_started
        self._emit_worker_finished = emit_worker_finished
        self._todo_controller = todo_controller
        self.cursor = DispatchStepCursor()
        self.step_results: list[StepResult] = []

    # ------------------------------------------------------------------
    # TODO emission
    # ------------------------------------------------------------------

    def _begin_canonical_todos(self) -> None:
        """Initialize canonical TODO objectives from the plan."""
        if self._todo_controller is None:
            return
        objectives: list[dict[str, Any]] = []
        for step in self.plan.steps:
            raw_label = step.title or step.goal or ""
            description = compact_todo_label(raw_label, fallback=step.id or "Worker step")
            objectives.append({
                "id": step.id,
                "description": description,
                "files": list(step.files),
            })
        self._todo_controller.begin(self.tool_call_id, objectives)

    def _emit_canonical_snapshot(self) -> None:
        """Emit the current canonical snapshot to the GUI."""
        if self._emit_todo_update is None:
            return
        if self._todo_controller is None:
            return
        tasks = self._todo_controller.snapshot(self.tool_call_id)
        self._emit_todo_update(self.tool_call_id, tasks)

    def _canonical_set_active(self, step_id: str) -> None:
        if self._todo_controller is None:
            return
        self._todo_controller.set_active(self.tool_call_id, step_id)
        self._emit_canonical_snapshot()

    def _canonical_mark_done(self, step_id: str) -> None:
        if self._todo_controller is None:
            return
        self._todo_controller.mark_done(self.tool_call_id, step_id)
        self._emit_canonical_snapshot()

    def _canonical_mark_blocked(self, step_id: str) -> None:
        if self._todo_controller is None:
            return
        self._todo_controller.mark_blocked(self.tool_call_id, step_id)
        self._emit_canonical_snapshot()

    def _canonical_finish(self) -> None:
        if self._todo_controller is None:
            return
        self._todo_controller.finish(self.tool_call_id)
        self._emit_canonical_snapshot()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(self) -> WorkerDispatchResult:
        """Execute every plan step in order and return the aggregate result.

        workerStarted is emitted once before the first step.
        workerFinished is emitted once after the last/blocking step with the
        aggregate ok/summary/needs_followup/status — never per internal step.

        Steps run sequentially under the same tool_call_id. The first step that
        triggers _step_should_stop halts the campaign; that step's id is recorded
        as blocked_step_id. Steps completed before the halt contribute their
        modified files to the aggregate and appear as done in the final TODO state.
        """
        if not self.plan.steps:
            result = WorkerDispatchResult(
                ok=False,
                summary=(
                    "Aura paused this campaign without needing user action."
                ),
                status=WorkerOutcomeStatus.needs_followup.value,
                needs_followup=True,
                recoverable=True,
                extras={
                    "dispatch_session": True,
                    "dispatch_session_error": "empty_plan",
                    "planner_resolution_needed": True,
                    "campaign_recovery_classification": PLANNER_RESOLUTION_NEEDED,
                    "internal_campaign_continuation": True,
                    "suppress_user_followup_card": True,
                    "user_visible_blocker": False,
                },
            )
            self._emit_lifecycle_pair(result)
            return result

        # Initialize canonical TODO state: all steps pending.
        self._begin_canonical_todos()
        self._emit_canonical_snapshot()

        # Campaign starts — one visible Worker start event for the whole run.
        if self._emit_worker_started is not None:
            self._emit_worker_started(self.tool_call_id)

        final_worker_result: WorkerDispatchResult | None = None

        for i, step in enumerate(self.plan.steps):
            self.cursor.index = i

            # Activate this step in the TODO rail.
            self._canonical_set_active(step.id)

            worker_result = self._run_one_step(step)
            final_worker_result = worker_result

            step_result = _step_result_for(step, worker_result)
            self.step_results.append(step_result)

            if _step_should_stop(step_result, worker_result):
                # Record as blocked; final TODO emit below shows active+blocked.
                self.cursor.blocked_step_id = step.id
                self._canonical_mark_blocked(step.id)
                break

            # Step completed — advance cursor; mark done.
            self.cursor.completed_step_ids.append(step.id)
            self._canonical_mark_done(step.id)

        # Emit final TODO state once: completed=done, blocked=active+blocked, rest=pending.
        self._canonical_finish()

        if final_worker_result is None:
            # Guard — can't happen: plan.steps was verified non-empty above.
            result = WorkerDispatchResult(
                ok=False,
                summary=(
                    "Aura paused this campaign before completion. "
                    "No user action is required."
                ),
                status=WorkerOutcomeStatus.needs_followup.value,
                needs_followup=True,
                recoverable=True,
                extras={
                    "dispatch_session": True,
                    "dispatch_session_error": "no_steps_executed",
                    "planner_resolution_needed": True,
                    "campaign_recovery_classification": PLANNER_RESOLUTION_NEEDED,
                    "internal_campaign_continuation": True,
                    "suppress_user_followup_card": True,
                    "user_visible_blocker": False,
                },
            )
            self._emit_lifecycle_finished(result)
            return result

        # ------------------------------------------------------------------
        # Campaign composition-validation gate (all-green path only)
        # ------------------------------------------------------------------
        if self.cursor.blocked_step_id is None:
            commands = self.plan.campaign_validation_commands()
            if commands:
                modified_files_union = _collect_modified_files(self.step_results)
                validation_request = request_for_campaign_validation(
                    self.plan,
                    self.original_request,
                    commands,
                    modified_files_union,
                )
                validation_result = self._run_worker_step(
                    self.tool_call_id,
                    validation_request,
                    self._pending,
                )
                if not validation_result.ok:
                    self.cursor.blocked_step_id = "campaign-validation"
                    validation_result.extras["composition_failure"] = True
                    validation_result.extras["planner_resolution_needed"] = True
                    final_worker_result = validation_result

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
            self._emit_worker_started(self.tool_call_id)
        self._emit_lifecycle_finished(result)

    def _emit_lifecycle_finished(self, result: WorkerDispatchResult) -> None:
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
        return self._run_worker_step(self.tool_call_id, step_request, self._pending)

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
        is_explicit_campaign = bool(self.original_request.steps)
        session_metadata = _session_metadata(
            self.plan,
            self.cursor,
            self.step_results,
            is_explicit_campaign=is_explicit_campaign,
            worker_result=worker_result,
        )
        visible_result = _visible_campaign_result(
            worker_result,
            session_metadata,
            is_explicit_campaign=is_explicit_campaign,
        )
        return WorkerDispatchResult(
            ok=visible_result["ok"],
            summary=visible_result["summary"],
            cancelled=worker_result.cancelled,
            needs_followup=visible_result["needs_followup"],
            phase_boundary=worker_result.phase_boundary,
            followup_reason=worker_result.followup_reason,
            recoverable=visible_result["recoverable"],
            status=visible_result["status"],
            completed=list(worker_result.completed),
            remaining=list(worker_result.remaining),
            modified_files=modified_files,
            validation=worker_result.validation,
            suggested_next_spec=worker_result.suggested_next_spec,
            extras={
                **worker_extras,
                **session_metadata,
            },
            mismatch=worker_result.mismatch,
        )

    # ------------------------------------------------------------------
    # Future seams (inert)
    # ------------------------------------------------------------------

    def _resolve_step_blocker_with_planner(
        self,
        *,
        step: WorkerStepSpec,
        result: WorkerDispatchResult,
        changed_files_so_far: list[str],
    ) -> WorkerDispatchPlan | None:
        """Phase 3E+ seam: ask the Planner to clarify/split/reorder before surfacing a user blocker."""
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _step_result_for(step: WorkerStepSpec, worker_result: WorkerDispatchResult) -> StepResult:
    """Convert a WorkerDispatchResult to a StepResult for the given step."""
    return StepResult.from_worker_result(step.id, worker_result)


def _step_should_stop(
    step_result: StepResult,
    worker_result: WorkerDispatchResult,
) -> bool:
    """Return True if this step's outcome should halt the multi-step campaign.

    Stop conditions (any one is sufficient):
    - cancelled: user or harness stopped the Worker
    - not ok: any failure classification (validation, edit mechanics, no-progress,
      harness error, unverified acceptance, recoverable blocker, etc.)
    - phase_boundary: Worker hit its context/tool limit; not safe to continue
    - mismatch: Worker surfaced a planner-resolution request; stop so the
      Planner can update the plan before retrying
    """
    if worker_result.cancelled:
        return True
    if not step_result.ok:
        return True
    if worker_result.phase_boundary:
        return True
    if worker_result.mismatch is not None:
        return True
    return False


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


def _session_metadata(
    plan: WorkerDispatchPlan,
    cursor: DispatchStepCursor,
    step_results: list[StepResult],
    *,
    is_explicit_campaign: bool,
    worker_result: WorkerDispatchResult,
) -> dict[str, Any]:
    """Build session-level extras for the aggregate result."""
    recovery_classification = _campaign_recovery_classification(worker_result, step_results)
    user_visible_blocker = recovery_classification in {
        USER_VISIBLE_BLOCKER,
        USER_ONLY_BLOCKER,
        TERMINAL_ENVIRONMENT_BLOCKER,
    }
    internal_campaign_continuation = bool(
        is_explicit_campaign
        and not user_visible_blocker
        and recovery_classification
        in {INTERNAL_RECOVERABLE_ERROR, PLANNER_RESOLUTION_NEEDED}
    )
    recovery_attempts = _campaign_recovery_attempts(cursor, recovery_classification)
    recovery_budget_exhausted = bool(
        recovery_attempts
        and max(recovery_attempts.values()) >= CAMPAIGN_RECOVERY_BUDGET_PER_STEP
    )
    return {
        "dispatch_session": True,
        "dispatch_plan": plan.to_dict(),
        "dispatch_cursor": {
            "index": cursor.index,
            "completed_step_ids": list(cursor.completed_step_ids),
            "blocked_step_id": cursor.blocked_step_id,
        },
        "dispatch_step_results": [sr.to_dict() for sr in step_results],
        "campaign_recovery_classification": recovery_classification,
        "campaign_recovery_budget": CAMPAIGN_RECOVERY_BUDGET_PER_STEP,
        "campaign_recovery_attempts": recovery_attempts,
        "campaign_recovery_budget_exhausted": recovery_budget_exhausted,
        "internal_campaign_continuation": internal_campaign_continuation,
        "user_visible_blocker": user_visible_blocker,
        "user_only_blocker": recovery_classification == USER_ONLY_BLOCKER,
        "terminal_environment_blocker": recovery_classification == TERMINAL_ENVIRONMENT_BLOCKER,
        "suppress_user_followup_card": internal_campaign_continuation,
    }


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


def _campaign_recovery_classification(
    worker_result: WorkerDispatchResult,
    step_results: list[StepResult],
) -> str:
    extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
    status = worker_result.status or ""
    failure_class = str(extras.get("failure_class") or "")
    if worker_result.cancelled:
        return USER_VISIBLE_BLOCKER
    if status == "approval_rejected" or failure_class == "approval_rejected":
        return USER_VISIBLE_BLOCKER
    if extras.get("user_only_blocker"):
        return USER_ONLY_BLOCKER
    if _is_terminal_environment_blocker(extras, failure_class):
        return TERMINAL_ENVIRONMENT_BLOCKER
    if extras.get("user_visible_blocker"):
        return USER_VISIBLE_BLOCKER
    if any(step.user_only_blocker for step in step_results):
        return USER_ONLY_BLOCKER
    if _is_internal_recoverable_error(worker_result):
        return INTERNAL_RECOVERABLE_ERROR
    if (
        worker_result.needs_followup
        or worker_result.phase_boundary
        or worker_result.recoverable
        or worker_result.mismatch is not None
        or extras.get("planner_resolution_needed")
        or any(step.needs_planner_resolution for step in step_results)
    ):
        return PLANNER_RESOLUTION_NEEDED
    if not worker_result.ok and not (
        worker_result.recoverable
        or worker_result.needs_followup
        or worker_result.phase_boundary
        or worker_result.mismatch is not None
    ):
        return PLANNER_RESOLUTION_NEEDED
    return NO_RECOVERY_NEEDED


def _is_internal_recoverable_error(worker_result: WorkerDispatchResult) -> bool:
    extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
    status = worker_result.status or ""
    failure_class = str(extras.get("failure_class") or "")
    if status == WorkerOutcomeStatus.harness_error.value:
        return True
    if extras.get("worker_internal_error") or extras.get("internal_error"):
        return True
    if failure_class in {
        "harness_error",
        "harness_no_progress",
        "internal_error",
        "worker_flow_thrash",
        "worker_flow_zero_work_no_progress",
        "worker_internal_error",
    }:
        return True
    return False


def _is_terminal_environment_blocker(extras: dict[str, Any], failure_class: str) -> bool:
    if extras.get("terminal_environment_blocker"):
        return True
    if failure_class.startswith("project_environment_missing_"):
        return True
    blockers = extras.get("environment_setup_blockers")
    return isinstance(blockers, list) and bool(blockers)


def _campaign_recovery_attempts(
    cursor: DispatchStepCursor,
    recovery_classification: str,
) -> dict[str, int]:
    if recovery_classification in {NO_RECOVERY_NEEDED, USER_VISIBLE_BLOCKER, USER_ONLY_BLOCKER, TERMINAL_ENVIRONMENT_BLOCKER}:
        return {}
    step_id = cursor.blocked_step_id
    return {step_id: 1} if step_id else {}


def _visible_campaign_result(
    worker_result: WorkerDispatchResult,
    session_metadata: dict[str, Any],
    *,
    is_explicit_campaign: bool,
) -> dict[str, Any]:
    if not is_explicit_campaign or not session_metadata.get("internal_campaign_continuation"):
        return {
            "ok": worker_result.ok,
            "summary": worker_result.summary,
            "needs_followup": worker_result.needs_followup,
            "recoverable": worker_result.recoverable,
            "status": worker_result.status,
        }

    blocked_step = session_metadata.get("dispatch_cursor", {}).get("blocked_step_id")
    classification = str(session_metadata.get("campaign_recovery_classification") or "")
    summary = _calm_campaign_recovery_summary(blocked_step, classification)
    return {
        "ok": False,
        "summary": summary,
        "needs_followup": True,
        "recoverable": True,
        "status": WorkerOutcomeStatus.needs_followup.value,
    }


def _calm_campaign_recovery_summary(blocked_step: Any, classification: str) -> str:
    step_text = f" at {blocked_step}" if blocked_step else ""
    if classification == INTERNAL_RECOVERABLE_ERROR:
        return (
            f"Aura paused this campaign{step_text} without needing user action."
        )
    return (
        f"Aura paused this campaign{step_text} before completion. "
        "No user action is required."
    )


__all__ = [
    "DispatchSession",
    "DispatchStepCursor",
    "RunWorkerStep",
]
