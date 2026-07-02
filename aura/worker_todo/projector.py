"""EventBus projector for Worker TODO snapshots."""

from __future__ import annotations

from typing import Callable

from aura.events import WORKER_TODO_UPDATED, AuraEvent, EventBus
from aura.worker_todo.model import WorkerTodoSnapshot, parse_worker_todo_snapshot

WorkerTodoChangeCallback = Callable[[str, list[dict[str, str]]], None]


class WorkerTodoProjector:
    """Owns the current Worker TODO snapshot for each active run."""

    def __init__(self, bus: EventBus) -> None:
        self._snapshots: dict[str, WorkerTodoSnapshot] = {}
        self._on_change: WorkerTodoChangeCallback | None = None
        bus.subscribe(WORKER_TODO_UPDATED, self._on_worker_todo_updated)

    def set_on_change(self, callback: WorkerTodoChangeCallback | None) -> None:
        self._on_change = callback

    def snapshot(self, run_id: str) -> WorkerTodoSnapshot | None:
        return self._snapshots.get(run_id)

    def snapshot_dicts(self, run_id: str) -> list[dict[str, str]]:
        snapshot = self.snapshot(run_id)
        return snapshot.item_dicts() if snapshot is not None else []

    def clear(self, run_id: str | None = None) -> None:
        if run_id is None:
            self._snapshots.clear()
        else:
            self._snapshots.pop(run_id, None)

    def _on_worker_todo_updated(self, ev: AuraEvent) -> None:
        snapshot, errors = parse_worker_todo_snapshot(ev.payload)
        if snapshot is None or errors:
            return

        run_id = ev.run_id or ev.campaign_id
        if not run_id:
            return

        self._snapshots[run_id] = snapshot
        if self._on_change is not None:
            self._on_change(run_id, snapshot.item_dicts())
