"""Turning one workflow plus the agent library into what the surface shows.

Pure functions, no widgets and no storage: a graph, the agent summaries that
resolve its references, and a verdict go in; the visuals the canvas paints and
the three shapes the inspector renders come out. Keeping this out of the
controller means the wording of a node, a missing agent, or a connection is
decided in one readable place instead of scattered through signal handlers.
"""

from __future__ import annotations

from collections.abc import Mapping

from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
)
from aura.agents.graph_validation import MISSING_AGENT_LABEL, GraphValidation
from aura.agents.store import AgentSummary
from aura.gui.agents_workflow_inspector import (
    ConnectionInfo,
    OccurrenceInfo,
    WorkflowInfo,
)
from aura.gui.agents_workflow_node import NodeVisual

TASK_SUBTITLE = "What Aura hands to this workflow."
RESULT_SUBTITLE = "What this workflow hands back to Aura."
NO_ASSIGNMENT = "No assignment yet."


def node_visuals(
    graph: WorkflowGraph,
    agents: Mapping[str, AgentSummary],
    validation: GraphValidation,
) -> dict[str, NodeVisual]:
    """One visual per node, with a missing agent left visibly missing."""
    visuals: dict[str, NodeVisual] = {}
    for node in graph.nodes:
        issues = tuple(issue.message for issue in validation.for_node(node.node_id))
        summary = agents.get(node.agent_id) if node.is_agent else None
        missing = node.is_agent and (summary is None or not summary.valid)
        visuals[node.node_id] = NodeVisual(
            node_id=node.node_id,
            kind=node.kind,
            title=_node_title(node, summary, missing),
            subtitle=_node_subtitle(node, missing),
            missing=missing,
            issues=issues,
        )
    return visuals


def workflow_info(
    graph: WorkflowGraph, validation: GraphValidation
) -> WorkflowInfo:
    return WorkflowInfo(
        graph_id=graph.graph_id,
        scope_label=graph.scope.label,
        name=graph.name,
        description=graph.description,
        runnable=validation.runnable,
        issues=validation.messages,
        summary=validation.summary,
    )


def occurrence_info(
    graph: WorkflowGraph,
    node: WorkflowNode,
    agents: Mapping[str, AgentSummary],
    validation: GraphValidation,
) -> OccurrenceInfo | None:
    """What the inspector shows for a selected agent occurrence."""
    del graph
    if not node.is_agent:
        return None
    summary = agents.get(node.agent_id)
    missing = summary is None or not summary.valid
    return OccurrenceInfo(
        node_id=node.node_id,
        agent_name=MISSING_AGENT_LABEL if missing else summary.name,
        assignment=node.assignment,
        missing=missing,
        issues=tuple(issue.message for issue in validation.for_node(node.node_id)),
    )


def connection_info(
    graph: WorkflowGraph,
    edge: WorkflowConnection,
    agents: Mapping[str, AgentSummary],
    validation: GraphValidation,
) -> ConnectionInfo:
    return ConnectionInfo(
        connection_id=edge.connection_id,
        kind_label=edge.kind.label,
        source_label=node_label(graph.node(edge.source_id), agents),
        target_label=node_label(graph.node(edge.target_id), agents),
        order=edge.order,
        routed_by_hand=edge.bend is not None,
        issues=tuple(
            issue.message for issue in validation.for_connection(edge.connection_id)
        ),
    )


def run_edge_states(
    graph: WorkflowGraph, node_states: Mapping[str, str]
) -> dict[str, str]:
    """What each connection shows, derived from the node that actually ran.

    A line carries work *into* the box at its end, so ordinarily it wears that
    box's state — except the last line, which arrives at the Aura Result and
    has no step of its own to speak for, so it wears the state of the step it
    left.

    A line arriving at a *join* is different, and deliberately so. Several
    branches end there, and they did not all do the same thing: painting every
    incoming line with the join's one final state would say a branch that
    succeeded had been skipped along with the one that failed. So a join's
    incoming line wears the state of the branch it carries, falling back to the
    join itself only for the line that comes from the Task and has no branch of
    its own to report.

    A dashed line likewise wears its helper target's state. An optional helper
    that was never called has no node state, so its node and line stay unmarked.
    """
    states: dict[str, str] = {}
    for edge in graph.connections_of_kind(ConnectionKind.STEP):
        target = graph.node(edge.target_id)
        source = graph.node(edge.source_id)
        source_state = (
            str(node_states.get(edge.source_id, ""))
            if source is not None and source.is_agent
            else ""
        )
        target_state = (
            str(node_states.get(edge.target_id, ""))
            if target is not None and target.is_agent
            else ""
        )
        joins = target is not None and len(
            graph.incoming(edge.target_id, ConnectionKind.STEP)
        ) > 1
        state = (source_state or target_state) if joins else (target_state or source_state)
        if state:
            states[edge.connection_id] = state
    for edge in graph.connections_of_kind(ConnectionKind.SUB_AGENT):
        state = str(node_states.get(edge.target_id, ""))
        if state:
            states[edge.connection_id] = state
    return states


def node_label(node: WorkflowNode | None, agents: Mapping[str, AgentSummary]) -> str:
    """A short name for one node, for use in a sentence about a connection."""
    if node is None:
        return "a node that is no longer here"
    if not node.is_agent:
        return node.kind.label
    summary = agents.get(node.agent_id)
    if summary is None or not summary.valid:
        return MISSING_AGENT_LABEL
    return summary.name


def _node_title(
    node: WorkflowNode, summary: AgentSummary | None, missing: bool
) -> str:
    if not node.is_agent:
        return node.kind.label
    if missing:
        return MISSING_AGENT_LABEL
    return summary.name if summary is not None else MISSING_AGENT_LABEL


def _node_subtitle(node: WorkflowNode, missing: bool) -> str:
    if node.kind is WorkflowNodeKind.TASK:
        return TASK_SUBTITLE
    if node.kind is WorkflowNodeKind.AURA_RESULT:
        return RESULT_SUBTITLE
    if missing:
        return f"This workflow still points at {node.agent_id}."
    return node.assignment or NO_ASSIGNMENT


__all__ = [
    "NO_ASSIGNMENT",
    "RESULT_SUBTITLE",
    "TASK_SUBTITLE",
    "connection_info",
    "node_label",
    "node_visuals",
    "occurrence_info",
    "run_edge_states",
    "workflow_info",
]
