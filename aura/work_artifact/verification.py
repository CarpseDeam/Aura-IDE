"""WorkArtifact item verification — simplified completion authority.

After post-Worker validation removal, the only outcomes are:
- cancelled — user cancelled.
- pause — infrastructure/harness/provider failure (detected by explicit
  extras markers only, never by ``status`` alone).
- done — Worker finished (always; no validation evidence required).

``_compute_outcome_status`` in ``worker_outcome_classifier.py`` may
assign ``status=harness_error`` for non-infrastructure reasons (tool
hard failures, edit mechanics, policy blockers).  ``is_infrastructure_-
failure`` relies on extras markers, not ``status``, so those results
classify as ``done`` rather than ``pause``.
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

    Infrastructure is determined by explicit extras markers only, never
    by ``status`` alone.  The Worker outcome classifier may assign
    ``status=harness_error`` for non-infrastructure reasons (tool-level
    hard failures, edit mechanics, read-before-edit findings, terminal
    policy blockers), and those must NOT pause the item loop.
    """
    extras = result.extras if isinstance(result.extras, dict) else {}
    if extras.get("worker_internal_error"):
        return True
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

    ``status=harness_error`` alone does NOT trigger a pause — only
    explicit infrastructure extras markers (``worker_internal_error``,
    ``api_errors``, or infrastructure-class ``failure_class``) do.
    """
    if result.cancelled:
        return WorkArtifactAttemptOutcome.cancelled
    if is_infrastructure_failure(result):
        return WorkArtifactAttemptOutcome.pause
    # Worker finished — mark done regardless of validation evidence.
    return WorkArtifactAttemptOutcome.done
