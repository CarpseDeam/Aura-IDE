from __future__ import annotations

import json
from typing import Any

from aura.conversation.dispatch import (
    WorkerDispatchResult,
    WorkerOutcomeStatus,
    infer_outcome_status,
)
from aura.conversation.tool_limits import MAX_WORKER_REDISPATCHES_PER_USER_TURN

__all__ = ["classify_failed_worker_dispatch"]


def classify_failed_worker_dispatch(
    *,
    args: dict[str, Any],
    result: WorkerDispatchResult,
    failures: dict[str, int],
    failed_attempts: int,
) -> dict[str, Any]:
    """Record a failed dispatch and decide whether the planner may continue.

    Returns a dict with keys:
      counts_as_attempt  — bool
      blocker_reason     — str ("", "internal", "failed", "repeated", "limit")
      failure_constraint — str; non-empty when the planner should see a specific
                           constraint on the next attempt. Empty when nothing
                           specific is extractable.
    """
    if _is_quiet_internal_campaign_result(result):
        return {"counts_as_attempt": False, "blocker_reason": "", "failure_constraint": ""}
    if _is_worker_internal_error(result):
        return {"counts_as_attempt": False, "blocker_reason": "internal", "failure_constraint": ""}

    if not _failed_dispatch_allows_planner_continuation(result):
        return {"counts_as_attempt": False, "blocker_reason": "failed", "failure_constraint": ""}

    signature = _worker_dispatch_failure_signature(args, result)
    repeated_count = failures.get(signature, 0) + 1
    failures[signature] = repeated_count

    if repeated_count >= 2:
        return {
            "counts_as_attempt": True,
            "blocker_reason": "repeated",
            "failure_constraint": (
                "CONSTRAINT FOR NEXT ATTEMPT: Do not repeat this approach. "
                "It has already been attempted and failed with the same plan. "
                "Try a fundamentally different strategy."
            ),
        }

    failure_constraint = _compute_failure_constraint(result)

    if failed_attempts + 1 >= MAX_WORKER_REDISPATCHES_PER_USER_TURN:
        return {
            "counts_as_attempt": True,
            "blocker_reason": "limit",
            "failure_constraint": failure_constraint,
        }

    return {
        "counts_as_attempt": True,
        "blocker_reason": "",
        "failure_constraint": failure_constraint,
    }


def _compute_failure_constraint(result: WorkerDispatchResult) -> str:
    """Extract a specific failure constraint from a dispatch result.

    Returns a short, marked directive the planner must obey on the next
    attempt, or an empty string when nothing specific can be extracted.
    """
    extras = result.extras or {}

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

    # planner_resolution_needed / mismatch: use the resolution/mismatch text
    if extras.get("planner_resolution_needed") or result.mismatch is not None:
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
        if extras.get("planner_resolution_needed"):
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

    return ""


def _failed_dispatch_allows_planner_continuation(
    result: WorkerDispatchResult,
) -> bool:
    if result.ok or result.cancelled:
        return False
    if result.extras.get("dispatch_spec_rejected"):
        return True
    if result.mismatch is not None:
        return True
    if result.extras.get("planner_resolution_needed"):
        return True
    if infer_outcome_status(result) == WorkerOutcomeStatus.needs_planner_resolution.value:
        return True
    return bool(result.needs_followup or result.recoverable or result.phase_boundary)


def _is_worker_internal_error(result: WorkerDispatchResult) -> bool:
    if result.extras.get("internal_campaign_continuation"):
        return False
    return bool(
        result.extras.get("worker_internal_error")
        or result.extras.get("dispatch_internal_error")
    )


def _is_quiet_internal_campaign_result(result: WorkerDispatchResult) -> bool:
    return bool(
        result.extras.get("internal_campaign_continuation")
        and result.extras.get("suppress_user_followup_card")
        and not result.extras.get("user_visible_blocker")
        and not result.extras.get("user_only_blocker")
    )


def _worker_dispatch_failure_signature(
    args: dict[str, Any],
    result: WorkerDispatchResult,
) -> str:
    spec = {
        "goal": str(args.get("goal", "")),
        "files": [str(item) for item in args.get("files", [])]
        if isinstance(args.get("files"), list)
        else [],
        "spec": str(args.get("spec", "")),
        "acceptance": str(args.get("acceptance", "")),
        "summary": str(args.get("summary", "")),
    }
    payload = {
        "spec": spec,
        "error": _worker_dispatch_error_signature(result),
    }
    return json.dumps(payload, sort_keys=True, ensure_ascii=False)


def _worker_dispatch_error_signature(result: WorkerDispatchResult) -> str:
    extras = result.extras or {}
    if extras.get("dispatch_spec_rejected"):
        errors = extras.get("quality_errors")
        if isinstance(errors, list):
            return "dispatch_spec_rejected:" + "|".join(str(e) for e in errors)
        return "dispatch_spec_rejected"
    if extras.get("worker_internal_error"):
        return "worker_internal_error"

    parts: list[str] = []
    if result.followup_reason:
        parts.append(f"reason:{result.followup_reason}")
    for key in ("errors", "caveats"):
        values = extras.get(key)
        if isinstance(values, list) and values:
            parts.append(
                f"{key}:"
                + "|".join(
                    " ".join(str(value).split())[:160] for value in values[:3]
                )
            )
    if result.needs_followup:
        parts.append("needs_followup")
    if result.recoverable:
        parts.append("recoverable")
    if result.phase_boundary:
        parts.append("phase_boundary")
    if result.mismatch is not None:
        parts.extend([
            result.mismatch.kind,
            result.mismatch.requested,
            result.mismatch.observed,
            result.mismatch.question_for_planner,
        ])
    if not parts:
        parts.append(" ".join(result.summary.split())[:240])
    return ";".join(parts)
