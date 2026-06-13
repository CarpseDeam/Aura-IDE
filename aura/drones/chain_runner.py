"""Chain runner — sequences drone executions in topological order."""

from __future__ import annotations

import datetime as dt
import json
import logging
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.consequential import is_consequential
from aura.drones.chain import (
    ChainDefinition,
    ChainNode,
    topological_order,
    validate,
)
from aura.drones.definition import DroneDefinition
from aura.drones.sync_runner import run_read_only_drone_sync, run_write_capable_drone_sync

logger = logging.getLogger(__name__)


@dataclass
class ChainRun:
    """Mutable runtime state for a chain execution.

    Updated in-place during run_chain().  node_runs is keyed by node_id,
    each value is a dict with: node_id, drone_id, status, receipt,
    artifact_path, met, evidence, error.
    """

    run_id: str
    chain_id: str
    status: str = "running"  # running | completed | failed | stopped
    node_runs: dict[str, dict] = field(default_factory=dict)
    started_at: str = ""
    ended_at: str = ""


# ── Path helpers ───────────────────────────────────────────────────


def _runs_dir(workspace_root: Path) -> Path:
    return workspace_root / ".aura" / "chains" / "runs"


def _run_dir(workspace_root: Path, run_id: str) -> Path:
    return _runs_dir(workspace_root) / run_id


def _node_output_dir(workspace_root: Path, run_id: str, node_id: str) -> Path:
    return _run_dir(workspace_root, run_id) / node_id


def _save_run_state(workspace_root: Path, chain_run: ChainRun) -> None:
    """Persist ChainRun state as run.json."""
    d = _run_dir(workspace_root, chain_run.run_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "run.json"
    data = asdict(chain_run)
    fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".json")
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    Path(tmp_path).replace(path)


def _load_run_state(workspace_root: Path, run_id: str) -> ChainRun | None:
    """Load ChainRun state from run.json. Returns None if not found."""
    path = _run_dir(workspace_root, run_id) / "run.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ChainRun(**data)
    except Exception:
        logger.warning("Failed to load chain run state: %s", path)
        return None


# ── Run history queries ────────────────────────────────────────


def get_last_chain_run(workspace_root: Path, chain_id: str) -> ChainRun | None:
    """Return the most recent ChainRun for a given chain."""
    runs = _runs_dir(workspace_root)
    if not runs.exists():
        return None

    best: ChainRun | None = None
    best_time = ""

    for subdir in runs.iterdir():
        if not subdir.is_dir():
            continue
        run_json = subdir / "run.json"
        if not run_json.exists():
            continue
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Skipping malformed run.json: %s", run_json)
            continue

        if data.get("chain_id") != chain_id:
            continue

        ended_at = data.get("ended_at", "") or data.get("started_at", "")
        if ended_at > best_time:
            best_time = ended_at
            try:
                best = ChainRun(**data)
            except Exception:
                logger.warning(
                    "Failed to construct ChainRun from: %s", run_json
                )
                continue

    return best


def list_chain_runs(
    workspace_root: Path, chain_id: str, limit: int = 10
) -> list[ChainRun]:
    """Return the most recent N ChainRuns for a given chain."""
    runs = _runs_dir(workspace_root)
    if not runs.exists():
        return []

    results: list[tuple[str, ChainRun]] = []

    for subdir in runs.iterdir():
        if not subdir.is_dir():
            continue
        run_json = subdir / "run.json"
        if not run_json.exists():
            continue
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Skipping malformed run.json: %s", run_json)
            continue

        if data.get("chain_id") != chain_id:
            continue

        ended_at = data.get("ended_at", "") or data.get("started_at", "")
        try:
            cr = ChainRun(**data)
        except Exception:
            logger.warning(
                "Failed to construct ChainRun from: %s", run_json
            )
            continue

        results.append((ended_at, cr))

    results.sort(key=lambda x: x[0], reverse=True)
    return [cr for _, cr in results[:limit]]


# ── Consequential node classification ──────────────────────────


