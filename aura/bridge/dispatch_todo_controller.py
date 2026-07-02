"""Bridge-owned state controller for canonical dispatch TODO snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class DispatchTodoRow:
    id: str
    description: str
    files: tuple[str, ...] = ()
    status: str = "pending"
    owning_step_id: str = ""


class DispatchTodoController:
    """Own visible checklist state for one canonical dispatch rail."""

    def __init__(self) -> None:
        self._tool_call_id: str = ""
        self._rows: tuple[DispatchTodoRow, ...] = ()
        self._active_step_id: str = ""

    def begin(self, tool_call_id: str, objectives: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
        return self.snapshot(tool_call_id)

    def activate_step(self, tool_call_id: str, step_id: str) -> list[dict[str, Any]] | None:
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
        return self.snapshot(tool_call_id)

    def complete_step(self, tool_call_id: str, step_id: str) -> list[dict[str, Any]] | None:
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
        return self.snapshot(tool_call_id)

    def finish(self, tool_call_id: str) -> list[dict[str, Any]] | None:
        if not self._matches_tool_call(tool_call_id):
            return None
        self._active_step_id = ""
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

    def _matches_tool_call(self, tool_call_id: str) -> bool:
        return bool(self._tool_call_id and self._tool_call_id == tool_call_id)


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
