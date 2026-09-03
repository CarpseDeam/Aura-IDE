from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Self


@dataclass
class ProjectSpace:
    id: str
    name: str
    root_path: Path
    created_at: str = ""
    updated_at: str = ""
    last_thread_id: str | None = None
    pinned: bool = False
    archived: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.root_path, Path):
            object.__setattr__(self, "root_path", Path(self.root_path))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "root_path": self.root_path.as_posix(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "last_thread_id": self.last_thread_id,
            "pinned": self.pinned,
            "archived": self.archived,
        }

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        return cls(
            id=data.get("id", ""),
            name=data.get("name", ""),
            root_path=Path(data["root_path"]) if data.get("root_path") else Path(),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            last_thread_id=data.get("last_thread_id"),
            pinned=bool(data.get("pinned", False)),
            archived=bool(data.get("archived", False)),
        )


@dataclass
class ProjectThread:
    id: str
    project_id: str
    title: str
    conversation_path: Path | None
    created_at: str = ""
    updated_at: str = ""
    summary: str = ""
    pinned: bool = False
    archived: bool = False
    tags: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.conversation_path is not None and not isinstance(self.conversation_path, Path):
            object.__setattr__(self, "conversation_path", Path(self.conversation_path))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "conversation_path": self.conversation_path.as_posix() if self.conversation_path is not None else None,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "summary": self.summary,
            "pinned": self.pinned,
            "archived": self.archived,
            "tags": list(self.tags),
        }

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        cp = data.get("conversation_path")
        return cls(
            id=data.get("id", ""),
            project_id=data.get("project_id", ""),
            title=data.get("title", ""),
            conversation_path=Path(cp) if cp else None,
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            summary=data.get("summary", ""),
            pinned=bool(data.get("pinned", False)),
            archived=bool(data.get("archived", False)),
            tags=list(data.get("tags", [])),
        )
