"""Run a frozen Task → Agent DAG → Aura Result plan in the foreground.

A focused deterministic scheduler selects dependency-ready waves. Independent
read-only Steps in the wave run concurrently against one stable workspace
view; every mutation-capable Step runs alone. Every outgoing branch remains
unconditional, each occurrence runs at most once, and a join runs only after
every Step it waits for has settled successfully.

Each Step uses :class:`ChildExecutor` with private history and receives the
task, its assignment, and its predecessors' structured results — one of them
for an ordinary hand-off, an ordered bundle of all of them at a join. A Step
may call only its frozen immediate dashed helpers. Each helper follows the
same rule for its own direct children, and every synchronous call reuses the
child-execution path before returning into its immediate caller's history.
Writable plans use one shared isolated worktree and checkpoint it once on
every exit path. This module creates no conversation manager, alternate Agent
runtime, history, or worktree system.
"""
from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from aura.agents.child_execution import ChildExecutor
from aura.agents.child_prompt import (
    compose_workflow_join_message,
    compose_workflow_step_message,
)
from aura.agents.delegation import (
    DelegationFailure,
    DelegationResult,
    DelegationStatus,
    DelegationUsage,
)
from aura.agents.workflow_children import WorkflowChildSource
from aura.agents.workflow_helper_execution import (
    WorkflowHelperExecutor,
    WorkflowHelperInvocation,
    WorkflowInvocationRecorder,
    WorkflowStepState,
    workflow_state_of,
)
from aura.agents.workflow_plan import (
    WorkflowRunPlan,
    WorkflowStepPlan,
)
from aura.agents.workflow_scheduler import WorkflowWaveScheduler
from aura.agents.worktree import AgentWorktree, AgentWorktreeError, AgentWorktreeManager
from aura.config import redact_secrets

logger = logging.getLogger(__name__)


class WorkflowRunStatus(str, Enum):
    """How a workflow run ended, as a fact about the run."""

    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


#: Presentation-only ``(node_id, state)`` callback.
StepObserver = Callable[[str, WorkflowStepState], None]


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
class WorkflowUsageGroup:
    """One provider/model telemetry group in deterministic plan order."""

    provider: str
    model: str
    usage: DelegationUsage

    def payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            **self.usage.as_dict(),
        }


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
    usage_groups: tuple[WorkflowUsageGroup, ...] = ()
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
        if self.usage_groups:
            body["usage_groups"] = [group.payload() for group in self.usage_groups]
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


@dataclass(frozen=True)
class _StepWorkerResult:
    result: DelegationResult
    helper_invocations: tuple[WorkflowHelperInvocation, ...] = ()


@dataclass(frozen=True)
class _WorkerDone:
    step: WorkflowStepPlan
    future: concurrent.futures.Future[_StepWorkerResult]


@dataclass(frozen=True)
class _WorkerObserverEvent:
    node_id: str
    state: WorkflowStepState


