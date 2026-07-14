"""Generic records shared by Godot asset catalog adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class GodotAssetSocket:
    id: str
    position: tuple[float, float, float]
    facing: tuple[float, float, float]

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "position": list(self.position), "facing": list(self.facing)}


@dataclass(frozen=True)
class GodotAsset:
    id: str
    resource_path: str
    domain: str
    kind: str
    tags: tuple[str, ...]
    semantic_roles: tuple[str, ...]
    footprint_m: tuple[float, float] | None
    height_m: float | None
    local_bounds_m: tuple[float, float, float] | None
    allowed_rotations_deg: tuple[float, ...]
    sockets: tuple[GodotAssetSocket, ...]
    weight: float
    placement_mode: str
    source: str
    semantic_source: str
    calibration: dict[str, Any] = field(default_factory=dict)
    orientation: dict[str, Any] = field(default_factory=dict)
    wall_face_placement: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "resource_path": self.resource_path,
            "domain": self.domain,
            "kind": self.kind,
            "tags": list(self.tags),
            "semantic_roles": list(self.semantic_roles),
            "footprint_m": list(self.footprint_m) if self.footprint_m else None,
            "height_m": self.height_m,
            "local_bounds_m": list(self.local_bounds_m) if self.local_bounds_m else None,
            "allowed_rotations_deg": list(self.allowed_rotations_deg),
            "sockets": [socket.to_dict() for socket in self.sockets],
            "weight": self.weight,
            "placement_mode": self.placement_mode,
            "source": self.source,
            "semantic_source": self.semantic_source,
            "calibration": self.calibration,
            "orientation": self.orientation,
            "wall_face_placement": self.wall_face_placement,
        }


@dataclass(frozen=True)
class AssetCatalogSnapshot:
    sources: tuple[str, ...]
    assets: tuple[GodotAsset, ...]
    diagnostics: tuple[dict[str, Any], ...]


class GodotAssetCatalogSource(Protocol):
    """Adapter boundary for project-specific asset metadata."""

    name: str

    def is_available(self, project_root) -> bool: ...

    def load(self, project_root) -> AssetCatalogSnapshot: ...


__all__ = [
    "AssetCatalogSnapshot",
    "GodotAsset",
    "GodotAssetCatalogSource",
    "GodotAssetSocket",
]
