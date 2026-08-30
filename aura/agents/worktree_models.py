"""Public value objects for writable Agent worktrees and retained results."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AgentWorktree:
    """One active writable child checkout."""

    change_set_id: str
    agent_id: str
    base_sha: str
    branch_ref: str
    path: Path


@dataclass(frozen=True)
class AgentChangeSet:
    """The minimal durable facts about a checkpointed child result."""

    status: str
    change_set_id: str
    agent_id: str
    base_sha: str
    result_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    diffstat: str = ""
    worktree_path: str = ""
    failure_class: str = ""
    error: str = ""

    @property
    def retained(self) -> bool:
        return self.status != "empty" and bool(self.result_sha or self.worktree_path)

    def payload(self) -> dict[str, Any]:
        visible_paths = self.changed_paths[:50]
        return {
            "status": self.status,
            "change_set_id": self.change_set_id,
            "agent_id": self.agent_id,
            "base_sha": self.base_sha,
            "result_sha": self.result_sha,
            "changed_paths": list(visible_paths),
            "changed_path_count": len(self.changed_paths),
            "changed_paths_truncated": len(self.changed_paths) > len(visible_paths),
            "diffstat": self.diffstat,
            "worktree_path": self.worktree_path,
            "failure_class": self.failure_class,
            "error": self.error,
        }


__all__ = ["AgentChangeSet", "AgentWorktree"]
