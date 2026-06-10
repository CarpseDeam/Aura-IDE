from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from aura.drones.capabilities import CapabilityBinding, CapabilityRequirement
from aura.drones.definition import DroneBudget, DroneDefinition, slugify
from aura.drones.receipt import DroneReceipt

logger = logging.getLogger(__name__)

_DRONE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WRITE_POLICIES = {"read_only", "ask_before_writes", "normal_diff_approval"}


def _is_safe_drone_id(drone_id: str) -> bool:
    return bool(_DRONE_ID_RE.fullmatch(str(drone_id or "")))


def _drone_from_dict(data: dict) -> DroneDefinition:
    """Reconstruct a DroneDefinition from a JSON-deserialized dict.

    asdict() converts nested dataclasses and tuples to plain dicts/lists
    during serialization; restore them to their proper types.
    """
    if "allowed_tools" in data and isinstance(data["allowed_tools"], list):
        data = {**data, "allowed_tools": tuple(data["allowed_tools"])}
    if "budget" in data and isinstance(data["budget"], dict):
        data = {**data, "budget": DroneBudget(**data["budget"])}
    if "capability_requirements" in data and isinstance(data["capability_requirements"], list):
        data = {
            **data,
            "capability_requirements": tuple(
                CapabilityRequirement.from_dict(d) for d in data["capability_requirements"]
            ),
        }
    if "capability_bindings" in data and isinstance(data["capability_bindings"], list):
        data = {
            **data,
            "capability_bindings": tuple(
                CapabilityBinding.from_dict(d) for d in data["capability_bindings"]
            ),
        }
    if "setup_steps" in data and isinstance(data["setup_steps"], list):
        data = {**data, "setup_steps": tuple(str(x) for x in data["setup_steps"])}
    drone = DroneDefinition(**data)
    DroneStore.validate_drone(drone)
    return drone


class DroneStore:
    """Read/write Drones from/to the .aura/drones/ directory.

    All methods are static; workspace_root is always passed explicitly.
    """

    @staticmethod
    def drones_dir(workspace_root: Path) -> Path:
        """Return the .aura/drones path without creating it."""
        return workspace_root / ".aura" / "drones"

    @staticmethod
    def _ensure_drones_dir(workspace_root: Path) -> Path:
        """Create and return the .aura/drones directory."""
        d = workspace_root / ".aura" / "drones"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def list_drones(workspace_root: Path) -> list[DroneDefinition]:
        d = DroneStore.drones_dir(workspace_root)
        if not d.exists():
            return []
        results: list[DroneDefinition] = []
        for p in sorted(d.iterdir()):
            if p.suffix != ".json":
                continue
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                results.append(_drone_from_dict(data))
            except Exception:
                logger.warning("Skipping invalid drone file: %s", p)
        return results

    @staticmethod
    def load_drone(workspace_root: Path, drone_id: str) -> DroneDefinition | None:
        if not _is_safe_drone_id(drone_id):
            return None
        d = DroneStore.drones_dir(workspace_root)
        p = d / f"{drone_id}.json"
        if not p.exists():
            return None
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return _drone_from_dict(data)
        except Exception:
            logger.warning("Failed to load drone %s", drone_id)
            return None

    @staticmethod
    def save_drone(workspace_root: Path, drone: DroneDefinition) -> None:
        DroneStore.validate_drone(drone)
        d = DroneStore._ensure_drones_dir(workspace_root)
        p = d / f"{drone.id}.json"
        data = asdict(drone)
        fd, tmp_path = tempfile.mkstemp(dir=str(d), suffix=".json")
        with open(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        Path(tmp_path).replace(p)

    @staticmethod
    def delete_drone(workspace_root: Path, drone_id: str) -> bool:
        """Remove a drone definition file. Returns True if deleted."""
        if not _is_safe_drone_id(drone_id):
            return False
        d = DroneStore.drones_dir(workspace_root)
        p = d / f"{drone_id}.json"
        if not p.exists():
            return False
        p.unlink()
        return True

    @staticmethod
    def next_id(workspace_root: Path, name: str) -> str:
        base = slugify(name)
        if not base:
            base = "drone"
        d = DroneStore.drones_dir(workspace_root)
        candidate = base
        counter = 0
        while (d / f"{candidate}.json").exists():
            counter += 1
            candidate = f"{base}-{counter}"
        return candidate

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
        if drone.budget.max_tool_rounds < 1:
            raise ValueError("Drone max_tool_rounds must be at least 1")
        if drone.budget.timeout_seconds < 30:
            raise ValueError("Drone timeout_seconds must be at least 30")


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
