from __future__ import annotations

from pathlib import Path

from aura.drones.folder_runner import run_drone_readiness
from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore


def install_candidate(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    candidate_folder = candidate_dir(Path(workspace.project_root), workspace.workspace_id)
    drone = DroneStore.load_drone_from_folder(candidate_folder)
    result = run_drone_readiness(candidate_folder, drone, workspace_root)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "Readiness failed"), "detail": result}

    DroneStore.register_drone_folder(workspace_root, candidate_folder, readiness_result=result)
    workspace.phase = "installed"
    workspace.installed_drone_id = drone.id
    workspace.candidate_drone_id = drone.id
    DroneWorkspaceStore.save_workspace(workspace)
    return {"ok": True, "drone_id": drone.id, "drone_name": drone.name}


def reinstall_candidate(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    candidate_folder = candidate_dir(Path(workspace.project_root), workspace.workspace_id)
    drone = DroneStore.load_drone_from_folder(candidate_folder)
    result = run_drone_readiness(candidate_folder, drone, workspace_root)
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "Readiness failed"), "detail": result}

    DroneStore.register_drone_folder(workspace_root, candidate_folder, readiness_result=result)
    workspace.phase = "installed"
    workspace.installed_drone_id = drone.id
    DroneWorkspaceStore.save_workspace(workspace)
    return {"ok": True, "drone_id": drone.id, "drone_name": drone.name}


def install_or_reinstall(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    if workspace.mode == "edit":
        return reinstall_candidate(workspace, workspace_root)
    return install_candidate(workspace, workspace_root)
