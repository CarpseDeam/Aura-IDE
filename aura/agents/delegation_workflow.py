"""Writable delegation orchestration around child execution and worktrees."""
from __future__ import annotations

import logging
import threading
from dataclasses import replace

from aura.agents.child_execution import ChildExecutor
from aura.agents.delegation import DelegationFailure, DelegationResult, DelegationStatus
from aura.agents.local_state import AgentPermission
from aura.agents.model_resolution import ResolvedTarget
from aura.agents.roster import AgentRosterEntry
from aura.agents.worktree import AgentChangeSet, AgentWorktreeError, AgentWorktreeManager
from aura.config import redact_secrets

logger = logging.getLogger(__name__)


class WritableDelegationWorkflow:
    """Create, run, recover, and describe one isolated writable invocation."""

    def __init__(self, worktrees: AgentWorktreeManager, child: ChildExecutor) -> None:
        self._worktrees = worktrees
        self._child = child

    def run(
        self,
        entry: AgentRosterEntry,
        task: str,
        resolved: ResolvedTarget,
        cancel_event: threading.Event | None,
        permission: AgentPermission,
    ) -> DelegationResult:
        definition = entry.definition
        try:
            worktree = self._worktrees.create(definition.agent_id)
        except AgentWorktreeError as exc:
            return DelegationResult(
                status=DelegationStatus.FAILED,
                agent_id=definition.agent_id,
                agent_name=definition.name,
                failure_class=exc.failure_class,
                error=str(exc),
                provider=resolved.provider,
                model=resolved.model,
                permission=permission.value,
                change_set_id=exc.change_set_id,
                base_sha=exc.base_sha,
                result_sha=exc.result_sha,
                extras={"recovery_path": exc.recovery_path} if exc.recovery_path else {},
            )

        tests: tuple[dict, ...] = ()
        try:
            child_result, tests = self._child.run(
                entry,
                task,
                resolved,
                cancel_event,
                workspace_root=worktree.path,
                permission=permission,
                worktree=worktree,
            )
        except Exception as exc:
            logger.exception("agents: writable child failed for %s", definition.agent_id)
            child_result = DelegationResult(
                status=DelegationStatus.FAILED,
                agent_id=definition.agent_id,
                agent_name=definition.name,
                failure_class=DelegationFailure.INTERNAL_ERROR.value,
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
                provider=resolved.provider,
                model=resolved.model,
            )

        # ChildExecutor has closed its ToolRunner and terminated the command
        # tree before returning. Stable worktree state can now be checkpointed.
        try:
            checkpoint = self._worktrees.recover(worktree)
        except AgentWorktreeError as exc:
            status = child_result.status
            if status is DelegationStatus.COMPLETED:
                status = (
                    DelegationStatus.PARTIAL
                    if child_result.result
                    else DelegationStatus.FAILED
                )
            detail = str(exc)
            if child_result.error:
                detail = f"{child_result.error} Checkpoint recovery also failed: {detail}"
            return replace(
                child_result,
                status=status,
                failure_class=exc.failure_class,
                error=detail,
                permission=permission.value,
                change_set_id=worktree.change_set_id,
                base_sha=worktree.base_sha,
                result_sha=exc.result_sha,
                changed_paths=(),
                diffstat="",
                tests_reported=tests,
                extras={
                    **child_result.extras,
                    "recovery_path": exc.recovery_path or str(worktree.path),
                },
            )
        return attach_checkpoint(
            child_result, checkpoint, permission=permission, tests=tests
        )


def attach_checkpoint(
    result: DelegationResult,
    checkpoint: AgentChangeSet,
    *,
    permission: AgentPermission,
    tests: tuple[dict, ...],
) -> DelegationResult:
    extras = dict(result.extras)
    if checkpoint.failure_class:
        extras["lifecycle_warning"] = {
            "failure_class": checkpoint.failure_class,
            "error": checkpoint.error,
            "recovery_path": checkpoint.worktree_path,
        }
    return replace(
        result,
        permission=permission.value,
        change_set_id=checkpoint.change_set_id,
        base_sha=checkpoint.base_sha,
        result_sha=checkpoint.result_sha,
        changed_paths=checkpoint.changed_paths,
        diffstat=checkpoint.diffstat,
        tests_reported=tests,
        extras=extras,
    )


__all__ = ["WritableDelegationWorkflow", "attach_checkpoint"]
