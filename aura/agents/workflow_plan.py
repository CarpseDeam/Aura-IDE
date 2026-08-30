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
* the **solid and helper agent definitions**, so editing a brief mid-run does
  not change what any child is told;
* the **occurrence assignments and dashed ownership**, which belong to nodes
  and connections rather than reusable agents;
* **Aura's own provider**, because an agent never chooses one;
* the **resolved model** and **thinking selection** for every solid and helper
  occurrence, under that provider; and
* the **local permission** for every occurrence, which decides whether the run
  needs a writable worktree before the solid path starts.

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

from aura.agents.graph_models import ConnectionKind, WorkflowGraph, WorkflowNode
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
class WorkflowHelperPlan:
    """One frozen helper occurrence owned by exactly one solid workflow step."""

    node_id: str
    owning_step_node_id: str
    connection_id: str
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
        """Durable identity and authority for this dashed occurrence."""
        return {
            "helper_node_id": self.node_id,
            "owning_step_node_id": self.owning_step_node_id,
            "connection_id": self.connection_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission.value,
            "permission_label": self.permission.label,
            "model": self.resolved.model,
        }

    def catalog_row(self) -> dict[str, str]:
        """The occurrence facts its owning step may see in the helper tool."""
        return {
            **self.summary_row(),
            "name": self.agent_name,
            "description": self.definition.description,
            "assignment": self.assignment,
        }


@dataclass(frozen=True)
class WorkflowStepPlan:
    """One solid occurrence and the frozen helpers available only to it."""

    node_id: str
    entry: AgentRosterEntry
    assignment: str
    resolved: ResolvedTarget
    helpers: tuple[WorkflowHelperPlan, ...] = ()

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
        """True when any solid step or attached helper may edit."""
        return any(
            step.writable or any(helper.writable for helper in step.helpers)
            for step in self.steps
        )

    @property
    def agent_ids(self) -> tuple[str, ...]:
        return tuple(
            agent_id
            for step in self.steps
            for agent_id in (
                step.agent_id,
                *(helper.agent_id for helper in step.helpers),
            )
        )

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
                    **(
                        {
                            "helpers": [
                                helper.catalog_row() for helper in step.helpers
                            ]
                        }
                        if step.helpers
                        else {}
                    ),
                }
                for index, step in enumerate(self.steps, start=1)
            ],
        }


def _resolve_occurrence(
    node: WorkflowNode,
    *,
    definitions: Any,
    permissions: Any,
    provider: str,
    model: str,
    thinking: str,
) -> tuple[AgentRosterEntry | None, ResolvedTarget | None, str]:
    """Freeze one graph occurrence without retaining a live store dependency."""
    try:
        definition = definitions.get(node.agent_id)
    except Exception:
        definition = None
    if definition is None:
        return None, None, f"no readable definition for {node.agent_id}"
    try:
        permission = AgentPermission(permissions.permission(node.agent_id))
    except Exception:
        # An unreadable grant always collapses to least authority.
        permission = DEFAULT_PERMISSION
    resolved, _failure, message = resolve_agent_model(
        definition.model,
        definition.thinking,
        provider=provider,
        turn_model=model,
        turn_thinking=thinking,
    )
    if resolved is None:
        return None, None, f"{definition.name}: {message}"
    return AgentRosterEntry(definition=definition, permission=permission), resolved, ""


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
        entry, resolved, occurrence_error = _resolve_occurrence(
            node,
            definitions=definitions,
            permissions=permissions,
            provider=provider,
            model=model,
            thinking=thinking,
        )
        if occurrence_error:
            errors.append(occurrence_error)
            continue

        helpers: list[WorkflowHelperPlan] = []
        for edge in graph.outgoing(node.node_id, ConnectionKind.SUB_AGENT):
            helper_node = graph.node(edge.target_id)
            if helper_node is None:
                # validate_graph() already rejects this. Keep the freeze seam
                # independently fail-closed if a future validator regresses.
                errors.append(
                    f"helper connection {edge.connection_id} has no target node"
                )
                continue
            helper_entry, helper_resolved, helper_error = _resolve_occurrence(
                helper_node,
                definitions=definitions,
                permissions=permissions,
                provider=provider,
                model=model,
                thinking=thinking,
            )
            if helper_error:
                errors.append(helper_error)
                continue
            assert helper_entry is not None and helper_resolved is not None
            helpers.append(
                WorkflowHelperPlan(
                    node_id=helper_node.node_id,
                    owning_step_node_id=node.node_id,
                    connection_id=edge.connection_id,
                    entry=helper_entry,
                    assignment=str(helper_node.assignment or "").strip(),
                    resolved=helper_resolved,
                )
            )

        assert entry is not None and resolved is not None
        steps.append(
            WorkflowStepPlan(
                node_id=node.node_id,
                entry=entry,
                assignment=str(node.assignment or "").strip(),
                resolved=resolved,
                helpers=tuple(helpers),
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
    "WorkflowHelperPlan",
    "WorkflowRunPlan",
    "WorkflowStepPlan",
    "freeze_workflow_plan",
]
