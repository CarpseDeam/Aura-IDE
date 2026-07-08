"""Simplified outcome classification for worker dispatch results.

No validation-based signals.  Only hard failures (result errors) and
internal failures (harness/API errors) influence the outcome.
"""
from __future__ import annotations

from typing import Any

from aura.bridge.event_relay import WorkerEventRelay
from aura.conversation import WorkerDispatchRequest, WorkerMismatch
from aura.conversation.worker_outcome import WorkerOutcomeStatus

__all__ = [
    "_classify_worker_completion",
    "_compute_outcome_status",
]


def _classify_worker_completion(
    *,
    relay: WorkerEventRelay,
    completion: dict[str, Any],
    messages: dict[str, Any],
    internal_error: str | None,
    req: WorkerDispatchRequest | None = None,
) -> dict[str, Any]:
    """Classify a Worker completion based only on hard errors and infrastructure.

    No validation-based signals influence the outcome:
    - cancelled / approval_rejected are handled at the dispatch level.
    - result_errors (writes without reads, tool failures) produce a hard failure.
    - api_errors and internal_error produce harness_error.
    - Everything else is completed.
    """
    mismatch = WorkerMismatch.from_dict(completion["continuation"].get("mismatch"))

    has_hard_failure = bool(messages["result_errors"])
    has_internal_failure = bool(
        internal_error
        or relay.api_errors
    )

    if has_hard_failure:
        ok = False
        needs_followup = not has_internal_failure
        recoverable = not has_internal_failure
    elif has_internal_failure:
        ok = False
        needs_followup = False
        recoverable = False
    else:
        ok = True
        needs_followup = False
        recoverable = False

    status = _compute_outcome_status(ok, has_internal_failure)
    return {
        "mismatch": mismatch,
        "phase_boundary": False,
        "summary_continuation": {},
        "status": status,
        "ok": ok,
        "needs_followup": needs_followup,
        "recoverable": recoverable,
    }


def _compute_outcome_status(ok: bool, has_internal_failure: bool) -> str:
    """Map the boolean severity classification to a WorkerOutcomeStatus."""
    if has_internal_failure:
        return WorkerOutcomeStatus.harness_error.value
    if ok:
        return WorkerOutcomeStatus.completed.value
    return WorkerOutcomeStatus.harness_error.value
