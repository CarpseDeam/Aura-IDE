from __future__ import annotations

import datetime as dt
import importlib.util
import inspect
import json
import os
import sys
import time
import traceback
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable

from aura.drones.definition import DroneDefinition
from aura.drones.receipt import DroneReceipt
from aura.drones.run import DroneRun
from aura.drones.store import DroneStore, RunHistoryStore


def is_folder_backed_drone(drone: DroneDefinition) -> bool:
    return drone.runtime == "python" and bool(drone.entrypoint)


def run_drone_smoke(folder: Path, drone: DroneDefinition) -> dict[str, Any]:
    """Run a folder-backed Drone's smoke function."""
    if not drone.smoke:
        return {"ok": False, "error": "smoke is required"}
    try:
        result = _call_ref(folder, drone.smoke, {"smoke": True, "drone_id": drone.id})
        return _normalize_result(result)
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
        result = _call_ref(folder, drone.entrypoint, payload)
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


def _call_ref(folder: Path, ref: str, payload: dict[str, Any]) -> Any:
    module_name, function_name = _parse_ref(ref)
    func = _load_function(folder, module_name, function_name)
    with _execution_context(folder):
        return _invoke(func, payload)


def _parse_ref(ref: str) -> tuple[str, str]:
    module_name, sep, function_name = str(ref or "").partition(":")
    if not sep or not module_name.strip() or not function_name.strip():
        raise ValueError(f"Invalid function reference: {ref!r}")
    return module_name.strip(), function_name.strip()


def _load_function(folder: Path, module_name: str, function_name: str) -> Callable[..., Any]:
    module_path = folder / f"{module_name.replace('.', '/')}.py"
    if not module_path.exists():
        raise FileNotFoundError(f"Module not found: {module_path}")
    unique_name = f"_aura_drone_{module_name.replace('.', '_')}_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(unique_name, module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not load module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    with _execution_context(folder):
        spec.loader.exec_module(module)
    func = getattr(module, function_name, None)
    if not callable(func):
        raise AttributeError(f"{module_name}:{function_name} is not callable")
    return func


def _invoke(func: Callable[..., Any], payload: dict[str, Any]) -> Any:
    signature = inspect.signature(func)
    params = list(signature.parameters.values())
    if not params:
        return func()
    if len(params) == 1:
        return func(payload)
    kwargs = {p.name: payload.get(p.name) for p in params if p.kind in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY)}
    return func(**kwargs)


@contextmanager
def _execution_context(folder: Path):
    folder_str = str(folder)
    old_cwd = Path.cwd()
    inserted = False
    if folder_str not in sys.path:
        sys.path.insert(0, folder_str)
        inserted = True
    try:
        os.chdir(folder)
        yield
    finally:
        os.chdir(old_cwd)
        if inserted:
            try:
                sys.path.remove(folder_str)
            except ValueError:
                pass
