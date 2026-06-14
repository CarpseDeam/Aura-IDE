from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from aura.drones.definition import DroneBudget, DroneDefinition, slugify
from aura.drones.receipt import DroneReceipt
from aura.paths import data_dir

logger = logging.getLogger(__name__)

_DRONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WRITE_POLICIES = {"read_only", "ask_before_writes", "normal_diff_approval"}
def _is_safe_drone_id(drone_id: str) -> bool:
    return bool(_DRONE_ID_RE.fullmatch(str(drone_id or "")))


def _global_drones_root() -> Path:
    """Return the global drones storage directory."""
    return data_dir() / "drones"


def _drone_from_dict(data: dict) -> DroneDefinition:
    """Reconstruct a DroneDefinition from a JSON-deserialized dict.

    asdict() converts nested dataclasses and tuples to plain dicts/lists
    during serialization; restore them to their proper types.
    """
    if "budget" in data and isinstance(data["budget"], dict):
        known_budget_fields = {f.name for f in fields(DroneBudget)}
        budget_filtered = {k: v for k, v in data["budget"].items() if k in known_budget_fields}
        data = {**data, "budget": DroneBudget(**budget_filtered)}
    if "secrets" in data and isinstance(data["secrets"], list):
        data = {**data, "secrets": tuple(str(x) for x in data["secrets"])}
    if "dependencies" in data and isinstance(data["dependencies"], list):
        data = {**data, "dependencies": tuple(str(x) for x in data["dependencies"])}
    # Detect legacy string entrypoint and fail early
    if isinstance(data.get("entrypoint"), str):
        raise ValueError(
            "Legacy string entrypoint is no longer supported. "
            "Drone must use command entrypoint: {'kind': 'command', ...}"
        )
    known_fields = {f.name for f in fields(DroneDefinition)}
    filtered = {k: v for k, v in data.items() if k in known_fields}
    drone = DroneDefinition(**filtered)
    DroneStore.validate_drone(drone)
    return drone


@dataclass(frozen=True)
class DroneListEntry:
    """UI-facing Drone row, including Drones currently in the Builder."""

    id: str
    name: str
    description: str
    write_policy: str
    status: str
    ready: bool
    drone: DroneDefinition | None = None
    workspace_id: str | None = None
    last_error: str | None = None
    updated_at: str = ""



def _validate_entrypoint(entrypoint: dict) -> None:
    """Validate the command entrypoint structure."""
    if entrypoint.get("kind") != "command":
        raise ValueError("Drone entrypoint kind must be 'command'")
    if entrypoint.get("protocol") != "json-stdio":
        raise ValueError("Drone entrypoint protocol must be 'json-stdio'")
    command = entrypoint.get("command")
    if not isinstance(command, list) or len(command) == 0:
        raise ValueError("Drone entrypoint command must be a non-empty list")
    for i, part in enumerate(command):
        if not isinstance(part, str):
            raise ValueError(f"Drone entrypoint command[{i}] must be a string")
    if not command[0].strip():
        raise ValueError("Drone entrypoint command[0] must be a non-empty string")


def _builder_status_for_phase(phase: str) -> str:
    """Map internal Builder phases to the user-facing Drone list statuses."""
    phase = str(phase or "").lower()
    if phase == "workshop":
        return "Draft"
    if phase in {"building", "iterating"}:
        return "Building"
    if phase in {"readiness_running", "proof_running", "installing", "awaiting_decision"}:
        return "Testing"
    if phase in {"readiness_failed", "proof_failed"}:
        return "Needs Fix"
    if phase == "installed":
        return "Ready"
    return "Draft"


def _read_candidate_manifest_summary(folder: Path) -> dict[str, Any]:
    """Read non-authoritative row metadata from a Builder candidate manifest."""
    manifest = folder / "drone.json"
    if not manifest.exists():
        return {}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Could not read Builder Drone manifest: %s", manifest, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "description": data.get("description"),
        "write_policy": data.get("write_policy"),
    }


