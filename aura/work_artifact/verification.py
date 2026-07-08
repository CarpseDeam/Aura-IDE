"""WorkArtifact item verification — simplified completion authority.

After post-Worker validation removal, the only outcomes are:
- cancelled — user cancelled.
- pause — infrastructure/harness/provider failure.
- done — Worker finished (always; no validation evidence required).
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult

_log = logging.getLogger(__name__)

__all__ = [
    "WorkArtifactAttemptOutcome",
    "classify_item_attempt",
    "is_infrastructure_failure",
]


# ── Outcome enum ──────────────────────────────────────────────────────────────────


class WorkArtifactAttemptOutcome(Enum):
    """Classification of a single WorkArtifact item attempt."""

    done = "done"
    pause = "pause"
    cancelled = "cancelled"


# ── Infrastructure failure detection ─────────────────────────────────────────────


def is_infrastructure_failure(result: WorkerDispatchResult) -> bool:
    """True for harness/provider/auth/network failures.

    These pause the job rather than marking the item done, and the job
    can be resumed later when the infrastructure is healthy.
    """
    from aura.conversation.dispatch import WorkerOutcomeStatus

    if result.status == WorkerOutcomeStatus.harness_error.value:
        return True
    extras = result.extras if isinstance(result.extras, dict) else {}
    if extras.get("api_errors"):
        return True
    fc = str(extras.get("failure_class", "") or "")
    if any(marker in fc for marker in ("provider", "network", "auth", "api_error", "unavailable")):
        return True
    return False


# ── Attempt classification ───────────────────────────────────────────────────────


def classify_item_attempt(
    item_req: WorkerDispatchRequest,
    result: WorkerDispatchResult,
) -> WorkArtifactAttemptOutcome:
    """Classify a single WorkArtifact item attempt.

    No post-Worker validation required.  The Worker's own tool-loop
    validation is sufficient for completion.
    """
    if result.cancelled:
        return WorkArtifactAttemptOutcome.cancelled
    if is_infrastructure_failure(result):
        return WorkArtifactAttemptOutcome.pause
    # Worker finished — mark done regardless of validation evidence.
    return WorkArtifactAttemptOutcome.done
