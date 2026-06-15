from __future__ import annotations

from pathlib import Path

from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore


def install_candidate(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    try:
        candidate_folder = candidate_dir(Path(workspace.project_root), workspace.workspace_id)
        drone = DroneStore.load_drone_from_folder(candidate_folder)
        DroneStore.register_drone_folder(workspace_root, candidate_folder)
        if DroneStore.load_drone(workspace_root, drone.id) is None:
            raise RuntimeError(f"Installed Drone '{drone.id}' could not be loaded back after registration")
        workspace.phase = "installed"
        workspace.installed_drone_id = drone.id
        workspace.candidate_drone_id = drone.id
        workspace.last_error = None
        DroneWorkspaceStore.save_workspace(workspace)
        return {"ok": True, "drone_id": drone.id, "drone_name": drone.name}
    except Exception as exc:
        workspace.phase = "build_failed"
        workspace.last_error = str(exc)
        DroneWorkspaceStore.save_workspace(workspace)
        return {"ok": False, "error": str(exc)}


def reinstall_candidate(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    try:
        candidate_folder = candidate_dir(Path(workspace.project_root), workspace.workspace_id)
        drone = DroneStore.load_drone_from_folder(candidate_folder)
        DroneStore.register_drone_folder(workspace_root, candidate_folder)
        if DroneStore.load_drone(workspace_root, drone.id) is None:
            raise RuntimeError(f"Installed Drone '{drone.id}' could not be loaded back after registration")
        workspace.phase = "installed"
        workspace.installed_drone_id = drone.id
        workspace.last_error = None
        DroneWorkspaceStore.save_workspace(workspace)
        return {"ok": True, "drone_id": drone.id, "drone_name": drone.name}
    except Exception as exc:
        workspace.phase = "build_failed"
        workspace.last_error = str(exc)
        DroneWorkspaceStore.save_workspace(workspace)
        return {"ok": False, "error": str(exc)}


def install_or_reinstall(workspace: DroneWorkspace, workspace_root: Path) -> dict:
    if workspace.mode == "edit":
        return reinstall_candidate(workspace, workspace_root)
    return install_candidate(workspace, workspace_root)