class DroneStore:
    """Read registered folder-backed Drones from Aura's global Drone directory."""

    @staticmethod
    def list_drones(workspace_root: Path) -> list[DroneDefinition]:
        _ = workspace_root
        seen: dict[str, DroneDefinition] = {}

        global_root = _global_drones_root()
        if global_root.exists():
            for subdir in sorted(global_root.iterdir()):
                if not subdir.is_dir():
                    continue
                drone_file = subdir / "drone.json"
                if not drone_file.exists():
                    continue
                try:
                    data = json.loads(drone_file.read_text(encoding="utf-8"))
                    drone = _drone_from_dict(data)
                    seen[drone.id] = drone
                except Exception as exc:
                    logger.warning("Skipping invalid Drone folder %s: %s", subdir, exc)

        return sorted(seen.values(), key=lambda d: d.name)

    @staticmethod
    def list_drone_entries(workspace_root: Path) -> list[DroneListEntry]:
        """Return list rows for ready Drones plus Builder Drones in progress."""
        installed = {drone.id: drone for drone in DroneStore.list_drones(workspace_root)}
        rows: dict[str, DroneListEntry] = {
            drone_id: DroneListEntry(
                id=drone.id,
                name=drone.name,
                description=drone.description,
                write_policy=drone.write_policy,
                status="Ready",
                ready=True,
                drone=drone,
            )
            for drone_id, drone in installed.items()
        }

        try:
            from aura.drones.workspaces.model import WorkspacePhase
            from aura.drones.workspaces.paths import candidate_dir
            from aura.drones.workspaces.store import DroneWorkspaceStore
        except Exception:
            logger.debug("Builder workspace modules unavailable", exc_info=True)
            return sorted(rows.values(), key=lambda r: r.name.lower())

        for workspace in DroneWorkspaceStore.list_workspaces(workspace_root):
            phase = workspace.phase
            if phase == WorkspacePhase.DISCARDED.value:
                continue
            if phase == WorkspacePhase.INSTALLED.value and workspace.installed_drone_id:
                continue

            candidate_folder = candidate_dir(Path(workspace.project_root), workspace.workspace_id)
            candidate = _read_candidate_manifest_summary(candidate_folder)
            installed_drone = (
                installed.get(workspace.installed_drone_id)
                if workspace.installed_drone_id
                else None
            )

            drone_id = (
                str(candidate.get("id") or "")
                or workspace.candidate_drone_id
                or workspace.installed_drone_id
                or f"builder:{workspace.workspace_id}"
            )
            key = workspace.installed_drone_id or drone_id or f"builder:{workspace.workspace_id}"
            name = (
                str(candidate.get("name") or "").strip()
                or (installed_drone.name if installed_drone else "")
                or workspace.display_name
                or "New Drone"
            )
            description = (
                str(candidate.get("description") or "").strip()
                or (installed_drone.description if installed_drone else "")
                or workspace.build_brief
                or "Being designed in the Drone Builder."
            )
            write_policy = (
                str(candidate.get("write_policy") or "").strip()
                or (installed_drone.write_policy if installed_drone else "")
                or "read_only"
            )
            if write_policy not in _WRITE_POLICIES:
                write_policy = "read_only"

            rows[key] = DroneListEntry(
                id=key if str(key).startswith("builder:") else str(drone_id or key),
                name=name,
                description=description,
                write_policy=write_policy,
                status=_builder_status_for_phase(phase),
                ready=False,
                drone=installed_drone,
                workspace_id=workspace.workspace_id,
                last_error=workspace.last_error,
                updated_at=workspace.updated_at,
            )

        return sorted(rows.values(), key=lambda r: (r.ready, r.name.lower()))

    @staticmethod
    def load_drone(workspace_root: Path, drone_id: str) -> DroneDefinition | None:
        _ = workspace_root
        if not _is_safe_drone_id(drone_id):
            return None

        global_file = _global_drones_root() / drone_id / "drone.json"
        if global_file.exists():
            try:
                data = json.loads(global_file.read_text(encoding="utf-8"))
                return _drone_from_dict(data)
            except Exception as exc:
                logger.warning("Failed to load Drone %s: %s", drone_id, exc)
                return None

        return None

    @staticmethod
    def save_drone(workspace_root: Path, drone: DroneDefinition) -> None:
        """Update the manifest for an already registered folder-backed Drone.

        This is not a creation endpoint. New Drones must be installed with
        register_drone_folder so their code and readiness check are present.
        """
        _ = workspace_root
        DroneStore.validate_drone(drone)
        drone_dir = _global_drones_root() / drone.id
        if not (drone_dir / "drone.json").exists():
            raise ValueError("register_drone_folder is required before a Drone manifest can be updated")
        DroneStore._write_manifest(drone_dir, drone)

    @staticmethod
    def drone_folder(drone_id: str) -> Path:
        """Return the global folder for a registered Drone id."""
        return _global_drones_root() / drone_id

    @staticmethod
    def _write_manifest(drone_dir: Path, drone: DroneDefinition) -> None:
        drone_dir.mkdir(parents=True, exist_ok=True)
        p = drone_dir / "drone.json"
        data = asdict(drone)
        fd, tmp_path = tempfile.mkstemp(dir=str(drone_dir), suffix=".json")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        Path(tmp_path).replace(p)

    @staticmethod
    def load_drone_from_folder(folder: Path) -> DroneDefinition:
        """Load and validate a folder-backed Drone manifest."""
        folder = folder.resolve()
        manifest = folder / "drone.json"
        if not manifest.exists():
            raise ValueError("drone.json is required")
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"drone.json is not valid JSON: {exc}") from exc

        drone = _drone_from_dict(data)
        # Validate that the first command argument exists on PATH or in the folder
        command_list = drone.entrypoint.get("command", [])
        if command_list:
            first_arg = command_list[0]
            if first_arg.startswith("./"):
                # Relative path — check it exists in the Drone folder
                candidate = folder / first_arg
                if not candidate.exists():
                    raise ValueError(f"Entrypoint command '{first_arg}' not found in Drone folder")
            else:
                # Bare name — check on PATH
                import shutil
                if not shutil.which(first_arg):
                    raise ValueError(f"Entrypoint command '{first_arg}' not found on PATH")
        return drone

    @staticmethod
    def register_drone_folder(
        workspace_root: Path,
        source_folder: Path,
        *,
        readiness_result: dict | None = None,
    ) -> DroneDefinition:
        """Validate and install a folder-backed Drone into global storage."""
        _ = workspace_root
        source_folder = source_folder.resolve()
        drone = DroneStore.load_drone_from_folder(source_folder)
        if readiness_result is None:
            from aura.drones.folder_runner import run_drone_readiness

            readiness_result = run_drone_readiness(source_folder, drone, workspace_root)
        if not bool(readiness_result.get("ok")):
            raise ValueError(f"Drone readiness check failed: {readiness_result}")

        target_folder = _global_drones_root() / drone.id
        target_folder.parent.mkdir(parents=True, exist_ok=True)

        tmp_name = tempfile.mkdtemp(
            dir=str(target_folder.parent),
            prefix=f".{drone.id}-",
        )
        tmp_folder = Path(tmp_name)
        try:
            shutil.copytree(source_folder, tmp_folder, dirs_exist_ok=True)
            DroneStore.load_drone_from_folder(tmp_folder)
            if target_folder.exists():
                shutil.rmtree(target_folder)
            tmp_folder.replace(target_folder)
        except Exception:
            if tmp_folder.exists():
                shutil.rmtree(tmp_folder, ignore_errors=True)
            raise
        return drone

    @staticmethod
    def delete_drone(workspace_root: Path, drone_id: str) -> bool:
        """Remove a drone definition. Returns True if anything was deleted."""
        _ = workspace_root
        if not _is_safe_drone_id(drone_id):
            return False

        deleted = False

        global_dir = _global_drones_root() / drone_id
        global_file = global_dir / "drone.json"
        if global_file.exists():
            shutil.rmtree(global_dir, ignore_errors=True)
            deleted = True

        return deleted

    @staticmethod
    def next_id(workspace_root: Path, name: str) -> str:
        _ = workspace_root
        base = slugify(name)
        if not base:
            base = "drone"

        candidate = base
        counter = 0

        while True:
            global_exists = (_global_drones_root() / candidate / "drone.json").exists()
            if not global_exists:
                return candidate
            counter += 1
            candidate = f"{base}-{counter}"

    @staticmethod
    def validate_drone(drone: DroneDefinition) -> None:
        """Validate a DroneDefinition before saving or returning it."""
        if not _is_safe_drone_id(drone.id):
            raise ValueError("Drone id must be lowercase letters, numbers, and hyphens")
        if not drone.name.strip():
            raise ValueError("Drone name is required")
        if not drone.instructions.strip():
            raise ValueError("Drone instructions are required")
        if not drone.output_contract.strip():
            raise ValueError("Drone output contract is required")
        if drone.write_policy not in _WRITE_POLICIES:
            raise ValueError(f"Invalid Drone write policy: {drone.write_policy}")
        if drone.budget.timeout_seconds < 30:
            raise ValueError("Drone timeout_seconds must be at least 30")
        if drone.scope not in ("global", "project"):
            raise ValueError(f"Invalid Drone scope: {drone.scope}")
        if not isinstance(drone.entrypoint, dict) or not drone.entrypoint:
            raise ValueError("Drone entrypoint is required")
        _validate_entrypoint(drone.entrypoint)


