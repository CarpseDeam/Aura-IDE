from __future__ import annotations

import json
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path

from aura.drones.architect.results import ProofResult
from aura.drones.store import DroneStore
from aura.drones.workspaces.paths import candidate_dir, proof_runs_dir
from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.store import DroneWorkspaceStore


def run_candidate_proof(
    workspace_root: Path, workspace: DroneWorkspace
) -> ProofResult:
    """Run a proof against the uninstalled candidate in the workspace."""
    project_root = Path(workspace.project_root)
    cand = candidate_dir(project_root, workspace.workspace_id)

    # Load the drone.json from the candidate folder.
    try:
        drone = DroneStore.load_drone_from_folder(cand)
    except Exception as exc:
        return ProofResult(
            drone_name=workspace.display_name,
            proof_status="failed",
            what_tried=workspace.build_brief or "Test Run",
            route_used="",
            output_sample="",
            errors=[f"Drone definition could not be loaded: {exc}"],
        )

    # Build trial-safe payload.
    payload = {
        "goal": workspace.build_brief or "Test Run",
        "input": {},
        "workspace_root": str(workspace_root),
        "drone_id": drone.id,
        "trial_run": True,
    }

    entrypoint = drone.entrypoint
    timeout = drone.budget.timeout_seconds if hasattr(drone, "budget") else 120

    # Run the entrypoint via subprocess (replicating _run_command_drone logic).
    try:
        result = _run_command_drone(cand, entrypoint, payload, timeout)
    except Exception as exc:
        result = {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}

    result = _normalize_result(result)

    # Determine proof status.
    ok = bool(result.get("ok"))
    warnings_list: list[str] = []
    errors_list: list[str] = []
    if not ok:
        errors_list.append(result.get("error", "Unknown error"))
    stderr_val = result.get("_stderr", "")
    if stderr_val:
        warnings_list.append(stderr_val)

    if not ok or errors_list:
        proof_status = "failed"
    elif warnings_list:
        proof_status = "warnings"
    else:
        proof_status = "passed"

    # Collect metadata.
    what_tried = workspace.build_brief or drone.description or "Test Run"
    route_used = _extract_route(drone, result)
    output_sample = _extract_output_sample(result)

    proof_result = ProofResult(
        drone_name=drone.name,
        proof_status=proof_status,
        what_tried=what_tried,
        route_used=route_used,
        output_sample=output_sample,
        warnings=warnings_list,
        errors=errors_list,
        raw_result=result,
    )

    # Save proof run record.
    run_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "drone_id": drone.id,
        "drone_name": drone.name,
        "proof_status": proof_status,
        "what_tried": what_tried,
        "route_used": route_used,
        "output_sample": output_sample,
        "warnings": warnings_list,
        "errors": errors_list,
        "result": result,
    }
    DroneWorkspaceStore.append_proof_run(workspace, run_record)
    proof_result.proof_run_path = str(
        proof_runs_dir(project_root, workspace.workspace_id)
    )

    return proof_result


def _run_command_drone(
    folder: Path,
    entrypoint: dict,
    payload: dict,
    timeout_seconds: int,
) -> dict:
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
            result: dict = {
                "ok": False,
                "error": error,
                "returncode": proc.returncode,
                "stderr": stderr_text,
            }
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
        return {
            "ok": False,
            "error": f"Drone command timed out after {timeout_seconds}s",
        }
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {command[0]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}


def _normalize_result(result: dict) -> dict:
    if "ok" in result:
        return result
    return {"ok": True, "result": result}


def _extract_route(drone, result: dict) -> str:
    """Extract route_used from the result or drone definition."""
    cargo = result.get("result") or result
    if isinstance(cargo, dict):
        if isinstance(cargo.get("route_used"), dict):
            return json.dumps(cargo["route_used"])
        if isinstance(cargo.get("route"), dict):
            return json.dumps(cargo["route"])
    if isinstance(drone.route, dict) and drone.route:
        return json.dumps(drone.route)
    return ""


def _extract_output_sample(result: dict) -> str:
    """Extract a short output sample from the result."""
    summary = json.dumps(result, default=str)
    return summary[:500]
