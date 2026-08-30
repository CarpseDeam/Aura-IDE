"""Whether a drawn workflow is one Aura could actually run.

This is the whole definition of the first runnable topology, in one non-Qt
place, so the canvas, the inspector, and any later runtime all answer the
question identically:

* exactly one Task and one Aura Result;
* one unbranched, acyclic solid path from the Task to the Aura Result, with
  every agent occurrence on it;
* no solid branch, join, loop, or line that points at a node that is not
  there;
* sub-agent lines that hang one level off a step and never form a cycle;
* every agent reference resolving to a definition this workspace can read,
  and one a workflow of this scope is allowed to use.

Nothing here deletes, rewrites, or substitutes anything. A workflow that
fails every rule still loads, still draws, and still saves — an agent whose
definition has gone missing stays on the canvas saying so, because silently
dropping it would throw away the only record that it was ever meant to be
there. Validation only decides what is *marked*, never what survives.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowGraph,
    WorkflowNodeKind,
)
from aura.agents.identity import AgentScope

#: What a node says when the agent it refers to is not in the library. The
#: canvas shows this verbatim, so it is fixed here rather than in a widget.
MISSING_AGENT_LABEL = "Agent missing"


@dataclass(frozen=True)
class GraphIssue:
    """One reason a workflow cannot be run yet, and where it is visible."""

    message: str
    node_id: str = ""
    connection_id: str = ""


@dataclass(frozen=True)
class GraphValidation:
    """The complete verdict on one workflow."""

    issues: tuple[GraphIssue, ...] = ()

    @property
    def runnable(self) -> bool:
        return not self.issues

    @property
    def messages(self) -> tuple[str, ...]:
        return tuple(issue.message for issue in self.issues)

    def for_node(self, node_id: str) -> tuple[GraphIssue, ...]:
        return tuple(issue for issue in self.issues if issue.node_id == node_id)

    def for_connection(self, connection_id: str) -> tuple[GraphIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.connection_id == connection_id
        )

    @property
    def summary(self) -> str:
        if self.runnable:
            return "This workflow is complete and could be run."
        count = len(self.issues)
        noun = "thing" if count == 1 else "things"
        return f"{count} {noun} to fix before this workflow could be run."


def reference_scope_error(graph_scope: AgentScope, agent_scope: AgentScope) -> str:
    """The one rule about which agents a workflow of each scope may use.

    A project workflow travels with the repository, so it may only name
    agents that travel with it. A personal workflow is only ever read by the
    person it belongs to, so it may name either.
    """
    if graph_scope is AgentScope.PROJECT and agent_scope is AgentScope.PERSONAL:
        return (
            "a project workflow cannot use a personal agent — everyone who opens "
            "this project would see a workflow referring to an agent only you have"
        )
    return ""


def validate_graph(
    graph: WorkflowGraph,
    *,
    agents: Mapping[str, AgentScope],
) -> GraphValidation:
    """Check *graph* against the first runnable topology.

    *agents* maps every readable agent id to the scope it was discovered in.
    An id that is missing from it is reported as a missing agent, never
    quietly removed.
    """
    issues: list[GraphIssue] = []
    issues.extend(_fixed_end_issues(graph))
    issues.extend(_reference_issues(graph, agents))
    issues.extend(_dangling_issues(graph))
    issues.extend(_step_issues(graph))
    issues.extend(_sub_agent_issues(graph))
    return GraphValidation(tuple(issues))


# ---- the two fixed ends ----------------------------------------------------


def _fixed_end_issues(graph: WorkflowGraph) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    for kind in (WorkflowNodeKind.TASK, WorkflowNodeKind.AURA_RESULT):
        found = graph.nodes_of_kind(kind)
        if len(found) == 1:
            continue
        if not found:
            issues.append(GraphIssue(f"this workflow has no {kind.label} node"))
        else:
            issues.append(
                GraphIssue(
                    f"a workflow has exactly one {kind.label} node, and this one "
                    f"has {len(found)}"
                )
            )
    return issues


# ---- what the nodes refer to -----------------------------------------------


def _reference_issues(
    graph: WorkflowGraph, agents: Mapping[str, AgentScope]
) -> list[GraphIssue]:
    issues: list[GraphIssue] = []
    for node in graph.nodes:
        if not node.is_agent:
            continue
        scope = agents.get(node.agent_id)
        if scope is None:
            issues.append(
                GraphIssue(
                    f"{MISSING_AGENT_LABEL}: no readable definition for "
                    f"{node.agent_id}",
                    node_id=node.node_id,
                )
            )
            continue
        error = reference_scope_error(graph.scope, scope)
        if error:
            issues.append(GraphIssue(error, node_id=node.node_id))
    return issues


def _dangling_issues(graph: WorkflowGraph) -> list[GraphIssue]:
    known = {node.node_id for node in graph.nodes}
    issues: list[GraphIssue] = []
    for edge in graph.connections:
        missing = [end for end in (edge.source_id, edge.target_id) if end not in known]
        if missing:
            issues.append(
                GraphIssue(
                    "a connection points at a node that is not on this canvas",
                    connection_id=edge.connection_id,
                )
            )
        elif edge.source_id == edge.target_id:
            issues.append(
                GraphIssue(
                    "a connection cannot join a node to itself",
                    connection_id=edge.connection_id,
                )
            )
    return issues


# ---- the solid path --------------------------------------------------------


def _step_issues(graph: WorkflowGraph) -> list[GraphIssue]:
    known = {node.node_id for node in graph.nodes}
    steps = tuple(
        edge
        for edge in graph.connections_of_kind(ConnectionKind.STEP)
        if edge.source_id in known
        and edge.target_id in known
        and edge.source_id != edge.target_id
    )
    issues: list[GraphIssue] = []
    issues.extend(_branch_issues(graph, steps))

    task = graph.task_node
    result = graph.result_node
    if task is None or result is None:
        return issues

    for edge in steps:
        if edge.target_id == task.node_id:
            issues.append(
                GraphIssue(
                    "nothing runs before the Task — it is where the work arrives",
                    connection_id=edge.connection_id,
                )
            )
        if edge.source_id == result.node_id:
            issues.append(
                GraphIssue(
                    "nothing runs after the Aura Result — it is where the answer "
                    "goes back",
                    connection_id=edge.connection_id,
                )
            )

    walked, looped = _walk(task.node_id, steps)
    if looped:
        issues.append(GraphIssue("the steps in this workflow run in a loop"))
    elif result.node_id not in walked:
        issues.append(
            GraphIssue("the Task does not lead to the Aura Result yet")
        )

    helpers = {
        edge.target_id
        for edge in graph.connections_of_kind(ConnectionKind.SUB_AGENT)
        if edge.source_id in walked
    }
    for node in graph.nodes:
        if node.node_id in walked or node.node_id in helpers or not node.is_agent:
            continue
        issues.append(
            GraphIssue(
                "this agent is not connected to the workflow yet",
                node_id=node.node_id,
            )
        )
    return issues


def _branch_issues(graph: WorkflowGraph, steps: tuple) -> list[GraphIssue]:
    """One step in, one step out: a solid path never forks or merges."""
    issues: list[GraphIssue] = []
    for node in graph.nodes:
        outgoing = [edge for edge in steps if edge.source_id == node.node_id]
        incoming = [edge for edge in steps if edge.target_id == node.node_id]
        if len(outgoing) > 1:
            issues.append(
                GraphIssue(
                    "a step can only lead to one next step, and this one leads to "
                    f"{len(outgoing)}",
                    node_id=node.node_id,
                )
            )
        if len(incoming) > 1:
            issues.append(
                GraphIssue(
                    "a step can only follow one other step, and this one follows "
                    f"{len(incoming)}",
                    node_id=node.node_id,
                )
            )
    return issues


def _walk(start: str, steps: tuple) -> tuple[set[str], bool]:
    """Follow steps from *start*, reporting where it got and whether it looped."""
    by_source: dict[str, list] = {}
    for edge in steps:
        by_source.setdefault(edge.source_id, []).append(edge)

    seen: set[str] = {start}
    current = start
    while True:
        outgoing = by_source.get(current, [])
        if len(outgoing) != 1:
            return seen, False
        current = outgoing[0].target_id
        if current in seen:
            return seen, True
        seen.add(current)


# ---- the dashed helpers ----------------------------------------------------


def _sub_agent_issues(graph: WorkflowGraph) -> list[GraphIssue]:
    """A helper hangs off one step, and has no helpers of its own.

    Depth is what makes this acyclic: a node that is already somebody's
    helper may not be a helper's owner, so no chain can ever come back
    around to where it started.
    """
    by_id = {node.node_id: node for node in graph.nodes}
    helpers = graph.connections_of_kind(ConnectionKind.SUB_AGENT)
    helper_targets = {edge.target_id for edge in helpers}
    issues: list[GraphIssue] = []
    for edge in helpers:
        source = by_id.get(edge.source_id)
        target = by_id.get(edge.target_id)
        if source is None or target is None or source is target:
            continue
        if not target.is_agent:
            issues.append(
                GraphIssue(
                    f"a {target.kind.label} node cannot be a sub-agent",
                    connection_id=edge.connection_id,
                )
            )
            continue
        if not source.is_agent:
            issues.append(
                GraphIssue(
                    f"a {source.kind.label} node cannot have sub-agents",
                    connection_id=edge.connection_id,
                )
            )
            continue
        if edge.source_id in helper_targets:
            issues.append(
                GraphIssue(
                    "a sub-agent cannot have sub-agents of its own — helpers go "
                    "one level deep",
                    connection_id=edge.connection_id,
                )
            )
            continue
        if any(
            step.source_id == edge.target_id or step.target_id == edge.target_id
            for step in graph.connections_of_kind(ConnectionKind.STEP)
        ):
            issues.append(
                GraphIssue(
                    "an agent is either a step in the workflow or a sub-agent "
                    "helping one, not both",
                    connection_id=edge.connection_id,
                )
            )
    return issues


__all__ = [
    "MISSING_AGENT_LABEL",
    "GraphIssue",
    "GraphValidation",
    "reference_scope_error",
    "validate_graph",
]
