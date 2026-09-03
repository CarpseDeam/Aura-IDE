"""The frozen plan one workflow run executes.

A drawing on a canvas is not a plan. It is a document the user may still be
editing, pointing at definitions they may still be rewriting, under grants
they may still be changing. A :class:`WorkflowRunPlan` is what that drawing
resolved to at one exact moment — and once resolved it never moves again, so
a run cannot acquire authority, an agent, a model, or a step that was not
there when it was authorized.

What is frozen, and why each of them:

* the **workflow identity and its validated solid DAG** — the Steps in a
  settled order, and for each of them the ordered Steps it waits for and the
  ordered Steps it unblocks — so the run cannot follow a shape that was drawn
  after it started, and a join receives its branches in the same order every
  time;
* the **solid and recursively attached helper definitions**, so editing a
  brief mid-run does not change what any child is told;
* the **occurrence assignments and dashed ownership**, which belong to nodes
  and connections rather than reusable agents;
* **Aura's own provider/model/thinking baseline**, from which definitions may
  inherit; and
* the **resolved provider, model, and thinking selection** for every solid and
  helper occurrence at every depth; and
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
from dataclasses import dataclass, field
from typing import Any

from aura.agents.graph_dag import runnable_dag
from aura.agents.graph_models import WorkflowGraph, WorkflowNode
from aura.agents.graph_validation import validate_graph
from aura.agents.helper_topology import read_helper_topology
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
    """One frozen helper occurrence and its ordered direct children."""

    node_id: str
    root_step_node_id: str
    immediate_parent_node_id: str
    connection_id: str
    depth: int
    lineage: tuple[str, ...]
    entry: AgentRosterEntry
    assignment: str
    resolved: ResolvedTarget
    children: tuple["WorkflowHelperPlan", ...] = ()

    @property
    def owning_step_node_id(self) -> str:
        """Compatibility name for the root solid Step."""
        return self.root_step_node_id

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

    @property
    def subtree_writable(self) -> bool:
        """Whether this occurrence or any descendant may edit."""
        return any(item.writable for item in self.preorder())

    def preorder(self) -> tuple["WorkflowHelperPlan", ...]:
        """This occurrence and every descendant in frozen dashed order."""
        ordered: list[WorkflowHelperPlan] = []
        pending = [self]
        visited: set[str] = set()
        while pending:
            item = pending.pop()
            if item.node_id in visited:
                continue
            visited.add(item.node_id)
            ordered.append(item)
            pending.extend(reversed(item.children))
        return tuple(ordered)

    def _base_summary_row(self) -> dict[str, Any]:
        return {
            "helper_node_id": self.node_id,
            "owning_step_node_id": self.root_step_node_id,
            "root_step_node_id": self.root_step_node_id,
            "immediate_parent_node_id": self.immediate_parent_node_id,
            "connection_id": self.connection_id,
            "depth": self.depth,
            "lineage": list(self.lineage),
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission.value,
            "permission_label": self.permission.label,
            "provider": self.resolved.provider,
            "model": self.resolved.model,
        }

    def summary_row(self) -> dict[str, Any]:
        """Durable identity and authority for this dashed occurrence."""
        rows: dict[str, dict[str, Any]] = {}
        for item in reversed(self.preorder()):
            row = item._base_summary_row()
            if item.children:
                row["helpers"] = [rows[child.node_id] for child in item.children]
            rows[item.node_id] = row
        return rows[self.node_id]

    def catalog_row(self) -> dict[str, Any]:
        """Occurrence facts for a workflow catalog or its immediate parent."""
        rows: dict[str, dict[str, Any]] = {}
        for item in reversed(self.preorder()):
            row = {
                **item._base_summary_row(),
                "name": item.agent_name,
                "description": item.definition.description,
                "assignment": item.assignment,
            }
            if item.children:
                row["helpers"] = [rows[child.node_id] for child in item.children]
            rows[item.node_id] = row
        return rows[self.node_id]


@dataclass(frozen=True)
class WorkflowStepPlan:
    """One solid occurrence, its frozen place in the DAG, and its helpers.

    ``predecessors`` is the ordered list of Step node ids this one waits for —
    empty when the workflow task reaches it directly, more than one when it is
    a join. ``successors`` is the ordered list it unblocks. Both are frozen
    with the plan, so a join's bundle and the order branches are considered in
    are decided at submission and never by whichever branch finished first.
    """

    node_id: str
    entry: AgentRosterEntry
    assignment: str
    resolved: ResolvedTarget
    helpers: tuple[WorkflowHelperPlan, ...] = ()
    predecessors: tuple[str, ...] = ()
    successors: tuple[str, ...] = ()
    from_task: bool = False
    to_result: bool = False
    mutation_capable: bool = field(init=False)

    def __post_init__(self) -> None:
        """Freeze the occurrence-wide mutation classification once.

        The solid Step keeps its own permission. Scheduling authority is a
        separate fact: a read-only Step with any Read / Write descendant must
        own the shared worktree exclusively for its whole invocation because
        that descendant can be called while the Step is active.
        """
        object.__setattr__(
            self,
            "mutation_capable",
            self.permission.allows_edit
            or any(helper.subtree_writable for helper in self.helpers),
        )

    @property
    def is_join(self) -> bool:
        return len(self.predecessors) > 1

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

    def summary_row(self) -> dict[str, Any]:
        """The compact identity of this step, for a description or a result."""
        row: dict[str, Any] = {
            "node_id": self.node_id,
            "agent_id": self.agent_id,
            "agent_name": self.agent_name,
            "permission": self.permission.value,
            "permission_label": self.permission.label,
            "provider": self.resolved.provider,
            "model": self.resolved.model,
        }
        if self.helpers:
            row["helpers"] = [helper.summary_row() for helper in self.helpers]
        return row


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

    def step(self, node_id: str) -> WorkflowStepPlan | None:
        return next((item for item in self.steps if item.node_id == node_id), None)

    @property
    def terminal_steps(self) -> tuple[WorkflowStepPlan, ...]:
        """The Steps that hand an answer to the Aura Result, in frozen order.

        This is what makes the hand-off back to Aura deterministic: which
        branches speak to the Aura Result, and in what order, is decided here
        at submission rather than by whichever branch happened to finish last.
        """
        return tuple(step for step in self.steps if step.to_result)

    @property
    def branched(self) -> bool:
        """True when this plan is anything other than one straight line."""
        return (
            any(step.is_join or len(step.successors) > 1 for step in self.steps)
            or len(self.terminal_steps) > 1
            or sum(1 for step in self.steps if step.from_task) > 1
        )

    @property
    def writable(self) -> bool:
        """True when any solid Step or helper descendant may edit."""
        return any(step.mutation_capable for step in self.steps)

    @property
    def agent_ids(self) -> tuple[str, ...]:
        agent_ids: list[str] = []
        for step in self.steps:
            agent_ids.append(step.agent_id)
            for helper in step.helpers:
                agent_ids.extend(item.agent_id for item in helper.preorder())
        return tuple(agent_ids)

    def summary_rows(self) -> tuple[dict[str, Any], ...]:
        return tuple(step.summary_row() for step in self.steps)

    def catalog_row(self) -> dict[str, Any]:
        """What the model is told about this workflow before it calls it.

        The agents' names and what each was asked to do here, in order — the
        shape of the hand-off, including which steps a branched workflow waits
        for. Never their instructions: those are each child's own brief and
        stay in the definition.
        """
        branched = self.branched
        names = {step.node_id: step.agent_name for step in self.steps}
        return {
            "graph_id": self.graph_id,
            "name": self.name,
            "description": self.description,
            "writable": self.writable,
            "branched": branched,
            "steps": [
                {
                    "position": index,
                    "agent_name": step.agent_name,
                    "assignment": step.assignment,
                    "permission_label": step.permission.label,
                    **(
                        {
                            "after": [
                                names.get(node_id, node_id)
                                for node_id in step.predecessors
                            ]
                        }
                        if branched and step.predecessors
                        else {}
                    ),
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
        agent_provider=definition.provider,
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

    dag = runnable_dag(graph)
    if dag is None:
        return None, ("this workflow has no steps between the Task and the Aura Result",)
    if len(dag.steps) > MAX_WORKFLOW_STEPS:
        return None, (
            f"a workflow runs at most {MAX_WORKFLOW_STEPS} steps, and this one "
            f"has {len(dag.steps)}",
        )

    topology = read_helper_topology(graph)
    if not topology.valid:
        # Validation uses this same reader. Keep the freeze boundary
        # independently fail-closed if those seams ever drift apart.
        return None, tuple(issue.message for issue in topology.issues)

    steps: list[WorkflowStepPlan] = []
    errors: list[str] = []
    helper_facts: dict[
        str, tuple[WorkflowNode, AgentRosterEntry, ResolvedTarget]
    ] = {}
    for occurrence in topology.occurrences:
        helper_node = graph.node(occurrence.node_id)
        if helper_node is None:
            errors.append(f"helper {occurrence.node_id} is no longer on the canvas")
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
        helper_facts[occurrence.node_id] = (
            helper_node,
            helper_entry,
            helper_resolved,
        )
    if errors:
        return None, tuple(errors)

    helper_plans: dict[str, WorkflowHelperPlan] = {}
    for occurrence in reversed(topology.occurrences):
        helper_node, helper_entry, helper_resolved = helper_facts[
            occurrence.node_id
        ]
        helper_plans[occurrence.node_id] = WorkflowHelperPlan(
            node_id=helper_node.node_id,
            root_step_node_id=occurrence.root_step_node_id,
            immediate_parent_node_id=occurrence.immediate_parent_node_id,
            connection_id=occurrence.connection_id,
            depth=occurrence.depth,
            lineage=occurrence.lineage,
            entry=helper_entry,
            assignment=str(helper_node.assignment or "").strip(),
            resolved=helper_resolved,
            children=tuple(
                helper_plans[node_id] for node_id in occurrence.child_node_ids
            ),
        )

    for placed in dag.steps:
        node = graph.node(placed.node_id)
        if node is None:
            # solid_dag() only names nodes it read off this graph. Keep the
            # freeze seam independently fail-closed if that ever regresses.
            errors.append(f"step {placed.node_id} is no longer on the canvas")
            continue
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

        direct_helpers = topology.children_of(node.node_id)
        missing_helpers = tuple(
            occurrence.node_id
            for occurrence in direct_helpers
            if occurrence.node_id not in helper_plans
        )
        if missing_helpers:
            errors.append(f"step {node.node_id} has an unresolved direct helper")
            continue

        assert entry is not None and resolved is not None
        steps.append(
            WorkflowStepPlan(
                node_id=node.node_id,
                entry=entry,
                assignment=str(node.assignment or "").strip(),
                resolved=resolved,
                helpers=tuple(
                    helper_plans[occurrence.node_id]
                    for occurrence in direct_helpers
                ),
                predecessors=placed.predecessors,
                successors=placed.successors,
                from_task=placed.from_task,
                to_result=placed.to_result,
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
