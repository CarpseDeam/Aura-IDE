from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Self


class WorkspacePhase(Enum):
    WORKSHOP = "workshop"
    BUILDING = "building"
    ITERATING = "iterating"
    READY = "ready"
    DISCARDED = "discarded"
    BUILD_FAILED = "build_failed"


@dataclass
class DroneWorkspace:
    workspace_id: str
    display_name: str
    project_root: str
    workspace_root: str
    mode: str = "new"
    phase: str = "workshop"
    candidate_drone_id: str | None = None
    installed_drone_id: str | None = None
    build_brief: str = ""
    last_build_run: str | None = None
    active_thread_id: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        """Create workspace from JSON dict, handling old/stale keys."""
        # Drop stale keys that existed in older workspace versions.
        data = {k: v for k, v in data.items() if k not in ("last_readiness_result",)}
        # Map old phase strings to current equivalents.
        phase = str(data.get("phase", "")).lower()
        if phase == "readiness_running":
            data["phase"] = "building"
        elif phase == "readiness_failed":
            data["phase"] = "build_failed"
        elif phase in ("installing", "installed"):
            data["phase"] = "ready"
        elif phase == "awaiting_decision":
            data["phase"] = "building"
        return cls(
            **{k: v for k, v in data.items() if k in {f.name for f in __import__("dataclasses").fields(cls)}}
        )


@dataclass
class DroneThread:
    id: str
    workspace_id: str
    title: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    summary: str = ""
    pinned: bool = False
    archived: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "messages": list(self.messages),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "pinned": self.pinned,
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(
            id=data.get("id", ""),
            workspace_id=data.get("workspace_id", ""),
            title=data.get("title", ""),
            messages=list(data.get("messages", [])),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            summary=data.get("summary", ""),
            pinned=bool(data.get("pinned", False)),
            archived=bool(data.get("archived", False)),
        )
