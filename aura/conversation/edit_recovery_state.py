"""Helper functions for edit recovery state and detail queries.

Extracted from ConversationManager to keep edit recovery query/state
helpers focused and testable without a manager instance.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.path_utils import normalize_worker_path as _normalize_worker_path


def edit_recovery_details(
    edit_fallback_required: dict[str, dict[str, Any]],
    line_range_reread_required: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    pending = edit_fallback_required or line_range_reread_required
    if not pending:
        return {}
    path = sorted(pending)[0]
    record = pending[path]
    details: dict[str, Any] = {
        "path": path,
        "tool": record.get("tool") or record.get("name") or "",
        "failure_class": record.get("failure_class") or "",
        "error": record.get("error") or "",
    }
    for key in (
        "operation_index",
        "failed_operation",
        "reason",
        "stale",
        "ambiguous",
        "not_found",
        "candidate_count",
        "candidates",
        "suggested_next_tool",
        "suggested_next_action",
    ):
        if key in record:
            details[key] = record[key]
    return details


def default_edit_failure_class(name: str) -> str:
    if name == "edit_symbol":
        return "edit_mechanics_symbol_not_found"
    if name == "edit_line_range":
        return "edit_mechanics_stale_line_range"
    return "edit_mechanics_old_str_not_found"


def worker_file_state_for_path(
    worker_file_state: dict[str, dict[str, Any]],
    path: str,
) -> dict[str, Any] | None:
    normalized_candidates = {
        _normalize_worker_path(path),
        _normalize_worker_path(str(path).lstrip("/\\")),
    }
    for normalized in normalized_candidates:
        state = worker_file_state.get(normalized)
        if state is not None:
            return state
    for existing_path, existing_state in worker_file_state.items():
        if _normalize_worker_path(existing_path) in normalized_candidates:
            return existing_state
    return None


def worker_path_is_existing_file(workspace_root: Path, path: str) -> bool:
    try:
        root = Path(workspace_root).resolve()
        candidate = Path(str(path).lstrip("/\\"))
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        resolved.relative_to(root)
        return resolved.is_file()
    except (OSError, ValueError):
        return False
