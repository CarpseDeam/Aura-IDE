"""Persistence and metadata recording for completed worker dispatches."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation import History, WorkerDispatchRequest, WorkerDispatchResult, WorkerTaskSpec
from aura.conversation.persistence import WorkerDispatchRecord
from aura.skills.outcome_log import record_outcome_join

__all__ = [
    "_record_worker_completion",
]


def _record_worker_completion(
    *,
    records: list[WorkerDispatchRecord],
    result_metadata: dict[str, dict[str, Any]],
    workspace_root: Path | None,
    worker_model: str,
    tool_call_id: str,
    req: WorkerDispatchRequest,
    task_spec: WorkerTaskSpec,
    worker_history: History,
    summary: str,
    modified_files: list[str],
    continuation: dict[str, Any],
    extras: dict[str, Any],
    status: str,
    structured_failure: dict[str, Any],
    task_shape_summary: dict[str, Any],
    result_errors: list[str],
    context_gearbox: dict[str, Any] | None = None,
    replayable: bool = True,
) -> WorkerDispatchRecord | None:
    """Record a completed worker dispatch.

    Args:
        replayable: Historical name for whether the diagnostic
            WorkerDispatchRecord is appended to *records* and persisted to
            project memory. Now always True since every Worker run is visible
            and reviewable.
    """
    spec_dict = req.to_dict()
    spec_dict["task_spec"] = task_spec.to_dict()
    if replayable:
        spec_dict["replay_kind"] = "worker_dispatch"
        spec_dict["replayable"] = True
    # Include artifact metadata in the record when available.
    if req.artifact_id:
        spec_dict["artifact_id"] = req.artifact_id
        spec_dict["artifact_item_id"] = req.artifact_item_id
        spec_dict["artifact_item_title"] = req.summary
    record = WorkerDispatchRecord(
        after_message_index=-1,
        tool_call_id=tool_call_id,
        spec=spec_dict,
        worker_history=list(worker_history.messages),
        result_summary=summary,
    )
    if replayable:
        records.append(record)

    # Auto-save this dispatch record to project memory (Tier 2).
    if replayable and workspace_root is not None:
        from aura.conversation.persistence import save_dispatch_record_to_memory

        save_dispatch_record_to_memory(record, workspace_root)

    if workspace_root is not None:
        from aura.hazard.capture import record_hazard

        record_hazard(
            workspace_root=workspace_root,
            model=worker_model,
            status=status,
            structured_failure=structured_failure,
            target_files=spec_dict.get("files") or [],
            task_shape=task_shape_summary,
            errors=result_errors,
            tool_call_id=tool_call_id,
        )

        record_outcome_join(
            workspace_root=workspace_root,
            tool_call_id=tool_call_id,
            status=status,
            worker_model=worker_model,
            task_kind=(
                task_shape_summary.get("task_kind")
                if isinstance(task_shape_summary, dict)
                else None
            ),
            target_files=spec_dict.get("files") or [],
            ledger=context_gearbox,
        )

    result_metadata[tool_call_id] = {
        "modified_files": modified_files,
        "validation": continuation.get("validation_text"),
        "extras": extras,
    }
    return record
