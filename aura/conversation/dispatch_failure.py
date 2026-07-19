from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.dispatch import WorkerDispatchResult

MAX_PLANNER_DISPATCH_RECOVERY_FAILURES = 3
PLANNER_DISPATCH_RECOVERY_EXHAUSTED_REASON = (
    "Planner dispatch recovery exhausted after {attempts} failed attempts; "
    "Worker was not started. Final failure: {failure}"
)

__all__ = [
    "MAX_PLANNER_DISPATCH_RECOVERY_FAILURES",
    "PLANNER_DISPATCH_RECOVERY_EXHAUSTED_REASON",
    "PlannerDispatchRecovery",
    "classify_failed_worker_dispatch",
    "is_recoverable_worker_continuation",
    "is_recoverable_pre_worker_failure",
]


@dataclass
class PlannerDispatchRecovery:
    """Canonical per-turn owner of Planner dispatch recovery state.

    Attempts are telemetry.  Only ``accepted`` means a dispatch crossed the
    pre-Worker boundary, so a rejected attempt can never satisfy completion.
    """

    attempts: int = 0
    accepted: int = 0
    failed_attempts: int = 0
    last_failure_payload: dict[str, Any] = field(default_factory=dict)

    def begin_attempt(self) -> int:
        self.attempts += 1
        return self.attempts

    def mark_accepted(self) -> None:
        self.accepted += 1
        self.last_failure_payload = {}

    def record_pre_worker_failure(self, result: WorkerDispatchResult) -> None:
        self.failed_attempts += 1
        self.last_failure_payload = result.to_tool_payload()

    @property
    def recovery_required(self) -> bool:
        return bool(self.failed_attempts and not self.accepted and not self.exhausted)

    @property
    def exhausted(self) -> bool:
        return self.failed_attempts >= MAX_PLANNER_DISPATCH_RECOVERY_FAILURES

    def corrective_message(self) -> str:
        payload = json.dumps(self.last_failure_payload, ensure_ascii=False, sort_keys=True)
        return "\n".join(
            [
                "INTERNAL PLANNER DISPATCH RECOVERY:",
                "The attempted dispatch was not accepted and no Worker started.",
                f"Exact structured failure: {payload}",
                "Correct the dispatch_to_worker arguments and call dispatch_to_worker again now.",
                "Do not answer with an empty response or implementation narration.",
            ]
        )

    def exhaustion_reason(self) -> str:
        extras = self.last_failure_payload.get("extras")
        extras = extras if isinstance(extras, dict) else {}
        quality_errors = extras.get("quality_errors")
        exact_failure = (
            str(quality_errors[0])
            if isinstance(quality_errors, list) and quality_errors
            else ""
        )
        failure = str(
            exact_failure
            or self.last_failure_payload.get("summary")
            or self.last_failure_payload.get("error")
            or self.last_failure_payload
            or "unknown dispatch failure"
        )
        return PLANNER_DISPATCH_RECOVERY_EXHAUSTED_REASON.format(
            attempts=self.failed_attempts,
            failure=failure,
        )


def classify_failed_worker_dispatch(
    *,
    result: WorkerDispatchResult,
    **_legacy: object,
) -> dict[str, str]:
    """Build the terminal failure metadata for a failed dispatch.

    Returns a dict with keys:
      blocker_reason     — str ("internal" or "failed")
      failure_constraint — str; non-empty when the planner should see a specific
                           constraint in the terminal receipt. Empty when nothing
                           specific is extractable.
    """
    if _is_worker_internal_error(result):
        return {"blocker_reason": "internal", "failure_constraint": ""}
    if is_recoverable_pre_worker_failure(result):
        return {
            "blocker_reason": "",
            "failure_constraint": _compute_failure_constraint(result),
        }
    if is_recoverable_worker_continuation(result):
        return {"blocker_reason": "", "failure_constraint": ""}
    if result.extras.get("internal_planner_handoff"):
        return {
            "blocker_reason": "",
            "failure_constraint": _compute_failure_constraint(result),
        }

    return {
        "blocker_reason": "failed",
        "failure_constraint": _compute_failure_constraint(result),
    }


