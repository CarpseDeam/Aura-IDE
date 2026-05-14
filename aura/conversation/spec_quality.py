"""Fatal checks for Planner -> Worker dispatch requests."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SpecQualityResult:
    ok: bool
    errors: list[str]


def validate_worker_dispatch_spec(
    spec: str,
    acceptance: str,
    *,
    goal: str = "",
) -> SpecQualityResult:
    errors: list[str] = []

    if not goal.strip():
        errors.append("goal is required")
    if not spec.strip():
        errors.append("spec is required")
    if not acceptance.strip():
        errors.append("acceptance is required")

    return SpecQualityResult(ok=not errors, errors=errors)


__all__ = ["SpecQualityResult", "validate_worker_dispatch_spec"]
