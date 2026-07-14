"""Read the existing V_Ruins/RuinLab catalogs without mutating them."""

from __future__ import annotations

import json
import math
from pathlib import Path, PurePosixPath
from typing import Any

from aura.godot_assets.models import AssetCatalogSnapshot, GodotAsset, GodotAssetSocket
from aura.godot_assets.orientation import (
    load_orientation_catalog,
    resolve_orientation,
    verified_allowed_rotations,
)

CATALOG_PATH = Path("assets/ruins/catalog/ruin_pieces.json")
STRUCTURAL_CATALOG_PATH = Path("assets/ruins/catalog/interior_structural_assets.json")
CALIBRATIONS_PATH = Path("assets/ruins/catalog/calibrations.json")

_ROLE_TAGS = {
    "arch": "opening",
    "barrier": "barrier",
    "ceiling": "ceiling",
    "column": "support",
    "cover": "cover",
    "decor": "decoration",
    "decoration": "decoration",
    "detail": "decoration",
    "door": "entrance",
    "doorway": "entrance",
    "entrance": "entrance",
    "floor": "floor",
    "gate": "entrance",
    "opening": "opening",
    "pier": "support",
    "pillar": "support",
    "rubble": "scatter",
    "scatter": "scatter",
    "silhouette": "silhouette",
    "stair": "circulation",
    "structural": "structure",
    "support": "support",
    "wall": "barrier",
    "wall_fragment": "decoration",
    "window": "opening",
}


class RuinLabCatalogSource:
    """Adapter for both trackable RuinLab exact-piece catalogs."""

    name = "ruinlab_json"

    def is_available(self, project_root: Path) -> bool:
        return (project_root / CATALOG_PATH).is_file()

    def load(self, project_root: Path) -> AssetCatalogSnapshot:
        root = project_root.resolve()
        diagnostics: list[dict[str, Any]] = []
        entries = _read_json(root / CATALOG_PATH, list, diagnostics, "catalog")
        structural = _read_json(
            root / STRUCTURAL_CATALOG_PATH,
            dict,
            diagnostics,
            "structural_catalog",
        )
        calibrations = _read_json(
            root / CALIBRATIONS_PATH,
            dict,
            diagnostics,
            "calibrations",
            optional=True,
        )
        orientation_catalog = load_orientation_catalog(root, diagnostics)
        if not isinstance(entries, list):
            entries = []
        if not isinstance(calibrations, dict):
            calibrations = {}

        assets: list[GodotAsset] = []
        seen_ids: set[str] = set()
        for index, entry in enumerate(entries):
            asset = _parse_base_entry(
                root, entry, index, calibrations, orientation_catalog, diagnostics
            )
            _append_unique(asset, assets, seen_ids, diagnostics)
        if isinstance(structural, dict):
            for asset in _parse_structural_catalog(
                root, structural, orientation_catalog, diagnostics
            ):
                _append_unique(asset, assets, seen_ids, diagnostics)
        assets.sort(key=lambda asset: asset.id.casefold())
        return AssetCatalogSnapshot((self.name,), tuple(assets), tuple(diagnostics))


def _append_unique(
    asset: GodotAsset | None,
    assets: list[GodotAsset],
    seen_ids: set[str],
    diagnostics: list[dict[str, Any]],
) -> None:
    if asset is None:
        return
    identity = asset.id.casefold()
    if identity in seen_ids:
        diagnostics.append(_diagnostic("duplicate_id", f"duplicate asset id: {asset.id}", asset.id))
        return
    seen_ids.add(identity)
    assets.append(asset)


