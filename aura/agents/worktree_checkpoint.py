"""Create one stable runtime commit from an Agent's final worktree state."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aura.agents.worktree_git import AgentWorktreeError, GitRunner
from aura.agents.worktree_material import ChangeSetMaterializer
from aura.agents.worktree_records import WorktreeRecord

_GIT_IDENTITY = (
    "-c", "user.name=Aura Agent",
    "-c", "user.email=agent@aura.local",
    "-c", "commit.gpgSign=false",
)


@dataclass(frozen=True)
class CheckpointOutcome:
    empty: bool
    result_sha: str
    changed_paths: tuple[str, ...] = ()
    diffstat: str = ""


class WorktreeCheckpointer:
    def __init__(self, git: GitRunner, material: ChangeSetMaterializer) -> None:
        self._git = git
        self._material = material

    def create_checkpoint(
        self, record: WorktreeRecord, worktree_path: Path, hooks: Path
    ) -> CheckpointOutcome:
        symbolic = self._git.text(
            worktree_path,
            ["symbolic-ref", "-q", "HEAD"],
            change_set_id=record.change_set_id,
            failure_class="checkpoint_failed",
        ).strip()
        if symbolic != record.branch_ref:
            raise AgentWorktreeError(
                "checkpoint_ref_changed",
                "The child left its Aura-owned branch. Its worktree was preserved "
                "for recovery and no foreign ref was changed.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                recovery_path=str(worktree_path),
            )

        current = self._git.rev_parse(
            worktree_path, "HEAD", change_set_id=record.change_set_id
        )
        if current != record.base_sha:
            self._git.run(
                worktree_path,
                ["reset", "--soft", record.base_sha],
                change_set_id=record.change_set_id,
                failure_class="checkpoint_failed",
            )
        self._git.run(
            worktree_path,
            ["add", "-A", "--", "."],
            change_set_id=record.change_set_id,
            failure_class="checkpoint_failed",
        )
        staged = self._git.run(
            worktree_path,
            ["diff", "--cached", "--quiet", "--exit-code"],
            check=False,
            change_set_id=record.change_set_id,
            failure_class="checkpoint_failed",
        )
        if staged.returncode not in (0, 1):
            self._git.raise_failure(
                staged,
                "checkpoint_failed",
                record.change_set_id,
                record.base_sha,
                str(worktree_path),
            )
        if staged.returncode == 0:
            return CheckpointOutcome(empty=True, result_sha=record.base_sha)

        hooks.mkdir(parents=True, exist_ok=True)
        self._git.run(
            worktree_path,
            [
                *_GIT_IDENTITY,
                "-c",
                f"core.hooksPath={hooks}",
                "commit",
                "--no-verify",
                "-m",
                f"Aura agent change set {record.change_set_id}",
            ],
            change_set_id=record.change_set_id,
            failure_class="checkpoint_failed",
        )
        result_sha = self._git.rev_parse(
            worktree_path, "HEAD", change_set_id=record.change_set_id
        )
        parents = self._git.text(
            worktree_path,
            ["rev-list", "--parents", "-n", "1", result_sha],
            change_set_id=record.change_set_id,
            failure_class="checkpoint_failed",
        ).strip().split()
        if len(parents) != 2 or parents[1] != record.base_sha:
            raise AgentWorktreeError(
                "checkpoint_shape_invalid",
                "The Agent result was not one commit directly above its frozen base.",
                change_set_id=record.change_set_id,
                base_sha=record.base_sha,
                result_sha=result_sha,
                recovery_path=str(worktree_path),
            )
        return CheckpointOutcome(
            empty=False,
            result_sha=result_sha,
            changed_paths=self._material.changed_paths(
                worktree_path, record.base_sha, result_sha, record.change_set_id
            ),
            diffstat=self._material.diffstat(
                worktree_path, record.base_sha, result_sha, record.change_set_id
            ),
        )


__all__ = ["CheckpointOutcome", "WorktreeCheckpointer"]
