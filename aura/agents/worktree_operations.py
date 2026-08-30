"""High-level Git invariants for the writable Agent lifecycle."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_records import WorktreeRecord


class WorktreeGitOperations:
    """Repository/ref/worktree operations above raw Git process execution."""

    def __init__(self, git: GitRunner, *, branch_prefix: str) -> None:
        self.git = git
        self.branch_prefix = branch_prefix

    def require_repository(
        self, workspace_root: Path | None, change_set_id: str
    ) -> Path:
        root = workspace_root
        if root is None or not root.is_dir():
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires an open Git repository.",
                change_set_id=change_set_id,
            )
        proc = self.git.run(
            root,
            ["rev-parse", "--show-toplevel"],
            check=False,
            change_set_id=change_set_id,
            failure_class="git_repository_required",
        )
        if proc.returncode != 0:
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires a Git repository.",
                change_set_id=change_set_id,
            )
        if Path(str(proc.stdout).strip()).resolve() != root.resolve():
            raise AgentWorktreeError(
                "git_repository_root_required",
                "Writable delegation requires the open workspace to be the Git repository root.",
                change_set_id=change_set_id,
            )
        if self.git.text(
            root,
            ["rev-parse", "--is-bare-repository"],
            change_set_id=change_set_id,
            failure_class="git_repository_required",
        ).strip() != "false":
            raise AgentWorktreeError(
                "git_repository_required",
                "Writable delegation requires a non-bare Git working repository.",
                change_set_id=change_set_id,
            )
        return root

    def verify_primary_unchanged(self, record: WorktreeRecord, root: Path) -> None:
        current_ref = self.git.symbolic_head(root, change_set_id=record.change_set_id)
        if current_ref != record.primary_ref:
            self._primary_moved(record, "The primary checkout changed branches")
        head = self.git.rev_parse(root, "HEAD", change_set_id=record.change_set_id)
        if head != record.base_sha:
            self._primary_moved(record, "The primary branch moved")
        if self.git.status_bytes(root, change_set_id=record.change_set_id):
            raise AgentWorktreeError(
                "primary_worktree_dirty",
                "The primary worktree is no longer clean. Nothing was applied; "
                "the Agent result was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )

    def verify_result_ref(self, record: WorktreeRecord) -> None:
        self._require_owned_ref(record, "The recorded result ref is not Aura-owned.")
        current = self.git.ref_sha(
            Path(record.workspace_root),
            record.branch_ref,
            change_set_id=record.change_set_id,
        )
        if current is None:
            raise AgentWorktreeError(
                "change_set_ref_missing",
                "The Aura-owned result ref disappeared. Nothing was changed.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )
        if current != record.result_sha:
            raise AgentWorktreeError(
                "change_set_ref_changed",
                "The Aura-owned result ref moved. Nothing was changed.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )

    def delete_exact_ref(self, record: WorktreeRecord, *, expected: str) -> None:
        self._require_owned_ref(record, "Refusing to delete a ref that is not Aura-owned.")
        root = Path(record.workspace_root)
        current = self.git.ref_sha(
            root, record.branch_ref, change_set_id=record.change_set_id
        )
        if current is None:
            return
        if current != expected:
            raise AgentWorktreeError(
                "change_set_ref_changed",
                "The Aura-owned ref no longer names the expected commit and was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )
        checked_out = self.git.checked_out_worktree(
            root, record.branch_ref, change_set_id=record.change_set_id
        )
        if checked_out:
            raise AgentWorktreeError(
                "change_set_ref_checked_out",
                "The Aura-owned branch is still checked out by a retained worktree "
                f"at {checked_out}; it was preserved.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
                recovery_path=checked_out,
            )
        self.git.run(
            root,
            ["update-ref", "-d", record.branch_ref, expected],
            change_set_id=record.change_set_id,
            failure_class="change_set_cleanup_failed",
        )

    def cleanup_clean_worktree(
        self,
        record: WorktreeRecord,
        *,
        is_owned: Callable[[WorktreeRecord], bool],
    ) -> str:
        """Remove only the exact clean retained checkout; return a refusal reason."""
        root = Path(record.workspace_root)
        try:
            checked_out = self.git.checked_out_worktree(
                root, record.branch_ref, change_set_id=record.change_set_id
            )
            if not record.worktree_path:
                if not checked_out:
                    return ""
                # Recover a stale empty record only from Git's exact branch
                # registration, then subject that path to Aura ownership
                # checks before removing anything.
                record.worktree_path = checked_out
                if not is_owned(record):
                    record.worktree_path = ""
                    return (
                        "The Aura-owned branch is registered to a worktree outside "
                        "its exact Aura-owned path; it was preserved."
                    )
            path = Path(record.worktree_path)
            if not is_owned(record):
                return "Refusing to remove a path that is not the exact Aura-owned worktree."
            if checked_out and Path(checked_out).resolve() != path.resolve():
                return (
                    "The retained path is not the linked worktree checking out "
                    "this exact Aura-owned branch; it was preserved."
                )
            if checked_out:
                if path.exists():
                    if self.git.status_bytes(path, change_set_id=record.change_set_id):
                        return "The recovery worktree is dirty and was preserved."
                    remove_args = ["worktree", "remove", str(path)]
                else:
                    # A missing directory is not proof that Git forgot the
                    # linked worktree. Remove only this exact, verified
                    # registration; never use blanket ``worktree prune``.
                    remove_args = ["worktree", "remove", "--force", str(path)]
                self.git.run(
                    root,
                    remove_args,
                    change_set_id=record.change_set_id,
                    failure_class="worktree_cleanup_failed",
                )
                if self.git.checked_out_worktree(
                    root,
                    record.branch_ref,
                    change_set_id=record.change_set_id,
                ):
                    return "Git still registers the exact retained worktree; it was preserved."
            elif path.exists():
                return (
                    "The retained path is not registered as the linked worktree "
                    "for this exact Aura-owned branch; it was preserved."
                )
        except AgentWorktreeError as exc:
            return str(exc)
        record.worktree_path = ""
        return ""

    def fast_forward(self, record: WorktreeRecord, root: Path, hooks: Path) -> None:
        self.git.run(
            root,
            ["-c", f"core.hooksPath={hooks}", "merge", "--ff-only", "--no-edit", record.result_sha],
            change_set_id=record.change_set_id,
            failure_class="change_set_apply_failed",
        )
        landed = self.git.rev_parse(root, "HEAD", change_set_id=record.change_set_id)
        if landed != record.result_sha:
            raise AgentWorktreeError(
                "change_set_apply_failed",
                "Git did not leave the primary worktree at the approved result commit.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=record.result_sha,
            )

    def _require_owned_ref(self, record: WorktreeRecord, message: str) -> None:
        if not record.branch_ref.startswith(self.branch_prefix):
            raise AgentWorktreeError(
                "change_set_ref_invalid", message, change_set_id=record.change_set_id
            )

    @staticmethod
    def _primary_moved(record: WorktreeRecord, prefix: str) -> None:
        raise AgentWorktreeError(
            "primary_branch_moved",
            f"{prefix} after this Agent started. Nothing was applied; the Agent "
            "result was preserved.",
            change_set_id=record.change_set_id,
            base_sha=record.base_sha,
            result_sha=record.result_sha,
        )


__all__ = ["WorktreeGitOperations"]
