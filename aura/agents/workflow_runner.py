"""Run a frozen Task → Agent DAG → Aura Result plan in the foreground.

One deterministic scheduler walks the plan's frozen Steps in their settled
order and runs each ready one to completion before looking at the next: every
outgoing branch runs unconditionally, each occurrence runs at most once, and a
join runs only once every Step it waits for has succeeded. Nothing here is
concurrent — a branch is a shape, not a thread.

Each Step uses :class:`ChildExecutor` with private history and receives the
task, its assignment, and its predecessors' structured results — one of them
for an ordinary hand-off, an ordered bundle of all of them at a join. A Step
may call only its frozen dashed helpers; those calls synchronously reuse
ChildExecutor and return into that Step's history. Writable plans use one
shared isolated worktree and checkpoint it once on every exit path. This
module creates no conversation manager, alternate Agent runtime, history, or
worktree system.
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
    compose_workflow_join_message,
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
class WorkflowBranchResult:
    """One terminal branch's answer, as the Aura Result received it.

    Which branches speak to the Aura Result, and in what order, is frozen with
    the plan — so this is never a report of whichever branch finished last.
    """

    node_id: str
    agent_name: str
    state: WorkflowStepState
    result: str = ""
    error: str = ""

    def payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "node_id": self.node_id,
            "agent_name": self.agent_name,
            "state": self.state.value,
            "result": self.result,
        }
        if self.error:
            body["error"] = self.error
        return body


@dataclass(frozen=True)
class WorkflowRunResult:
    """The complete, self-contained outcome of one workflow run."""

    status: WorkflowRunStatus
    graph_id: str
    workflow_name: str = ""
    result: str = ""
    steps: tuple[WorkflowStepOutcome, ...] = ()
    branch_results: tuple[WorkflowBranchResult, ...] = ()
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
        if len(self.branch_results) > 1:
            # Only a workflow that actually ends in several branches reports
            # them. A linear run has one answer and says exactly that.
            body["branch_results"] = [
                branch.payload() for branch in self.branch_results
            ]
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
        state = _state_of(result)
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
            branches = _branch_results(plan, outcomes)
            run = WorkflowRunResult(
                status=status,
                graph_id=plan.graph_id,
                workflow_name=plan.name,
                result=_final_answer(outcomes, branches),
                steps=tuple(outcomes),
                branch_results=branches,
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
        """Walk the frozen DAG once, in order, and say how the whole run ended.

        The plan's Steps are already in dependency order, so one pass is the
        whole scheduler: by the time a Step is reached every Step it waits for
        has been decided. A Step whose predecessors all succeeded runs; one
        blocked by a failed predecessor is marked not run and named for what
        blocked it; an independent branch beside it is untouched and carries
        on. Cancellation stops the pass wherever it is: the Step that was
        about to start is cancelled and the rest are left unrun.
        """
        root = worktree.path if worktree is not None else self._workspace_root
        outcomes: list[WorkflowStepOutcome] = []
        settled: dict[str, WorkflowStepOutcome] = {}
        succeeded: set[str] = set()
        stopped = False
        failure_class = ""
        error = ""

        for step in plan.steps:
            if cancel.is_set():
                outcome = self._cancelled_outcome(step, first=not stopped)
                if not stopped:
                    stopped = True
                    failure_class = failure_class or "cancelled"
                    error = error or outcome.result.error
            else:
                blocked = tuple(
                    node_id
                    for node_id in step.predecessors
                    if node_id not in succeeded
                )
                if blocked:
                    outcome = self._blocked_outcome(plan, step, blocked)
                else:
                    self._notify(on_step, step.node_id, WorkflowStepState.RUNNING)
                    result = self._run_step(
                        step,
                        plan,
                        task,
                        tuple(settled[node_id] for node_id in step.predecessors),
                        cancel,
                        root,
                        worktree,
                        on_step,
                        helper_invocations,
                    )
                    outcome = WorkflowStepOutcome(
                        step.node_id, _state_of(result), result
                    )
                    if outcome.state is WorkflowStepState.SUCCEEDED:
                        succeeded.add(step.node_id)
                    elif not failure_class:
                        failure_class = result.failure_class or outcome.state.value
                        error = (
                            result.error
                            or f"{step.agent_name} did not finish its step."
                        )
                    if outcome.state is WorkflowStepState.CANCELLED:
                        stopped = True

            outcomes.append(outcome)
            settled[step.node_id] = outcome
            self._notify(on_step, step.node_id, outcome.state)

        status = _run_status(outcomes)
        if status is WorkflowRunStatus.CANCELLED:
            # Cancellation is a fact about the whole run, not just the first
            # non-success it encountered. Preserve earlier failures on their
            # Step outcomes, while sourcing the run-level reason from the
            # first cancelled Step in the plan's frozen order.
            cancelled = next(
                outcome
                for outcome in outcomes
                if outcome.state is WorkflowStepState.CANCELLED
            )
            failure_class = cancelled.result.failure_class or "cancelled"
            error = (
                cancelled.result.error
                or "The workflow was stopped before it could finish."
            )
        return outcomes, status, failure_class, error

    @staticmethod
    def _cancelled_outcome(
        step: WorkflowStepPlan, *, first: bool
    ) -> WorkflowStepOutcome:
        """The Step a stop landed on, and every Step left behind it."""
        if first:
            return WorkflowStepOutcome(
                step.node_id,
                WorkflowStepState.CANCELLED,
                DelegationResult(
                    status=DelegationStatus.CANCELLED,
                    agent_id=step.agent_id,
                    agent_name=step.agent_name,
                    failure_class="cancelled",
                    error="The run was stopped before this step started.",
                    provider=step.resolved.provider,
                    model=step.resolved.model,
                ),
            )
        return WorkflowStepOutcome(
            step.node_id,
            WorkflowStepState.SKIPPED,
            DelegationResult.failure(
                step.agent_id,
                DelegationFailure.DELEGATION_UNAVAILABLE,
                "The workflow stopped before this step ran.",
                agent_name=step.agent_name,
            ),
        )

    @staticmethod
    def _blocked_outcome(
        plan: WorkflowRunPlan, step: WorkflowStepPlan, blocked: tuple[str, ...]
    ) -> WorkflowStepOutcome:
        """A Step that never ran because a Step it joins did not succeed.

        Named rather than merely skipped: the reason carries the node ids and
        the agents that blocked it, so a person reading the result can see
        which branch stopped and which ones carried on regardless.
        """
        names = []
        for node_id in blocked:
            blocker = plan.step(node_id)
            names.append(blocker.agent_name if blocker is not None else node_id)
        reason = (
            f"This step runs after {names[0]}, which did not succeed"
            if len(names) == 1
            else "This step runs after "
            + ", ".join(names[:-1])
            + f" and {names[-1]}, and not all of them succeeded"
        )
        result = DelegationResult.failure(
            step.agent_id,
            DelegationFailure.DEPENDENCY_NOT_MET,
            f"{reason}, so it was not run. Other branches of this workflow were "
            "not affected.",
            agent_name=step.agent_name,
        )
        return WorkflowStepOutcome(
            step.node_id,
            WorkflowStepState.SKIPPED,
            replace(result, extras={**result.extras, "blocked_by": list(blocked)}),
        )

    def _run_step(
        self,
        step: WorkflowStepPlan,
        plan: WorkflowRunPlan,
        task: str,
        inbound: tuple[WorkflowStepOutcome, ...],
        cancel: threading.Event,
        root: Path,
        worktree: AgentWorktree | None,
        on_step: StepObserver | None,
        helper_invocations: list[WorkflowHelperInvocation],
    ) -> DelegationResult:
        """One step: one ordinary child run, with this step's own authority."""
        message = _step_message(task, step, inbound)
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
    def _notify(
        on_step: StepObserver | None, node_id: str, state: WorkflowStepState
    ) -> None:
        if on_step is None:
            return
        try:
            on_step(node_id, state)
        except Exception:  # pragma: no cover - presentation must never fail a run
            logger.debug("agents: workflow step observer raised", exc_info=True)


