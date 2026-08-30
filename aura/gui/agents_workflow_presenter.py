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
    "workflow_info",
]
