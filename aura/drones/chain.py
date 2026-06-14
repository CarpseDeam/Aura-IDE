from __future__ import annotations

from dataclasses import dataclass, field

from aura.drones.contracts import BUILTIN_TYPES, is_compatible
from aura.drones.definition import DroneDefinition


@dataclass(frozen=True)
class ChainGoal:
    id: str
    title: str = ""
    objective: str = ""
    position: tuple[float, float] = (0.0, 0.0)


@dataclass(frozen=True)
class ChainNode:
    id: str
    drone_id: str
    goal_template: str = ""
    position: tuple[float, float] = (0.0, 0.0)
    is_draft: bool = False
    draft_name: str = ""
    draft_accepts: str = ""
    draft_produces: str = ""
    draft_brief: str = ""
    is_assignment: bool = False
    goal_id: str = ""


@dataclass(frozen=True)
class ChainEdge:
    from_node: str
    to_node: str


@dataclass(frozen=True)
class ChainDefinition:
    id: str
    name: str
    description: str
    nodes: tuple[ChainNode, ...] = ()
    edges: tuple[ChainEdge, ...] = ()
    created_at: str = ""
    updated_at: str = ""
    enabled: bool = True
    schedule: str = ""
    mission_goal: str = ""
    goals: tuple[ChainGoal, ...] = ()


@dataclass
class ChainValidation:
    ok: bool = False
    errors: list[str] = field(default_factory=list)


def topological_order(chain: ChainDefinition) -> list[str]:
    """Return node ids in execution order using Kahn's algorithm.

    Raises ValueError if a cycle is detected.
    """
    ordered_ids = [n.id for n in chain.nodes]
    node_ids = set(ordered_ids)
    adj: dict[str, list[str]] = {nid: [] for nid in ordered_ids}
    in_degree: dict[str, int] = {nid: 0 for nid in ordered_ids}

    for edge in chain.edges:
        if edge.from_node in adj and edge.to_node in adj:
            adj[edge.from_node].append(edge.to_node)
            in_degree[edge.to_node] += 1

    queue = [nid for nid in ordered_ids if in_degree[nid] == 0]
    result: list[str] = []

    while queue:
        nid = queue.pop(0)
        result.append(nid)
        for neighbor in adj.get(nid, []):
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(node_ids):
        remaining = sorted(node_ids - set(result))
        raise ValueError(
            f"Cycle detected in chain: "
            f"{', '.join(remaining)} nodes are part of a cycle"
        )

    return result


def validate(
    chain: ChainDefinition, drone_lookup: dict[str, DroneDefinition]
) -> ChainValidation:
    """Validate a chain definition against a set of available drones.

    Checks in order: draft nodes, start nodes, drone existence, drone enabled,
    cycles, and type compatibility on every edge.
    """
    errors: list[str] = []

    # 0. Draft nodes must be saved as real drones before running
    for node in chain.nodes:
        if node.is_draft:
            errors.append(
                f"Node '{node.id}' is a draft Drone"
                f" — save it before running."
            )

    # 1. At least one start node (no inbound edges)
    all_node_ids = {n.id for n in chain.nodes}
    to_nodes = {e.to_node for e in chain.edges}
    start_nodes = all_node_ids - to_nodes
    if not start_nodes:
        errors.append("Chain has no start node.")

    node_map = {n.id: n for n in chain.nodes}

    # 2. Every referenced drone_id exists (skip draft nodes; already caught)
    for node in chain.nodes:
        if node.is_draft:
            continue
        if node.drone_id not in drone_lookup:
            errors.append(
                f"Node '{node.id}' references unknown drone_id "
                f"'{node.drone_id}'."
            )

    # 3. Every referenced drone is enabled (skip draft nodes)
    for node in chain.nodes:
        if node.is_draft:
            continue
        drone = drone_lookup.get(node.drone_id)
        if drone is not None and not drone.enabled:
            errors.append(
                f"Node '{node.id}' references disabled drone "
                f"'{node.drone_id}'."
            )

    # 4 + 6. No cycles (topological order)
    try:
        topological_order(chain)
    except ValueError as exc:
        errors.append(str(exc))

    # 5. Type compatibility on every edge (skip draft endpoints)
    for edge in chain.edges:
        producer_node = node_map.get(edge.from_node)
        consumer_node = node_map.get(edge.to_node)
        if not producer_node or not consumer_node:
            continue
        if producer_node.is_draft or consumer_node.is_draft:
            continue

        producer_drone = drone_lookup.get(producer_node.drone_id)
        consumer_drone = drone_lookup.get(consumer_node.drone_id)
        if not producer_drone or not consumer_drone:
            continue

        producer_type_name = (producer_drone.cargo_contract or {}).get("type", "")
        consumer_type_name = (consumer_drone.input_contract or {}).get("type", "")

        # Free-form consumer accepts anything
        if not consumer_type_name:
            continue

        # Producer has no type but consumer requires one
        if not producer_type_name:
            errors.append(
                f"Edge from '{edge.from_node}' to '{edge.to_node}': "
                f"producer drone '{producer_drone.id}' has no produces type "
                f"but consumer '{consumer_drone.id}' requires "
                f"'{consumer_type_name}'."
            )
            continue

        producer_type = BUILTIN_TYPES.get(producer_type_name)
        consumer_type = BUILTIN_TYPES.get(consumer_type_name)

        if producer_type is None:
            errors.append(
                f"Edge from '{edge.from_node}' to '{edge.to_node}': "
                f"producer type '{producer_type_name}' is not recognized."
            )
            continue

        if consumer_type is None:
            errors.append(
                f"Edge from '{edge.from_node}' to '{edge.to_node}': "
                f"consumer type '{consumer_type_name}' is not recognized."
            )
            continue

        if not is_compatible(producer_type, consumer_type):
            errors.append(
                f"Edge from '{edge.from_node}' to '{edge.to_node}': "
                f"type mismatch — '{producer_type_name}' is not compatible "
                f"with '{consumer_type_name}'."
            )

    # 7 + 8. Multi-goal goal_id checks
    if len(chain.goals) > 1:
        goal_ids = {g.id for g in chain.goals}
        for node in chain.nodes:
            if not node.is_assignment:
                continue
            # Blank goal_id when multiple goals exist
            if not node.goal_id.strip():
                errors.append(
                    f"Assignment node '{node.id}' has no goal_id but the "
                    f"workflow defines multiple goals. Target a specific goal."
                )
            # Unknown goal_id
            elif node.goal_id.strip() not in goal_ids:
                errors.append(
                    f"Assignment node '{node.id}' targets unknown "
                    f"goal_id '{node.goal_id}'."
                )

    return ChainValidation(ok=len(errors) == 0, errors=errors)
