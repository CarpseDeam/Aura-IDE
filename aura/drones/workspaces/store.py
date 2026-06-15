from __future__ import annotations

import json
import logging
import re
import shutil
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneThread, DroneWorkspace
from aura.drones.workspaces.paths import (
    active_workspace_path,
    artifacts_dir,
    build_runs_dir,
    candidate_dir,
    chats_dir,
    repair_runs_dir,
    workspace_folder,
    workspace_manifest_path,
    workspaces_dir,
)

logger = logging.getLogger(__name__)

DEFAULT_THREAD_TITLE = "New thread"


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid4().hex[:12]


def _slugify(name: str) -> str:
    """Convert a display name to a filesystem-safe id slug."""
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug if slug else "workspace"


def _safe_run_timestamp() -> str:
    """Return a Windows-safe UTC timestamp for run-record filenames."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")


def _recover_workspace_from_candidate(
    project_root: Path, folder: Path, candidate_manifest: Path
) -> DroneWorkspace:
    """Recover a DroneWorkspace from candidate/drone.json when workspace.json is missing or invalid."""
    data = json.loads(candidate_manifest.read_text(encoding="utf-8"))
    name = str(data.get("name") or "").strip() or folder.name
    now = _utc_iso()
    ws = DroneWorkspace(
        workspace_id=folder.name,
        display_name=name,
        project_root=str(project_root),
        workspace_root=str(folder),
        mode="new",
        phase="build_failed",
        candidate_drone_id=data.get("id"),
        last_error="Workspace manifest was missing or invalid. Recovered from candidate folder.",
        created_at=now,
        updated_at=now,
    )
    DroneWorkspaceStore.save_workspace(ws)
    logger.info("Recovered workspace %s from candidate manifest", folder.name)
    return ws


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
        build_runs_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        repair_runs_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        artifacts_dir(project_root, wid).mkdir(parents=True, exist_ok=True)
        # For edit-mode workspaces the drone folder already exists; skip creating candidate.
        # For builder workspaces, create candidate dir.
        if not workspace.edit_source_folder:
            candidate_dir(project_root, wid).mkdir(parents=True, exist_ok=True)

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
            ws: DroneWorkspace | None = None
            if manifest.exists():
                try:
                    ws = DroneWorkspaceStore.load_workspace(
                        project_root, entry.name
                    )
                except Exception:
                    logger.warning("Failed to load workspace %s", entry.name)

            if ws is None:
                # Try recovery from candidate/drone.json
                candidate_manifest = entry / "candidate" / "drone.json"
                if candidate_manifest.exists():
                    try:
                        ws = _recover_workspace_from_candidate(
                            project_root, entry, candidate_manifest
                        )
                    except Exception:
                        logger.warning(
                            "Failed to recover workspace from candidate: %s",
                            entry.name,
                        )
                if ws is None:
                    continue

            results.append(ws)
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
            return DroneWorkspace.from_dict(data)
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
    def sync_display_name_from_candidate(
        project_root: Path, workspace: DroneWorkspace
    ) -> bool:
        """Sync workspace.display_name from candidate/drone.json if it has a different name.

        Reads the drone.json from the edit_source_folder (for edit-mode workspaces)
        or candidate_dir(project_root, workspace.workspace_id).
        If the file exists and contains a non-empty "name" field that differs
        from workspace.display_name, updates it, saves, and returns True.
        Otherwise returns False.
        """
        if workspace.edit_source_folder:
            drone_file = Path(workspace.edit_source_folder) / "drone.json"
        else:
            drone_file = candidate_dir(project_root, workspace.workspace_id) / "drone.json"
        if not drone_file.exists():
            return False
        try:
            data = json.loads(drone_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        name = data.get("name", "")
        if not isinstance(name, str) or not name.strip():
            return False
        name = name.strip()
        if name == workspace.display_name:
            return False
        workspace.display_name = name
        DroneWorkspaceStore.save_workspace(workspace)
        return True

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
            if ws.phase in {
                "discarded",
                "installed",
                "build_failed",
            }:
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
    def append_repair_run(
        workspace: DroneWorkspace, run_record: dict
    ) -> None:
        """Write a repair run record and persist the workspace."""
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
        workspace.edit_source_folder = str(source_folder)
        workspace.phase = "ready"
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
    @staticmethod
    def list_threads(
        project_root: Path, workspace_id: str, include_archived: bool = False
    ) -> list[DroneThread]:
        """List threads in a workspace's chats directory, sorted by updated_at desc."""
        cd = chats_dir(project_root, workspace_id)
        if not cd.is_dir():
            return []
        threads: list[DroneThread] = []
        for path in sorted(cd.iterdir()):
            if not path.suffix == ".json":
                continue
            thread = DroneWorkspaceStore._load_thread_from_path(path)
            if thread is None:
                continue
            if not include_archived and thread.archived:
                continue
            threads.append(thread)
        threads.sort(key=lambda t: t.updated_at, reverse=True)
        return threads

    @staticmethod
    def create_thread(
        project_root: Path, workspace_id: str, title: str = DEFAULT_THREAD_TITLE
    ) -> DroneThread:
        """Create a new thread and persist it."""
        now = _utc_iso()
        thread = DroneThread(
            id=_new_id(),
            workspace_id=workspace_id,
            title=title,
            created_at=now,
            updated_at=now,
        )
        DroneWorkspaceStore.save_thread(project_root, workspace_id, thread)
        return thread

    @staticmethod
    def load_thread(
        project_root: Path, workspace_id: str, thread_id: str
    ) -> DroneThread | None:
        """Load a single thread from its JSON file."""
        path = chats_dir(project_root, workspace_id) / f"{thread_id}.json"
        return DroneWorkspaceStore._load_thread_from_path(path)

    @staticmethod
    def save_thread(
        project_root: Path, workspace_id: str, thread: DroneThread
    ) -> None:
        """Persist a thread to its JSON file."""
        thread.updated_at = _utc_iso()
        cd = chats_dir(project_root, workspace_id)
        cd.mkdir(parents=True, exist_ok=True)
        path = cd / f"{thread.id}.json"
        path.write_text(
            json.dumps(thread.to_dict(), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def _load_thread_from_path(path: Path) -> DroneThread | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return DroneThread.from_dict(data)

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

    @staticmethod
    def rename_workspace(
        project_root: Path, workspace: DroneWorkspace, new_id: str
    ) -> DroneWorkspace:
        """Rename a workspace folder and update its manifest.

        If new_id already exists on disk, appends a -N suffix until unique.
        Also updates _active.json if it points at the old id.
        """
        if workspace.workspace_id == new_id:
            return workspace

        old_id = workspace.workspace_id
        old_folder = workspace_folder(project_root, old_id)
        new_folder = workspace_folder(project_root, new_id)

        if new_folder.exists():
            counter = 2
            while workspace_folder(project_root, f"{new_id}-{counter}").exists():
                counter += 1
            new_id = f"{new_id}-{counter}"
            new_folder = workspace_folder(project_root, new_id)

        if old_folder.exists():
            shutil.move(str(old_folder), str(new_folder))

        workspace.workspace_id = new_id
        workspace.workspace_root = str(new_folder)

        # Update _active.json if it points at old_id.
        active_path = active_workspace_path(project_root)
        if active_path.exists():
            try:
                data = json.loads(active_path.read_text(encoding="utf-8"))
                if data.get("active_workspace_id") == old_id:
                    data["active_workspace_id"] = new_id
                    active_path.write_text(
                        json.dumps(data, indent=2), encoding="utf-8"
                    )
            except Exception:
                logger.warning("Failed to update _active.json during rename")

        DroneWorkspaceStore.save_workspace(workspace)
        return workspace

    @staticmethod
    def migrate_stale_folders(project_root: Path) -> None:
        """Migrate old workspace/build folders to canonical locations.

        Moves subdirectories from .aura/drones/workspaces/ into
        .aura/drone-workspaces/ (canonical).  If a target already exists
        in canonical, moves to .aura/backups/stale_workspaces/ instead.
        Removes the stale directory if empty afterwards.

        Also moves .aura/drone-build/ to .aura/backups/stale_drone_build/.
        """
        stale_workspaces = project_root / ".aura" / "drones" / "workspaces"
        if stale_workspaces.is_dir():
            canonical = workspaces_dir(project_root)
            backup_base = project_root / ".aura" / "backups" / "stale_workspaces"
            for entry in sorted(stale_workspaces.iterdir()):
                if not entry.is_dir():
                    continue
                try:
                    target = canonical / entry.name
                    if target.exists():
                        backup_target = backup_base / entry.name
                        backup_target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(entry), str(backup_target))
                        logger.info(
                            "Moved stale workspace %s -> %s", entry, backup_target
                        )
                    else:
                        shutil.move(str(entry), str(target))
                        logger.info(
                            "Migrated workspace %s -> %s", entry, target
                        )
                except Exception as exc:
                    logger.warning("Failed to migrate %s: %s", entry, exc)
            try:
                remaining = list(stale_workspaces.iterdir())
                if not remaining:
                    stale_workspaces.rmdir()
                    logger.info("Removed empty stale directory: %s", stale_workspaces)
            except Exception as exc:
                logger.warning(
                    "Failed to remove stale directory %s: %s", stale_workspaces, exc
                )

        stale_build = project_root / ".aura" / "drone-build"
        if stale_build.is_dir():
            backup_dir = project_root / ".aura" / "backups" / "stale_drone_build"
            try:
                backup_dir.parent.mkdir(parents=True, exist_ok=True)
                if backup_dir.exists():
                    shutil.rmtree(str(backup_dir))
                shutil.move(str(stale_build), str(backup_dir))
                logger.info(
                    "Migrated stale build dir %s -> %s", stale_build, backup_dir
                )
            except Exception as exc:
                logger.warning(
                    "Failed to migrate stale build dir %s: %s", stale_build, exc
                )
