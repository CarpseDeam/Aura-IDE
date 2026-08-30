"""The frozen plan one workflow run executes.

A drawing on a canvas is not a plan. It is a document the user may still be
editing, pointing at definitions they may still be rewriting, under grants
they may still be changing. A :class:`WorkflowRunPlan` is what that drawing
resolved to at one exact moment — and once resolved it never moves again, so
a run cannot acquire authority, an agent, a model, or a step that was not
there when it was authorized.

What is frozen, and why each of them:

* the **workflow identity and its validated solid order**, so the run cannot
  follow a path that was drawn after it started;
* the **agent definitions**, so editing a brief mid-run does not change what
  the next step is told;
* the **step assignments**, which belong to the occurrence, not the agent;
* **Aura's own provider**, because an agent never chooses one;
* the **resolved model** for each step, under that provider;
* the **thinking selection** for each step; and
* the **local permission** for each step, which is what decides whether the
  run needs a writable worktree at all.

This is the workflow analogue of :class:`~aura.agents.roster.AgentTurnRoster`,
and it is frozen at the same moment for the same reason: a queued turn must
run under the authority it was submitted with, not whatever the window
happens to show when it is finally dequeued.

Nothing here is Qt-aware and nothing here runs anything — see
:mod:`aura.agents.workflow_runner`.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aura.agents.graph_models import WorkflowGraph
from aura.agents.graph_validation import solid_execution_order, validate_graph
from aura.agents.identity import AgentScope
from aura.agents.local_state import DEFAULT_PERMISSION, AgentPermission
from aura.agents.model_resolution import ResolvedTarget, resolve_agent_model
from aura.agents.models import AgentDefinition
from aura.agents.roster import AgentRosterEntry

#: The most steps one frozen workflow may run. A workflow is an ordered
#: hand-off a person drew and can see; past this it is a batch job wearing a
#: canvas, and every step is a full model run the user pays for and waits on.
MAX_WORKFLOW_STEPS = 12


@dataclass(frozen=True)
class WorkflowStepPlan:
    """One agent occurrence, resolved: who runs, on what, and with what authority."""

    node_id: str
    entry: AgentRosterEntry
    assignment: str
    resolved: ResolvedTarget

    @property
    def agent_id(self) -> str:
        return self.entry.agent_id

    @property
    def agent_name(self) -> str:
        return self.entry.name

    @property
    def definition(self) -> AgentDefinition:
        return self.entry.definition

    @property
    def permission(self) -> AgentPermission:
        return self.entry.permission

    @property
    def writable(self) -> bool:
        return self.permission.allows_edit

    def summary_row(self) -> dict[str, str]:
        """The compact identity of this step, for a description or a result."""
        return {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission.value,
            "permission_label": self.permission.label,
            "model": self.resolved.model,
        }


@dataclass(frozen=True)
class WorkflowRunPlan:
    """One workflow, resolved and immutable, ready to be run exactly once."""

    graph_id: str
    scope: AgentScope
    name: str
    description: str
    provider: str
    graph: WorkflowGraph
    steps: tuple[WorkflowStepPlan, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.steps

    @property
    def writable(self) -> bool:
        """True when any reachable step may edit, so the run needs a worktree."""
        return any(step.writable for step in self.steps)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(step.agent_id for step in self.steps)

    def summary_rows(self) -> tuple[dict[str, str], ...]:
        return tuple(step.summary_row() for step in self.steps)

    def catalog_row(self) -> dict[str, Any]:
        """What the model is told about this workflow before it calls it.

        The agents' names and what each was asked to do here, in order — the
        shape of the hand-off. Never their instructions: those are each
        child's own brief and stay in the definition.
        """
        return {
            "graph_id": self.graph_id,
            "name": self.name,
            "description": self.description,
            "writable": self.writable,
            "steps": [
                {
                    "position": index,
                    "agent_name": step.agent_name,
                    "assignment": step.assignment,
                    "permission_label": step.permission.label,
                }
                for index, step in enumerate(self.steps, start=1)
            ],
        }


def freeze_workflow_plan(
    graph: WorkflowGraph | None,
    *,
    definitions: Any,
    permissions: Any,
    agent_scopes: Mapping[str, AgentScope],
    provider: str,
    model: str,
    thinking: str,
) -> tuple[WorkflowRunPlan | None, tuple[str, ...]]:
    """Resolve *graph* into a plan, or report why it cannot be run.

    ``definitions`` is anything with ``get(agent_id) -> AgentDefinition | None``
    and ``permissions`` anything with ``permission(agent_id) -> AgentPermission``
    — the same two injected collaborators
    :func:`~aura.agents.roster.resolve_agent_turn_roster` takes, so this stays
    free of the filesystem and of the user's data directory.

    A workflow that does not validate produces no plan and its reasons. So
    does one whose provider or model cannot be resolved: a run that could not
    have happened is refused here, before anything is created, rather than
    failing halfway through with a worktree already on disk.
    """
    if graph is None:
        return None, ("no workflow is open",)
    verdict = validate_graph(graph, agents=agent_scopes)
    if not verdict.runnable:
        return None, verdict.messages

    order = solid_execution_order(graph)
    if not order:
        return None, ("this workflow has no steps between the Task and the Aura Result",)
    if len(order) > MAX_WORKFLOW_STEPS:
        return None, (
            f"a workflow runs at most {MAX_WORKFLOW_STEPS} steps, and this one "
            f"has {len(order)}",
        )

    steps: list[WorkflowStepPlan] = []
    errors: list[str] = []
    for node in order:
        try:
            definition = definitions.get(node.agent_id)
        except Exception:
            definition = None
        if definition is None:
            errors.append(f"no readable definition for {node.agent_id}")
            continue
        try:
            permission = AgentPermission(permissions.permission(node.agent_id))
        except Exception:
            # A grant that cannot be read falls back to the least authority,
            # never to more — the same rule the agent roster follows.
            permission = DEFAULT_PERMISSION
        resolved, _failure, message = resolve_agent_model(
            definition.model,
            definition.thinking,
            provider=provider,
            turn_model=model,
            turn_thinking=thinking,
        )
        if resolved is None:
            errors.append(f"{definition.name}: {message}")
            continue
        steps.append(
            WorkflowStepPlan(
                node_id=node.node_id,
                entry=AgentRosterEntry(definition=definition, permission=permission),
                assignment=str(node.assignment or "").strip(),
                resolved=resolved,
            )
        )

    if errors:
        return None, tuple(errors)
    return (
        WorkflowRunPlan(
            graph_id=graph.graph_id,
            scope=graph.scope,
            name=graph.name,
            description=graph.description,
            provider=str(provider or "").strip(),
            graph=graph,
            steps=tuple(steps),
        ),
        (),
    )


__all__ = [
    "MAX_WORKFLOW_STEPS",
    "WorkflowRunPlan",
    "WorkflowStepPlan",
    "freeze_workflow_plan",
]
