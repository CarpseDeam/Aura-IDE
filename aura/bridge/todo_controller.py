"""Unified TODO rail owner for canonical dispatch campaigns.

DispatchTodoController is the single source of truth for the visible TODO
checklist during a Planner -> Worker dispatch. Once canonical objectives exist
for a tool_call_id, only the controller may emit visible TODO snapshots.

Rules:
- IDs and order never change after begin().
- Descriptions never change after begin().
- Unknown Worker objective IDs are ignored.
- Worker-local TODOs with unknown IDs, ad-hoc descriptions, or replacement
  task lists are ignored during canonical dispatch.
- Worker updates may only update status for known objective IDs.
- Worker cannot add, remove, reorder, or rename visible rows.
- Final checklist remains visible after dispatch finish.
- Clear canonical state only when a new dispatch begins for that tool ID,
  when the run is cancelled/reset, or when the owning conversation resets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TodoObjective:
    """One stable row in the canonical dispatch TODO checklist."""

    id: str
    description: str
    status: str = "pending"
    files: list[str] = field(default_factory=list)
    blocked: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "step_id": self.id,
            "description": self.description,
            "status": self.status,
        }
        if self.files:
            result["files"] = list(self.files)
        if self.blocked:
            result["blocked"] = True
        if self.metadata:
            result["metadata"] = dict(self.metadata)
        return result


class DispatchTodoController:
    """Owns canonical TODO state for every active tool_call_id.

    Once begin() is called for a tool_call_id, all visible TODO emissions
    for that ID must come through snapshot(). Worker-local updates are
    absorbed (status-only for known IDs) or ignored.
    """

    def __init__(self) -> None:
        self._canonical: dict[str, dict[str, TodoObjective]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def begin(
        self,
        tool_call_id: str,
        objectives: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Set the canonical objective checklist for a tool_call_id.

        objectives: list of dicts with keys id, description, files, metadata.
        Returns the initial full snapshot.
        """
        ordered: dict[str, TodoObjective] = {}
        for obj in objectives:
            obj_id = str(obj.get("id") or "")
            if not obj_id:
                continue
            ordered[obj_id] = TodoObjective(
                id=obj_id,
                description=str(obj.get("description") or obj_id),
                status="pending",
                files=_str_list(obj.get("files")),
                blocked=False,
                metadata=dict(obj.get("metadata") or {}),
            )
        self._canonical[tool_call_id] = ordered
        return self.snapshot(tool_call_id)

    def snapshot(self, tool_call_id: str) -> list[dict[str, Any]]:
        """Return the full canonical checklist as a list of dicts.

        Order is stable (insertion order from begin()).
        """
        ordered = self._canonical.get(tool_call_id)
        if ordered is None:
            return []
        return [obj.to_dict() for obj in ordered.values()]

    def set_active(
        self, tool_call_id: str, objective_id: str
    ) -> list[dict[str, Any]]:
        """Mark one objective as active. Returns the full snapshot.

        Enforces one-active-row: any other non-done, non-blocked active
        row is returned to pending. Done rows and blocked rows are
        never touched.
        """
        obj = self._get(tool_call_id, objective_id)
        if obj is None:
            return self.snapshot(tool_call_id)
        if obj.status in ("done",):
            return self.snapshot(tool_call_id)

        ordered = self._canonical.get(tool_call_id)
        if ordered is not None:
            for other_id, other in ordered.items():
                if other_id == objective_id:
                    continue
                if other.status == "done":
                    continue
                if other.blocked:
                    continue
                if other.status == "active":
                    other.status = "pending"

        obj.status = "active"
        return self.snapshot(tool_call_id)

    def mark_done(
        self, tool_call_id: str, objective_id: str
    ) -> list[dict[str, Any]]:
        """Mark one objective as done. Returns the full snapshot."""
        obj = self._get(tool_call_id, objective_id)
        if obj is None:
            return self.snapshot(tool_call_id)
        obj.status = "done"
        obj.blocked = False
        return self.snapshot(tool_call_id)

    def mark_blocked(
        self, tool_call_id: str, objective_id: str
    ) -> list[dict[str, Any]]:
        """Mark one objective as active + blocked. Returns the full snapshot."""
        obj = self._get(tool_call_id, objective_id)
        if obj is None:
            return self.snapshot(tool_call_id)
        obj.status = "active"
        obj.blocked = True
        return self.snapshot(tool_call_id)

    def finish(self, tool_call_id: str) -> list[dict[str, Any]]:
        """Finalize: mark any still-active objective back to pending.

        The checklist stays visible after finish.
        Returns the final full snapshot.
        """
        ordered = self._canonical.get(tool_call_id)
        if ordered is None:
            return []
        for obj in ordered.values():
            if obj.status == "active" and not obj.blocked:
                # Active-but-not-blocked at finish time means the step was
                # interrupted. Leave it as-is so the user sees what state it
                # was in. Don't force-downgrade.
                pass
        return self.snapshot(tool_call_id)

    def clear(self, tool_call_id: str) -> None:
        """Remove canonical state for a tool_call_id."""
        self._canonical.pop(tool_call_id, None)

    def clear_all(self) -> None:
        """Remove all canonical state across every tool_call_id.

        Called on conversation reset / new-chat to prevent stale TODO
        checklists from surviving into a fresh conversation.
        """
        self._canonical.clear()

    def has_canonical(self, tool_call_id: str) -> bool:
        """Return True if canonical objectives exist for this tool_call_id."""
        return tool_call_id in self._canonical

    # ------------------------------------------------------------------
    # Worker update absorption
    # ------------------------------------------------------------------

    def absorb_worker_update(
        self,
        tool_call_id: str,
        tasks: list[Any],
    ) -> list[dict[str, Any]] | None:
        """Absorb a Worker-local TODO update during canonical dispatch.

        Returns the canonical snapshot if any status was absorbed, or None
        if the worker update should be completely suppressed (no emission).

        Enforces one-active-row: when a known objective is set to active,
        any other non-done, non-blocked active row is returned to pending.
        """
        if not self.has_canonical(tool_call_id):
            return None

        ordered = self._canonical[tool_call_id]
        changed = False

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("id") or task.get("step_id") or "")
            if not task_id:
                continue
            obj = ordered.get(task_id)
            if obj is None:
                # Unknown ID — ignore, do not add
                continue
            # Known ID — absorb status only
            worker_status = _normalize_status(task.get("status") or task.get("state") or "")
            if worker_status in ("done", "active") and obj.status != "done":
                if obj.status != worker_status:
                    obj.status = worker_status
                    changed = True
                    # Enforce one active row when setting a known ID to active
                    if worker_status == "active" and not obj.blocked:
                        for other_id, other in ordered.items():
                            if other_id == task_id:
                                continue
                            if other.status == "done":
                                continue
                            if other.blocked:
                                continue
                            if other.status == "active":
                                other.status = "pending"

        # Only re-emit if something actually changed
        if changed:
            return self.snapshot(tool_call_id)
        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get(
        self, tool_call_id: str, objective_id: str
    ) -> TodoObjective | None:
        ordered = self._canonical.get(tool_call_id)
        if ordered is None:
            return None
        return ordered.get(objective_id)


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def _normalize_status(raw: str) -> str:
    s = str(raw or "").strip().lower()
    if s in ("done", "completed", "complete"):
        return "done"
    if s in ("active", "in_progress", "doing", "current"):
        return "active"
    if s in ("blocked",):
        return "blocked"
    return "pending"


__all__ = [
    "DispatchTodoController",
    "TodoObjective",
]
