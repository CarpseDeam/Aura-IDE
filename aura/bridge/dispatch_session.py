"""Internal Worker dispatch session orchestration seam.

DispatchSession is the engine boundary between the visible GUI dispatch bridge
and the step-sized Worker execution model. In this foundation pass it runs the
one-step compatibility plan through the existing Worker path so visible behavior
stays unchanged while the architecture gets a real cursor owner.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.dispatch_plan import (
    AggregatedDispatchResult,
    StepResult,
    WorkerDispatchPlan,
    WorkerStepSpec,
    request_for_step,
    todo_tasks_from_plan,
)

RunWorkerStep = Callable[[str, WorkerDispatchRequest, Any], WorkerDispatchResult]


@dataclass
class DispatchStepCursor:
    """Mutable cursor for one visible dispatch campaign."""

    index: int = 0
    completed_step_ids: list[str] = field(default_factory=list)
    blocked_step_id: str | None = None

    @property
    def completed_set(self) -> set[str]:
        return set(self.completed_step_ids)


class DispatchSession:
    """Orchestrates one visible dispatch as one or more Worker steps.

    Product invariant:
    The user expressed intent. That intent is durable until completed,
    cancelled, or truly blocked by a user-only decision.

    Step terminal outcomes allowed by the session model:
    1. completed with concrete write or validation proof,
    2. concrete planner-resolvable blocker,
    3. concrete user-only blocker,
    4. user cancelled/rejected,
    5. tool/environment failure.

    Invalid user-facing outcomes this boundary is meant to absorb in later
    phases: zero-write orientation/thrash, "redispatch narrower" after no work,
    and final receipts with no changed files and no real blocker.
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
    ) -> None:
        self.tool_call_id = tool_call_id
        self.original_request = original_request
        self.plan = plan
        self._run_worker_step = run_worker_step
        self._pending = pending
        self._emit_todo_update = emit_todo_update
        self.cursor = DispatchStepCursor()
        self.step_results: list[StepResult] = []

    def _emit_plan_todos(self, *, active_step_id: str | None = None) -> None:
        if self._emit_todo_update is None:
            return
        tasks = todo_tasks_from_plan(
            self.plan,
            active_step_id=active_step_id,
            completed_step_ids=self.cursor.completed_set,
            blocked_step_id=self.cursor.blocked_step_id,
        )
        self._emit_todo_update(self.tool_call_id, tasks)

    def run(self) -> WorkerDispatchResult:
        """Run the current compatibility session and return one aggregate result."""
        if not self.plan.steps:
            return WorkerDispatchResult(
                ok=False,
                summary="Worker dispatch plan contained no executable steps.",
                status="harness_error",
                recoverable=True,
                extras={
                    "dispatch_session_error": "empty_plan",
                    "planner_resolution_needed": True,
                },
            )

        # Foundation pass: run exactly one compatibility step through today's
        # Worker path. Multi-step sequencing lands on this cursor in the next
        # phase without changing the visible dispatch lifecycle.
        step = self.plan.steps[0]
        self._emit_plan_todos(active_step_id=step.id)
        worker_result = self._run_one_step(step)
        step_result = StepResult.from_worker_result(step.id, worker_result)
        self.step_results.append(step_result)
        if step_result.ok:
            self.cursor.completed_step_ids.append(step.id)
        else:
            self.cursor.blocked_step_id = step.id
        self._emit_plan_todos(active_step_id=None)
        result = self._aggregate_from_worker_result(worker_result)
        self._emit_plan_todos(active_step_id=None)
        return result

    def _run_one_step(self, step: WorkerStepSpec) -> WorkerDispatchResult:
        step_request = request_for_step(self.plan, step, self.original_request)
        return self._run_worker_step(self.tool_call_id, step_request, self._pending)

    def _aggregate_from_worker_result(
        self,
        worker_result: WorkerDispatchResult,
    ) -> WorkerDispatchResult:
        extras = worker_result.extras if isinstance(worker_result.extras, dict) else {}
        aggregate = AggregatedDispatchResult(
            ok=worker_result.ok,
            summary=worker_result.summary,
            status=worker_result.status,
            modified_files=_dedupe(worker_result.modified_files),
            validation=worker_result.validation,
            step_results=list(self.step_results),
            extras={
                **extras,
                "dispatch_session": True,
                "dispatch_plan": self.plan.to_dict(),
                "dispatch_cursor": {
                    "index": self.cursor.index,
                    "completed_step_ids": list(self.cursor.completed_step_ids),
                    "blocked_step_id": self.cursor.blocked_step_id,
                },
            },
        )
        result = aggregate.to_worker_result()
        return WorkerDispatchResult(
            ok=result.ok,
            summary=result.summary,
            cancelled=worker_result.cancelled,
            needs_followup=worker_result.needs_followup,
            phase_boundary=worker_result.phase_boundary,
            followup_reason=worker_result.followup_reason,
            recoverable=worker_result.recoverable,
            status=result.status,
            completed=list(worker_result.completed),
            remaining=list(worker_result.remaining),
            modified_files=list(result.modified_files),
            validation=result.validation,
            suggested_next_spec=worker_result.suggested_next_spec,
            extras=result.extras,
            mismatch=worker_result.mismatch,
        )

    def _resolve_step_blocker_with_planner(
        self,
        *,
        step: WorkerStepSpec,
        result: WorkerDispatchResult,
        changed_files_so_far: list[str],
    ) -> WorkerDispatchPlan | None:
        """Future seam for private Planner clarification.

        A planner-resolvable blocker belongs inside DispatchSession. Later this
        method should ask the Planner for a clarified/split/reordered step before
        surfacing anything user-visible. This foundation pass intentionally keeps
        the seam inert.
        """



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
    "DispatchStepCursor",
    "RunWorkerStep",
]
