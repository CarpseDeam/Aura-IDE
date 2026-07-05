"""GUI-friendly read-only snapshots of WorkArtifact state.

Produced by the controller after every mutation and consumed by the GUI
for rendering without accessing the artifact model directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.work_artifact.model import WorkArtifact, WorkArtifactItem


@dataclass(frozen=True)
class WorkArtifactProjection:
    """Read-only GUI-friendly snapshot of a WorkArtifact."""

    artifact_id: str
    goal: str
    constraints: list[str] = field(default_factory=list)
    allowed_files: list[str] = field(default_factory=list)
    items: list[dict[str, Any]] = field(default_factory=list)
    current_item_id: str = ""
    current_item: dict[str, Any] | None = None
    completed_count: int = 0
    active_count: int = 0
    pending_count: int = 0
    is_complete: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "goal": self.goal,
            "constraints": list(self.constraints),
            "allowed_files": list(self.allowed_files),
            "items": list(self.items),
            "current_item_id": self.current_item_id,
            "current_item": self.current_item,
            "completed_count": self.completed_count,
            "active_count": self.active_count,
            "pending_count": self.pending_count,
            "is_complete": self.is_complete,
        }

    @classmethod
    def from_artifact(cls, artifact: WorkArtifact) -> WorkArtifactProjection:
        """Build a projection from a WorkArtifact instance."""
        completed_count = 0
        active_count = 0
        pending_count = 0

        items: list[dict[str, Any]] = []
        for item in artifact.work_items:
            item_dict: dict[str, Any] = {
                "id": item.id,
                "title": item.title,
                "intent": item.intent,
                "target_files": list(item.target_files),
                "acceptance": item.acceptance,
                "status": item.status.value,
            }
            if item.receipt is not None:
                item_dict["receipt"] = item.receipt.to_dict()

            if item.status.value == "done":
                completed_count += 1
            elif item.status.value == "active":
                active_count += 1
            elif item.status.value == "pending":
                pending_count += 1

            items.append(item_dict)

        current = artifact.current_item()
        current_dict: dict[str, Any] | None = None
        if current is not None:
            current_dict = {
                "id": current.id,
                "title": current.title,
                "intent": current.intent,
                "target_files": list(current.target_files),
                "acceptance": current.acceptance,
                "status": current.status.value,
            }
            if current.receipt is not None:
                current_dict["receipt"] = current.receipt.to_dict()

        return cls(
            artifact_id=artifact.artifact_id,
            goal=artifact.goal,
            constraints=list(artifact.constraints),
            allowed_files=list(artifact.allowed_files),
            items=items,
            current_item_id=artifact.current_item_id,
            current_item=current_dict,
            completed_count=completed_count,
            active_count=active_count,
            pending_count=pending_count,
            is_complete=bool(items) and completed_count == len(items),
        )
