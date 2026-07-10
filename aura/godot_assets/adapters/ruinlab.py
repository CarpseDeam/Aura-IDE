"""Read the existing V_Ruins/RuinLab JSON catalog without mutating it."""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

from aura.godot_assets.models import AssetCatalogSnapshot, GodotAsset, GodotAssetSocket

CATALOG_PATH = Path("assets/ruins/catalog/ruin_pieces.json")
CALIBRATIONS_PATH = Path("assets/ruins/catalog/calibrations.json")

_ROLE_TAGS = {
    "cover": "cover",
    "decor": "decoration",
    "detail": "decoration",
    "doorway": "entrance",
    "entrance": "entrance",
    "gate": "entrance",
    "pillar": "support",
    "rubble": "scatter",
    "scatter": "scatter",
    "silhouette": "silhouette",
    "wall": "barrier",
    "wall_fragment": "decoration",
    "window": "opening",
}


class RuinLabCatalogSource:
    """Adapter for the trackable RuinLab piece and calibration catalogs."""

    name = "ruinlab_json"

    def is_available(self, project_root: Path) -> bool:
        return (project_root / CATALOG_PATH).is_file()

    def load(self, project_root: Path) -> AssetCatalogSnapshot:
        root = project_root.resolve()
        diagnostics: list[dict[str, Any]] = []
        catalog_file = root / CATALOG_PATH
        entries = _read_json(catalog_file, list, diagnostics, "catalog")
        calibrations = _read_json(root / CALIBRATIONS_PATH, dict, diagnostics, "calibrations", optional=True)
        if not isinstance(entries, list):
            return AssetCatalogSnapshot((self.name,), (), tuple(diagnostics))
        if not isinstance(calibrations, dict):
            calibrations = {}

        assets: list[GodotAsset] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(entries):
            asset = _parse_entry(root, entry, index, calibrations, diagnostics)
            if asset is None:
                continue
            if asset.id in seen_ids:
                diagnostics.append(_diagnostic("duplicate_id", f"duplicate asset id: {asset.id}", asset.id))
                continue
            seen_ids.add(asset.id)
            assets.append(asset)
        assets.sort(key=lambda asset: asset.id.casefold())
        return AssetCatalogSnapshot((self.name,), tuple(assets), tuple(diagnostics))


def _parse_entry(
    root: Path,
    entry: Any,
    index: int,
    calibrations: dict[str, Any],
    diagnostics: list[dict[str, Any]],
) -> GodotAsset | None:
    if not isinstance(entry, dict):
        diagnostics.append(_diagnostic("invalid_entry", f"entry {index} must be an object"))
        return None
    asset_id = str(entry.get("id") or "").strip()
    resource_path = str(entry.get("path") or "").strip()
    kind = str(entry.get("kind") or "").strip()
    if not asset_id or not resource_path or not kind:
        diagnostics.append(
            _diagnostic("missing_required_field", f"entry {index} requires id, path, and kind", asset_id)
        )
        return None
    disk_path = _resolve_resource_path(root, resource_path)
    if disk_path is None:
        diagnostics.append(_diagnostic("invalid_resource_path", f"invalid res:// path: {resource_path}", asset_id))
        return None
    if disk_path.suffix.lower() != ".tscn":
        diagnostics.append(_diagnostic("unsupported_resource", f"asset is not a .tscn: {resource_path}", asset_id))
        return None
    if not disk_path.is_file():
        diagnostics.append(_diagnostic("missing_asset", f"asset file does not exist: {resource_path}", asset_id))
        return None

    footprint = _vector(entry.get("footprint_m"), 2)
    height = _positive_number(entry.get("height_m"))
    weight = _positive_number(entry.get("weight", 1.0))
    if footprint is None or any(value <= 0 for value in footprint):
        diagnostics.append(_diagnostic("invalid_footprint", "footprint_m must contain two positive numbers", asset_id))
        return None
    if height is None:
        diagnostics.append(_diagnostic("invalid_height", "height_m must be a positive number", asset_id))
        return None
    if weight is None:
        diagnostics.append(_diagnostic("invalid_weight", "weight must be a positive number", asset_id))
        return None

    raw_tags = entry.get("tags", [])
    if not isinstance(raw_tags, list) or any(not isinstance(tag, str) or not tag.strip() for tag in raw_tags):
        diagnostics.append(_diagnostic("invalid_tags", "tags must be non-empty strings", asset_id))
        return None
    tags = tuple(dict.fromkeys(tag.strip().lower() for tag in raw_tags))
    sockets = _parse_sockets(entry.get("sockets", []), asset_id, diagnostics)
    if sockets is None:
        return None
    roles = tuple(sorted({_ROLE_TAGS[tag] for tag in tags if tag in _ROLE_TAGS}))
    if not roles:
        diagnostics.append(
            _diagnostic(
                "ambiguous_semantics",
                "asset has no recognized generic semantic role; project metadata should enrich it",
                asset_id,
            )
        )
    placement_mode = "socket" if sockets else "ground"
    calibration = calibrations.get(asset_id, {})
    if not isinstance(calibration, dict):
        diagnostics.append(_diagnostic("invalid_calibration", "calibration must be an object", asset_id))
        calibration = {}
    return GodotAsset(
        id=asset_id,
        resource_path=resource_path,
        domain="ruins",
        kind=kind,
        tags=tags,
        semantic_roles=roles,
        footprint_m=(footprint[0], footprint[1]),
        height_m=height,
        local_bounds_m=(footprint[0], height, footprint[1]),
        allowed_rotations_deg=(),
        sockets=sockets,
        weight=weight,
        placement_mode=placement_mode,
        source="assets/ruins/catalog/ruin_pieces.json",
        semantic_source="ruinlab_adapter_inference",
        calibration=dict(calibration),
    )


