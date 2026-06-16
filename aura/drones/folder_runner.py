from __future__ import annotations

import datetime as dt
import json
import subprocess
import time
import traceback
from pathlib import Path
from typing import Any

from aura.config import get_subprocess_kwargs
from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore

_PROCESS_POLL_SECONDS = 0.05
_CANCEL_GRACE_SECONDS = 2.0


def is_folder_backed_drone(drone: DroneDefinition) -> bool:
    entrypoint = drone.entrypoint
    if not isinstance(entrypoint, dict):
        return False
    return entrypoint.get("kind") == "command" and entrypoint.get("protocol") == "json-stdio"


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
    folder = DroneStore.drone_folder(workspace_root, drone_id)
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
        result = _run_command_drone(
            folder,
            drone.entrypoint,
            payload,
            timeout_seconds=drone.budget.timeout_seconds,
            cancel_event=run.cancel_event,
        )
        cargo = result
        if isinstance(result, dict) and (
            result.get("cancelled") or result.get("status") == "cancelled"
        ):
            run.mark("cancelled")
        elif isinstance(result, dict) and (
            result.get("timed_out") or result.get("status") == "timed_out"
        ):
            run.mark("timed_out")
        elif isinstance(result, dict) and result.get("ok") is False:
            run.mark("failed")
        else:
            run.mark("completed")
    except Exception as exc:
        run.mark("failed")
        errors.append(str(exc))
        errors.append(traceback.format_exc())

    if run.status != "completed" and isinstance(cargo, dict):
        error = cargo.get("error")
        if error:
            errors.append(str(error))

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
        produced_artifact=(
            cargo
            if run.status == "completed" and isinstance(cargo, dict)
            else {"result": cargo}
            if run.status == "completed" and cargo is not None
            else None
        ),
        met=True if run.status == "completed" else False,
        evidence=_evidence_for_status(run.status),
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


def _evidence_for_status(status: str) -> str:
    if status == "completed":
        return "Folder-backed Drone returned cargo."
    if status == "cancelled":
        return "Folder-backed Drone run was cancelled."
    if status == "timed_out":
        return "Folder-backed Drone command timed out."
    return ""


def _run_command_drone(
    folder: Path,
    entrypoint: dict,
    payload: dict[str, Any],
    timeout_seconds: int,
    cancel_event: Any = None,
) -> dict[str, Any]:
    """Run a Drone command, send JSON payload on stdin, return parsed stdout JSON."""
    command = entrypoint.get("command", [])
    if not command:
        return {"ok": False, "error": "entrypoint.command is empty"}
    if cancel_event is not None and cancel_event.is_set():
        return _cancelled_result("", "")

    proc: subprocess.Popen[str] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(folder),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **get_subprocess_kwargs(),
        )
        stdout_text, stderr_text = _communicate_with_cancel(
            proc,
            json.dumps(payload),
            timeout_seconds=timeout_seconds,
            cancel_event=cancel_event,
        )
        stdout_text = stdout_text.strip()
        stderr_text = stderr_text.strip()

        if cancel_event is not None and cancel_event.is_set():
            return _cancelled_result(stdout_text, stderr_text, proc.returncode)

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
        stdout_text, stderr_text, returncode = _stop_process(
            proc,
            kill_after=_CANCEL_GRACE_SECONDS,
        )
        result = {
            "ok": False,
            "status": "timed_out",
            "timed_out": True,
            "error": f"Drone command timed out after {timeout_seconds}s",
            "returncode": returncode,
        }
        if stdout_text:
            result["stdout"] = stdout_text[:1000]
        if stderr_text:
            result["stderr"] = stderr_text[:1000]
        return result
    except FileNotFoundError:
        return {"ok": False, "error": f"Command not found: {command[0]}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "traceback": traceback.format_exc()}


def _communicate_with_cancel(
    proc: subprocess.Popen[str],
    input_text: str,
    *,
    timeout_seconds: int,
    cancel_event: Any = None,
) -> tuple[str, str]:
    deadline = time.monotonic() + max(0, timeout_seconds)
    pending_input: str | None = input_text

    while True:
        if cancel_event is not None and cancel_event.is_set():
            stdout_text, stderr_text, _returncode = _stop_process(
                proc,
                kill_after=_CANCEL_GRACE_SECONDS,
            )
            return stdout_text, stderr_text

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout_seconds)

        try:
            stdout_text, stderr_text = proc.communicate(
                input=pending_input,
                timeout=min(_PROCESS_POLL_SECONDS, remaining),
            )
            return stdout_text or "", stderr_text or ""
        except subprocess.TimeoutExpired:
            pending_input = None


def _stop_process(
    proc: subprocess.Popen[str] | None,
    *,
    kill_after: float,
) -> tuple[str, str, int | None]:
    if proc is None:
        return "", "", None

    if proc.poll() is None:
        try:
            proc.terminate()
        except Exception:
            pass

    try:
        stdout_text, stderr_text = proc.communicate(timeout=kill_after)
        return stdout_text or "", stderr_text or "", proc.returncode
    except subprocess.TimeoutExpired:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            stdout_text, stderr_text = proc.communicate(timeout=kill_after)
            return stdout_text or "", stderr_text or "", proc.returncode
        except Exception as exc:
            return "", str(exc), proc.returncode


def _cancelled_result(
    stdout_text: str,
    stderr_text: str,
    returncode: int | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": "cancelled",
        "cancelled": True,
        "error": "Drone run cancelled.",
    }
    if returncode is not None:
        result["returncode"] = returncode
    if stdout_text:
        result["stdout"] = stdout_text[:1000]
    if stderr_text:
        result["stderr"] = stderr_text[:1000]
    return result
