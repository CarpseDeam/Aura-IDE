"""Freeze a semantic automatic team using the shared native Workflow builder."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from aura.agents.local_state import AgentPermission
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentTurnRoster
from aura.agents.team_spec import AgentTeamSpec
from aura.agents.workflow_builder import FrozenModelTargetLookup, build_workflow
from aura.agents.workflow_plan import WorkflowRunPlan, freeze_workflow_plan


@dataclass(frozen=True)
class CompiledAgentTeam:
    task: str
    generated_definitions: tuple[AgentDefinition, ...]
    plan: WorkflowRunPlan


@dataclass(frozen=True)
class _PermissionLookup:
    values: Mapping[str, AgentPermission]

    def permission(self, agent_id: str) -> AgentPermission:
        return self.values.get(agent_id, AgentPermission.READ_ONLY)


def compile_agent_team(
    spec: AgentTeamSpec, *, roster: AgentTurnRoster,
    model_targets: FrozenModelTargetLookup, provider: str, model: str, thinking: str,
) -> tuple[CompiledAgentTeam | None, tuple[str, ...]]:
    if not spec.task.strip():
        return None, ("the Agent team needs a task",)
    built, errors = build_workflow(spec.workflow, roster=roster, model_targets=model_targets)
    if built is None:
        return None, errors
    plan, errors = freeze_workflow_plan(
        built.graph, definitions=built.definitions,
        permissions=_PermissionLookup(built.permissions),
        agent_scopes={key: value.scope for key, value in built.definitions.items()},
        provider=provider.strip(), model=model.strip(), thinking=thinking.strip() or "off",
    )
    if plan is None:
        return None, tuple(f"could not freeze Agent team: {error}" for error in errors)
    return CompiledAgentTeam(spec.task.strip(), built.generated_definitions, plan), ()


__all__ = ["CompiledAgentTeam", "FrozenModelTargetLookup", "compile_agent_team"]
