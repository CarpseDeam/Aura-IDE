"""Deterministic readiness and worktree-exclusivity scheduling for workflows.

This collaborator is deliberately non-Qt and does not execute children.  The
coordinator gives it the frozen plan plus the Steps already settled, and it
returns the next deterministic decision: newly blocked Steps and one runnable
wave.  It never observes completion order and owns no mutable run state.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aura.agents.workflow_plan import WorkflowRunPlan, WorkflowStepPlan


@dataclass(frozen=True)
class BlockedWorkflowStep:
    """One fully-settled dependency failure, in frozen predecessor order."""

    step: WorkflowStepPlan
    blocked_by: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowScheduleDecision:
    """The coordinator work discovered at one stable scheduling boundary."""

    blocked: tuple[BlockedWorkflowStep, ...] = ()
    wave: tuple[WorkflowStepPlan, ...] = ()


class WorkflowWaveScheduler:
    """Choose dependency-ready waves under the shared-worktree contract."""

    def __init__(self, plan: WorkflowRunPlan) -> None:
        self._steps = plan.steps

    def decide(
        self, settled_success: Mapping[str, bool]
    ) -> WorkflowScheduleDecision:
        """Return newly blocked Steps and the next runnable wave.

        ``settled_success`` is coordinator-owned and maps every settled node to
        whether it succeeded.  A Step is not classified until all of its
        predecessors appear there.  Both blocked and ready rows retain frozen
        plan order; ``blocked_by`` retains each Step's frozen predecessor order.
        """
        blocked: list[BlockedWorkflowStep] = []
        ready: list[WorkflowStepPlan] = []
        for step in self._steps:
            if step.node_id in settled_success:
                continue
            if not all(node_id in settled_success for node_id in step.predecessors):
                continue
            failed = tuple(
                node_id
                for node_id in step.predecessors
                if not settled_success[node_id]
            )
            if failed:
                blocked.append(BlockedWorkflowStep(step=step, blocked_by=failed))
            else:
                ready.append(step)

        wave: tuple[WorkflowStepPlan, ...] = ()
        if ready:
            if ready[0].mutation_capable:
                wave = (ready[0],)
            else:
                prefix: list[WorkflowStepPlan] = []
                for step in ready:
                    if step.mutation_capable:
                        break
                    prefix.append(step)
                wave = tuple(prefix)
        return WorkflowScheduleDecision(blocked=tuple(blocked), wave=wave)


__all__ = [
    "BlockedWorkflowStep",
    "WorkflowScheduleDecision",
    "WorkflowWaveScheduler",
]