def classify_consequential_nodes(
    chain: ChainDefinition,
    drone_lookup: dict[str, DroneDefinition],
) -> list[dict]:
    """Classify chain nodes that are write-capable and have consequential tools.

    Returns a list of dicts (one per write-capable node) in topological order
    with keys: node_id, drone_id, drone_name, write_policy, consequential_tools.
    Returns [] if no consequential nodes exist.
    """
    consequential_nodes: list[dict] = []
    for node in chain.nodes:
        drone = drone_lookup.get(node.drone_id)
        if drone is None:
            continue
        if drone.write_policy != "read_only":
            consequential_tools = [
                t for t in (drone.allowed_tools or [])
                if is_consequential(t)
            ]
            consequential_nodes.append({
                "node_id": node.id,
                "drone_id": node.drone_id,
                "drone_name": drone.name,
                "write_policy": drone.write_policy,
                "consequential_tools": consequential_tools,
            })
    return consequential_nodes


def _execute_node(
    workspace_root: Path,
    chain_run: ChainRun,
    chain: ChainDefinition,
    node_id: str,
    node: ChainNode,
    drone: DroneDefinition,
    node_map: dict[str, ChainNode],
    drone_lookup: dict[str, DroneDefinition],
    timeout_seconds: int,
    max_tool_rounds: int,
    writes_approved: bool,
    mission_goal: str = "",
) -> dict:
    """Execute a single chain node and return its node_run dict.

    Resolves inbound artifacts, builds goal text, dispatches the drone,
    normalizes results, persists output.json, and returns the node_run dict.
    Does NOT set chain_run.status/ended_at or call _save_run_state.
    """
    # ── Resolve inbound artifact ─────────────────────────
    inbound_artifacts: dict[str, Any] = {}
    inbound_summaries: dict[str, str] = {}
    for edge in chain.edges:
        if edge.to_node == node_id:
            upstream_output = (
                _node_output_dir(
                    workspace_root, chain_run.run_id, edge.from_node
                )
                / "output.json"
            )
            if not upstream_output.exists():
                continue
            try:
                data = json.loads(
                    upstream_output.read_text(encoding="utf-8")
                )
                if not isinstance(data, dict):
                    data = {"artifact": data}
                upstream_node = node_map.get(edge.from_node)
                upstream_drone = (
                    drone_lookup.get(upstream_node.drone_id)
                    if upstream_node
                    else None
                )
                if upstream_drone and upstream_drone.produces:
                    # Structured artifact
                    inbound_artifacts[edge.from_node] = data
                else:
                    # Free-form — extract summary
                    summary = data.get("summary", "")
                    if summary:
                        inbound_summaries[edge.from_node] = summary
            except Exception as exc:
                logger.warning(
                    "Failed to read artifact from %s: %s",
                    upstream_output,
                    exc,
                )

    # ── Build goal text ────────────────────────────────
    goal_text = node.goal_template.strip()
    if not goal_text:
        goal_text = f"Execute the {drone.name} step."

    if inbound_artifacts:
        goal_text += "\n\n## Input Artifact\n```json\n"
        if len(inbound_artifacts) == 1:
            goal_text += json.dumps(
                next(iter(inbound_artifacts.values())), indent=2
            )
        else:
            goal_text += json.dumps(inbound_artifacts, indent=2)
        goal_text += "\n```"

    for upstream_nid, summary in inbound_summaries.items():
        if summary:
            goal_text += (
                f"\n\n## Input from {upstream_nid}\n{summary}"
            )

    # ── Mission Objective (Goal Planet) ────────────────
    if node.is_assignment and mission_goal:
        goal_text = (
            f"## Mission Objective\n{mission_goal}\n\n"
            f"## Assignment\n{goal_text}"
        )

    # ── Warehouse cargo for assignment nodes ────────────
    if node.is_assignment:
        warehouse_cargo: list[dict[str, Any]] = []
        for completed_nid, nr in chain_run.node_runs.items():
            if completed_nid == node_id:
                continue
            if nr.get("status") != "completed":
                continue
            artifact_path = nr.get("artifact_path", "")
            if not artifact_path:
                continue
            output_path = workspace_root / artifact_path
            if not output_path.exists():
                continue
            try:
                data = json.loads(output_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                data = {"artifact": data}
            cargo_entry: dict[str, Any] = {
                "node_id": completed_nid,
                "drone_id": nr.get("drone_id", "?"),
            }
            summary = data.get("summary", "")
            evidence = data.get("evidence", "")
            if summary:
                cargo_entry["summary"] = summary
            if evidence:
                cargo_entry["evidence"] = evidence
            for k in ("result", "output", "findings", "data"):
                if k in data:
                    cargo_entry[k] = data[k]
            warehouse_cargo.append(cargo_entry)

        if warehouse_cargo:
            goal_text += (
                "\n\n## Mission Warehouse Cargo\n"
                "The following cargo was returned by previously completed "
                "drones in this mission. "
                "Use this information in your work.\n\n"
            )
            for entry in warehouse_cargo:
                goal_text += (
                    f"- **{entry['drone_id']}** ({entry['node_id']}):"
                )
                if "summary" in entry:
                    goal_text += f" {entry['summary']}"
                goal_text += "\n"
                extras = {
                    k: v
                    for k, v in entry.items()
                    if k not in ("node_id", "drone_id", "summary", "evidence")
                }
                if extras:
                    goal_text += (
                        "  ```json\n  "
                        + json.dumps(extras, indent=2).replace(
                            "\n", "\n  "
                        )
                        + "\n  ```\n"
                    )

    # ── Execute ────────────────────────────────────────
    try:
        if drone.write_policy == "read_only":
            result = run_read_only_drone_sync(
                workspace_root=workspace_root,
                drone_id=node.drone_id,
                drone=drone,
                goal=goal_text,
                timeout_seconds=timeout_seconds,
                max_tool_rounds=max_tool_rounds,
            )
        else:
            node_approval_cb = (
                (lambda req: ApprovalDecision(
                    action="approve",
                    note="Chain approved upfront",
                ))
                if writes_approved
                else None
            )
            result = run_write_capable_drone_sync(
                workspace_root=workspace_root,
                drone_id=node.drone_id,
                drone=drone,
                goal=goal_text,
                approval_callback=node_approval_cb,
                timeout_seconds=timeout_seconds,
                max_tool_rounds=max_tool_rounds,
            )
    except Exception as exc:
        logger.exception("Chain node '%s' crashed", node_id)
        return {
            "node_id": node_id,
            "drone_id": node.drone_id,
            "status": "failed",
            "receipt": None,
            "artifact_path": "",
            "met": None,
            "evidence": "",
            "error": str(exc),
        }

    receipt_dict = result.get("receipt", {})
    run_status = result.get("status", "failed")
    met = receipt_dict.get("met")
    evidence = receipt_dict.get("evidence", "")
    produced_artifact = receipt_dict.get("produced_artifact")
    rejected_write_actions = result.get("rejected_write_actions", 0)

    # If write-capable node had rejected writes, mark met as ambivalent
    if rejected_write_actions > 0:
        met = None
        if evidence:
            evidence += "; "
        evidence += f"{rejected_write_actions} write action(s) rejected."
    error: str | None = None

    # ── Persist node output ───────────────────────────
    node_dir = _node_output_dir(
        workspace_root, chain_run.run_id, node_id
    )
    node_dir.mkdir(parents=True, exist_ok=True)
    output_path = node_dir / "output.json"

    if drone.produces and produced_artifact is not None:
        output_data = produced_artifact
    else:
        # Free-form or unmet: save the full receipt dict
        output_data = receipt_dict

    # Atomic write (tempfile → replace)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(node_dir), suffix=".json"
    )
    with open(fd, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2, ensure_ascii=False)
    Path(tmp_path).replace(output_path)

    return {
        "node_id": node_id,
        "drone_id": node.drone_id,
        "status": run_status,
        "receipt": receipt_dict,
        "artifact_path": str(
            output_path.relative_to(workspace_root)
        ),
        "met": met,
        "evidence": evidence,
        "error": error,
    }


