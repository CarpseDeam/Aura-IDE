"""Run a frozen Task → Agent steps → Aura Result plan serially.

Each solid step uses :class:`ChildExecutor` with private history and receives
the task, its assignment, and the previous structured result. A step may call
only its frozen dashed helpers; those calls synchronously reuse ChildExecutor
and return into that step's history. Writable plans use one shared isolated
worktree and checkpoint it once on every exit path. This module creates no
conversation manager, alternate Agent runtime, history, or worktree system.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_execution import ChildExecutor
from aura.agents.child_prompt import (
    compose_workflow_helper_message,
    compose_workflow_step_message,
)
from aura.agents.delegation import DelegationFailure, DelegationResult, DelegationStatus
from aura.agents.workflow_plan import (
    WorkflowHelperPlan,
    WorkflowRunPlan,
    WorkflowStepPlan,
)
from aura.agents.worktree import AgentWorktree, AgentWorktreeError, AgentWorktreeManager
from aura.config import redact_secrets

logger = logging.getLogger(__name__)


class WorkflowRunStatus(str, Enum):
    """How a workflow run ended, as a fact about the run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowStepState(str, Enum):
    """What one step is doing, for a surface that wants to show it."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


#: Presentation-only ``(node_id, state)`` callback.
StepObserver = Callable[[str, WorkflowStepState], None]


@dataclass(frozen=True)
class WorkflowHelperInvocation:
    """One actual helper call, without any of the helper's private history."""

    invocation: int
    owning_step_node_id: str
    helper_node_id: str
    connection_id: str
    agent_id: str
    agent_name: str
    permission: str
    state: WorkflowStepState
    result: DelegationResult

    def payload(self) -> dict[str, Any]:
        body = self.result.payload()
        body.pop("tool", None)
        return {
            **body,
            "invocation": self.invocation,
            "owning_step_node_id": self.owning_step_node_id,
            "helper_node_id": self.helper_node_id,
            "connection_id": self.connection_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class WorkflowStepOutcome:
    """One step's own structured result, paired to its place on the canvas."""

    node_id: str
    state: WorkflowStepState
    result: DelegationResult

    def payload(self) -> dict[str, Any]:
        body = self.result.payload()
        body.pop("tool", None)
        return {"node_id": self.node_id, "state": self.state.value, **body}


@dataclass(frozen=True)
class WorkflowRunResult:
    """The complete, self-contained outcome of one workflow run."""

    status: WorkflowRunStatus
    graph_id: str
    workflow_name: str = ""
    result: str = ""
    steps: tuple[WorkflowStepOutcome, ...] = ()
    helper_invocations: tuple[WorkflowHelperInvocation, ...] = ()
    failure_class: str = ""
    error: str = ""
    change_set_id: str = ""
    base_sha: str = ""
    result_sha: str = ""
    changed_paths: tuple[str, ...] = ()
    diffstat: str = ""
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is WorkflowRunStatus.COMPLETED

    def payload(self) -> dict[str, Any]:
        """The tool-result body returned to Aura's root conversation."""
        visible = self.changed_paths[:50]
        body: dict[str, Any] = {
            "ok": self.ok,
            "tool": "run_agent_workflow",
            "status": self.status.value,
            "workflow": self.workflow_name,
            "graph_id": self.graph_id,
            "result": self.result,
            "steps": [step.payload() for step in self.steps],
        }
        if self.failure_class:
            body["failure_class"] = self.failure_class
        if self.error:
            body["error"] = self.error
        if self.helper_invocations:
            body["helper_invocations"] = [
                invocation.payload() for invocation in self.helper_invocations
            ]
        if self.change_set_id:
            body.update(
                {
                    "change_set_id": self.change_set_id,
                    "base_sha": self.base_sha,
                    "result_sha": self.result_sha,
                    "changed_paths": list(visible),
                    "changed_path_count": len(self.changed_paths),
                    "changed_paths_truncated": len(self.changed_paths) > len(visible),
                    "diffstat": self.diffstat,
                }
            )
        for key, value in self.extras.items():
            body.setdefault(key, value)
        return body

    @classmethod
    def failure(
        cls,
        graph_id: str,
        failure: DelegationFailure,
        error: str,
        *,
        workflow_name: str = "",
    ) -> "WorkflowRunResult":
        return cls(
            status=WorkflowRunStatus.FAILED,
            graph_id=graph_id,
            workflow_name=workflow_name,
            failure_class=failure.value,
            error=error,
        )


class _WorkflowStepHelperRunner:
    """Executes only the frozen helpers owned by one currently running Step."""

    def __init__(
        self,
        *,
        child: ChildExecutor,
        step: WorkflowStepPlan,
        workflow_task: str,
        workspace_root: Path,
        worktree: AgentWorktree | None,
        cancel_event: threading.Event,
        invocations: list[WorkflowHelperInvocation],
        notify: Callable[[str, WorkflowStepState], None],
    ) -> None:
        self._child = child
        self._step = step
        self._workflow_task = workflow_task
        self._workspace_root = workspace_root
        self._worktree = worktree
        self._cancel = cancel_event
        self._invocations = invocations
        self._notify = notify

    def run(
        self,
        helper: WorkflowHelperPlan,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
    ) -> DelegationResult:
        """Run one attached occurrence synchronously and retain its outcome."""
        # The registry relays this same object. Deliberately keep the workflow's
        # event authoritative even if a caller supplies None or another event.
        del cancel_event
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
                self._step.agent_name,
            )
            try:
                result, _tests = self._child.run(
                    helper.entry,
                    message,
                    helper.resolved,
                    self._cancel,
                    workspace_root=self._workspace_root,
                    permission=helper.permission,
                    worktree=self._worktree if helper.writable else None,
                    workflow_helper=True,
                )
            except Exception as exc:
                logger.exception(
                    "agents: workflow helper failed step_node=%s helper_node=%s",
                    self._step.node_id,
                    helper.node_id,
                )
                if self._cancel.is_set():
                    result = DelegationResult(
                        status=DelegationStatus.CANCELLED,
                        agent_id=helper.agent_id,
                        agent_name=helper.agent_name,
                        failure_class="cancelled",
                        error="The workflow was stopped while this helper was running.",
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
                "owning_step_node_id": self._step.node_id,
                "helper_node_id": helper.node_id,
                "connection_id": helper.connection_id,
                "permission": helper.permission.value,
            },
        )
        state = (
            WorkflowStepState.SUCCEEDED
            if result.status is DelegationStatus.COMPLETED
            else WorkflowStepState.CANCELLED
            if result.status is DelegationStatus.CANCELLED
            else WorkflowStepState.FAILED
        )
        self._invocations.append(
            WorkflowHelperInvocation(
                invocation=len(self._invocations) + 1,
                owning_step_node_id=self._step.node_id,
                helper_node_id=helper.node_id,
                connection_id=helper.connection_id,
                agent_id=helper.agent_id,
                agent_name=helper.agent_name,
                permission=helper.permission.value,
                state=state,
                result=result,
            )
        )
        self._notify(helper.node_id, state)
        return result


