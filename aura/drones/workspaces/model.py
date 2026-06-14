from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Self


class WorkspacePhase(Enum):
    WORKSHOP = "workshop"
    BUILDING = "building"
    READINESS_RUNNING = "readiness_running"
    READINESS_FAILED = "readiness_failed"
    AWAITING_DECISION = "awaiting_decision"
    ITERATING = "iterating"
    INSTALLING = "installing"
    INSTALLED = "installed"
    DISCARDED = "discarded"


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
    last_readiness_result: dict | None = None
    active_thread_id: str | None = None
    last_error: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class DroneThread:
    id: str
    workspace_id: str
    title: str
    messages: list[dict[str, str]] = field(default_factory=list)
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
