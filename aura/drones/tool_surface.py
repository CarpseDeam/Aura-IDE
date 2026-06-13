from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.drones.capabilities import CapabilityBinding  # noqa: F401
from aura.drones.definition import DroneDefinition


@dataclass(frozen=True)
class DroneToolSurface:
    registry: Any
    allowed_tools: frozenset[str]
    tool_defs: tuple[dict[str, Any], ...]
    setup_notes: tuple[str, ...] = ()


def build_drone_tool_surface(*_args: Any, **_kwargs: Any) -> DroneToolSurface:
    raise RuntimeError(
        "Drone tool surfaces are unsupported. Drones execute their folder entrypoint."
    )
