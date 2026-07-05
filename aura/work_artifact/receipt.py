"""Convert WorkerDispatchResult into WorkArtifactReceipt.

One Worker run equals one item receipt. No aggregate campaign receipts.
"""
from __future__ import annotations

from typing import Any

from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.dispatch_failure import is_recoverable_worker_continuation
from aura.work_artifact.model import WorkArtifactReceipt


def worker_result_to_receipt(
    result: WorkerDispatchResult,
    *,
    status_override: str | None = None,
) -> WorkArtifactReceipt:
    """Convert a WorkerDispatchResult into a WorkArtifactReceipt.

    Preserves modified files, validation results, mismatch state, errors,
    status, and metadata. Does not create aggregate campaign receipts.
    """
    extras = result.extras if isinstance(result.extras, dict) else {}

    errors: list[str] = []
    raw_errors = extras.get("errors")
    if isinstance(raw_errors, list):
        errors = [str(e) for e in raw_errors]

    mismatch: dict[str, Any] | None = None
    if result.mismatch is not None:
        mismatch = result.mismatch.to_dict()

    # Determine status: explicit override, or derive from result
    if status_override:
        status = status_override
    elif result.ok:
        status = "ok"
    elif result.cancelled:
        status = "cancelled"
    elif result.mismatch is not None:
        status = "mismatch"
    elif is_recoverable_worker_continuation(result):
        status = "continuing"
    elif extras.get("unrecoverable"):
        status = "failed"
    else:
        status = "interrupted"

    return WorkArtifactReceipt(
        status=status,
        summary=result.summary or "",
        modified_files=list(result.modified_files) if result.modified_files else [],
        validation_summary=result.validation or "",
        errors=errors,
        mismatch=mismatch,
        result_status=result.status or "",
        metadata=dict(extras),
    )
