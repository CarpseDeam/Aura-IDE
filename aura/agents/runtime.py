"""Public foreground runner for root-to-Agent delegation.

Model resolution, private child execution, and writable worktree orchestration
live in focused collaborators. This module owns invocation policy: frozen
authority, prerequisites, and the one active foreground slot.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_execution import ChildExecutor, reported_tests
from aura.agents.child_prompt import compose_child_task_message
from aura.agents.delegation import DelegationFailure, DelegationResult
from aura.agents.delegation_workflow import WritableDelegationWorkflow
from aura.agents.local_state import AgentPermission
from aura.agents.model_resolution import ResolvedTarget, resolve_agent_model
from aura.agents.roster import AgentRosterEntry
from aura.agents.worktree import AgentWorktree, AgentWorktreeManager
from aura.config import redact_secrets
from aura.conversation.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)


class AgentDelegationRunner:
    """Resolve and run delegated children, one foreground invocation at a time."""

    def __init__(
        self,
        *,
        workspace_root: Path | str | None,
        inherited_provider: str = "",
        inherited_model: str = "",
        inherited_thinking: str = "off",
        backend_factory: Callable[[str], Any] | None = None,
        registry_factory: Callable[[Path], ToolRegistry] | None = None,
        worktree_manager: AgentWorktreeManager | None = None,
    ) -> None:
        self._workspace_root = Path(workspace_root) if workspace_root is not None else None
        self._inherited_provider = str(inherited_provider or "")
        self._inherited_model = str(inherited_model or "")
        self._inherited_thinking = str(inherited_thinking or "off")
        self._worktrees = worktree_manager or AgentWorktreeManager(workspace_root)
        self._child = ChildExecutor(
            backend_factory=backend_factory or _default_backend_factory,
            registry_factory=registry_factory,
        )
        self._writable = WritableDelegationWorkflow(self._worktrees, self._child)
        self._lock = threading.Lock()

    def set_inherited_target(self, *, provider: str, model: str, thinking: str) -> None:
        self._inherited_provider = str(provider or "")
        self._inherited_model = str(model or "")
        self._inherited_thinking = str(thinking or "off")

    def set_workspace_root(self, root: Path | str | None) -> None:
        self._workspace_root = Path(root) if root is not None else None
        self._worktrees.set_workspace_root(root)

    @property
    def worktree_manager(self) -> AgentWorktreeManager:
        return self._worktrees

    def run(
        self,
        entry: AgentRosterEntry,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DelegationResult:
        definition = entry.definition
        agent_id = definition.agent_id
        permission = AgentPermission(entry.permission)
        brief = compose_child_task_message(task)
        if not brief:
            return DelegationResult.failure(
                agent_id, DelegationFailure.TASK_MISSING,
                "No task was given to the agent.", agent_name=definition.name,
            )
        workspace_root = self._workspace_root
        if workspace_root is None:
            return DelegationResult.failure(
                agent_id, DelegationFailure.WORKSPACE_REQUIRED,
                "No workspace is open, so there is nothing an agent could read.",
                agent_name=definition.name,
            )

        resolved, failure, message = resolve_agent_model(
            definition.model,
            definition.thinking,
            provider=self._inherited_provider,
            turn_model=self._inherited_model,
            turn_thinking=self._inherited_thinking,
            agent_provider=definition.provider,
        )
        if resolved is None:
            return DelegationResult.failure(
                agent_id, failure or DelegationFailure.INTERNAL_ERROR, message,
                agent_name=definition.name,
                provider=definition.provider.strip() or self._inherited_provider,
                model=definition.model,
            )
        if not self._lock.acquire(blocking=False):
            return DelegationResult.failure(
                agent_id, DelegationFailure.DELEGATION_BUSY,
                "Another agent is already running. Delegation is one at a time.",
                agent_name=definition.name,
                provider=resolved.provider,
                model=resolved.model,
            )
        try:
            if permission.allows_edit:
                return self._run_writable(entry, brief, resolved, cancel_event, permission)
            result, _tests = self._run_child(
                entry,
                brief,
                resolved,
                cancel_event,
                workspace_root=workspace_root,
                permission=permission,
            )
            return result
        except Exception as exc:
            logger.exception("agents: delegated run failed for %s", agent_id)
            return DelegationResult.failure(
                agent_id,
                DelegationFailure.INTERNAL_ERROR,
                redact_secrets(f"{type(exc).__name__}: {exc}"),
                agent_name=definition.name,
                provider=resolved.provider,
                model=resolved.model,
            )
        finally:
            self._lock.release()

    def _run_writable(
        self,
        entry: AgentRosterEntry,
        task: str,
        resolved: ResolvedTarget,
        cancel_event: threading.Event | None,
        permission: AgentPermission,
    ) -> DelegationResult:
        return self._writable.run(entry, task, resolved, cancel_event, permission)

    def _run_child(
        self,
        entry: AgentRosterEntry,
        task: str,
        resolved: ResolvedTarget,
        cancel_event: threading.Event | None,
        *,
        workspace_root: Path,
        permission: AgentPermission,
        worktree: AgentWorktree | None = None,
    ):
        """Compatibility seam whose implementation belongs to ChildExecutor."""
        return self._child.run(
            entry,
            task,
            resolved,
            cancel_event,
            workspace_root=workspace_root,
            permission=permission,
            worktree=worktree,
        )

    def _child_registry(
        self, workspace_root: Path, permission: AgentPermission
    ) -> ToolRegistry:
        """Compatibility seam; ChildExecutor owns registry construction."""
        return self._child._registry(workspace_root, permission)


def _reported_tests(history):
    """Backward-compatible import seam for structured child validations."""
    return reported_tests(history)


def _default_backend_factory(provider: str) -> Any:
    from aura.backends import APIAgentBackend

    return APIAgentBackend(provider=provider)


__all__ = ["AgentDelegationRunner", "ResolvedTarget", "resolve_agent_model"]