def _parse_base_entry(
    root: Path,
    entry: Any,
    index: int,
    calibrations: dict[str, Any],
    orientation_catalog: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]],
) -> GodotAsset | None:
    checked = _required_entry(root, entry, index, diagnostics, source="catalog")
    if checked is None:
        return None
    asset_id, resource_path, kind = checked
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
    tags = _parse_tags(entry.get("tags", []), asset_id, diagnostics)
    if tags is None:
        return None
    sockets = _parse_sockets(entry.get("sockets", []), asset_id, diagnostics)
    if sockets is None:
        return None
    architecture = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    roles = _semantic_roles(tags, architecture, asset_id, diagnostics)
    orientation = resolve_orientation(
        entry, orientation_catalog, diagnostics, asset_id=asset_id
    )
    calibration = calibrations.get(asset_id, {})
    if not isinstance(calibration, dict):
        diagnostics.append(_diagnostic("invalid_calibration", "calibration must be an object", asset_id))
        calibration = {}
    calibration = dict(calibration)
    pivot = _vector(architecture.get("pivot_to_center_m"), 3)
    if pivot is not None:
        calibration.setdefault("pivot_to_center_m", list(pivot))
    if not calibration and pivot is None:
        diagnostics.append(
            _diagnostic(
                "incomplete_calibration",
                "asset has no audited pivot or position calibration; authored root placement remains available",
                asset_id,
            )
        )
    wall_face = architecture.get("wall_placement")
    if wall_face is not None and not isinstance(wall_face, dict):
        diagnostics.append(_diagnostic("invalid_wall_face_placement", "wall_placement must be an object", asset_id))
        wall_face = {}
    elif isinstance(wall_face, dict):
        _validate_wall_face_metadata(wall_face, asset_id, diagnostics)
    placement_mode = _placement_mode(sockets, architecture, wall_face)
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
        allowed_rotations_deg=verified_allowed_rotations(
            orientation, diagnostics, asset_id=asset_id
        ),
        sockets=sockets,
        weight=weight,
        placement_mode=placement_mode,
        source=CATALOG_PATH.as_posix(),
        semantic_source="ruinlab_catalog_metadata",
        calibration=calibration,
        orientation=dict(orientation or {}),
        wall_face_placement=dict(wall_face or {}),
    )


def _parse_structural_catalog(
    root: Path,
    parsed: dict[str, Any],
    orientation_catalog: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]],
) -> list[GodotAsset]:
    families = parsed.get("families")
    entries = parsed.get("assets")
    if not isinstance(families, dict) or not families or not isinstance(entries, list):
        diagnostics.append(_diagnostic("invalid_structural_catalog", "structural catalog requires families and assets"))
        return []
    assets: list[GodotAsset] = []
    for index, entry in enumerate(entries):
        asset = _parse_structural_entry(
            root, entry, index, families, orientation_catalog, diagnostics
        )
        if asset is not None:
            assets.append(asset)
    return assets


def _parse_structural_entry(
    root: Path,
    entry: Any,
    index: int,
    families: dict[str, Any],
    orientation_catalog: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]],
) -> GodotAsset | None:
    if not isinstance(entry, dict):
        diagnostics.append(_diagnostic("invalid_entry", f"structural entry {index} must be an object"))
        return None
    family_id = str(entry.get("family") or "").strip()
    family = families.get(family_id)
    if not family_id or not isinstance(family, dict):
        diagnostics.append(_diagnostic("invalid_structural_family", f"structural entry {index} references an unknown family", str(entry.get("id") or "")))
        return None
    expanded = {"kind": family.get("kind"), **entry}
    checked = _required_entry(root, expanded, index, diagnostics, source="structural catalog")
    if checked is None:
        return None
    asset_id, resource_path, kind = checked
    bounds = _vector(entry.get("exact_aabb_size_m"), 3)
    pivot = _vector(entry.get("pivot_to_center_m"), 3)
    if bounds is None or any(value <= 0 for value in bounds):
        diagnostics.append(_diagnostic("invalid_local_bounds", "exact_aabb_size_m must contain three positive numbers", asset_id))
        return None
    if pivot is None:
        diagnostics.append(_diagnostic("invalid_calibration", "pivot_to_center_m must contain three finite numbers", asset_id))
        return None
    family_tags = family.get("tags", [])
    entry_tags = entry.get("tags", [])
    if not isinstance(family_tags, list) or not isinstance(entry_tags, list):
        diagnostics.append(_diagnostic("invalid_tags", "structural family and asset tags must be arrays", asset_id))
        return None
    tags = _parse_tags(
        [*family_tags, *entry_tags, str(entry.get("presentation") or ""), "verified_mesh_bounds"],
        asset_id,
        diagnostics,
    )
    if tags is None:
        return None
    metadata = dict(family)
    metadata["family_id"] = family_id
    for key, value in entry.items():
        if key not in {"id", "path", "family", "exact_aabb_size_m", "tags"}:
            metadata[key] = value
    roles = _semantic_roles(tags, metadata, asset_id, diagnostics)
    wall_face = entry.get("wall_placement")
    if wall_face is not None and not isinstance(wall_face, dict):
        diagnostics.append(_diagnostic("invalid_wall_face_placement", "wall_placement must be an object", asset_id))
        wall_face = {}
    elif isinstance(wall_face, dict):
        _validate_wall_face_metadata(wall_face, asset_id, diagnostics)
    profile_id = (
        "wall_face_local_negative_z_cardinal"
        if isinstance(wall_face, dict) and wall_face
        else str(family.get("orientation_profile") or "")
    )
    orientation_entry = {"kind": kind, "tags": list(tags)}
    orientation = resolve_orientation(
        orientation_entry,
        orientation_catalog,
        diagnostics,
        asset_id=asset_id,
        profile_id=profile_id,
    )
    aabb_min_y = pivot[1] - bounds[1] * 0.5
    y_offset = -aabb_min_y
    if str(family.get("vertical_alignment") or "ground") == "crown":
        y_offset = -(aabb_min_y + bounds[1])
    calibration = {
        "position_offset": [-pivot[0], y_offset, -pivot[2]],
        "pivot_to_center_m": list(pivot),
        "note": "Derived from audited scene mesh AABB and root-relative pivot.",
    }
    return GodotAsset(
        id=asset_id,
        resource_path=resource_path,
        domain="ruins",
        kind=kind,
        tags=tags,
        semantic_roles=roles,
        footprint_m=(bounds[0], bounds[2]),
        height_m=bounds[1],
        local_bounds_m=bounds,
        allowed_rotations_deg=verified_allowed_rotations(
            orientation, diagnostics, asset_id=asset_id
        ),
        sockets=(),
        weight=1.0,
        placement_mode=_placement_mode((), family, wall_face),
        source=STRUCTURAL_CATALOG_PATH.as_posix(),
        semantic_source="audited_structural_catalog_metadata",
        calibration=calibration,
        orientation=dict(orientation or {}),
        wall_face_placement=dict(wall_face or {}),
    )