def _state_of(result: DelegationResult) -> WorkflowStepState:
    if result.status is DelegationStatus.COMPLETED:
        return WorkflowStepState.SUCCEEDED
    if result.status is DelegationStatus.CANCELLED:
        return WorkflowStepState.CANCELLED
    return WorkflowStepState.FAILED


def _run_status(outcomes: list[WorkflowStepOutcome]) -> WorkflowRunStatus:
    """How the whole DAG ended, read off every Step rather than the last one.

    Earlier branches did real work and their answers are reported, so calling
    the whole run "failed" when two of three branches finished would be the
    untrue half of the story.
    """
    states = [outcome.state for outcome in outcomes]
    if any(state is WorkflowStepState.CANCELLED for state in states):
        return WorkflowRunStatus.CANCELLED
    if all(state is WorkflowStepState.SUCCEEDED for state in states):
        return WorkflowRunStatus.COMPLETED
    if any(state is WorkflowStepState.SUCCEEDED for state in states):
        return WorkflowRunStatus.PARTIAL
    return WorkflowRunStatus.FAILED


def _step_message(
    task: str, step: WorkflowStepPlan, inbound: tuple[WorkflowStepOutcome, ...]
) -> str:
    """What this Step is asked, given what reached it.

    One predecessor is the ordinary hand-off and is worded exactly as it
    always has been — a linear workflow reads no differently for having a DAG
    behind it. Several is a join, and gets the whole ordered bundle.
    """
    if len(inbound) > 1:
        return compose_workflow_join_message(
            task, step.assignment, tuple(item.payload() for item in inbound)
        )
    previous = inbound[0].result if inbound else None
    return compose_workflow_step_message(
        task,
        step.assignment,
        previous.payload() if previous is not None else None,
        previous.agent_name if previous is not None else "",
    )


