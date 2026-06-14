"""Lightweight synchronous Drone runner for folder-backed Drones."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aura.drones.definition import DroneDefinition
from aura.drones.folder_runner import is_folder_backed_drone, run_folder_drone_sync
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import RunHistoryStore

if TYPE_CHECKING:
    from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
else:
    ApprovalDecision = Any
    ApprovalRequest = Any


def _unsupported_non_folder_result(
    workspace_root: Path,
    drone: DroneDefinition,
) -> dict[str, Any]:
    run = DroneRun(drone=drone)
    run.mark("failed")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    error = "Only folder-backed Drones with a command entrypoint and json-stdio protocol are supported."
    receipt = DroneReceipt(
        run_id=run.run_id,
        drone_id=drone.id,
        drone_name=drone.name,
        status="failed",
        started_at=dt.datetime.fromtimestamp(run.started_at, tz=dt.timezone.utc).isoformat(),
        ended_at=now,
        tool_calls_made=0,
        tool_errors=0,
        summary="",
        output_contract=drone.output_contract,
        tool_calls=[],
        errors=[error],
        elapsed_seconds=run.elapsed_seconds,
        produced_artifact=None,
        met=False,
        evidence=error,
    )
    RunHistoryStore.save_run(workspace_root, receipt)
    return {
        "ok": False,
        "run_id": run.run_id,
        "drone_id": drone.id,
        "drone_name": drone.name,
        "status": "failed",
        "summary": "",
        "tool_calls_made": 0,
        "tool_errors": 0,
        "elapsed_seconds": run.elapsed_seconds,
        "receipt": receipt.to_dict(),
        "approved_write_actions": 0,
        "rejected_write_actions": 0,
        "error": error,
    }


def _run_drone_sync_impl(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    _ = (drone_id, goal)
    return _unsupported_non_folder_result(workspace_root, drone)


def run_read_only_drone_sync(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
) -> dict[str, Any]:
    """Run a registered folder-backed Drone synchronously."""
    _ = (timeout_seconds, max_tool_rounds)
    if not is_folder_backed_drone(drone):
        return _run_drone_sync_impl(
            workspace_root,
            drone_id,
            drone,
            goal,
            write_enabled=False,
            timeout_seconds=timeout_seconds,
            max_tool_rounds=max_tool_rounds,
        )
    return run_folder_drone_sync(workspace_root, drone_id, drone, goal)


def run_write_capable_drone_sync(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    *,
    approval_callback: Callable[[ApprovalRequest], ApprovalDecision] | None = None,
    timeout_seconds: int = 120,
    max_tool_rounds: int = 8,
) -> dict[str, Any]:
    """Run a registered folder-backed Drone synchronously.

    Folder-backed Drones run their own entrypoint. Aura no longer exposes a
    write-capable LLM tool menu for Drone execution.
    """
    _ = (approval_callback, timeout_seconds, max_tool_rounds)
    if not is_folder_backed_drone(drone):
        return _run_drone_sync_impl(
            workspace_root,
            drone_id,
            drone,
            goal,
            write_enabled=True,
            approval_callback=approval_callback,
            timeout_seconds=timeout_seconds,
            max_tool_rounds=max_tool_rounds,
        )
    return run_folder_drone_sync(workspace_root, drone_id, drone, goal)
