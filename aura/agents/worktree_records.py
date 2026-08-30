"""Private durable records for retained writable Agent change sets."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from aura.agents.local_state import workspace_key
from aura.conversation.tools.fs_write import atomic_write_bytes


@dataclass
class WorktreeRecord:
    change_set_id: str
    agent_id: str
    workspace_root: str
    base_sha: str
    primary_ref: str
    branch_ref: str
    worktree_path: str
    result_sha: str = ""
    state: str = "creating"
    changed_paths: list[str] = field(default_factory=list)
    diffstat: str = ""
    failure_class: str = ""
    error: str = ""

    @classmethod
    def from_json(cls, raw: object) -> "WorktreeRecord | None":
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                change_set_id=str(raw["change_set_id"]),
                agent_id=str(raw["agent_id"]),
                workspace_root=str(raw["workspace_root"]),
                base_sha=str(raw["base_sha"]),
                primary_ref=str(raw.get("primary_ref") or ""),
                branch_ref=str(raw["branch_ref"]),
                worktree_path=str(raw.get("worktree_path") or ""),
                result_sha=str(raw.get("result_sha") or ""),
                state=str(raw.get("state") or "recovery"),
                changed_paths=[str(p) for p in raw.get("changed_paths", [])],
                diffstat=str(raw.get("diffstat") or ""),
                failure_class=str(raw.get("failure_class") or ""),
                error=str(raw.get("error") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def token(self) -> tuple[object, ...]:
        """Immutable identity used to revalidate after an approval wait."""
        return (
            self.change_set_id,
            self.agent_id,
            self.workspace_root,
            self.base_sha,
            self.primary_ref,
            self.branch_ref,
            self.worktree_path,
            self.result_sha,
            self.state,
            tuple(self.changed_paths),
            self.diffstat,
            self.failure_class,
            self.error,
        )


class WorktreeRecordStore:
    """Bind, reload, and atomically save one workspace's runtime records."""

    def __init__(self, runtime_base: Path, workspace_root: Path | None) -> None:
        self.runtime_base = Path(runtime_base)
        self.workspace_root: Path | None = None
        self.records: dict[str, WorktreeRecord] = {}
        self.bind(workspace_root)

    def scope_dir(self, root: Path) -> Path:
        return self.runtime_base / workspace_key(root)[:12]

    @property
    def state_path(self) -> Path | None:
        root = self.workspace_root
        return self.scope_dir(root) / "state.json" if root is not None else None

    def bind(self, root: Path | None) -> None:
        self.workspace_root = root.resolve() if root is not None else None
        self.records = {}
        path = self.state_path
        if path is None or not path.is_file():
            return
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return
        records = raw.get("change_sets") if isinstance(raw, dict) else None
        if not isinstance(records, list):
            return
        expected_root = str(self.workspace_root)
        for item in records:
            record = WorktreeRecord.from_json(item)
            if record is not None and record.workspace_root == expected_root:
                self.records[record.change_set_id] = record

    def save(self) -> None:
        path = self.state_path
        if path is None:
            return
        payload = {
            "version": 1,
            "workspace": str(self.workspace_root),
            "change_sets": [asdict(record) for record in self.records.values()],
        }
        atomic_write_bytes(
            path,
            json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
        )


__all__ = ["WorktreeRecord", "WorktreeRecordStore"]
