"""Render cache for canonical execution checklist snapshots.

``DispatchTodoRail`` caches canonical snapshots so the GUI can repaint the
TODO rail after widget clears (e.g. ``begin_assistant``). It contains no
status logic, matching, or Planner-step seeding — it is a pure replay cache.

The canonical checklist state is owned by ``ExecutionChecklistController``
which projects from EventBus lifecycle events.
"""

from __future__ import annotations

from typing import Any


class DispatchTodoRail:
    """Cache the last canonical dispatch TODO snapshot by tool call id."""

    def __init__(self) -> None:
        self._snapshots: dict[str, list[Any]] = {}

    def set(self, tool_call_id: str | None, tasks: list[Any]) -> list[Any]:
        if tool_call_id is None:
            return list(tasks) if isinstance(tasks, list) else []
        snapshot = list(tasks) if isinstance(tasks, list) else []
        self._snapshots[tool_call_id] = snapshot
        return snapshot

    def replay(self, tool_call_id: str) -> list[Any]:
        return list(self._snapshots.get(tool_call_id, []))

    def has(self, tool_call_id: str | None) -> bool:
        return bool(tool_call_id and tool_call_id in self._snapshots)

    def reset(self, tool_call_id: str) -> None:
        self._snapshots.pop(tool_call_id, None)

    def clear(self) -> None:
        self._snapshots.clear()