def _branch_results(
    plan: WorkflowRunPlan, outcomes: list[WorkflowStepOutcome]
) -> tuple[WorkflowBranchResult, ...]:
    """What each terminal branch handed the Aura Result, in frozen order."""
    settled = {outcome.node_id: outcome for outcome in outcomes}
    branches: list[WorkflowBranchResult] = []
    for step in plan.terminal_steps:
        outcome = settled.get(step.node_id)
        if outcome is None:
            continue
        branches.append(
            WorkflowBranchResult(
                node_id=step.node_id,
                agent_name=step.agent_name,
                state=outcome.state,
                result=outcome.result.result,
                error=outcome.result.error,
            )
        )
    return tuple(branches)


def _final_answer(
    outcomes: list[WorkflowStepOutcome], branches: tuple[WorkflowBranchResult, ...]
) -> str:
    """The workflow's answer to Aura, chosen by the drawing and never by time.

    One terminal branch keeps the behaviour a linear workflow has always had:
    the last Step in the frozen order that actually answered. Several terminal
    branches have no single answer to pick, and choosing one would be choosing
    for Aura — so all of them are handed over, labelled and in the workflow's
    own order, for Aura to write the user-facing response from.
    """
    if len(branches) > 1:
        answered = [branch for branch in branches if branch.result]
        if answered:
            return "\n\n".join(
                f"{branch.agent_name}\n{branch.result}" for branch in answered
            )
    return next(
        (
            outcome.result.result
            for outcome in reversed(outcomes)
            if outcome.state is WorkflowStepState.SUCCEEDED and outcome.result.result
        ),
        "",
    )


__all__ = [
    "StepObserver",
    "WorkflowBranchResult",
    "WorkflowHelperInvocation",
    "WorkflowRunResult",
    "WorkflowRunStatus",
    "WorkflowRunner",
    "WorkflowStepOutcome",
    "WorkflowStepState",
]
