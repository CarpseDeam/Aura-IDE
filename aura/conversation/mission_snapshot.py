"""Mission Control snapshot helpers — pure functions, no Qt imports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aura.drones.chain import ChainDefinition
from aura.drones.chain_runner import get_last_chain_run
from aura.drones.chain_store import ChainStore
from aura.drones.store import DroneStore


def _read_cargo_for_chain(workspace_root: Path, chain_id: str) -> tuple[list[dict], str]:
    """Read the latest ChainRun node outputs as cargo items.

    Returns (cargo_items, run_status).
    Duplicated from chain_editor.py to avoid circular GUI imports.
    """
    if not chain_id:
        return [], "idle"

    chain_run = get_last_chain_run(workspace_root, chain_id)
    if chain_run is None:
        return [], "idle"

    cargo_items: list[dict] = []
    for node_run in chain_run.node_runs.values():
        if node_run.get("status") != "completed":
            continue

        artifact_path = node_run.get("artifact_path", "")
        drone_id = node_run.get("drone_id", "?")

        label = f"Output from {drone_id}"
        if artifact_path:
            output_path = workspace_root / artifact_path
            if output_path.exists():
                try:
                    data = json.loads(output_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = {"artifact": data}
                    label = data.get("summary", label)
                except (json.JSONDecodeError, OSError):
                    label = "(unreadable output)"
            else:
                label = "(missing output)"

        cargo_items.append({
            "node_id": node_run.get("node_id", ""),
            "drone_id": drone_id,
            "status": node_run.get("status", "unknown"),
            "artifact_path": artifact_path,
            "met": node_run.get("met", False),
            "error": node_run.get("error", ""),
            "label": label,
        })

    return cargo_items, chain_run.status


def resolve_mission_snapshot(workspace_root: Path, chain_id: str) -> dict[str, Any]:
    """Load a full mission snapshot: chain definition, last run, and cargo."""
    chain = ChainStore.load_chain(workspace_root, chain_id)
    if chain is None:
        return {"ok": False, "error": f"mission not found: {chain_id}"}

    last_run = get_last_chain_run(workspace_root, chain_id)
    cargo_items, run_status = _read_cargo_for_chain(workspace_root, chain_id)

    node_list: list[dict[str, Any]] = []
    for n in chain.nodes:
        node_list.append({
            "id": n.id,
            "drone_id": n.drone_id,
            "goal_template": n.goal_template,
            "is_draft": n.is_draft,
            "draft_name": n.draft_name,
        })

    edge_list: list[dict[str, Any]] = []
    for e in chain.edges:
        edge_list.append({
            "from_node": e.from_node,
            "to_node": e.to_node,
        })

    last_run_summary: dict[str, Any] | None = None
    if last_run:
        node_results: list[dict[str, Any]] = []
        for nid, nr in last_run.node_runs.items():
            node_results.append({
                "node_id": nid,
                "drone_id": nr.get("drone_id", ""),
                "status": nr.get("status", "unknown"),
                "met": nr.get("met"),
                "error": nr.get("error", ""),
            })
        last_run_summary = {
            "run_id": last_run.run_id,
            "status": last_run.status,
            "started_at": last_run.started_at,
            "ended_at": last_run.ended_at,
            "node_results": node_results,
        }

    return {
        "ok": True,
        "chain": {
            "id": chain.id,
            "name": chain.name,
            "description": chain.description,
            "mission_goal": chain.mission_goal,
            "nodes": node_list,
            "edges": edge_list,
            "node_count": len(chain.nodes),
            "edge_count": len(chain.edges),
            "mission_core": {
                "mission_goal": chain.mission_goal,
            },
        },
        "last_run": last_run_summary,
        "cargo": cargo_items,
    }


def find_chain_by_name_or_id(
    workspace_root: Path,
    name: str | None,
    chain_id: str | None,
) -> ChainDefinition | None:
    """Find a chain by exact id match or case-insensitive name match."""
    if chain_id:
        chain = ChainStore.load_chain(workspace_root, chain_id)
        if chain is not None:
            return chain

    if name:
        chains = ChainStore.list_chains(workspace_root)
        name_lower = name.lower()
        for c in chains:
            if c.name.lower() == name_lower:
                return c

    return None


def build_mission_list(workspace_root: Path) -> list[dict[str, Any]]:
    """Build a summary list of all saved missions."""
    chains = ChainStore.list_chains(workspace_root)
    drones = DroneStore.list_drones(workspace_root)
    drone_lookup = {d.id: d for d in drones}

    result: list[dict[str, Any]] = []
    for c in chains:
        has_write_capable = False
        for node in c.nodes:
            drone = drone_lookup.get(node.drone_id)
            if drone and drone.write_policy != "read_only":
                has_write_capable = True
                break

        last_run = get_last_chain_run(workspace_root, c.id)
        last_run_status: str | None = None
        last_run_time: str | None = None
        if last_run:
            last_run_status = last_run.status
            last_run_time = last_run.ended_at or last_run.started_at

        result.append({
            "id": c.id,
            "name": c.name,
            "description": c.description,
            "node_count": len(c.nodes),
            "has_write_capable": has_write_capable,
            "last_run_status": last_run_status,
            "last_run_time": last_run_time,
        })

    return result