def _parse_sockets(
    raw_sockets: Any,
    asset_id: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[GodotAssetSocket, ...] | None:
    if not isinstance(raw_sockets, list):
        diagnostics.append(_diagnostic("invalid_sockets", "sockets must be an array", asset_id))
        return None
    sockets: list[GodotAssetSocket] = []
    seen: set[str] = set()
    for raw in raw_sockets:
        if not isinstance(raw, dict):
            diagnostics.append(_diagnostic("invalid_socket", "each socket must be an object", asset_id))
            return None
        socket_id = str(raw.get("id") or "").strip()
        position = _vector(raw.get("position"), 3)
        facing = _vector(raw.get("facing"), 3)
        if not socket_id or socket_id in seen or position is None or facing is None:
            diagnostics.append(_diagnostic("invalid_socket", "socket id/vector is missing or duplicated", asset_id))
            return None
        if math.sqrt(sum(value * value for value in facing)) < 1e-6:
            diagnostics.append(_diagnostic("invalid_socket_facing", f"socket {socket_id} has zero facing", asset_id))
            return None
        seen.add(socket_id)
        sockets.append(GodotAssetSocket(socket_id, position, facing))
    return tuple(sockets)


def _resolve_resource_path(root: Path, resource_path: str) -> Path | None:
    if not resource_path.startswith("res://"):
        return None
    relative = PurePosixPath(resource_path[6:])
    if relative.is_absolute() or ".." in relative.parts:
        return None
    candidate = (root / Path(*relative.parts)).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _read_json(
    path: Path,
    expected_type: type,
    diagnostics: list[dict[str, Any]],
    label: str,
    *,
    optional: bool = False,
) -> Any:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not optional:
            diagnostics.append(_diagnostic(f"missing_{label}", f"{label} file does not exist: {path}"))
        return None
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(_diagnostic(f"invalid_{label}", f"could not read {label}: {exc}"))
        return None
    if not isinstance(parsed, expected_type):
        diagnostics.append(_diagnostic(f"invalid_{label}", f"{label} has the wrong JSON root type"))
        return None
    return parsed


def _vector(value: Any, size: int):
    if not isinstance(value, list) or len(value) != size:
        return None
    try:
        vector = tuple(float(component) for component in value)
    except (TypeError, ValueError):
        return None
    return vector if all(math.isfinite(component) for component in vector) else None


def _positive_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _diagnostic(code: str, message: str, asset_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"severity": "warning", "code": code, "message": message}
    if asset_id:
        result["asset_id"] = asset_id
    return result


__all__ = ["RuinLabCatalogSource"]
