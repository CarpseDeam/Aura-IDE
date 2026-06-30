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
    """Record a failed dispatch and decide whether the planner may continue."""
    if _is_quiet_internal_campaign_result(result):
        return {"counts_as_attempt": False, "blocker_reason": ""}
    if _is_worker_internal_error(result):
        return {"counts_as_attempt": False, "blocker_reason": "internal"}

    if not _failed_dispatch_allows_planner_continuation(result):
        return {"counts_as_attempt": False, "blocker_reason": "failed"}

    signature = _worker_dispatch_failure_signature(args, result)
    repeated_count = failures.get(signature, 0) + 1
    failures[signature] = repeated_count

    if repeated_count >= 2:
        return {"counts_as_attempt": True, "blocker_reason": "repeated"}

    if failed_attempts + 1 >= MAX_WORKER_REDISPATCHES_PER_USER_TURN:
        return {"counts_as_attempt": True, "blocker_reason": "limit"}

    return {"counts_as_attempt": True, "blocker_reason": ""}


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
