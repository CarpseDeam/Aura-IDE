"""Pure model and validation for Task Checklist snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

UPDATE_TASK_CHECKLIST_TOOL = "update_task_checklist"

CHECKLIST_PENDING = "pending"
CHECKLIST_ACTIVE = "active"
CHECKLIST_DONE = "done"
CHECKLIST_STATUSES = frozenset({CHECKLIST_PENDING, CHECKLIST_ACTIVE, CHECKLIST_DONE})


@dataclass(frozen=True)
class TaskChecklistItem:
    """One stable progress marker in the model's task checklist."""

    id: str
    text: str
    status: str = CHECKLIST_PENDING

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "text": self.text,
            "status": self.status,
        }


@dataclass(frozen=True)
class TaskChecklistSnapshot:
    """Full replacement snapshot maintained within one continuous task."""

    items: tuple[TaskChecklistItem, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
        }

    def item_dicts(self) -> list[dict[str, str]]:
        return [item.to_dict() for item in self.items]


def parse_task_checklist_snapshot(payload: Any) -> tuple[TaskChecklistSnapshot | None, list[str]]:
    """Parse and validate a Task Checklist snapshot payload.

    The snapshot is a display-only fact. Invalid payloads are rejected for
    rendering, but callers should not use validation failure to block execution.
    """
    if not isinstance(payload, dict):
        return None, ["payload must be an object"]

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        return None, ["items must be a list"]
    if not raw_items:
        return None, ["items must contain at least one item"]

    items: list[TaskChecklistItem] = []
    errors: list[str] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            errors.append(f"items[{index}] must be an object")
            continue

        item_id = _clean_text(raw.get("id"))
        text = _clean_text(raw.get("text"))
        status = _clean_text(raw.get("status")).lower()

        if not item_id:
            errors.append(f"items[{index}].id is required")
        elif item_id in seen_ids:
            errors.append(f"items[{index}].id duplicates {item_id!r}")
        else:
            seen_ids.add(item_id)

        if not text:
            errors.append(f"items[{index}].text is required")

        if status not in CHECKLIST_STATUSES:
            errors.append(
                f"items[{index}].status must be one of: active, done, pending"
            )

        if item_id and text and status in CHECKLIST_STATUSES:
            items.append(TaskChecklistItem(id=item_id, text=text, status=status))

    if errors:
        return None, errors

    return TaskChecklistSnapshot(tuple(items)), []


def _clean_text(value: Any) -> str:
    """Collapse whitespace without silently truncating the value."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())
