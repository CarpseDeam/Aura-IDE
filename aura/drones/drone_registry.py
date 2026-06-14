from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.drones.store import DroneStore


class DroneRegistry:
    """Instance-based facade over DroneStore for use in GUI components."""

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root

    def list_drones(self) -> list[dict[str, Any]]:
        drones = DroneStore.list_drones(self._root)
        return [
            {
                "id": d.id,
                "name": d.name,
                "description": d.description,
                "input_type": (d.input_contract or {}).get("type", ""),
                "cargo_type": (d.cargo_contract or {}).get("type", ""),            }
            for d in drones
        ]

    def get_drone(self, drone_id: str) -> dict[str, Any] | None:
        d = DroneStore.load_drone(self._root, drone_id)
        if d is None:
            return None
        return {
            "id": d.id,
            "name": d.name,
            "description": d.description,
            "input_type": (d.input_contract or {}).get("type", ""),
            "cargo_type": (d.cargo_contract or {}).get("type", ""),        }
