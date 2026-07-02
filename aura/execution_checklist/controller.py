"""EventBus projector for the Canonical Execution Checklist."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any, TYPE_CHECKING

from aura.events import (
    DISPATCH_CAMPAIGN_FINISHED,
    DISPATCH_CHECKLIST_DECLARED,
    DISPATCH_STEP_COMPLETED,
    DISPATCH_STEP_STARTED,
)
from aura.execution_checklist.events import (
    campaign_id_from_event,
    checklist_rows_from_event,
    step_id_from_event,
)
from aura.execution_checklist.models import ExecutionChecklistItem
from aura.execution_checklist.validation import validate_checklist_items

if TYPE_CHECKING:
    from aura.events import AuraEvent, EventBus

OnChange = Callable[[str, list[dict[str, Any]]], None]


class ExecutionChecklistController:
    """Project dispatch lifecycle events into visible checklist snapshots.

    The controller is pure Python and owns only visible checklist state. It
    subscribes to dispatch lifecycle topics, ignores Worker/tool topics, and
    emits snapshots through ``on_change`` after real state changes.
    """

    def __init__(self, event_bus: "EventBus | None" = None) -> None:
        self._campaign_id = ""
        self._rows: tuple[ExecutionChecklistItem, ...] = ()
        self._on_change: OnChange | None = None
        if event_bus is not None:
            self.subscribe(event_bus)

    def subscribe(self, event_bus: "EventBus") -> None:
        """Subscribe this controller to dispatch lifecycle topics."""
        event_bus.subscribe(DISPATCH_CHECKLIST_DECLARED, self._on_checklist_declared)
        event_bus.subscribe(DISPATCH_STEP_STARTED, self._on_step_started)
        event_bus.subscribe(DISPATCH_STEP_COMPLETED, self._on_step_completed)
        event_bus.subscribe(DISPATCH_CAMPAIGN_FINISHED, self._on_campaign_finished)

    def set_on_change(self, callback: OnChange | None) -> None:
        self._on_change = callback

    def begin(
        self,
        campaign_id: str,
        rows: list[Any],
    ) -> list[dict[str, Any]]:
        """Seed a new campaign with stable pending rows.

        A repeated declaration for the active campaign is ignored so a Worker
        or late duplicate event cannot replace the canonical checklist mid-run.
        A different campaign id starts a new checklist.
        """
        campaign_id = str(campaign_id or "").strip()
        if not campaign_id:
            return []
        if self._campaign_id == campaign_id and self._rows:
            return self.snapshot(campaign_id)

        items = [
            replace(ExecutionChecklistItem.from_raw(row), status="pending")
            for row in rows
        ]
        normalized = validate_checklist_items(items)
        self._campaign_id = campaign_id
        self._rows = tuple(normalized)
        self._notify_change()
        return self.snapshot(campaign_id)

    def activate_step(
        self,
        campaign_id: str,
        step_id: str,
    ) -> list[dict[str, Any]] | None:
        if not self._matches_campaign(campaign_id):
            return None
        old_rows = self._rows
        new_rows, changed = _activate_matching_rows(self._rows, step_id)
        if not changed:
            new_rows, changed = _activate_ownerless_fallback(self._rows)
        if changed:
            self._rows = new_rows
            self._notify_change_if_changed(old_rows)
        return self.snapshot(campaign_id)

    def complete_step(
        self,
        campaign_id: str,
        step_id: str,
    ) -> list[dict[str, Any]] | None:
        if not self._matches_campaign(campaign_id):
            return None
        old_rows = self._rows
        new_rows, changed = _complete_matching_rows(self._rows, step_id)
        if not changed:
            new_rows, changed = _complete_ownerless_fallback(self._rows)
        if changed:
            self._rows = new_rows
            self._notify_change_if_changed(old_rows)
        return self.snapshot(campaign_id)

    def finish(self, campaign_id: str) -> list[dict[str, Any]] | None:
        """Finalize a campaign while preserving rows.

        Completed rows remain done. Any still-active row moves forward to
        skipped so the final snapshot has no active state.
        """
        if not self._matches_campaign(campaign_id):
            return None
        self._rows = tuple(
            replace(row, status="skipped") if row.status == "active" else row
            for row in self._rows
        )
        self._notify_change()
        return self.snapshot(campaign_id)

    def snapshot(self, campaign_id: str | None = None) -> list[dict[str, Any]]:
        if campaign_id is not None and not self._matches_campaign(campaign_id):
            return []
        return [_snapshot_row(row) for row in self._rows]

    def has_active_campaign(self, campaign_id: str) -> bool:
        return self._matches_campaign(campaign_id) and bool(self._rows)

    def clear(self) -> None:
        self._campaign_id = ""
        self._rows = ()

    def _matches_campaign(self, campaign_id: str | None) -> bool:
        return bool(self._campaign_id and self._campaign_id == str(campaign_id or ""))

    def _notify_change(self) -> None:
        if self._on_change is not None and self._campaign_id:
            self._on_change(self._campaign_id, self.snapshot(self._campaign_id))

    def _notify_change_if_changed(
        self,
        old_rows: tuple[ExecutionChecklistItem, ...],
    ) -> None:
        if old_rows != self._rows:
            self._notify_change()

    def _on_checklist_declared(self, event: "AuraEvent") -> None:
        campaign_id = campaign_id_from_event(event)
        rows = checklist_rows_from_event(event)
        if rows:
            self.begin(campaign_id, rows)

    def _on_step_started(self, event: "AuraEvent") -> None:
        self.activate_step(campaign_id_from_event(event), step_id_from_event(event))

    def _on_step_completed(self, event: "AuraEvent") -> None:
        self.complete_step(campaign_id_from_event(event), step_id_from_event(event))

    def _on_campaign_finished(self, event: "AuraEvent") -> None:
        self.finish(campaign_id_from_event(event))


def _snapshot_row(row: ExecutionChecklistItem) -> dict[str, Any]:
    step_id = row.owning_step_id or row.id
    payload: dict[str, Any] = {
        "id": row.id,
        "step_id": step_id,
        "owning_step_id": row.owning_step_id,
        "description": row.description,
        "status": row.status,
    }
    if row.files:
        payload["files"] = list(row.files)
    if row.metadata:
        payload["metadata"] = dict(row.metadata)
    return payload


def _row_matches_step(row: ExecutionChecklistItem, step_id: str) -> bool:
    step_id = str(step_id or "").strip()
    if not step_id:
        return False
    return row.id == step_id or row.owning_step_id == step_id


def _activate_matching_rows(
    rows: tuple[ExecutionChecklistItem, ...],
    step_id: str,
) -> tuple[tuple[ExecutionChecklistItem, ...], bool]:
    matched = any(_row_matches_step(row, step_id) for row in rows)
    if not matched:
        return rows, False
    return tuple(
        _move_forward(row, "active") if _row_matches_step(row, step_id) else row
        for row in rows
    ), True


def _complete_matching_rows(
    rows: tuple[ExecutionChecklistItem, ...],
    step_id: str,
) -> tuple[tuple[ExecutionChecklistItem, ...], bool]:
    matched = any(_row_matches_step(row, step_id) for row in rows)
    if not matched:
        return rows, False
    return tuple(
        _move_forward(row, "done") if _row_matches_step(row, step_id) else row
        for row in rows
    ), True


def _activate_ownerless_fallback(
    rows: tuple[ExecutionChecklistItem, ...],
) -> tuple[tuple[ExecutionChecklistItem, ...], bool]:
    if not _ownerless_fallback_is_safe(rows):
        return rows, False
    updated: list[ExecutionChecklistItem] = []
    activated = False
    for row in rows:
        if not activated and row.status == "pending":
            updated.append(replace(row, status="active"))
            activated = True
        else:
            updated.append(row)
    return tuple(updated), activated


def _complete_ownerless_fallback(
    rows: tuple[ExecutionChecklistItem, ...],
) -> tuple[tuple[ExecutionChecklistItem, ...], bool]:
    if not _ownerless_fallback_is_safe(rows):
        return rows, False

    active_indices = [idx for idx, row in enumerate(rows) if row.status == "active"]
    if len(active_indices) > 1:
        return rows, False
    target_index: int | None = active_indices[0] if active_indices else None
    if target_index is None:
        target_index = next(
            (idx for idx, row in enumerate(rows) if row.status == "pending"),
            None,
        )
    if target_index is None:
        return rows, False

    updated = list(rows)
    updated[target_index] = _move_forward(updated[target_index], "done")
    return tuple(updated), updated[target_index] != rows[target_index]


def _ownerless_fallback_is_safe(rows: tuple[ExecutionChecklistItem, ...]) -> bool:
    return bool(rows) and all(not row.owning_step_id for row in rows)


def _move_forward(
    row: ExecutionChecklistItem,
    status: str,
) -> ExecutionChecklistItem:
    order = {"pending": 0, "active": 1, "done": 2, "failed": 2, "skipped": 2}
    current = order.get(row.status, 0)
    target = order.get(status, current)
    if target < current:
        return row
    if row.status in {"done", "failed", "skipped"}:
        return row
    return replace(row, status=status)  # type: ignore[arg-type]


__all__ = [
    "ExecutionChecklistController",
    "OnChange",
]
