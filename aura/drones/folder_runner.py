from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore


def is_folder_backed_drone(drone: DroneDefinition) -> bool:
    entrypoint = drone.entrypoint
    if not isinstance(entrypoint, dict):
        return False
    return entrypoint.get("kind") == "command" and entrypoint.get("protocol") == "json-stdio"


def run_drone_readiness(folder: Path, drone: DroneDefinition, workspace_root: Path) -> dict[str, Any]:
    """Run a safe readiness check for a folder-backed Drone.

    Calls the entrypoint with a trial payload that should not mutate state,
    post data, push git, spend money, or call risky APIs.

    Args:
        folder: Path to the candidate Drone folder being validated.
        drone: The DroneDefinition to validate.
        workspace_root: The real Aura project root.
    """
    if not isinstance(drone.entrypoint, dict) or not drone.entrypoint:
        return {"ok": False, "error": "entrypoint is required"}
    try:
        payload = {
            "goal": "readiness",
            "input": {},
            "workspace_root": str(workspace_root),
            "drone_id": drone.id,
            "trial_run": True,
            "readiness": True,
        }
        result = _run_command_drone(folder, drone.entrypoint, payload, timeout_seconds=drone.budget.timeout_seconds)
        result = _normalize_result(result)
        # Verify the result is JSON-compatible
        json.dumps(result)
        return result
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }


def run_folder_drone_sync(
    workspace_root: Path,
    drone_id: str,
    drone: DroneDefinition,
    goal: str,
    *,
    input_payload: dict[str, Any] | None = None,
    run: DroneRun | None = None,
) -> dict[str, Any]:
    """Execute a registered folder-backed Drone and persist a receipt."""
    run = run or DroneRun(drone=drone)
    run.mark("running")
    folder = DroneStore.drone_folder(drone_id)
    if not is_folder_backed_drone(drone):
        raise ValueError("Only folder-backed command Drones with json-stdio protocol can be executed")
    if not folder.is_dir():
        raise ValueError(f"Registered Drone folder not found: {drone_id}")
    DroneStore.load_drone_from_folder(folder)
    started_at = dt.datetime.fromtimestamp(run.started_at, tz=dt.timezone.utc).isoformat()
    errors: list[str] = []
    cargo: Any = None
    summary = ""

    payload = {
        "goal": goal,
        "input": input_payload or {},
        "workspace_root": str(workspace_root),
        "drone_id": drone.id,
    }

    try:
        result = _run_command_drone(folder, drone.entrypoint, payload, timeout_seconds=drone.budget.timeout_seconds)
        cargo = result
        run.mark("completed")
    except Exception as exc:
        run.mark("failed")
        errors.append(str(exc))
        errors.append(traceback.format_exc())

    ended_at = dt.datetime.now(dt.timezone.utc).isoformat()
    if isinstance(cargo, (dict, list)):
        summary = json.dumps(cargo, indent=2, ensure_ascii=False)
    elif cargo is not None:
        summary = str(cargo)

    # Determine route_used with fallback priority.
    route_used = None
    if isinstance(cargo, dict):
        if isinstance(cargo.get("route_used"), dict):
            route_used = cargo["route_used"]
        elif isinstance(cargo.get("route"), dict):
            route_used = cargo["route"]
    if route_used is None and isinstance(drone.route, dict) and drone.route:
        route_used = drone.route

    receipt = DroneReceipt(
        run_id=run.run_id,
        drone_id=drone.id,
        drone_name=drone.name,
        status=run.status,
        started_at=started_at,
        ended_at=ended_at,
        tool_calls_made=0,
        tool_errors=0,
        summary=summary,
        output_contract=drone.output_contract,
        tool_calls=[],
        errors=errors,
        elapsed_seconds=run.elapsed_seconds,
        produced_artifact=cargo if isinstance(cargo, dict) else {"result": cargo} if cargo is not None else None,
        met=True if run.status == "completed" else False,
        evidence="Folder-backed Drone returned cargo." if run.status == "completed" else "",
        route_used=route_used,
    )
    RunHistoryStore.save_run(workspace_root, receipt)

    return {
        "ok": run.status == "completed",
        "run_id": run.run_id,
        "drone_id": drone.id,
        "drone_name": drone.name,
        "status": run.status,
        "summary": summary,
        "tool_calls_made": 0,
        "tool_errors": 0,
        "elapsed_seconds": run.elapsed_seconds,
        "receipt": receipt.to_dict(),
        "cargo": cargo,
    }


def _normalize_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        if "ok" in result:
            return result
        return {"ok": True, "result": result}
    if result is None:
        return {"ok": True}
    return {"ok": True, "result": result}


def _run_command_drone(folder: Path, entrypoint: dict, payload: dict[str, Any], timeout_seconds: int) -> dict[str, Any]:
    """Run a Drone command, send JSON payload on stdin, return parsed stdout JSON."""
    command = entrypoint.get("command", [])
    if not command:
        return {"ok": False, "error": "entrypoint.command is empty"}
    try:
        proc = subprocess.run(
            command,
            cwd=str(folder),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        stdout_text = proc.stdout.strip()
        stderr_text = proc.stderr.strip()

        if proc.returncode != 0:
            error = f"Drone exited with non-zero return code ({proc.returncode})"
            result: dict[str, Any] = {"ok": False, "error": error, "returncode": proc.returncode, "stderr": stderr_text}
            if stdout_text:
                try:
                    result["stdout_json"] = json.loads(stdout_text)
                except json.JSONDecodeError:
                    result["stdout"] = stdout_text[:1000]
            return result

        if not stdout_text:
            return {
                "ok": False,
                "error": "Drone produced no stdout output",
                "stderr": stderr_text,
                "returncode": proc.returncode,
            }
        try:
            result = json.loads(stdout_text)
        except json.JSONDecodeError as e:
            return {
                "ok": False,
                "error": f"Drone stdout is not valid JSON: {e}",
                "stderr": stderr_text,
                "stdout": stdout_text[:1000],
                "returncode": proc.returncode,
            }
        result = _normalize_result(result)
        if stderr_text:
            result.setdefault("_stderr", stderr_text)
        result.setdefault("_returncode", proc.returncode)
        return result
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"Drone command timed out after {timeout_seconds}s"}
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {command[0]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}