# ── Main entry point ──────────────────────────────────────────────


def run_chain(
    workspace_root: Path,
    chain: ChainDefinition,
    *,
    drone_lookup: dict[str, DroneDefinition],
    resume_run_id: str | None = None,
    start_node: str | None = None,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
    approval_callback: Callable[[list[dict]], bool] | None = None,
) -> ChainRun:
    """Execute a chain by running each node's drone in topological order.

    Parameters
    ----------
    workspace_root:
        Root of the workspace (for artifact persistence).
    chain:
        The chain definition to execute.
    drone_lookup:
        Maps drone_id → DroneDefinition for all nodes in the chain.
    resume_run_id:
        If set, load the saved ChainRun state and continue.
    start_node:
        If set (with resume), start execution from this node onward.
        Nodes before it must already be completed with saved output.
    timeout_seconds:
        Per-node timeout passed to run_read_only_drone_sync.
    max_tool_rounds:
        Per-node max tool-call rounds.

    Returns
    -------
    ChainRun with final status and per-node results.
    """
    # ── Step 1 — Validate ──────────────────────────────────────
    if not chain.nodes:
        raise ValueError("Chain has no nodes to run.")

    chain_validation = validate(chain, drone_lookup)
    if not chain_validation.ok:
        raise ValueError(
            f"Chain validation failed: "
            f"{chr(10).join(chain_validation.errors)}"
        )

    # ── Step 2 — Classify consequential nodes ──────────────────
    consequential_nodes = classify_consequential_nodes(chain, drone_lookup)
    if consequential_nodes:
        if approval_callback is None:
            names = ", ".join(n["drone_name"] for n in consequential_nodes)
            raise ValueError(
                f"Chain contains write-capable nodes: {names}. "
                f"An approval_callback is required."
            )
        approved = approval_callback(consequential_nodes)
        if not approved:
            raise ValueError("Chain execution declined by operator.")

    # ── Step 3 — Create or load ChainRun ───────────────────────
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    if resume_run_id:
        existing = _load_run_state(workspace_root, resume_run_id)
        if existing is None:
            raise ValueError(
                f"No saved run state found for resume_run_id "
                f"'{resume_run_id}'."
            )
        if existing.chain_id != chain.id:
            raise ValueError(
                f"Resume chain_id mismatch: saved run has "
                f"'{existing.chain_id}', but chain has '{chain.id}'."
            )
        chain_run = existing
        chain_run.status = "running"
    else:
        run_id = uuid.uuid4().hex[:12]
        chain_run = ChainRun(
            run_id=run_id,
            chain_id=chain.id,
            started_at=now,
        )

    # ── Step 4 — Resolve node order ────────────────────────────
    node_map = {n.id: n for n in chain.nodes}
    order = topological_order(chain)
    if start_node:
        try:
            start_idx = order.index(start_node)
        except ValueError:
            raise ValueError(
                f"start_node '{start_node}' not found in chain node order."
            )
        order = order[start_idx:]
        # Warn about missing output from previously-completed nodes
        for nid in topological_order(chain)[:start_idx]:
            if nid in chain_run.node_runs:
                expected = (
                    _node_output_dir(workspace_root, chain_run.run_id, nid)
                    / "output.json"
                )
                if not expected.exists():
                    logger.warning(
                        "Resume: completed node '%s' missing output.json at %s",
                        nid,
                        expected,
                    )

    # ── Step 5 — Walk nodes in order ───────────────────────────
    for node_id in order:
        node = node_map[node_id]
        drone = drone_lookup[node.drone_id]

        # Skip already-completed nodes on resume
        if node_id in chain_run.node_runs:
            existing_run = chain_run.node_runs[node_id]
            if existing_run.get("status") == "completed":
                logger.info("Skipping already-completed node '%s'", node_id)
                continue
            # Re-run failed nodes
            chain_run.node_runs.pop(node_id, None)

        # Mark as running
        chain_run.node_runs[node_id] = {
            "node_id": node_id,
            "drone_id": node.drone_id,
            "status": "running",
            "receipt": None,
            "artifact_path": "",
            "met": None,
            "evidence": "",
            "error": None,
        }

        node_run = _execute_node(
            workspace_root=workspace_root,
            chain_run=chain_run,
            chain=chain,
            node_id=node_id,
            node=node,
            drone=drone,
            node_map=node_map,
            drone_lookup=drone_lookup,
            timeout_seconds=timeout_seconds,
            max_tool_rounds=max_tool_rounds,
            writes_approved=(approval_callback is not None),
            mission_goal=chain.mission_goal,
        )
        chain_run.node_runs[node_id] = node_run

        # ── Check for failure ────────────────────────────
        if node_run["status"] != "completed" or node_run.get("met") is False:
            if not node_run.get("error"):
                if node_run["status"] != "completed":
                    node_run["error"] = f"Node status: {node_run['status']}"
                elif node_run["met"] is False:
                    node_run["error"] = f"Node unmet: {node_run['evidence']}"
            chain_run.status = "failed"
            chain_run.ended_at = dt.datetime.now(
                dt.timezone.utc
            ).isoformat()
            _save_run_state(workspace_root, chain_run)
            break

        # ── Save progress after each successful node ─────
        _save_run_state(workspace_root, chain_run)

    else:
        # All nodes completed without break
        chain_run.status = "completed"
        chain_run.ended_at = dt.datetime.now(dt.timezone.utc).isoformat()
        _save_run_state(workspace_root, chain_run)

    return chain_run
