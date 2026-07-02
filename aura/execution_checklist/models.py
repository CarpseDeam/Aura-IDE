"""Pure data models for the visible execution checklist."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

ExecutionChecklistStatus = Literal["pending", "active", "done", "failed", "skipped"]
VALID_STATUSES: frozenset[str] = frozenset(
    {"pending", "active", "done", "failed", "skipped"}
)


@dataclass(frozen=True)
class ExecutionChecklistItem:
    """One user-visible execution checklist row.

    The row describes visible progress only. It is not a Worker prompt and it
    does not grant permission for the Worker to plan or execute the campaign.
    """

    id: str
    description: str
    status: ExecutionChecklistStatus = "pending"
    files: tuple[str, ...] = ()
    owning_step_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "description": self.description,
            "status": self.status,
        }
        if self.files:
            payload["files"] = list(self.files)
        if self.owning_step_id:
            payload["owning_step_id"] = self.owning_step_id
            payload["step_id"] = self.owning_step_id
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload

    @classmethod
    def from_raw(cls, raw: Any) -> "ExecutionChecklistItem":
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, dict):
            if any(hasattr(raw, name) for name in ("id", "description", "owning_step_id")):
                item_id = str(getattr(raw, "id", "") or "").strip()
                description = str(
                    getattr(raw, "description", "")
                    or getattr(raw, "content", "")
                    or getattr(raw, "text", "")
                    or item_id
                )
                return cls(
                    id=item_id,
                    description=description,
                    status=normalize_status(getattr(raw, "status", None)),
                    files=tuple(_str_list(getattr(raw, "files", []))),
                    owning_step_id=str(getattr(raw, "owning_step_id", "") or ""),
                    metadata=dict(getattr(raw, "metadata", {}) or {}),
                )
            description = str(raw or "").strip()
            return cls(id="", description=description)

        item_id = str(raw.get("id") or raw.get("checklist_item_id") or "").strip()
        description = str(
            raw.get("description")
            or raw.get("content")
            or raw.get("text")
            or raw.get("task")
            or item_id
        )
        status = normalize_status(raw.get("status"))
        files = tuple(_str_list(raw.get("files")))
        owning_step_id = str(raw.get("owning_step_id") or raw.get("step_id") or "")
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        return cls(
            id=item_id,
            description=description,
            status=status,
            files=files,
            owning_step_id=owning_step_id,
            metadata=dict(metadata),
        )


@dataclass(frozen=True)
class ExecutionChecklistSnapshot:
    """Immutable visible checklist snapshot."""

    items: tuple[ExecutionChecklistItem, ...] = ()

    def to_dicts(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items]

    def with_items(self, items: list[ExecutionChecklistItem]) -> "ExecutionChecklistSnapshot":
        return ExecutionChecklistSnapshot(items=tuple(items))


def normalize_status(value: Any) -> ExecutionChecklistStatus:
    status = str(value or "pending").lower().strip()
    if status not in VALID_STATUSES:
        return "pending"
    return status  # type: ignore[return-value]


def _str_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw if str(item or "").strip()]


__all__ = [
    "ExecutionChecklistItem",
    "ExecutionChecklistSnapshot",
    "ExecutionChecklistStatus",
    "VALID_STATUSES",
    "normalize_status",
]
