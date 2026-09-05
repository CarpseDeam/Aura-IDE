"""Deterministic native graph layout shared by construction and previews."""

from __future__ import annotations

from dataclasses import replace

from aura.agents.graph_dag import runnable_dag
from aura.agents.graph_models import Point, WorkflowGraph
from aura.agents.helper_topology import read_helper_topology

_SOLID_X_GAP = 320.0
_SOLID_Y_GAP = 180.0
_HELPER_X_GAP = 220.0
_HELPER_Y_GAP = 180.0


def layout_workflow(graph: WorkflowGraph) -> WorkflowGraph:
    """Give a valid graph a small deterministic rank-and-row layout."""
    dag = runnable_dag(graph)
    if dag is None:
        return graph

    ranks: dict[str, int] = {}
    for step in dag.steps:
        ranks[step.node_id] = 1 + max(
            (ranks[node_id] for node_id in step.predecessors),
            default=0,
        )
    result_rank = 1 + max(
        (ranks[node_id] for node_id in dag.terminal_node_ids),
        default=0,
    )
    center_x = result_rank * _SOLID_X_GAP / 2.0
    positions: dict[str, Point] = {
        dag.task_node_id: Point(-center_x, 0.0),
        dag.result_node_id: Point(result_rank * _SOLID_X_GAP - center_x, 0.0),
    }

    by_rank: dict[int, list[str]] = {}
    for step in dag.steps:
        by_rank.setdefault(ranks[step.node_id], []).append(step.node_id)
    for rank, node_ids in by_rank.items():
        middle = (len(node_ids) - 1) / 2.0
        for index, node_id in enumerate(node_ids):
            positions[node_id] = Point(
                rank * _SOLID_X_GAP - center_x,
                (index - middle) * _SOLID_Y_GAP,
            )

    topology = read_helper_topology(graph)
    solid_bottom = max((point.y for point in positions.values()), default=0.0)
    next_helper_y = solid_bottom + _HELPER_Y_GAP
    for root_node_id in dag.node_ids:
        descendants = topology.preorder_for_root(root_node_id)
        if not descendants:
            continue
        max_depth = max(item.depth for item in descendants)
        base_y = next_helper_y
        pending = [root_node_id]
        while pending:
            parent_id = pending.pop(0)
            children = topology.children_of(parent_id)
            if not children:
                continue
            parent_x = positions[parent_id].x
            middle = (len(children) - 1) / 2.0
            for index, child in enumerate(children):
                positions[child.node_id] = Point(
                    parent_x + (index - middle) * _HELPER_X_GAP,
                    base_y + (child.depth - 1) * _HELPER_Y_GAP,
                )
                pending.append(child.node_id)
        next_helper_y += max_depth * _HELPER_Y_GAP

    return replace(
        graph,
        nodes=tuple(replace(node, position=positions.get(node.node_id, node.position)) for node in graph.nodes),
    )
