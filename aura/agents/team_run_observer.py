"""Presentation observer for one automatically assembled Agent team.

The automatic-team tool owns compilation and execution.  A caller may attach
this observer to project those already-authoritative facts somewhere visible,
but the observer grants no capability and may never influence the run.
"""
from __future__ import annotations

from typing import Protocol

from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.workflow_helper_execution import WorkflowStepState
from aura.agents.workflow_runner import WorkflowRunResult


class AgentTeamRunObserver(Protocol):
    """Receives the accepted plan, live occurrence states, and final result."""

    def team_accepted(self, team: CompiledAgentTeam) -> None: ...

    def step_changed(
        self,
        graph_id: str,
        node_id: str,
        state: WorkflowStepState,
    ) -> None: ...

    def team_finished(self, result: WorkflowRunResult) -> None: ...


__all__ = ["AgentTeamRunObserver"]