class WorkflowRunner:
    """Runs one frozen workflow at a time, serially, through ChildExecutor."""

    def __init__(
        self,
        *,
        workspace_root: Path | str | None,
        worktree_manager: AgentWorktreeManager | None = None,
        backend_factory: Callable[[str], Any] | None = None,
        registry_factory: Callable[[Path], Any] | None = None,
        child: ChildExecutor | None = None,
    ) -> None:
        from aura.agents.runtime import _default_backend_factory

        self._workspace_root = (
            Path(workspace_root) if workspace_root is not None else None
        )
        self._worktrees = worktree_manager or AgentWorktreeManager(workspace_root)
        self._child = child or ChildExecutor(
            backend_factory=backend_factory or _default_backend_factory,
            registry_factory=registry_factory,
        )
        self._lock = threading.Lock()

    def set_workspace_root(self, root: Path | str | None) -> None:
        self._workspace_root = Path(root) if root is not None else None
        self._worktrees.set_workspace_root(root)

    @property
    def worktree_manager(self) -> AgentWorktreeManager:
        return self._worktrees

    # ---- running -----------------------------------------------------------

    def run(
        self,
        plan: WorkflowRunPlan,
        task: str,
        *,
        cancel_event: threading.Event | None = None,
        on_step: StepObserver | None = None,
    ) -> WorkflowRunResult:
        """Run every step of *plan* in order and report one structured result."""
        graph_id = plan.graph_id if plan is not None else ""
        name = plan.name if plan is not None else ""
        brief = str(task or "").strip()
        if plan is None or plan.is_empty:
            return WorkflowRunResult.failure(
                graph_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "This workflow has no steps to run.",
                workflow_name=name,
            )
        if not brief:
            return WorkflowRunResult.failure(
                graph_id,
                DelegationFailure.TASK_MISSING,
                "No task was given to the workflow.",
                workflow_name=name,
            )
        if self._workspace_root is None:
            return WorkflowRunResult.failure(
                graph_id,
                DelegationFailure.WORKSPACE_REQUIRED,
                "No workspace is open, so there is nothing a workflow could read.",
                workflow_name=name,
            )
        if not self._lock.acquire(blocking=False):
            return WorkflowRunResult.failure(
                graph_id,
                DelegationFailure.DELEGATION_BUSY,
                "Another workflow is already running. Workflows run one at a time.",
                workflow_name=name,
            )
        started = time.monotonic()
        try:
            return self._run_locked(plan, brief, cancel_event, on_step)
        except Exception as exc:
            logger.exception("agents: workflow run failed for %s", graph_id)
            return WorkflowRunResult.failure(
                graph_id,
                DelegationFailure.INTERNAL_ERROR,
                redact_secrets(f"{type(exc).__name__}: {exc}"),
                workflow_name=name,
            )
        finally:
            self._lock.release()
            logger.info(
                "workflow_run_finished graph_id=%s steps=%s duration_ms=%s",
                graph_id,
                len(plan.steps) if plan is not None else 0,
                int((time.monotonic() - started) * 1000),
            )

    def _run_locked(
        self,
        plan: WorkflowRunPlan,
        task: str,
        cancel_event: threading.Event | None,
        on_step: StepObserver | None,
    ) -> WorkflowRunResult:
        cancel = cancel_event if cancel_event is not None else threading.Event()
        worktree: AgentWorktree | None = None
        helper_invocations: list[WorkflowHelperInvocation] = []
        if plan.writable:
            # One worktree makes every step see the edits before it.
            try:
                worktree = self._worktrees.create(f"workflow-{plan.graph_id}")
            except AgentWorktreeError as exc:
                return WorkflowRunResult(
                    status=WorkflowRunStatus.FAILED,
                    graph_id=plan.graph_id,
                    workflow_name=plan.name,
                    failure_class=exc.failure_class,
                    error=str(exc),
                    change_set_id=exc.change_set_id,
                    base_sha=exc.base_sha,
                    extras=(
                        {"recovery_path": exc.recovery_path}
                        if exc.recovery_path
                        else {}
                    ),
                )

        logger.info(
            "workflow_run_start graph_id=%s steps=%s writable=%s",
            plan.graph_id,
            len(plan.steps),
            plan.writable,
        )
        try:
            outcomes, status, failure_class, error = self._run_steps(
                plan,
                task,
                cancel,
                on_step,
                worktree,
                helper_invocations,
            )
            answer = next(
                (
                    outcome.result.result
                    for outcome in reversed(outcomes)
                    if outcome.state is WorkflowStepState.SUCCEEDED
                    and outcome.result.result
                ),
                "",
            )
            run = WorkflowRunResult(
                status=status,
                graph_id=plan.graph_id,
                workflow_name=plan.name,
                result=answer,
                steps=tuple(outcomes),
                helper_invocations=tuple(helper_invocations),
                failure_class=failure_class,
                error=error,
            )
        except Exception as exc:
            # Once a shared worktree exists, even an unexpected orchestration
            # failure must flow through the same final recovery/checkpoint.
            logger.exception("agents: workflow orchestration failed for %s", plan.graph_id)
            run = WorkflowRunResult.failure(
                plan.graph_id,
                DelegationFailure.INTERNAL_ERROR,
                redact_secrets(f"{type(exc).__name__}: {exc}"),
                workflow_name=plan.name,
            )
            run = replace(run, helper_invocations=tuple(helper_invocations))
        if worktree is None:
            return run
        return self._checkpoint(run, worktree)

    def _run_steps(
        self,
        plan: WorkflowRunPlan,
        task: str,
        cancel: threading.Event,
        on_step: StepObserver | None,
        worktree: AgentWorktree | None,
        helper_invocations: list[WorkflowHelperInvocation],
    ) -> tuple[list[WorkflowStepOutcome], WorkflowRunStatus, str, str]:
        """Run the steps until one stops the workflow, and say why it stopped."""
        root = worktree.path if worktree is not None else self._workspace_root
        outcomes: list[WorkflowStepOutcome] = []
        previous: DelegationResult | None = None
        status = WorkflowRunStatus.COMPLETED
        failure_class = ""
        error = ""

        for index, step in enumerate(plan.steps):
            if cancel.is_set():
                status = WorkflowRunStatus.CANCELLED
                failure_class = failure_class or "cancelled"
                error = error or "The run was stopped before this step started."
                cancelled = DelegationResult(
                    status=DelegationStatus.CANCELLED,
                    agent_id=step.agent_id,
                    agent_name=step.agent_name,
                    failure_class="cancelled",
                    error=error,
                    provider=step.resolved.provider,
                    model=step.resolved.model,
                )
                outcomes.append(
                    WorkflowStepOutcome(
                        step.node_id, WorkflowStepState.CANCELLED, cancelled
                    )
                )
                self._notify(on_step, step.node_id, WorkflowStepState.CANCELLED)
                outcomes.extend(self._skipped(plan.steps[index + 1 :], on_step))
                break

            self._notify(on_step, step.node_id, WorkflowStepState.RUNNING)
            result = self._run_step(
                step,
                plan,
                task,
                previous,
                cancel,
                root,
                worktree,
                on_step,
                helper_invocations,
            )

            if result.status is DelegationStatus.COMPLETED:
                outcomes.append(
                    WorkflowStepOutcome(
                        step.node_id, WorkflowStepState.SUCCEEDED, result
                    )
                )
                self._notify(on_step, step.node_id, WorkflowStepState.SUCCEEDED)
                previous = result
                continue

            state = (
                WorkflowStepState.CANCELLED
                if result.status is DelegationStatus.CANCELLED
                else WorkflowStepState.FAILED
            )
            outcomes.append(WorkflowStepOutcome(step.node_id, state, result))
            self._notify(on_step, step.node_id, state)
            failure_class = result.failure_class or state.value
            error = result.error or f"{step.agent_name} did not finish its step."
            if state is WorkflowStepState.CANCELLED:
                status = WorkflowRunStatus.CANCELLED
            else:
                # Earlier steps did real work and their answers are reported.
                # Calling the whole run "failed" when two of three finished
                # would be the untrue half of the story.
                status = (
                    WorkflowRunStatus.PARTIAL
                    if any(
                        item.state is WorkflowStepState.SUCCEEDED for item in outcomes
                    )
                    else WorkflowRunStatus.FAILED
                )
            outcomes.extend(self._skipped(plan.steps[index + 1:], on_step))
            break

        return outcomes, status, failure_class, error

    def _run_step(
        self,
        step: WorkflowStepPlan,
        plan: WorkflowRunPlan,
        task: str,
        previous: DelegationResult | None,
        cancel: threading.Event,
        root: Path,
        worktree: AgentWorktree | None,
        on_step: StepObserver | None,
        helper_invocations: list[WorkflowHelperInvocation],
    ) -> DelegationResult:
        """One step: one ordinary child run, with this step's own authority."""
        message = compose_workflow_step_message(
            task,
            step.assignment,
            previous.payload() if previous is not None else None,
            previous.agent_name if previous is not None else "",
        )
        try:
            helper_kwargs: dict[str, Any] = {}
            if step.helpers:
                helper_runner = _WorkflowStepHelperRunner(
                    child=self._child,
                    step=step,
                    workflow_task=task,
                    workspace_root=root,
                    worktree=worktree,
                    cancel_event=cancel,
                    invocations=helper_invocations,
                    notify=lambda node_id, state: self._notify(
                        on_step, node_id, state
                    ),
                )
                helper_kwargs = {
                    "workflow_helpers": step.helpers,
                    "workflow_helper_runner": helper_runner,
                }
            result, _tests = self._child.run(
                step.entry,
                message,
                step.resolved,
                cancel,
                workspace_root=root,
                permission=step.permission,
                # Read-only still reads the shared tree, without its write grant.
                worktree=worktree if step.writable else None,
                workflow_step=True,
                **helper_kwargs,
            )
            return result
        except Exception as exc:
            logger.exception(
                "agents: workflow step failed graph_id=%s agent_id=%s",
                plan.graph_id,
                step.agent_id,
            )
            return DelegationResult(
                status=DelegationStatus.FAILED,
                agent_id=step.agent_id,
                agent_name=step.agent_name,
                failure_class=DelegationFailure.INTERNAL_ERROR.value,
                error=redact_secrets(f"{type(exc).__name__}: {exc}"),
                provider=step.resolved.provider,
                model=step.resolved.model,
            )

    def _checkpoint(
        self, run: WorkflowRunResult, worktree: AgentWorktree
    ) -> WorkflowRunResult:
        """Retain the shared worktree once, however the run ended."""
        try:
            checkpoint = self._worktrees.recover(worktree)
        except AgentWorktreeError as exc:
            status = run.status
            if status is WorkflowRunStatus.COMPLETED:
                status = (
                    WorkflowRunStatus.PARTIAL if run.result else WorkflowRunStatus.FAILED
                )
            detail = str(exc)
            if run.error:
                detail = f"{run.error} Checkpoint recovery also failed: {detail}"
            return replace(
                run,
                status=status,
                failure_class=exc.failure_class,
                error=detail,
                change_set_id=worktree.change_set_id,
                base_sha=worktree.base_sha,
                result_sha=exc.result_sha,
                extras={
                    **run.extras,
                    "recovery_path": exc.recovery_path or str(worktree.path),
                },
            )
        extras = dict(run.extras)
        if checkpoint.failure_class:
            extras["lifecycle_warning"] = {
                "failure_class": checkpoint.failure_class,
                "error": checkpoint.error,
                "recovery_path": checkpoint.worktree_path,
            }
        return replace(
            run,
            change_set_id=checkpoint.change_set_id,
            base_sha=checkpoint.base_sha,
            result_sha=checkpoint.result_sha,
            changed_paths=checkpoint.changed_paths,
            diffstat=checkpoint.diffstat,
            extras=extras,
        )

    @staticmethod
    def _skipped(
        steps: tuple[WorkflowStepPlan, ...], on_step: StepObserver | None
    ) -> list[WorkflowStepOutcome]:
        """Report the steps that never ran, rather than leaving them unsaid."""
        outcomes: list[WorkflowStepOutcome] = []
        for step in steps:
            result = DelegationResult.failure(
                step.agent_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "The workflow stopped before this step ran.",
                agent_name=step.agent_name,
            )
            outcomes.append(
                WorkflowStepOutcome(step.node_id, WorkflowStepState.SKIPPED, result)
            )
            WorkflowRunner._notify(on_step, step.node_id, WorkflowStepState.SKIPPED)
        return outcomes

    @staticmethod
    def _notify(
        on_step: StepObserver | None, node_id: str, state: WorkflowStepState
    ) -> None:
        if on_step is None:
            return
        try:
            on_step(node_id, state)
        except Exception:  # pragma: no cover - presentation must never fail a run
            logger.debug("agents: workflow step observer raised", exc_info=True)


__all__ = [
    "StepObserver",
    "WorkflowHelperInvocation",
    "WorkflowRunResult",
    "WorkflowRunStatus",
    "WorkflowRunner",
    "WorkflowStepOutcome",
    "WorkflowStepState",
]