class WorkflowRunner:
    """Run one blocking workflow with a single coordinator and safe workers."""

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
        child_prototype = child or ChildExecutor(
            backend_factory=backend_factory or _default_backend_factory,
            registry_factory=registry_factory,
        )
        self._children = WorkflowChildSource(child_prototype)
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
        """Run every Step under the frozen DAG and report one ordered result."""
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
        helper_invocations: tuple[WorkflowHelperInvocation, ...] = ()
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
            outcomes, helper_invocations = self._run_steps(
                plan,
                task,
                cancel,
                on_step,
                worktree,
            )
            status, failure_class, error = _run_metadata(outcomes)
            branches = _branch_results(plan, outcomes)
            run = WorkflowRunResult(
                status=status,
                graph_id=plan.graph_id,
                workflow_name=plan.name,
                result=_final_answer(outcomes, branches),
                steps=tuple(outcomes),
                branch_results=branches,
                helper_invocations=helper_invocations,
                usage_groups=_usage_groups(plan, outcomes, helper_invocations),
                failure_class=failure_class,
                error=error,
            )
        except Exception as exc:
            # Once a shared worktree exists, even an unexpected orchestration
            # failure must flow through the same final recovery/checkpoint.
            cancel.set()
            logger.exception("agents: workflow orchestration failed for %s", plan.graph_id)
            run = WorkflowRunResult.failure(
                plan.graph_id,
                DelegationFailure.INTERNAL_ERROR,
                redact_secrets(f"{type(exc).__name__}: {exc}"),
                workflow_name=plan.name,
            )
            run = replace(run, helper_invocations=helper_invocations)
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
    ) -> tuple[list[WorkflowStepOutcome], tuple[WorkflowHelperInvocation, ...]]:
        """Coordinate deterministic waves and project only after quiescence."""
        root = worktree.path if worktree is not None else self._workspace_root
        assert root is not None
        scheduler = WorkflowWaveScheduler(plan)
        settled: dict[str, WorkflowStepOutcome] = {}
        settled_success: dict[str, bool] = {}
        helpers_by_step: dict[str, tuple[WorkflowHelperInvocation, ...]] = {}
        events: queue.Queue[_WorkerDone | _WorkerObserverEvent] = queue.Queue()
        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=len(plan.steps),
            thread_name_prefix="aura-workflow",
        )
        active: set[concurrent.futures.Future[_StepWorkerResult]] = set()

        try:
            while len(settled) < len(plan.steps):
                if cancel.is_set():
                    break

                decision = scheduler.decide(settled_success)
                for blocked in decision.blocked:
                    outcome = self._blocked_outcome(
                        plan, blocked.step, blocked.blocked_by
                    )
                    settled[blocked.step.node_id] = outcome
                    settled_success[blocked.step.node_id] = False
                    self._notify(on_step, blocked.step.node_id, outcome.state)

                launched: list[
                    tuple[
                        WorkflowStepPlan,
                        concurrent.futures.Future[_StepWorkerResult],
                    ]
                ] = []
                for step in decision.wave:
                    if cancel.is_set():
                        break
                    inbound = tuple(
                        settled[node_id] for node_id in step.predecessors
                    )
                    self._notify(on_step, step.node_id, WorkflowStepState.RUNNING)
                    if cancel.is_set():
                        break
                    future = executor.submit(
                        self._run_step,
                        step,
                        plan,
                        task,
                        inbound,
                        cancel,
                        root,
                        worktree,
                        events,
                    )
                    active.add(future)
                    launched.append((step, future))
                    future.add_done_callback(
                        lambda done, frozen_step=step: events.put(
                            _WorkerDone(frozen_step, done)
                        )
                    )

                # The whole wave settles before coordinator state changes or
                # readiness is recomputed. Helper presentation events are
                # relayed here, never from a pool thread.
                worker_results: dict[str, _StepWorkerResult] = {}
                remaining = len(launched)
                while remaining:
                    event = events.get()
                    if isinstance(event, _WorkerObserverEvent):
                        self._notify(on_step, event.node_id, event.state)
                        continue
                    remaining -= 1
                    active.discard(event.future)
                    try:
                        worker = event.future.result()
                    except Exception as exc:
                        worker = _StepWorkerResult(
                            self._internal_error_result(event.step, exc)
                        )
                    worker_results[event.step.node_id] = worker
                    self._notify(
                        on_step,
                        event.step.node_id,
                        workflow_state_of(worker.result),
                    )

                for step, _future in launched:
                    worker = worker_results[step.node_id]
                    outcome = WorkflowStepOutcome(
                        step.node_id,
                        workflow_state_of(worker.result),
                        worker.result,
                    )
                    settled[step.node_id] = outcome
                    settled_success[step.node_id] = (
                        outcome.state is WorkflowStepState.SUCCEEDED
                    )
                    helpers_by_step[step.node_id] = worker.helper_invocations

                if cancel.is_set():
                    break
                if not launched and not decision.blocked:
                    raise RuntimeError(
                        "The frozen workflow scheduler could not settle every Step."
                    )
        except Exception:
            # No recovery/checkpoint may race live child or tool activity.
            cancel.set()
            for future in tuple(active):
                try:
                    future.result()
                except Exception:
                    pass
            raise
        finally:
            executor.shutdown(wait=True, cancel_futures=False)

        if cancel.is_set() and len(settled) < len(plan.steps):
            already_cancelled = any(
                outcome.state is WorkflowStepState.CANCELLED
                for outcome in settled.values()
            )
            for step in plan.steps:
                if step.node_id in settled:
                    continue
                outcome = self._cancelled_outcome(
                    step, first=not already_cancelled
                )
                already_cancelled = (
                    already_cancelled
                    or outcome.state is WorkflowStepState.CANCELLED
                )
                settled[step.node_id] = outcome
                settled_success[step.node_id] = False
                self._notify(on_step, step.node_id, outcome.state)

        outcomes = [settled[step.node_id] for step in plan.steps]
        helper_invocations: list[WorkflowHelperInvocation] = []
        invocation = 0
        for step in plan.steps:
            local_to_global: dict[int, int] = {}
            for item in helpers_by_step.get(step.node_id, ()):
                invocation += 1
                local_to_global[item.local_ordinal] = invocation
                parent_invocation = (
                    local_to_global.get(item.parent_local_ordinal)
                    if item.parent_local_ordinal is not None
                    else None
                )
                if (
                    item.parent_local_ordinal is not None
                    and parent_invocation is None
                ):
                    raise RuntimeError(
                        "A nested helper was recorded before its immediate parent."
                    )
                helper_invocations.append(
                    replace(
                        item,
                        invocation=invocation,
                        parent_invocation=parent_invocation,
                    )
                )
        return outcomes, tuple(helper_invocations)

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
        events: queue.Queue[_WorkerDone | _WorkerObserverEvent],
    ) -> _StepWorkerResult:
        """One step: one ordinary child run, with this step's own authority."""
        message = _step_message(task, step, inbound)
        recorder = WorkflowInvocationRecorder()
        try:
            helper_kwargs: dict[str, Any] = {}
            if step.helpers:
                helper_runner = WorkflowHelperExecutor(
                    children=self._children,
                    parent=step,
                    parent_local_invocation=None,
                    workflow_task=task,
                    workspace_root=root,
                    worktree=worktree,
                    cancel_event=cancel,
                    recorder=recorder,
                    notify=lambda node_id, state: events.put(
                        _WorkerObserverEvent(node_id, state)
                    ),
                )
                helper_kwargs = {
                    "workflow_helpers": step.helpers,
                    "workflow_helper_runner": helper_runner,
                }
            with self._children.invocation() as child:
                result, _tests = child.run(
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
            return _StepWorkerResult(result, recorder.invocations())
        except Exception as exc:
            logger.exception(
                "agents: workflow step failed graph_id=%s agent_id=%s",
                plan.graph_id,
                step.agent_id,
            )
            return _StepWorkerResult(
                self._internal_error_result(step, exc), recorder.invocations()
            )

    @staticmethod
    def _internal_error_result(
        step: WorkflowStepPlan, exc: Exception
    ) -> DelegationResult:
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


def _run_metadata(
    outcomes: list[WorkflowStepOutcome],
) -> tuple[WorkflowRunStatus, str, str]:
    """Choose run metadata from frozen Step order, never completion order."""
    status = _run_status(outcomes)
    if status is WorkflowRunStatus.CANCELLED:
        cancelled = next(
            outcome
            for outcome in outcomes
            if outcome.state is WorkflowStepState.CANCELLED
        )
        return (
            status,
            cancelled.result.failure_class or "cancelled",
            cancelled.result.error
            or "The workflow was stopped before it could finish.",
        )
    failure = next(
        (
            outcome
            for outcome in outcomes
            if outcome.state is WorkflowStepState.FAILED
        ),
        None,
    )
    if failure is None:
        return status, "", ""
    return (
        status,
        failure.result.failure_class or failure.state.value,
        failure.result.error
        or f"{failure.result.agent_name or failure.node_id} did not finish its step.",
    )


def _usage_groups(
    plan: WorkflowRunPlan,
    outcomes: list[WorkflowStepOutcome],
    helpers: tuple[WorkflowHelperInvocation, ...],
) -> tuple[WorkflowUsageGroup, ...]:
    """Aggregate usage by exact provider/model in frozen occurrence order."""
    outcomes_by_node = {outcome.node_id: outcome for outcome in outcomes}
    helpers_by_step: dict[str, list[WorkflowHelperInvocation]] = {}
    for helper in helpers:
        helpers_by_step.setdefault(helper.owning_step_node_id, []).append(helper)

    totals: dict[tuple[str, str], list[int]] = {}

    def add(result: DelegationResult) -> None:
        usage = result.usage
        if usage is None or usage.is_empty or not result.provider or not result.model:
            return
        values = totals.setdefault((result.provider, result.model), [0, 0, 0, 0])
        values[0] += usage.prompt_tokens
        values[1] += usage.completion_tokens
        values[2] += usage.cache_hit_tokens
        values[3] += usage.cache_miss_tokens

    for step in plan.steps:
        outcome = outcomes_by_node.get(step.node_id)
        if outcome is not None:
            add(outcome.result)
        for helper in helpers_by_step.get(step.node_id, ()):  # local call order
            add(helper.result)

    return tuple(
        WorkflowUsageGroup(
            provider=provider,
            model=model,
            usage=DelegationUsage(
                prompt_tokens=values[0],
                completion_tokens=values[1],
                cache_hit_tokens=values[2],
                cache_miss_tokens=values[3],
            ),
        )
        for (provider, model), values in totals.items()
    )


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
    "WorkflowUsageGroup",
]
