"""Read the dashed helper forest of one workflow graph.

Dashed connections describe occurrence ownership, not execution order.  A
valid helper topology is a directed acyclic forest whose roots are solid
workflow Steps.  Every helper occurrence has one immediate parent, while a
parent may expose several children in the connections' persisted order.

This module is the only reader of that topology.  It is deliberately free of
Qt, Agent definitions, permissions, and runtime state: validation turns its
issues into canvas markings, and plan freezing turns its settled occurrences
into immutable execution authority.  Invalid drawings are never repaired or
partially interpreted.  Traversal is iterative and visited-safe so even a
malformed imported graph cannot recurse forever.
"""
from __future__ import annotations

from dataclasses import dataclass

from aura.agents.graph_dag import solid_dag, solid_step_edges
from aura.agents.graph_models import (
    ConnectionKind,
    WorkflowConnection,
    WorkflowGraph,
)


@dataclass(frozen=True)
class HelperTopologyIssue:
    """One structural reason the dashed forest cannot be frozen."""

    message: str
    node_id: str = ""
    connection_id: str = ""


@dataclass(frozen=True)
class HelperTopologyOccurrence:
    """One helper occurrence at its immutable place in the dashed forest."""

    node_id: str
    root_step_node_id: str
    immediate_parent_node_id: str
    connection_id: str
    depth: int
    lineage: tuple[str, ...]
    child_node_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class HelperTopology:
    """A complete dashed-topology reading, or its fail-closed issues."""

    occurrences: tuple[HelperTopologyOccurrence, ...] = ()
    helper_node_ids: tuple[str, ...] = ()
    issues: tuple[HelperTopologyIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.issues

    def occurrence(self, node_id: str) -> HelperTopologyOccurrence | None:
        return next(
            (item for item in self.occurrences if item.node_id == node_id), None
        )

    def children_of(self, parent_node_id: str) -> tuple[HelperTopologyOccurrence, ...]:
        """Direct children only, in persisted dashed-edge order."""
        return tuple(
            item
            for item in self.occurrences
            if item.immediate_parent_node_id == parent_node_id
        )

    def preorder_for_root(
        self, root_step_node_id: str
    ) -> tuple[HelperTopologyOccurrence, ...]:
        """One Step's descendants in stable parent-before-child preorder."""
        return tuple(
            item
            for item in self.occurrences
            if item.root_step_node_id == root_step_node_id
        )


def _ordered_helper_edges(graph: WorkflowGraph) -> tuple[WorkflowConnection, ...]:
    """Dashed edges by persisted order, stably tied by file order."""
    indexed = tuple(
        (index, edge)
        for index, edge in enumerate(graph.connections)
        if edge.kind is ConnectionKind.SUB_AGENT
    )
    return tuple(
        edge
        for _index, edge in sorted(
            indexed, key=lambda row: (row[1].order, row[0])
        )
    )


def _cycle_node_ids(
    node_ids: tuple[str, ...], successors: dict[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Exact directed-cycle members via iterative strongly connected sets."""
    inside = set(node_ids)
    visited: set[str] = set()
    finished: list[str] = []
    for start in node_ids:
        if start in visited:
            continue
        stack: list[tuple[str, bool]] = [(start, False)]
        while stack:
            node_id, expanded = stack.pop()
            if expanded:
                finished.append(node_id)
                continue
            if node_id in visited:
                continue
            visited.add(node_id)
            stack.append((node_id, True))
            stack.extend(
                (child, False)
                for child in reversed(successors.get(node_id, ()))
                if child in inside and child not in visited
            )

    predecessors: dict[str, list[str]] = {}
    for source_id, children in successors.items():
        if source_id not in inside:
            continue
        for child_id in children:
            if child_id in inside:
                predecessors.setdefault(child_id, []).append(source_id)

    assigned: set[str] = set()
    cyclic: set[str] = set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component: list[str] = []
        pending = [start]
        while pending:
            node_id = pending.pop()
            if node_id in assigned:
                continue
            assigned.add(node_id)
            component.append(node_id)
            pending.extend(reversed(predecessors.get(node_id, ())))
        if len(component) > 1 or start in successors.get(start, ()):
            cyclic.update(component)
    return tuple(node_id for node_id in node_ids if node_id in cyclic)


def read_helper_topology(graph: WorkflowGraph) -> HelperTopology:
    """Read *graph*'s dashed forest without guessing through invalid shapes.

    Ambiguous parentage, cycles, fixed-end participation, solid/helper
    collisions, and detached trees all produce issues and no occurrence tree.
    ``helper_node_ids`` still names every Agent occurrence participating as a
    would-be helper so the solid validator does not mislabel a nested helper
    as an unrelated disconnected Step.
    """
    by_id = {node.node_id: node for node in graph.nodes}
    ordered_edges = _ordered_helper_edges(graph)
    solid_ids = {
        node_id
        for edge in solid_step_edges(graph)
        for node_id in (edge.source_id, edge.target_id)
        if by_id[node_id].is_agent
    }

    issues: list[HelperTopologyIssue] = []
    agent_edges: list[WorkflowConnection] = []
    incoming: dict[str, list[WorkflowConnection]] = {}
    participant_ids: set[str] = set()
    for edge in ordered_edges:
        source = by_id.get(edge.source_id)
        target = by_id.get(edge.target_id)
        if source is not None and source.is_agent:
            participant_ids.add(source.node_id)
        if target is not None and target.is_agent:
            participant_ids.add(target.node_id)
            incoming.setdefault(target.node_id, []).append(edge)
        if source is None or target is None:
            # The graph-wide dangling check owns the visible wording.
            continue
        if not target.is_agent:
            issues.append(
                HelperTopologyIssue(
                    f"a {target.kind.label} node cannot be a sub-agent",
                    connection_id=edge.connection_id,
                )
            )
        if not source.is_agent:
            issues.append(
                HelperTopologyIssue(
                    f"a {source.kind.label} node cannot have sub-agents",
                    connection_id=edge.connection_id,
                )
            )
        if source.is_agent and target.is_agent:
            agent_edges.append(edge)

    outgoing: dict[str, list[WorkflowConnection]] = {}
    for edge in agent_edges:
        outgoing.setdefault(edge.source_id, []).append(edge)

    helper_ids = set(incoming) | {
        node_id for node_id in participant_ids if node_id not in solid_ids
    }
    helper_node_ids = tuple(
        node.node_id for node in graph.nodes if node.node_id in helper_ids
    )

    for node_id in helper_node_ids:
        parents = incoming.get(node_id, [])
        if len(parents) > 1:
            parent_ids = {edge.source_id for edge in parents}
            message = (
                "the same sub-agent occurrence cannot have multiple parents or "
                "belong to multiple steps"
                if len(parent_ids) > 1
                else "a sub-agent occurrence must have exactly one dashed connection"
            )
            issues.extend(
                HelperTopologyIssue(message, connection_id=edge.connection_id)
                for edge in parents
            )
        elif not parents and node_id not in solid_ids:
            issues.append(
                HelperTopologyIssue(
                    "a helper occurrence must have exactly one immediate parent",
                    node_id=node_id,
                )
            )

    for node_id in helper_node_ids:
        if node_id not in solid_ids:
            continue
        issues.extend(
            HelperTopologyIssue(
                "an agent occurrence is either a solid workflow Step or a "
                "sub-agent helping one, not both",
                connection_id=edge.connection_id,
            )
            for edge in incoming.get(node_id, ())
        )

    successors = {
        source_id: tuple(dict.fromkeys(edge.target_id for edge in edges))
        for source_id, edges in outgoing.items()
    }
    participant_order = tuple(
        node.node_id for node in graph.nodes if node.node_id in participant_ids
    )
    cyclic = set(_cycle_node_ids(participant_order, successors))
    issues.extend(
        HelperTopologyIssue(
            "the dashed sub-agent connections run in a cycle through this "
            "Agent occurrence",
            node_id=node_id,
        )
        for node_id in participant_order
        if node_id in cyclic
    )

    reachable: set[str] = set()
    pending = list(reversed(tuple(solid_ids)))
    while pending:
        node_id = pending.pop()
        if node_id in reachable:
            continue
        reachable.add(node_id)
        pending.extend(
            reversed(tuple(edge.target_id for edge in outgoing.get(node_id, ())))
        )
    issues.extend(
        HelperTopologyIssue(
            "this sub-agent tree is detached from every solid workflow Step",
            node_id=node_id,
        )
        for node_id in helper_node_ids
        if node_id not in reachable
    )

    if issues:
        return HelperTopology(
            helper_node_ids=helper_node_ids,
            issues=tuple(issues),
        )

    dag = solid_dag(graph)
    root_order = (
        dag.node_ids
        if dag is not None
        else tuple(
            node.node_id
            for node in graph.nodes
            if node.node_id in solid_ids
        )
    )
    occurrences: list[HelperTopologyOccurrence] = []
    visited: set[str] = set()
    for root_id in root_order:
        stack: list[tuple[WorkflowConnection, int, tuple[str, ...]]] = [
            (edge, 1, (root_id, edge.target_id))
            for edge in reversed(tuple(outgoing.get(root_id, ())))
        ]
        while stack:
            edge, depth, lineage = stack.pop()
            node_id = edge.target_id
            if node_id in visited:
                # All ambiguous and cyclic shapes were rejected above. Keep
                # this guard as a final fail-closed defence against regression.
                return HelperTopology(
                    helper_node_ids=helper_node_ids,
                    issues=(
                        HelperTopologyIssue(
                            "the dashed helper topology could not be read "
                            "unambiguously",
                            node_id=node_id,
                        ),
                    ),
                )
            visited.add(node_id)
            children = tuple(outgoing.get(node_id, ()))
            occurrences.append(
                HelperTopologyOccurrence(
                    node_id=node_id,
                    root_step_node_id=root_id,
                    immediate_parent_node_id=edge.source_id,
                    connection_id=edge.connection_id,
                    depth=depth,
                    lineage=lineage,
                    child_node_ids=tuple(child.target_id for child in children),
                )
            )
            stack.extend(
                (child, depth + 1, (*lineage, child.target_id))
                for child in reversed(children)
            )

    if visited != set(helper_node_ids):
        missing = next(
            node_id for node_id in helper_node_ids if node_id not in visited
        )
        return HelperTopology(
            helper_node_ids=helper_node_ids,
            issues=(
                HelperTopologyIssue(
                    "this sub-agent tree is detached from every solid workflow Step",
                    node_id=missing,
                ),
            ),
        )
    return HelperTopology(
        occurrences=tuple(occurrences),
        helper_node_ids=helper_node_ids,
    )


__all__ = [
    "HelperTopology",
    "HelperTopologyIssue",
    "HelperTopologyOccurrence",
    "read_helper_topology",
]
