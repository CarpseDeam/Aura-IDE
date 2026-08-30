"""Reading and writing one workflow graph as human-readable JSON.

The file is plain JSON so a workflow committed to a repository reviews as a
diff a person can actually read:

    {
      "version": 1,
      "id": "6f1c...",              // minted once, equals the file name stem
      "name": "Release review",
      "description": "",
      "nodes": [
        {"id": "n1...", "kind": "task", "x": -320.0, "y": 0.0},
        {"id": "n2...", "kind": "agent", "agent": "6f1c...",
         "assignment": "Read the diff twice.", "x": 0.0, "y": 0.0},
        {"id": "n3...", "kind": "aura_result", "x": 320.0, "y": 0.0}
      ],
      "connections": [
        {"id": "c1...", "kind": "step", "from": "n1...", "to": "n2...",
         "order": 0, "bend": {"x": 0.0, "y": -40.0}}
      ]
    }

Two rules keep the format honest, and they are the same two that keep agent
definitions honest. The declared ``id`` must equal the file name stem, so a
workflow's identity is visible in a directory listing. And a workflow may
never declare that it is available to Aura, or that anything it references
is allowed to do anything: both are private local state, so a workflow
committed to a repository cannot switch itself on, or hand an agent
authority, on a machine that merely opened the project.

An agent node stores an agent *id* and the assignment written for that one
occurrence. It never copies instructions, a model target, or a thinking
mode — those are read from the definition, so editing an agent once changes
every workflow that uses it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from aura.agents.graph_models import (
    ConnectionKind,
    Point,
    WorkflowConnection,
    WorkflowGraph,
    WorkflowNode,
    WorkflowNodeKind,
    is_valid_graph_id,
    is_valid_part_id,
)
from aura.agents.identity import AgentScope, is_valid_agent_id

DOCUMENT_VERSION = 1

#: Keys a workflow document may never declare, at the document or the node
#: level. A file that names one is refused outright rather than loaded with
#: the key ignored, so a repository cannot ship a workflow that looks like it
#: enables itself and quietly does nothing.
RESERVED_KEYS: tuple[str, ...] = (
    "available",
    "availability",
    "enabled",
    "active",
    "permission",
    "permissions",
    "grant",
    "grants",
    "authority",
    "allow",
    "allowed",
    "worktree",
    "terminal",
)


@dataclass(frozen=True)
class ParsedGraph:
    """One parse attempt: a graph, or the reasons there isn't one."""

    graph: WorkflowGraph | None
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.graph is not None and not self.errors


def parse_graph_document(
    raw: str,
    *,
    scope: AgentScope,
    expected_id: str,
) -> ParsedGraph:
    """Parse one workflow file's text against the id its file name claims."""
    text = str(raw or "").strip("﻿")
    if not text.strip():
        return ParsedGraph(None, ("the workflow file is empty",))
    try:
        document = json.loads(text)
    except ValueError as exc:
        return ParsedGraph(None, (f"the workflow is not valid JSON: {exc}",))
    if not isinstance(document, dict):
        return ParsedGraph(None, ("the workflow document is not an object",))

    errors: list[str] = list(_reserved_key_errors(document, "workflow"))
    graph_id = document.get("id")
    if not is_valid_graph_id(graph_id):
        errors.append(f"'{graph_id}' is not a valid immutable workflow id")
    elif graph_id != expected_id:
        errors.append(
            f"the declared id '{graph_id}' does not match the file name "
            f"'{expected_id}' — a workflow's id is its file"
        )

    name = document.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("a workflow needs a name")

    description = document.get("description", "")
    if not isinstance(description, str):
        errors.append("the workflow description must be text")
        description = ""

    nodes, node_errors = _parse_nodes(document.get("nodes"))
    errors.extend(node_errors)
    connections, connection_errors = _parse_connections(document.get("connections"))
    errors.extend(connection_errors)

    if errors:
        return ParsedGraph(None, tuple(errors))
    return ParsedGraph(
        WorkflowGraph(
            graph_id=str(graph_id),
            scope=scope,
            name=str(name).strip(),
            description=str(description).strip(),
            nodes=nodes,
            connections=connections,
        )
    )