def _required_entry(
    root: Path,
    entry: Any,
    index: int,
    diagnostics: list[dict[str, Any]],
    *,
    source: str,
) -> tuple[str, str, str] | None:
    if not isinstance(entry, dict):
        diagnostics.append(_diagnostic("invalid_entry", f"{source} entry {index} must be an object"))
        return None
    asset_id = str(entry.get("id") or "").strip()
    resource_path = str(entry.get("path") or "").strip()
    kind = str(entry.get("kind") or "").strip()
    if not asset_id or not resource_path or not kind:
        diagnostics.append(_diagnostic("missing_required_field", f"{source} entry {index} requires id, path, and kind", asset_id))
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
    return asset_id, resource_path, kind


def _semantic_roles(
    tags: tuple[str, ...],
    metadata: dict[str, Any],
    asset_id: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, ...]:
    roles = {_ROLE_TAGS[tag] for tag in tags if tag in _ROLE_TAGS}
    for key in ("semantic_role", "role", "selection_role", "circulation_role"):
        value = str(metadata.get(key) or "").strip().lower()
        if value:
            roles.add(value)
    if not roles:
        diagnostics.append(_diagnostic("ambiguous_semantics", "asset has no recognized generic semantic role; project metadata should enrich it", asset_id))
    return tuple(sorted(roles))


def _placement_mode(
    sockets: tuple[GodotAssetSocket, ...],
    metadata: dict[str, Any],
    wall_face: Any,
) -> str:
    if isinstance(wall_face, dict) and wall_face:
        return "wall_face"
    if sockets:
        return "socket"
    alignment = str(metadata.get("vertical_alignment") or "ground")
    return "crown" if alignment == "crown" else "ground"


def _validate_wall_face_metadata(
    metadata: dict[str, Any],
    asset_id: str,
    diagnostics: list[dict[str, Any]],
) -> None:
    required = {
        "attachment_mode",
        "backing_wall_remains",
        "cardinal_yaw_degrees",
        "compatible_bay_widths_m",
        "compatible_storey_heights_m",
        "compatible_wall_depths_m",
        "compatible_wall_faces",
        "embed_depth_m",
        "horizontal_alignment",
        "intended_visible_local_face",
        "valid_cardinal_orientations",
        "vertical_alignment",
        "visible_projection_m",
    }
    missing = sorted(required - metadata.keys())
    if missing:
        diagnostics.append(
            _diagnostic(
                "invalid_wall_face_placement",
                f"wall placement metadata lacks {missing[0]}",
                asset_id,
            )
        )


def _parse_tags(
    raw_tags: Any,
    asset_id: str,
    diagnostics: list[dict[str, Any]],
) -> tuple[str, ...] | None:
    if not isinstance(raw_tags, list) or any(
        not isinstance(tag, str) or not tag.strip() for tag in raw_tags
    ):
        diagnostics.append(_diagnostic("invalid_tags", "tags must be non-empty strings", asset_id))
        return None
    return tuple(dict.fromkeys(tag.strip().lower() for tag in raw_tags))


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


def _vector(value: Any, size: int) -> tuple[float, ...] | None:
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
