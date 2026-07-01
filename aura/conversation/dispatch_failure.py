from __future__ import annotations

from aura.conversation.dispatch import WorkerDispatchResult

__all__ = [
    "classify_failed_worker_dispatch",
]


def classify_failed_worker_dispatch(
    *,
    result: WorkerDispatchResult,
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
    # over synthesised messages.  ToolRunner and DispatchSession set this
    # when they already know the exact constraint for the Planner.
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
