"""Immutable, no-op-aware edits to Agent workflow connections."""
from __future__ import annotations

import pytest

from aura.agents.graph_edits import SOURCE_END, TARGET_END, connect, reconnect
from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
)
from aura.agents.identity import AgentScope


@pytest.mark.parametrize("kind", tuple(ConnectionKind))
@pytest.mark.parametrize("end", (SOURCE_END, TARGET_END))
def test_reconnect_refuses_an_exact_duplicate_and_leaves_the_graph_unchanged(
    kind: ConnectionKind, end: str
) -> None:
    nodes = tuple(
        WorkflowNode(node_id, WorkflowNodeKind.AGENT, agent_id="reviewer0000")
        for node_id in ("a", "b", "c")
    )
    existing = WorkflowConnection("existing", kind, "a", "b", 0)
    moving = (
        WorkflowConnection("moving", kind, "c", "b", 1)
        if end == SOURCE_END
        else WorkflowConnection("moving", kind, "a", "c", 1)
    )
    graph = WorkflowGraph(
        graph_id="editworkflow1",
        scope=AgentScope.PROJECT,
        name="Reconnect",
        nodes=nodes,
        connections=(existing, moving),
    )
    replacement = "a" if end == SOURCE_END else "b"

    assert connect(graph, "a", "b", kind) is None
    assert reconnect(graph, moving.connection_id, end, replacement) is None
    assert graph.connections == (existing, moving)