class RunHistoryStore:
    """Persistent store for completed Drone run receipts."""

    @staticmethod
    def history_dir(workspace_root: Path) -> Path:
        return workspace_root / ".aura" / "drones" / "runs"

    @staticmethod
    def save_run(workspace_root: Path, receipt: DroneReceipt) -> None:
        d = RunHistoryStore.history_dir(workspace_root)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{receipt.run_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(receipt.to_dict(), f, indent=2, ensure_ascii=False)

    @staticmethod
    def list_runs(workspace_root: Path, limit: int = 50) -> list[dict]:
        """Return run summaries sorted most-recent-first."""
        d = RunHistoryStore.history_dir(workspace_root)
        if not d.exists():
            return []
        runs: list[dict] = []
        for p in d.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                runs.append(data)
            except Exception:
                logger.warning("Skipping invalid run file: %s", p)
                continue
        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        return runs[:limit]

    @staticmethod
    def list_run_summaries(workspace_root: Path, limit: int = 50) -> list[dict]:
        """Parse receipt JSON files and return lightweight summary dicts.

        Each JSON file is fully parsed (unavoidable to extract fields)
        but only the lightweight fields are kept; tool_calls, summary,
        errors, and output_contract are discarded.
        """
        d = RunHistoryStore.history_dir(workspace_root)
        if not d.exists():
            return []
        runs: list[dict] = []
        for p in d.glob("*.json"):
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                summary = {
                    "run_id": data.get("run_id", ""),
                    "drone_id": data.get("drone_id", ""),
                    "drone_name": data.get("drone_name", ""),
                    "status": data.get("status", ""),
                    "started_at": data.get("started_at", ""),
                    "elapsed_seconds": data.get("elapsed_seconds", 0),
                }
                runs.append(summary)
            except Exception:
                logger.warning("Skipping invalid run file: %s", p)
                continue
        runs.sort(key=lambda r: r.get("started_at", ""), reverse=True)
        logger.debug("[RunHistory] list_run_summaries: %d runs in %s", len(runs), d)
        return runs[:limit]

    @staticmethod
    def load_run(workspace_root: Path, run_id: str) -> DroneReceipt | None:
        d = RunHistoryStore.history_dir(workspace_root)
        path = d / f"{run_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                return DroneReceipt.from_dict(json.load(f))
        except Exception:
            logger.warning("Failed to load run %s", run_id)
            return None

    @staticmethod
    def delete_run(workspace_root: Path, run_id: str) -> bool:
        d = RunHistoryStore.history_dir(workspace_root)
        path = d / f"{run_id}.json"
        if path.exists():
            path.unlink()
            return True
        return False

    @staticmethod
    def clear_history(workspace_root: Path) -> int:
        count = 0
        d = RunHistoryStore.history_dir(workspace_root)
        if d.exists():
            for p in list(d.glob("*.json")):
                p.unlink()
                count += 1
        return count
