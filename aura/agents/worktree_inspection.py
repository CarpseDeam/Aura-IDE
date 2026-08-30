"""Compact, bounded observation of retained writable Agent work."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable

from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_material import ChangeSetMaterializer
from aura.agents.worktree_models import AgentChangeSet
from aura.agents.worktree_records import WorktreeRecord
from aura.config import MAX_READ_BYTES


class ChangeSetInspector:
    """Project durable records into model-safe discovery and inspection data."""

    def __init__(self, git: GitRunner, material: ChangeSetMaterializer) -> None:
        self._git = git
        self._material = material

    def list_change_sets(
        self, records: Iterable[WorktreeRecord]
    ) -> dict[str, Any]:
        rows = [self._compact_row(record) for record in records]
        return {
            "ok": True,
            "tool": "list_agent_change_sets",
            "count": len(rows),
            "change_sets": rows,
        }

    def inspect(
        self,
        record: WorktreeRecord,
        *,
        paths: tuple[str, ...],
        verify_result_ref: Callable[[WorktreeRecord], None],
        is_owned_worktree: Callable[[WorktreeRecord], bool],
    ) -> dict[str, Any]:
        payload = _snapshot(record).payload()
        if record.result_sha:
            verify_result_ref(record)
            payload["diff_available"] = True
            if paths:
                payload.update(self._material.inspect_diff(record, paths))
                payload["files"] = self._material.file_metadata(
                    record, paths=set(paths)
                )
        else:
            payload["diff_available"] = False
            self._add_recovery_status(
                payload, record, is_owned_worktree=is_owned_worktree
            )
            if paths:
                raise AgentWorktreeError(
                    "change_set_not_ready",
                    "This recovery record has no stable result diff yet.",
                    change_set_id=record.change_set_id,
                )
        payload["ok"] = True
        payload["tool"] = "inspect_agent_change_set"
        return payload

    @staticmethod
    def _compact_row(record: WorktreeRecord) -> dict[str, Any]:
        visible_paths = record.changed_paths[:50]
        return {
            "change_set_id": record.change_set_id,
            "agent_id": record.agent_id,
            "status": record.state,
            "base_sha": record.base_sha,
            "result_sha": record.result_sha,
            "changed_path_count": len(record.changed_paths),
            "changed_paths": list(visible_paths),
            "changed_paths_truncated": len(record.changed_paths) > len(visible_paths),
            "diffstat": record.diffstat,
            "cleanup_pending": record.state == "applied_cleanup_pending",
            "warning": record.error,
        }

    def _add_recovery_status(
        self,
        payload: dict[str, Any],
        record: WorktreeRecord,
        *,
        is_owned_worktree: Callable[[WorktreeRecord], bool],
    ) -> None:
        if not record.worktree_path:
            return
        recovery = Path(record.worktree_path)
        if recovery.is_dir() and is_owned_worktree(record):
            data, size, truncated, digest = self._git.bounded_bytes(
                recovery,
                ["status", "--short", "--untracked-files=all"],
                max_bytes=MAX_READ_BYTES,
                change_set_id=record.change_set_id,
                failure_class="change_set_inspection_failed",
            )
            payload["worktree_status"] = data.decode("utf-8", "replace")
            payload["worktree_status_size_bytes"] = size
            payload["worktree_status_sha256"] = digest
            payload["worktree_status_truncated"] = truncated
            payload["worktree_status_max_bytes"] = MAX_READ_BYTES
        elif recovery.exists():
            raise AgentWorktreeError(
                "worktree_cleanup_refused",
                "The recovery path is not the exact Aura-owned worktree.",
                change_set_id=record.change_set_id,
                recovery_path=record.worktree_path,
            )


def _snapshot(record: WorktreeRecord) -> AgentChangeSet:
    return AgentChangeSet(
        status=record.state,
        change_set_id=record.change_set_id,
        agent_id=record.agent_id,
        base_sha=record.base_sha,
        result_sha=record.result_sha,
        changed_paths=tuple(record.changed_paths),
        diffstat=record.diffstat,
        worktree_path=record.worktree_path,
        failure_class=record.failure_class,
        error=record.error,
    )


__all__ = ["ChangeSetInspector"]
