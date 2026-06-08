from __future__ import annotations

import json
import logging
import tempfile
from dataclasses import asdict
from pathlib import Path

from aura.drones.definition import DroneBudget, DroneDefinition, slugify

logger = logging.getLogger(__name__)


def _drone_from_dict(data: dict) -> DroneDefinition:
    """Reconstruct a DroneDefinition from a JSON-deserialized dict.

    asdict() converts nested dataclasses and tuples to plain dicts/lists
    during serialization; restore them to their proper types.
    """
    if "allowed_tools" in data and isinstance(data["allowed_tools"], list):
        data = {**data, "allowed_tools": tuple(data["allowed_tools"])}
    if "budget" in data and isinstance(data["budget"], dict):
        data = {**data, "budget": DroneBudget(**data["budget"])}
    return DroneDefinition(**data)


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
