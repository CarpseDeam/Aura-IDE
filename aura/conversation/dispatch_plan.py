"""Dispatch request validation for the Worker.

Removed: all campaign step models (WorkerDispatchPlan, WorkerStepSpec,
StepResult, AggregatedDispatchResult, plan_from_request, request_for_step,
campaign validation).

Kept: a simple ``validate_dispatch_request`` that validates a single bounded
Worker request has the minimum required fields.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest


@dataclass(frozen=True)
class DispatchValidationResult:
    """Validation result for a single bounded Worker dispatch request."""

    ok: bool
    errors: list[str] = field(default_factory=list)


def validate_dispatch_request(req: WorkerDispatchRequest) -> DispatchValidationResult:
    """Validate a single bounded Worker dispatch request.

    Checks that the request has the minimum required fields for a
    single-item dispatch. This is a lightweight structural validation,
    not a campaign/shape check.
    """
    errors: list[str] = []

    if not req.goal.strip():
        errors.append("Worker dispatch request is missing goal.")
    if not req.spec.strip():
        errors.append("Worker dispatch request is missing spec.")
    if not req.files:
        errors.append("Worker dispatch request must include at least one target file.")
    if not req.acceptance.strip():
        errors.append("Worker dispatch request is missing acceptance.")

    return DispatchValidationResult(
        ok=not errors,
        errors=errors,
    )


__all__ = [
    "DispatchValidationResult",
    "validate_dispatch_request",
]
