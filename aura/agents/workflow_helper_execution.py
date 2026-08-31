"""Synchronous direct-child execution for frozen workflow helper trees.

The workflow runner owns scheduling and the root result.  This collaborator
owns only what happens after one running workflow Agent calls one of its
directly attached helpers: prompt composition, private child isolation,
recursive direct-child context, observer events, and Step-local invocation
recording.  It creates no scheduler, worktree, checkpoint, or conversation
manager.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_prompt import compose_workflow_helper_message
from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
)
from aura.agents.workflow_children import WorkflowChildSource
from aura.agents.workflow_plan import WorkflowHelperPlan, WorkflowStepPlan
from aura.agents.worktree import AgentWorktree
from aura.config import redact_secrets

logger = logging.getLogger(__name__)


class WorkflowStepState(str, Enum):
    """What one solid Step or helper occurrence is doing in the UI."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


def workflow_state_of(result: DelegationResult) -> WorkflowStepState:
    """Project one delegated result into the shared workflow state enum."""
    if result.status is DelegationStatus.COMPLETED:
        return WorkflowStepState.SUCCEEDED
    if result.status is DelegationStatus.CANCELLED:
        return WorkflowStepState.CANCELLED
    return WorkflowStepState.FAILED


@dataclass(frozen=True)
class WorkflowHelperInvocation:
    """One actual helper call, without any private conversation history."""

    invocation: int
    root_step_node_id: str
    immediate_parent_node_id: str
    parent_invocation: int | None
    helper_node_id: str
    connection_id: str
    depth: int
    lineage: tuple[str, ...]
    agent_id: str
    agent_name: str
    permission: str
    state: WorkflowStepState
    result: DelegationResult
    local_ordinal: int = field(default=0, repr=False)
    parent_local_ordinal: int | None = field(default=None, repr=False)

    @property
    def owning_step_node_id(self) -> str:
        """Compatibility name for the root solid Step."""
        return self.root_step_node_id

    def payload(self) -> dict[str, Any]:
        body = self.result.payload()
        body.pop("tool", None)
        return {
            **body,
            "invocation": self.invocation,
            "owning_step_node_id": self.root_step_node_id,
            "root_step_node_id": self.root_step_node_id,
            "immediate_parent_node_id": self.immediate_parent_node_id,
            "parent_invocation": self.parent_invocation,
            "helper_node_id": self.helper_node_id,
            "connection_id": self.connection_id,
            "depth": self.depth,
            "lineage": list(self.lineage),
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class WorkflowInvocationToken:
    """The Step-local identity allocated before a helper begins."""

    local_ordinal: int
    parent_local_ordinal: int | None


@dataclass
class _InvocationSlot:
    token: WorkflowInvocationToken
    helper: WorkflowHelperPlan
    result: DelegationResult | None = None


class WorkflowInvocationRecorder:
    """Record nested calls by local start order, independent of finish order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_ordinal = 0
        self._slots: dict[int, _InvocationSlot] = {}

    def start(
        self,
        helper: WorkflowHelperPlan,
        *,
        parent_local_ordinal: int | None,
    ) -> WorkflowInvocationToken:
        with self._lock:
            self._next_ordinal += 1
            token = WorkflowInvocationToken(
                local_ordinal=self._next_ordinal,
                parent_local_ordinal=parent_local_ordinal,
            )
            self._slots[token.local_ordinal] = _InvocationSlot(token, helper)
            return token

    def finish(
        self, token: WorkflowInvocationToken, result: DelegationResult
    ) -> None:
        with self._lock:
            slot = self._slots.get(token.local_ordinal)
            if slot is None or slot.token != token:
                raise RuntimeError("Unknown workflow helper invocation token.")
            if slot.result is not None:
                raise RuntimeError("Workflow helper invocation finished twice.")
            slot.result = result

    def invocations(self) -> tuple[WorkflowHelperInvocation, ...]:
        """Completed calls in local start order, ready for global projection."""
        with self._lock:
            slots = tuple(self._slots[index] for index in sorted(self._slots))
        if any(slot.result is None for slot in slots):
            raise RuntimeError("A workflow helper invocation did not quiesce.")
        return tuple(
            WorkflowHelperInvocation(
                invocation=0,
                root_step_node_id=slot.helper.root_step_node_id,
                immediate_parent_node_id=slot.helper.immediate_parent_node_id,
                parent_invocation=None,
                helper_node_id=slot.helper.node_id,
                connection_id=slot.helper.connection_id,
                depth=slot.helper.depth,
                lineage=slot.helper.lineage,
                agent_id=slot.helper.agent_id,
                agent_name=slot.helper.agent_name,
                permission=slot.helper.permission.value,
                state=workflow_state_of(slot.result),
                result=slot.result,
                local_ordinal=slot.token.local_ordinal,
                parent_local_ordinal=slot.token.parent_local_ordinal,
            )
            for slot in slots
            if slot.result is not None
        )


WorkflowParentPlan = WorkflowStepPlan | WorkflowHelperPlan


class WorkflowHelperExecutor:
    """Run only one frozen parent's ordered immediate children."""

    def __init__(
        self,
        *,
        children: WorkflowChildSource,
        parent: WorkflowParentPlan,
        parent_local_invocation: int | None,
        workflow_task: str,
        workspace_root: Path,
        worktree: AgentWorktree | None,
        cancel_event: threading.Event,
        recorder: WorkflowInvocationRecorder,
        notify: Callable[[str, WorkflowStepState], None],
    ) -> None:
        self._children = children
        self._parent = parent
        self._parent_local_invocation = parent_local_invocation
        self._workflow_task = workflow_task
        self._workspace_root = workspace_root
        self._worktree = worktree
        self._cancel = cancel_event
        self._recorder = recorder
        self._notify = notify

    @property
    def _direct_children(self) -> tuple[WorkflowHelperPlan, ...]:
        if isinstance(self._parent, WorkflowStepPlan):
            return self._parent.helpers
        return self._parent.children

    def run(
        self,
        helper: WorkflowHelperPlan,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DelegationResult:
        """Run one direct child synchronously and retain its flat projection."""
        # The registry relays the workflow event. It must remain the sole
        # cancellation authority even if an injected caller supplies another.
        del cancel_event
        frozen = next(
            (
                item
                for item in self._direct_children
                if item.node_id == helper.node_id
                and item.connection_id == helper.connection_id
            ),
            None,
        )
        if frozen is None:
            return DelegationResult.failure(
                helper.agent_id,
                DelegationFailure.AGENT_NOT_AVAILABLE,
                "That helper is not directly attached to this workflow Agent.",
                agent_name=helper.agent_name,
                provider=helper.resolved.provider,
                model=helper.resolved.model,
            )
        helper = frozen
        token = self._recorder.start(
            helper, parent_local_ordinal=self._parent_local_invocation
        )
        self._notify(helper.node_id, WorkflowStepState.RUNNING)
        brief = str(task or "").strip()

        if self._cancel.is_set():
            result = DelegationResult(
                status=DelegationStatus.CANCELLED,
                agent_id=helper.agent_id,
                agent_name=helper.agent_name,
                failure_class="cancelled",
                error="The workflow was stopped before this helper started.",
                provider=helper.resolved.provider,
                model=helper.resolved.model,
            )
        elif not brief:
            result = DelegationResult.failure(
                helper.agent_id,
                DelegationFailure.TASK_MISSING,
                "No bounded task was given to the workflow helper.",
                agent_name=helper.agent_name,
                provider=helper.resolved.provider,
                model=helper.resolved.model,
            )
        elif helper.writable and self._worktree is None:
            result = DelegationResult.failure(
                helper.agent_id,
                DelegationFailure.WORKTREE_CREATION_FAILED,
                "The writable workflow helper has no shared workflow worktree.",
                agent_name=helper.agent_name,
                provider=helper.resolved.provider,
                model=helper.resolved.model,
            )
        else:
            message = compose_workflow_helper_message(
                self._workflow_task,
                helper.assignment,
                brief,
                (
                    f"{self._parent.agent_name} "
                    f"(workflow node {self._parent.node_id})"
                ),
            )
            try:
                nested_runner = (
                    WorkflowHelperExecutor(
                        children=self._children,
                        parent=helper,
                        parent_local_invocation=token.local_ordinal,
                        workflow_task=self._workflow_task,
                        workspace_root=self._workspace_root,
                        worktree=self._worktree,
                        cancel_event=self._cancel,
                        recorder=self._recorder,
                        notify=self._notify,
                    )
                    if helper.children
                    else None
                )
                with self._children.invocation() as child:
                    result, _tests = child.run(
                        helper.entry,
                        message,
                        helper.resolved,
                        self._cancel,
                        workspace_root=self._workspace_root,
                        permission=helper.permission,
                        worktree=self._worktree if helper.writable else None,
                        workflow_helpers=helper.children,
                        workflow_helper_runner=nested_runner,
                        workflow_helper=True,
                    )
            except Exception as exc:
                logger.exception(
                    "agents: workflow helper failed root_step=%s parent_node=%s "
                    "helper_node=%s",
                    helper.root_step_node_id,
                    helper.immediate_parent_node_id,
                    helper.node_id,
                )
                if self._cancel.is_set():
                    result = DelegationResult(
                        status=DelegationStatus.CANCELLED,
                        agent_id=helper.agent_id,
                        agent_name=helper.agent_name,
                        failure_class="cancelled",
                        error=(
                            "The workflow was stopped while this helper was "
                            "running."
                        ),
                        provider=helper.resolved.provider,
                        model=helper.resolved.model,
                    )
                else:
                    result = DelegationResult.failure(
                        helper.agent_id,
                        DelegationFailure.INTERNAL_ERROR,
                        redact_secrets(f"{type(exc).__name__}: {exc}"),
                        agent_name=helper.agent_name,
                        provider=helper.resolved.provider,
                        model=helper.resolved.model,
                    )

        result = replace(
            result,
            agent_id=helper.agent_id,
            agent_name=helper.agent_name,
            provider=helper.resolved.provider,
            model=helper.resolved.model,
            extras={
                **result.extras,
                "workflow_helper": True,
                "owning_step_node_id": helper.root_step_node_id,
                "root_step_node_id": helper.root_step_node_id,
                "immediate_parent_node_id": helper.immediate_parent_node_id,
                "helper_node_id": helper.node_id,
                "connection_id": helper.connection_id,
                "depth": helper.depth,
                "lineage": list(helper.lineage),
                "permission": helper.permission.value,
            },
        )
        self._recorder.finish(token, result)
        self._notify(helper.node_id, workflow_state_of(result))
        return result


__all__ = [
    "WorkflowHelperExecutor",
    "WorkflowHelperInvocation",
    "WorkflowInvocationRecorder",
    "WorkflowStepState",
    "workflow_state_of",
]