def _compute_failure_constraint(result: WorkerDispatchResult) -> str:
    """Extract a specific failure constraint from a dispatch result.

    Returns a short, marked directive the planner must obey on the next
    attempt, or an empty string when nothing specific can be extracted.
    """
    extras = result.extras or {}

    # Passthrough: a pre-computed failure_constraint in extras always wins
    # over synthesised messages.  ToolRunner sets this
    # when it already knows the exact constraint for the Planner.
    if extras.get("failure_constraint"):
        return str(extras["failure_constraint"])

    # composition_failure: name the failing validation command and modified files
    if extras.get("composition_failure"):
        parts = []
        if result.validation:
            parts.append(f"validation: {result.validation}")
        if result.modified_files:
            parts.append(f"files: {', '.join(result.modified_files)}")
        if parts:
            return (
                "CONSTRAINT FOR NEXT ATTEMPT: This attempt failed composition "
                "verification: " + "; ".join(parts)
            )
        return "CONSTRAINT FOR NEXT ATTEMPT: This attempt failed composition verification."

    # mismatch_detected / mismatch: use the resolution/mismatch text
    if extras.get("mismatch_detected") or result.mismatch is not None:
        if result.mismatch is not None:
            texts = [
                p
                for p in (
                    result.mismatch.observed,
                    result.mismatch.question_for_planner,
                )
                if p
            ]
            if texts:
                return "CONSTRAINT FOR NEXT ATTEMPT: " + " ".join(texts)
        if extras.get("mismatch_detected"):
            return "CONSTRAINT FOR NEXT ATTEMPT: The plan needs revision before retry."
        return ""

    # plain validation failure: distill failing items from result.validation
    if result.validation:
        validation_text = str(result.validation).strip()
        if validation_text:
            return (
                "CONSTRAINT FOR NEXT ATTEMPT: Previous attempt failed validation: "
                + validation_text
            )

    # dispatch_spec_rejected without a richer mismatch / validation signal.
    if extras.get("dispatch_spec_rejected"):
        quality_errors = extras.get("quality_errors")
        if isinstance(quality_errors, list) and quality_errors:
            errors_text = "; ".join(str(e) for e in quality_errors[:5])
            return (
                "CONSTRAINT FOR NEXT ATTEMPT: Plan was rejected: " + errors_text
            )
        return (
            "CONSTRAINT FOR NEXT ATTEMPT: Plan was rejected before dispatch. "
            "Revise the plan to address quality requirements before retry."
        )

    return ""


def _is_worker_internal_error(result: WorkerDispatchResult) -> bool:
    return bool(
        result.extras.get("worker_internal_error")
        or result.extras.get("dispatch_internal_error")
    )


def is_recoverable_worker_continuation(result: WorkerDispatchResult) -> bool:
    """Return True for non-terminal Worker findings that should loop back."""
    extras = result.extras if isinstance(result.extras, dict) else {}
    phase_info = extras.get("phase_boundary")
    phase_payload = phase_info if isinstance(phase_info, dict) else {}
    details = extras.get("details")
    detail_payload = details if isinstance(details, dict) else {}
    suggested_next_tool = str(
        extras.get("suggested_next_tool")
        or phase_payload.get("suggested_next_tool")
        or detail_payload.get("suggested_next_tool")
        or ""
    )
    return bool(
        not result.cancelled
        and (result.recoverable or extras.get("recoverable"))
        and (result.phase_boundary or extras.get("phase_boundary"))
        and suggested_next_tool == "dispatch_to_worker"
    )


def is_recoverable_pre_worker_failure(result: WorkerDispatchResult) -> bool:
    """Return whether Planner can repair a failure before any Worker started."""
    extras = result.extras if isinstance(result.extras, dict) else {}
    status = str(result.status or extras.get("status") or "")
    return bool(
        not result.cancelled
        and extras.get("dispatch_not_started")
        and (result.recoverable or extras.get("recoverable"))
        and status != "approval_rejected"
        and not extras.get("approval_rejected")
        and not extras.get("terminal_environment_blocker")
        and not extras.get("worker_internal_error")
        and not extras.get("dispatch_internal_error")
    )
