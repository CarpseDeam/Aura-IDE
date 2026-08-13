"""EventBus projector for Task Checklist snapshots."""

from __future__ import annotations

from typing import Callable

from aura.events import TASK_CHECKLIST_UPDATED, AuraEvent, EventBus
from aura.task_checklist.model import TaskChecklistSnapshot, parse_task_checklist_snapshot

TaskChecklistChangeCallback = Callable[[str, list[dict[str, str]]], None]


class TaskChecklistProjector:
    """Owns the current Task Checklist snapshot for each active run."""

    def __init__(self, bus: EventBus) -> None:
        self._snapshots: dict[str, TaskChecklistSnapshot] = {}
        self._on_change: TaskChecklistChangeCallback | None = None
        bus.subscribe(TASK_CHECKLIST_UPDATED, self._on_task_checklist_updated)

    def set_on_change(self, callback: TaskChecklistChangeCallback | None) -> None:
        self._on_change = callback

    def snapshot(self, run_id: str) -> TaskChecklistSnapshot | None:
        return self._snapshots.get(run_id)

    def snapshot_dicts(self, run_id: str) -> list[dict[str, str]]:
        snapshot = self.snapshot(run_id)
        return snapshot.item_dicts() if snapshot is not None else []

    def clear(self, run_id: str | None = None) -> None:
        if run_id is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(run_id, None)

    def _on_task_checklist_updated(self, ev: AuraEvent) -> None:
        snapshot, errors = parse_task_checklist_snapshot(ev.payload)
        if snapshot is None or errors:
            return

        run_id = ev.run_id
        if not run_id:
            return

        self._snapshots[run_id] = snapshot
        if self._on_change is not None:
            self._on_change(run_id, snapshot.item_dicts())
