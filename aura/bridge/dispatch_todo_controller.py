"""Bridge-owned state controller for canonical dispatch TODO snapshots.

DispatchTodoController is the sole owner of visible dispatch checklist state.
The primary path is event-driven: the controller subscribes to the EventBus
and projects checklist state from these five lifecycle topics:

* ``dispatch.checklist_declared``  →  ``begin()``
* ``dispatch.campaign_started``    →  (no state change — rows already seeded)
* ``dispatch.step_started``        →  ``activate_step()``
* ``dispatch.step_completed``      →  ``complete_step()``
* ``dispatch.campaign_finished``   →  ``finish()``

Direct method calls (begin/activate_step/complete_step/finish) remain as a
public API for testing and non-event-bus consumers — they are not used in
the production dispatch path.

Every state mutation fires the optional ``_on_change`` callback so the
owning _DispatchProxy can relay a canonical snapshot to the GUI.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from aura.events import AuraEvent, EventBus


@dataclass(frozen=True)
class DispatchTodoRow:
    id: str
    description: str
    files: tuple[str, ...] = ()
    status: str = "pending"
    owning_step_id: str = ""


class DispatchTodoController:
    """Own visible checklist state for one canonical dispatch rail.

    Accepts an optional ``EventBus``.  When provided the controller subscribes
    to dispatch-lifecycle topics and updates its internal state automatically.
    Direct method calls are still supported for callback-driven usage.
    """

    def __init__(self, event_bus: EventBus | None = None) -> None:
        self._tool_call_id: str = ""
        self._rows: tuple[DispatchTodoRow, ...] = ()
        self._active_step_id: str = ""
        self._on_change: Callable[[str, list[dict[str, Any]]], None] | None = None
        self._event_bus = event_bus
        if event_bus is not None:
            self._subscribe_to_events(event_bus)

    # ── public API ──────────────────────────────────────────────────────

    def set_on_change(
        self, callback: Callable[[str, list[dict[str, Any]]], None] | None
    ) -> None:
        """Set (or clear) the callback invoked after every state mutation.

        The callback receives ``(tool_call_id, snapshot)``.
        """
        self._on_change = callback

    def begin(
        self, tool_call_id: str, objectives: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        rows: list[DispatchTodoRow] = []
        for obj in objectives:
            row_id = str(obj.get("id") or "")
            if not row_id:
                continue
            owning_step_id = str(obj.get("owning_step_id") or obj.get("step_id") or "")
            rows.append(
                DispatchTodoRow(
                    id=row_id,
                    description=str(obj.get("description") or row_id),
                    files=tuple(str(f) for f in (obj.get("files") or [])),
                    status="pending",
                    owning_step_id=owning_step_id,
                )
            )
        self._tool_call_id = tool_call_id
        self._rows = tuple(rows)
        self._active_step_id = ""
        self._notify_change()
        return self.snapshot(tool_call_id)

    def activate_step(
        self, tool_call_id: str, step_id: str
    ) -> list[dict[str, Any]] | None:
        if not self._matches_tool_call(tool_call_id):
            return None
        new_rows: list[DispatchTodoRow] = []
        found = False
        for row in self._rows:
            if _row_matches_step(row, step_id):
                found = True
                if row.status == "done":
                    new_rows.append(row)
                else:
                    new_rows.append(replace(row, status="active"))
            elif row.status == "active":
                new_rows.append(replace(row, status="pending"))
            else:
                new_rows.append(row)
        if found:
            self._rows = tuple(new_rows)
            self._active_step_id = step_id
        else:
            self._rows, found = _activate_next_pending_row(self._rows)
            if found:
                self._active_step_id = step_id
        self._notify_change()
        return self.snapshot(tool_call_id)

    def complete_step(
        self, tool_call_id: str, step_id: str
    ) -> list[dict[str, Any]] | None:
        if not self._matches_tool_call(tool_call_id):
            return None
        new_rows: list[DispatchTodoRow] = []
        found = False
        for row in self._rows:
            if _row_matches_step(row, step_id):
                found = True
                new_rows.append(replace(row, status="done"))
            else:
                new_rows.append(row)
        if found:
            self._rows = tuple(new_rows)
        else:
            self._rows, found = _complete_active_or_next_pending_row(self._rows)
        self._notify_change()
        return self.snapshot(tool_call_id)

    def finish(self, tool_call_id: str) -> list[dict[str, Any]] | None:
        if not self._matches_tool_call(tool_call_id):
            return None
        self._active_step_id = ""
        self._notify_change()
        return self.snapshot(tool_call_id)

    def snapshot(self, tool_call_id: str) -> list[dict[str, Any]]:
        if not self._matches_tool_call(tool_call_id):
            return []
        rows: list[dict[str, Any]] = []
        for row in self._rows:
            item: dict[str, Any] = {
                "id": row.id,
                "step_id": row.owning_step_id or row.id,
                "owning_step_id": row.owning_step_id,
                "description": row.description,
                "status": row.status,
            }
            if row.files:
                item["files"] = list(row.files)
            rows.append(item)
        return rows

    def has_active_tool_call(self, tool_call_id: str) -> bool:
        return self._matches_tool_call(tool_call_id) and bool(self._rows)

    def clear(self) -> None:
        """Reset all internal state (used on conversation reset)."""
        self._tool_call_id = ""
        self._rows = ()
        self._active_step_id = ""

    # ── internal ────────────────────────────────────────────────────────

    def _matches_tool_call(self, tool_call_id: str) -> bool:
        return bool(self._tool_call_id and self._tool_call_id == tool_call_id)

    def _notify_change(self) -> None:
        """Fire the on_change callback if set."""
        if self._on_change is not None and self._tool_call_id:
            self._on_change(self._tool_call_id, self.snapshot(self._tool_call_id))

    # ── event bus subscription ──────────────────────────────────────────

    def _subscribe_to_events(self, bus: EventBus) -> None:
        """Subscribe to dispatch-lifecycle topics on *bus*."""
        from aura.events import (
            DISPATCH_CAMPAIGN_FINISHED,
            DISPATCH_CAMPAIGN_STARTED,
            DISPATCH_CHECKLIST_DECLARED,
            DISPATCH_STEP_COMPLETED,
            DISPATCH_STEP_STARTED,
        )
        bus.subscribe(DISPATCH_CHECKLIST_DECLARED, self._on_checklist_declared)
        bus.subscribe(DISPATCH_CAMPAIGN_STARTED, self._on_campaign_started)
        bus.subscribe(DISPATCH_STEP_STARTED, self._on_step_started)
        bus.subscribe(DISPATCH_STEP_COMPLETED, self._on_step_completed)
        bus.subscribe(DISPATCH_CAMPAIGN_FINISHED, self._on_campaign_finished)

    def _on_checklist_declared(self, event: AuraEvent) -> None:
        """Seed visible TODO rows from the Planner-authored checklist."""
        objectives = event.payload.get("objectives", [])
        if not objectives:
            return
        self.begin(event.campaign_id, objectives)

    def _on_campaign_started(self, event: AuraEvent) -> None:
        """Campaign started — rows already seeded by checklist_declared."""
        pass

    def _on_step_started(self, event: AuraEvent) -> None:
        """Light the matching row(s) as active."""
        self.activate_step(event.campaign_id, event.step_id)

    def _on_step_completed(self, event: AuraEvent) -> None:
        """Mark the matching row(s) as done."""
        self.complete_step(event.campaign_id, event.step_id)

    def _on_campaign_finished(self, event: AuraEvent) -> None:
        """Finalise the TODO rail for this campaign."""
        self.finish(event.campaign_id)


def _row_matches_step(row: DispatchTodoRow, step_id: str) -> bool:
    return row.id == step_id or bool(row.owning_step_id and row.owning_step_id == step_id)


def _activate_next_pending_row(
    rows: tuple[DispatchTodoRow, ...],
) -> tuple[tuple[DispatchTodoRow, ...], bool]:
    activated = False
    updated: list[DispatchTodoRow] = []
    for row in rows:
        if row.status == "active":
            updated.append(replace(row, status="pending"))
            continue
        if not activated and row.status != "done":
            updated.append(replace(row, status="active"))
            activated = True
            continue
        updated.append(row)
    return tuple(updated), activated


def _complete_active_or_next_pending_row(
    rows: tuple[DispatchTodoRow, ...],
) -> tuple[tuple[DispatchTodoRow, ...], bool]:
    has_active = any(row.status == "active" for row in rows)
    completed = False
    updated: list[DispatchTodoRow] = []
    for row in rows:
        if has_active:
            if row.status == "active":
                updated.append(replace(row, status="done"))
                completed = True
            else:
                updated.append(row)
            continue
        if not completed and row.status != "done":
            updated.append(replace(row, status="done"))
            completed = True
            continue
        updated.append(row)
    return tuple(updated), completed


__all__ = ["DispatchTodoController", "DispatchTodoRow"]
