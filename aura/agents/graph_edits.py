"""The rules of editing a workflow, with no canvas anywhere near them.

Every function here takes a graph and returns the next one, or ``None`` when
what was asked for is not an edit at all. That keeps the decisions a canvas
implies — a Step may fan out to several next Steps and a join may follow
several, dropping an agent on a solid line splits it in two, a line may not
be drawn twice or pointed back at the node it already leaves — readable in
one place and testable without a widget.

Nothing here writes, validates, or renders. The caller saves what it gets
back, and :mod:`aura.agents.graph_validation` says whether the result could
be run.
"""
from __future__ import annotations

from dataclasses import replace

from aura.agents.graph_models import (
    ConnectionKind,
    Point,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    new_connection_id,
    new_node_id,
)

#: Which end of a line a reconnection is moving.
SOURCE_END = "source"
TARGET_END = "target"


def place_agent(
    graph: WorkflowGraph, agent_id: str, x: float, y: float
) -> tuple[WorkflowGraph, str]:
    """Put a new occurrence of *agent_id* on the canvas at ``(x, y)``.

    Every placement is its own node with its own id, so the same agent placed
    twice is two occurrences that each answer their own assignment.
    """
    node = WorkflowNode(
        node_id=new_node_id(),
        kind=WorkflowNodeKind.AGENT,
        position=Point(float(x), float(y)),
        agent_id=str(agent_id),
    )
    return graph.with_node(node), node.node_id


def insert_into_step(
    graph: WorkflowGraph, connection_id: str, node_id: str
) -> WorkflowGraph | None:
    """Split ``A → B`` into ``A → node → B``, keeping the first half's routing."""
    edge = graph.connection(connection_id)
    if edge is None or not edge.is_step or graph.node(node_id) is None:
        return None
    if node_id in (edge.source_id, edge.target_id):
        return None
    return graph.with_connection(edge.reconnected(target_id=node_id)).with_connection(
        WorkflowConnection(
            connection_id=new_connection_id(),
            kind=ConnectionKind.STEP,
            source_id=node_id,
            target_id=edge.target_id,
            order=graph.next_order(),
        )
    )


def connect(
    graph: WorkflowGraph, source_id: str, target_id: str, kind: ConnectionKind
) -> WorkflowGraph | None:
    """Draw a new line, or return None when there is nothing to draw.

    A step may lead to several next steps and follow several previous ones, so
    a new line is added beside whatever is already there rather than replacing
    it: fan-out and fan-in are shapes a person draws on purpose, and quietly
    deleting the line they drew a moment ago would be the surprising thing.
    What is still refused is a line that says nothing new — the same hand-off
    drawn twice, an end that is not on the canvas, or a node joined to itself.
    """
    if source_id == target_id:
        return None
    if graph.node(source_id) is None or graph.node(target_id) is None:
        return None
    if any(
        edge.kind is kind and edge.source_id == source_id and edge.target_id == target_id
        for edge in graph.connections
    ):
        return None
    return graph.with_connection(
        WorkflowConnection(
            connection_id=new_connection_id(),
            kind=kind,
            source_id=source_id,
            target_id=target_id,
            order=graph.next_order(),
        )
    )


def reconnect(
    graph: WorkflowGraph, connection_id: str, end: str, node_id: str
) -> WorkflowGraph | None:
    """Move one end of an existing line onto another node, keeping the line."""
    edge = graph.connection(connection_id)
    if edge is None or graph.node(node_id) is None or end not in (SOURCE_END, TARGET_END):
        return None
    moved = (
        edge.reconnected(source_id=node_id)
        if end == SOURCE_END
        else edge.reconnected(target_id=node_id)
    )
    if moved.source_id == moved.target_id or moved == edge:
        return None
    if any(
        other.connection_id != connection_id
        and other.kind is moved.kind
        and other.source_id == moved.source_id
        and other.target_id == moved.target_id
        for other in graph.connections
    ):
        return None
    return graph.with_connection(moved)


def remove(
    graph: WorkflowGraph, node_ids: tuple[str, ...], connection_ids: tuple[str, ...]
) -> WorkflowGraph:
    """Take occurrences and lines off the canvas.

    Removing an occurrence never touches the agent it referred to: the same
    agent is very likely standing in three other workflows.
    """
    updated = graph
    for connection_id in connection_ids:
        updated = updated.without_connection(str(connection_id))
    for node_id in node_ids:
        node = updated.node(str(node_id))
        if node is not None and not node.kind.is_fixed:
            updated = updated.without_node(node.node_id)
    return updated


def move_node(graph: WorkflowGraph, node_id: str, x: float, y: float) -> WorkflowGraph | None:
    node = graph.node(node_id)
    if node is None:
        return None
    return graph.with_node(node.moved_to(x, y))


def set_assignment(
    graph: WorkflowGraph, node_id: str, assignment: str
) -> WorkflowGraph | None:
    """Rewrite what one occurrence is asked to do, here and nowhere else."""
    node = graph.node(node_id)
    if node is None or not node.is_agent:
        return None
    return graph.with_node(replace(node, assignment=str(assignment).strip()))


def set_order(graph: WorkflowGraph, connection_id: str, order: int) -> WorkflowGraph | None:
    edge = graph.connection(connection_id)
    if edge is None:
        return None
    return graph.with_connection(replace(edge, order=int(order)))


def set_bend(
    graph: WorkflowGraph, connection_id: str, bend: Point | None
) -> WorkflowGraph | None:
    """Record a routing the user shaped by hand, or forget one."""
    edge = graph.connection(connection_id)
    if edge is None:
        return None
    return graph.with_connection(edge.rerouted(bend))


__all__ = [
    "SOURCE_END",
    "TARGET_END",
    "connect",
    "insert_into_step",
    "move_node",
    "place_agent",
    "reconnect",
    "remove",
    "set_assignment",
    "set_bend",
    "set_order",
]