def render_graph_document(graph: WorkflowGraph) -> str:
    """Render *graph* as the JSON text that is written to disk.

    Keys are emitted in a fixed order, and coordinates were already rounded
    when they were made (:data:`aura.agents.graph_models.COORDINATE_PLACES`),
    so re-saving a workflow nobody edited produces a byte-identical file and
    an actual edit shows up in a diff as itself.
    """
    document: dict[str, Any] = {
        "version": DOCUMENT_VERSION,
        "id": graph.graph_id,
        "name": graph.name,
        "description": graph.description,
        "nodes": [_render_node(node) for node in graph.nodes],
        "connections": [_render_connection(edge) for edge in graph.connections],
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


# ---- nodes -----------------------------------------------------------------


def _parse_nodes(raw: object) -> tuple[tuple[WorkflowNode, ...], tuple[str, ...]]:
    if raw is None:
        return (), ("the workflow declares no nodes",)
    if not isinstance(raw, list):
        return (), ("the workflow's nodes are not a list",)

    errors: list[str] = []
    nodes: list[WorkflowNode] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"node {index} is not an object")
            continue
        errors.extend(_reserved_key_errors(entry, "node"))
        node_id = entry.get("id")
        if not is_valid_part_id(node_id):
            errors.append(f"node {index} has an invalid id")
            continue
        if node_id in seen:
            errors.append(f"more than one node claims the id {node_id}")
            continue
        seen.add(str(node_id))

        kind = WorkflowNodeKind.parse(entry.get("kind"))
        if kind is None:
            errors.append(f"node {node_id} has an unknown kind {entry.get('kind')!r}")
            continue

        agent_id = entry.get("agent", "")
        if kind is WorkflowNodeKind.AGENT:
            if not is_valid_agent_id(agent_id):
                errors.append(f"node {node_id} does not reference a valid agent id")
                continue
        elif agent_id:
            errors.append(f"a {kind.value} node may not reference an agent")
            continue

        assignment = entry.get("assignment", "")
        if not isinstance(assignment, str):
            errors.append(f"node {node_id} has a non-text assignment")
            continue

        nodes.append(
            WorkflowNode(
                node_id=str(node_id),
                kind=kind,
                position=_parse_point(entry) or Point(),
                agent_id=str(agent_id or ""),
                assignment=assignment.strip(),
            )
        )
    return tuple(nodes), tuple(errors)


def _render_node(node: WorkflowNode) -> dict[str, Any]:
    document: dict[str, Any] = {"id": node.node_id, "kind": node.kind.value}
    if node.is_agent:
        document["agent"] = node.agent_id
        document["assignment"] = node.assignment
    document["x"] = node.position.x
    document["y"] = node.position.y
    return document


# ---- connections -----------------------------------------------------------


def _parse_connections(
    raw: object,
) -> tuple[tuple[WorkflowConnection, ...], tuple[str, ...]]:
    if raw is None:
        return (), ()
    if not isinstance(raw, list):
        return (), ("the workflow's connections are not a list",)

    errors: list[str] = []
    connections: list[WorkflowConnection] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            errors.append(f"connection {index} is not an object")
            continue
        errors.extend(_reserved_key_errors(entry, "connection"))
        connection_id = entry.get("id")
        if not is_valid_part_id(connection_id):
            errors.append(f"connection {index} has an invalid id")
            continue
        if connection_id in seen:
            errors.append(f"more than one connection claims the id {connection_id}")
            continue
        seen.add(str(connection_id))

        kind = ConnectionKind.parse(entry.get("kind"))
        if kind is None:
            errors.append(
                f"connection {connection_id} has an unknown kind {entry.get('kind')!r}"
            )
            continue
        source_id = entry.get("from")
        target_id = entry.get("to")
        if not is_valid_part_id(source_id) or not is_valid_part_id(target_id):
            errors.append(f"connection {connection_id} does not name two nodes")
            continue

        order = entry.get("order", index)
        if not isinstance(order, int) or isinstance(order, bool):
            errors.append(f"connection {connection_id} has a non-integer order")
            continue

        connections.append(
            WorkflowConnection(
                connection_id=str(connection_id),
                kind=kind,
                source_id=str(source_id),
                target_id=str(target_id),
                order=order,
                bend=_parse_point(entry.get("bend")),
            )
        )
    return tuple(connections), tuple(errors)


def _render_connection(edge: WorkflowConnection) -> dict[str, Any]:
    document: dict[str, Any] = {
        "id": edge.connection_id,
        "kind": edge.kind.value,
        "from": edge.source_id,
        "to": edge.target_id,
        "order": int(edge.order),
    }
    if edge.bend is not None:
        document["bend"] = {"x": edge.bend.x, "y": edge.bend.y}
    return document


# ---- shared ----------------------------------------------------------------


def _parse_point(raw: object) -> Point | None:
    """Read an ``{x, y}`` pair, tolerating ints, and refusing anything else."""
    if not isinstance(raw, dict):
        return None
    x = raw.get("x")
    y = raw.get("y")
    if isinstance(x, bool) or isinstance(y, bool):
        return None
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return Point(float(x), float(y))


def _reserved_key_errors(document: dict, where: str) -> tuple[str, ...]:
    return tuple(
        f"'{key}' is not allowed in a {where} — whether Aura may run a workflow, "
        "and what an agent may do, are private to each user and are never "
        "written into a workflow file"
        for key in RESERVED_KEYS
        if key in document
    )


__all__ = [
    "DOCUMENT_VERSION",
    "RESERVED_KEYS",
    "ParsedGraph",
    "parse_graph_document",
    "render_graph_document",
]
