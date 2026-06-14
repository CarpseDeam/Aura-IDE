from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.paths import (
    active_workspace_path,
    artifacts_dir,
    build_runs_dir,
    candidate_dir,
    chats_dir,
    proof_runs_dir,
    repair_runs_dir,
    workspace_folder,
    workspace_manifest_path,
    workspaces_dir,
)

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a display name to a filesystem-safe id slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug if slug else "workspace"


def _safe_run_timestamp() -> str:
    """Return a Windows-safe UTC timestamp for run-record filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


class DroneWorkspaceStore:
    """Persistent store for DroneWorkspace state and folders.

    All methods are static. The store manages on-disk JSON manifests,
    subfolder scaffolding, and run-history files. It does not dispatch
    execution, build prompts, or interact with the GUI.
    """

    @staticmethod
    def _ensure_folders(workspace: DroneWorkspace) -> None:
        """Create all subfolders for a workspace if they don't exist."""
        project_root = Path(workspace.project_root)
        wid = workspace.workspace_id
        chats_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        candidate_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        build_runs_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        proof_runs_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        repair_runs_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        artifacts_dir(project_root, wid).mkdir(parents=True, exist_ok=True)

    @staticmethod
    def list_workspaces(project_root: Path) -> list[DroneWorkspace]:
        """List all workspaces sorted by updated_at descending."""
        wd = workspaces_dir(project_root)
        if not wd.exists():
            return []
        results: list[DroneWorkspace] = []
        for entry in sorted(wd.iterdir()):
            if not entry.is_dir():
                continue
            manifest = entry / "workspace.json"
            if not manifest.exists():
                continue
            try:
                ws = DroneWorkspaceStore.load_workspace(
                    project_root, entry.name
                )
                if ws is not None:
                    results.append(ws)
            except Exception:
                logger.warning("Skipping invalid workspace: %s", entry.name)
        results.sort(key=lambda w: w.updated_at, reverse=True)
        return results

    @staticmethod
    def load_workspace(
        project_root: Path, workspace_id: str
    ) -> DroneWorkspace | None:
        """Load a single workspace from its workspace.json."""
        manifest = workspace_manifest_path(project_root, workspace_id)
        if not manifest.exists():
            return None
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return DroneWorkspace(**data)
        except Exception:
            logger.warning("Failed to load workspace %s", workspace_id)
            return None

    @staticmethod
    def save_workspace(workspace: DroneWorkspace) -> None:
        """Persist the workspace manifest and ensure subfolders exist."""
        workspace.updated_at = datetime.now(timezone.utc).isoformat()
        project_root = Path(workspace.project_root)
        manifest = workspace_manifest_path(project_root, workspace.workspace_id)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(asdict(workspace), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        DroneWorkspaceStore._ensure_folders(workspace)

    @staticmethod
    def create_workspace(
        project_root: Path,
        display_name: str,
        mode: str = "new",
        installed_drone_id: str | None = None,
    ) -> DroneWorkspace:
        """Create a new workspace, persisting it and its folder structure."""
        wid = _slugify(display_name)
        wd = workspaces_dir(project_root)
        existing_ids: set[str] = set()
        if wd.exists():
            existing_ids = {
                p.name
                for p in wd.iterdir()
                if p.is_dir() and (p / "workspace.json").exists()
            }
        if wid in existing_ids:
            counter = 2
            while f"{wid}-{counter}" in existing_ids:
                counter += 1
            wid = f"{wid}-{counter}"

        wf = workspace_folder(project_root, wid)
        now = datetime.now(timezone.utc).isoformat()
        workspace = DroneWorkspace(
            workspace_id=wid,
            display_name=display_name,
            project_root=str(project_root),
            workspace_root=str(wf),
            mode=mode,
            phase="workshop",
            installed_drone_id=installed_drone_id,
            created_at=now,
            updated_at=now,
        )
        DroneWorkspaceStore.save_workspace(workspace)
        return workspace

    @staticmethod
    def set_active_workspace(
        project_root: Path, workspace: DroneWorkspace
    ) -> None:
        """Write _active.json pointing to the given workspace."""
        path = active_workspace_path(project_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"active_workspace_id": workspace.workspace_id}, indent=2
            ),
            encoding="utf-8",
        )

    @staticmethod
    def load_active_workspace(project_root: Path) -> DroneWorkspace | None:
        """Load the currently active workspace from _active.json, if any."""
        path = active_workspace_path(project_root)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            active_id = data.get("active_workspace_id")
            if not active_id:
                return None
            ws = DroneWorkspaceStore.load_workspace(project_root, active_id)
            if ws is None:
                return None
            if ws.phase in {"discarded", "installed"}:
                return None
            return ws
        except Exception:
            logger.warning("Failed to load active workspace")
            return None

    @staticmethod
    def append_build_run(
        workspace: DroneWorkspace, run_record: dict
    ) -> None:
        """Write a build run record and update workspace.last_build_run."""
        timestamp = datetime.now(timezone.utc).isoformat()
        project_root = Path(workspace.project_root)
        dest = (
            build_runs_dir(project_root, workspace.workspace_id)
            / f"build_{_safe_run_timestamp()}.json"
        )
        dest.write_text(
            json.dumps(run_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        workspace.last_build_run = timestamp
        DroneWorkspaceStore.save_workspace(workspace)

    @staticmethod
    def append_proof_run(
        workspace: DroneWorkspace, run_record: dict
    ) -> None:
        """Write a proof run record and update workspace.last_proof_run."""
        timestamp = datetime.now(timezone.utc).isoformat()
        project_root = Path(workspace.project_root)
        dest = (
            proof_runs_dir(project_root, workspace.workspace_id)
            / f"proof_{_safe_run_timestamp()}.json"
        )
        dest.write_text(
            json.dumps(run_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        workspace.last_proof_run = timestamp
        DroneWorkspaceStore.save_workspace(workspace)

    @staticmethod
    def append_repair_run(
        workspace: DroneWorkspace, run_record: dict
    ) -> None:
        """Write a repair run record and persist the workspace."""
        timestamp = datetime.now(timezone.utc).isoformat()
        project_root = Path(workspace.project_root)
        dest = (
            repair_runs_dir(project_root, workspace.workspace_id)
            / f"repair_{_safe_run_timestamp()}.json"
        )
        dest.write_text(
            json.dumps(run_record, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        DroneWorkspaceStore.save_workspace(workspace)

    @staticmethod
    def create_workspace_for_drone(project_root: Path, drone_id: str) -> DroneWorkspace | None:
        drone = DroneStore.load_drone(project_root, drone_id)
        if drone is None:
            return None

        workspace = DroneWorkspaceStore.create_workspace(
            project_root, display_name=drone.name, mode="edit", installed_drone_id=drone_id
        )

        source_folder = DroneStore.drone_folder(drone_id)
        target_folder = candidate_dir(project_root, workspace.workspace_id)
        shutil.copytree(source_folder, target_folder, dirs_exist_ok=True)

        workspace.phase = "awaiting_decision"
        workspace.build_brief = drone.description or ""
        workspace.candidate_drone_id = drone_id
        DroneWorkspaceStore.save_workspace(workspace)
        return workspace

    @staticmethod
    def find_workspace_for_drone(project_root: Path, drone_id: str) -> DroneWorkspace | None:
        for ws in DroneWorkspaceStore.list_workspaces(project_root):
            if ws.installed_drone_id == drone_id and ws.mode == "edit":
                return ws
        return None

    @staticmethod
    def load_or_create_workspace_for_drone(project_root: Path, drone_id: str) -> DroneWorkspace | None:
        ws = DroneWorkspaceStore.find_workspace_for_drone(project_root, drone_id)
        if ws is not None:
            return ws
        return DroneWorkspaceStore.create_workspace_for_drone(project_root, drone_id)

    @staticmethod
    def discard_workspace(workspace: DroneWorkspace) -> None:
        """Mark a workspace as discarded without deleting its files."""
        workspace.phase = "discarded"
        DroneWorkspaceStore.save_workspace(workspace)
        # Clear _active.json so discarded workspace is not restored.
        project_root = Path(workspace.project_root)
        active_path = active_workspace_path(project_root)
        if active_path.exists():
            active_path.write_text(json.dumps({}), encoding="utf-8")
