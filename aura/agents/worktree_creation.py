"""Prepare and materialize one exact Aura-owned linked Git worktree."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_operations import WorktreeGitOperations
from aura.agents.worktree_records import WorktreeRecord


class WorktreeCreator:
    """Own the high-level Git mechanics of writable checkout creation."""

    def __init__(
        self,
        git: GitRunner,
        operations: WorktreeGitOperations,
        *,
        branch_prefix: str,
    ) -> None:
        self._git = git
        self._operations = operations
        self._branch_prefix = branch_prefix

    def prepare(
        self,
        *,
        workspace_root: Path | None,
        scope: Path,
        change_set_id: str,
        agent_id: str,
    ) -> WorktreeRecord:
        root = self._operations.require_repository(workspace_root, change_set_id)
        base_sha = self._git.rev_parse(root, "HEAD", change_set_id=change_set_id)
        primary_ref = self._git.symbolic_head(root, change_set_id=change_set_id)
        if self._git.status_bytes(root, change_set_id=change_set_id):
            raise AgentWorktreeError(
                "primary_worktree_dirty",
                "Writable delegation requires a completely clean primary worktree; "
                "staged, unstaged, or untracked changes are present.",
                change_set_id=change_set_id,
                base_sha=base_sha,
            )

        worktree_path = scope / change_set_id
        branch_ref = f"{self._branch_prefix}{scope.name}/{change_set_id}"
        if worktree_path.exists() or self._git.ref_sha(
            root, branch_ref, change_set_id=change_set_id
        ):
            raise AgentWorktreeError(
                "worktree_name_collision",
                "Aura generated a worktree identity that is already in use.",
                change_set_id=change_set_id,
                base_sha=base_sha,
            )
        return WorktreeRecord(
            change_set_id=change_set_id,
            agent_id=str(agent_id),
            workspace_root=str(root),
            base_sha=base_sha,
            primary_ref=primary_ref,
            branch_ref=branch_ref,
            worktree_path=str(worktree_path),
        )

    def materialize(self, record: WorktreeRecord, *, hooks: Path) -> None:
        path = Path(record.worktree_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        hooks.mkdir(parents=True, exist_ok=True)
        self._git.run(
            Path(record.workspace_root),
            [
                "-c",
                f"core.hooksPath={hooks}",
                "worktree",
                "add",
                "-b",
                record.branch_ref.removeprefix("refs/heads/"),
                str(path),
                record.base_sha,
            ],
            change_set_id=record.change_set_id,
            failure_class="worktree_creation_failed",
        )

    def cleanup_failed(
        self,
        record: WorktreeRecord,
        *,
        is_owned: Callable[[WorktreeRecord], bool],
    ) -> bool:
        """Best-effort exact cleanup; return whether no artifact remains."""
        path = Path(record.worktree_path)
        if path.exists() and is_owned(record):
            try:
                path.rmdir()
            except OSError:
                pass
        if path.exists() and is_owned(record):
            try:
                if not self._git.status_bytes(path, change_set_id=record.change_set_id):
                    self._git.run(
                        Path(record.workspace_root),
                        ["worktree", "remove", str(path)],
                        check=False,
                        change_set_id=record.change_set_id,
                        failure_class="worktree_cleanup_failed",
                    )
            except AgentWorktreeError:
                return False
        if path.exists():
            return False
        current = self._git.ref_sha(
            Path(record.workspace_root),
            record.branch_ref,
            change_set_id=record.change_set_id,
        )
        if current == record.base_sha:
            try:
                self._operations.delete_exact_ref(record, expected=record.base_sha)
            except AgentWorktreeError:
                return False
        return self._git.ref_sha(
            Path(record.workspace_root),
            record.branch_ref,
            change_set_id=record.change_set_id,
        ) is None


__all__ = ["WorktreeCreator"]
