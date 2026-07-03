"""Pure model and validation for Worker TODO snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UPDATE_WORKER_TODO_TOOL = "update_worker_todo"

TODO_PENDING = "pending"
TODO_ACTIVE = "active"
TODO_DONE = "done"
TODO_STATUSES = frozenset({TODO_PENDING, TODO_ACTIVE, TODO_DONE})
MAX_TODO_ITEMS = 7
MIN_TODO_ITEMS = 3


@dataclass(frozen=True)
class WorkerTodoItem:
    """One stable row in the Worker's live TODO list."""

    id: str
    text: str
    status: str = TODO_PENDING

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
        }


@dataclass(frozen=True)
class WorkerTodoSnapshot:
    """Full replacement snapshot emitted by the Worker."""

    items: tuple[WorkerTodoItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
        }

    def item_dicts(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.items]


def parse_worker_todo_snapshot(payload: Any) -> tuple[WorkerTodoSnapshot | None, list[str]]:
    """Parse and validate a Worker TODO snapshot payload.

    The snapshot is a display-only fact. Invalid payloads are rejected for
    rendering, but callers should not use validation failure to block execution.
    """
    if not isinstance(payload, dict):
        return None, ["payload must be an object"]

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None, ["items must be a list"]
    if len(raw_items) < MIN_TODO_ITEMS:
        return None, [f"items must contain at least {MIN_TODO_ITEMS} items"]
    if len(raw_items) > MAX_TODO_ITEMS:
        return None, [f"items must contain no more than {MAX_TODO_ITEMS} items"]

    items: list[WorkerTodoItem] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"items[{index}] must be an object")
            continue

        item_id = _clean_text(raw.get("id"), max_chars=80)
        text = _clean_text(raw.get("text"), max_chars=160)
        status = _clean_text(raw.get("status"), max_chars=24).lower()

        if not item_id:
            errors.append(f"items[{index}].id is required")
        elif item_id in seen_ids:
            errors.append(f"items[{index}].id duplicates {item_id!r}")
        else:
            seen_ids.add(item_id)

        if not text:
            errors.append(f"items[{index}].text is required")

        if status not in TODO_STATUSES:
            errors.append(
                f"items[{index}].status must be one of: active, done, pending"
            )

        if item_id and text and status in TODO_STATUSES:
            items.append(WorkerTodoItem(id=item_id, text=text, status=status))

    if errors:
        return None, errors

    active_count = sum(1 for item in items if item.status == TODO_ACTIVE)
    done_count = sum(1 for item in items if item.status == TODO_DONE)
    all_done = done_count == len(items)
    if active_count != 1 and not all_done:
        return None, ["exactly one item must be active unless every item is done"]
    if active_count > 1:
        return None, ["only one item may be active"]

    return WorkerTodoSnapshot(tuple(items)), []


def _clean_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(value.replace("\r", " ").replace("\n", " ").split())
    return text[:max_chars]
