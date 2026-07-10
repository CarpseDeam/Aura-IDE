"""Deterministic structural validation for Aura Godot preview scenes.

All checks are read-only and produce a stable StructuredValidation result
dict.  No scenes, catalog files, or editor state are mutated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aura.godot_assets.models import GodotAsset  # noqa: TC002

# ── Configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ValidationConfig:
    """Centralised thresholds for all deterministic structural checks."""

    footprint_overlap_threshold_m: float = 0.05
    ground_elevation_threshold_m: float = 0.05
    burial_threshold_m: float = 0.1
    socket_distance_threshold_m: float = 0.1
    socket_facing_dot_threshold: float = 0.85
    max_piece_count_warning: int = 64
    socket_candidate_radius_m: float = 12.0


# ── Public entry point ──────────────────────────────────────────────────────


def struct_validate_preview(
    project_root: Path,
    snapshot: dict[str, Any],
    config: ValidationConfig | None = None,
) -> dict[str, Any]:
    """Run all deterministic structural checks and return a structured result.

    Expects *snapshot*["instances"] to already be enriched with catalog
    metadata (asset_id, domain, kind, semantic_roles).  These are populated
    by ``analyze_preview_snapshot`` before delegating here.
    """
    if config is None:
        config = ValidationConfig()

    assets = _assets_by_resource(project_root.resolve())
    instances: list[dict[str, Any]] = list(snapshot.get("instances") or [])

    facts: list[dict[str, Any]] = []
    unavailable: list[str] = []
    not_applicable: list[str] = []

    # Pre-compute footprint bounds for every enriched instance.
    footprint_map: dict[str, tuple[float, float, float, float] | None] = {}
    for inst in instances:
        path = inst.get("path", "")
        asset = _lookup_asset(assets, inst)
        if asset is None:
            footprint_map[path] = None
        else:
            footprint_map[path] = _compute_footprint_bounds(inst, asset)

    # Run each check.
    _check_footprint_overlap(instances, footprint_map, config, facts, unavailable)
    _check_grounding(instances, assets, config, facts, unavailable)
    _check_socket_alignment(instances, assets, config, facts, unavailable, not_applicable)
    _check_duplicate_identity(instances, facts)
    _check_piece_count(instances, config, facts)
    _check_total_footprint_bounds(instances, footprint_map, facts, unavailable)
    _check_malformed_nodes(snapshot, facts)

    # Pair-requiring checks are not applicable with fewer than 2 instances.
    if len(instances) < 2:
        not_applicable.append("footprint_overlap")

    # ── Aggregate status ────────────────────────────────────────────────
    evaluated_checks = sorted({
        f["code"]
        for f in facts
        if f.get("code") and f.get("severity") != "info"
    } | {
        "footprint_overlap",
        "grounding",
        "socket_alignment",
        "duplicate_identity",
        "piece_count",
        "total_footprint_bounds",
        "malformed_nodes",
    } - set(unavailable) - set(not_applicable))

    has_error = any(f.get("severity") == "error" for f in facts)
    has_warning = any(f.get("severity") == "warning" for f in facts)
    unavailable_did_block = any(
        c in unavailable for c in ("footprint_overlap", "socket_alignment", "grounding")
    )

    if has_error:
        status = "failed"
    elif has_warning or unavailable_did_block:
        status = "partial"
    else:
        status = "passed"

    error_count = sum(1 for f in facts if f.get("severity") == "error")
    warning_count = sum(1 for f in facts if f.get("severity") == "warning")
    info_count = sum(1 for f in facts if f.get("severity") == "info")

    result: dict[str, Any] = {
        "status": status,
        "evaluated_checks": evaluated_checks,
        "unavailable_checks": sorted(unavailable),
        "not_applicable_checks": sorted(not_applicable),
        "facts": facts,
        "summary": {
            "error_count": error_count,
            "warning_count": warning_count,
            "info_count": info_count,
        },
    }
    return result


# ── Individual check implementations ────────────────────────────────────────


def _check_footprint_overlap(
    instances: list[dict[str, Any]],
    footprint_map: dict[str, tuple[float, float, float, float] | None],
    config: ValidationConfig,
    facts: list[dict[str, Any]],
    unavailable: list[str],
) -> None:
    has_footprint = any(v is not None for v in footprint_map.values())
    if not has_footprint:
        unavailable.append("footprint_overlap")
        return

    indexed: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for inst in instances:
        path = inst.get("path", "")
        b = footprint_map.get(path)
        if b is not None:
            indexed.append((inst, b))

    for i, (left, lb) in enumerate(indexed):
        for right, rb in indexed[i + 1 :]:
            ox = min(lb[1], rb[1]) - max(lb[0], rb[0])
            oz = min(lb[3], rb[3]) - max(lb[2], rb[2])
            if ox > config.footprint_overlap_threshold_m and oz > config.footprint_overlap_threshold_m:
                facts.append({
                    "code": "footprint_overlap",
                    "severity": "warning",
                    "message": "Catalog-footprint approximation; inspect visually before revising.",
                    "paths": [left.get("path", ""), right.get("path", "")],
                    "measured": {"overlap_x_m": round(ox, 3), "overlap_z_m": round(oz, 3)},
                })


def _check_grounding(
    instances: list[dict[str, Any]],
    assets: dict[str, GodotAsset],
    config: ValidationConfig,
    facts: list[dict[str, Any]],
    unavailable: list[str],
) -> None:
    has_bounds = False
    has_ground_instances = False
    has_ground_reference_y = False

    for inst in instances:
        asset = _lookup_asset(assets, inst)
        if asset is None:
            continue
        if asset.local_bounds_m is not None:
            has_bounds = True
        if asset.placement_mode == "ground":
            has_ground_instances = True
            if "ground_reference_y" in asset.calibration:
                has_ground_reference_y = True

    if not has_bounds:
        unavailable.append("grounding")

    if has_ground_instances and not has_ground_reference_y:
        unavailable.append("burial")

    for inst in instances:
        asset = _lookup_asset(assets, inst)
        if asset is None:
            continue
        pos = inst.get("position") or [0.0, 0.0, 0.0]
        y = float(pos[1]) if len(pos) == 3 else 0.0

        # Elevated (positive Y only)
        if asset.placement_mode == "ground" and y > config.ground_elevation_threshold_m:
            facts.append({
                "code": "ground_asset_elevated",
                "severity": "info",
                "message": f"Ground-placed asset has Y elevation {y:.3f}m.",
                "paths": [inst.get("path", "")],
                "measured": {"elevation_m": round(y, 3)},
            })

        # Buried (only when ground_reference_y calibration metadata is available)
        if (
            asset.placement_mode == "ground"
            and "ground_reference_y" in asset.calibration
        ):
            ground_y = float(asset.calibration["ground_reference_y"])
            bottom = y - ground_y
            if bottom < -config.burial_threshold_m:
                facts.append({
                    "code": "ground_asset_buried",
                    "severity": "warning",
                    "message": f"Asset is buried {abs(bottom):.3f}m below grade.",
                    "paths": [inst.get("path", "")],
                    "measured": {"burial_depth_m": round(abs(bottom), 3)},
                })


def _check_socket_alignment(
    instances: list[dict[str, Any]],
    assets: dict[str, GodotAsset],
    config: ValidationConfig,
    facts: list[dict[str, Any]],
    unavailable: list[str],
    not_applicable: list[str],
) -> None:
    # Collect instances that have sockets.
    socketed: list[tuple[dict[str, Any], GodotAsset]] = []
    for inst in instances:
        asset = _lookup_asset(assets, inst)
        if asset is not None and asset.sockets:
            socketed.append((inst, asset))

    if len(socketed) < 2:
        not_applicable.append("socket_alignment")
        return

    # Pre-compute world transforms for every socket.
    world_sockets: list[tuple[dict[str, Any], GodotAsset, list[dict[str, Any]]]] = []
    for inst, asset in socketed:
        transformed = []
        for s in asset.sockets:
            wp = _world_socket_position(inst, s.position)
            wf = _world_socket_facing(inst, s.facing)
            transformed.append({"id": s.id, "position": wp, "facing": wf, "socket": s})
        world_sockets.append((inst, asset, transformed))

    for i in range(len(world_sockets)):
        left_inst, left_asset, left_socks = world_sockets[i]
        left_pos = left_inst.get("position") or [0.0, 0.0, 0.0]
        for j in range(i + 1, len(world_sockets)):
            right_inst, right_asset, right_socks = world_sockets[j]
            right_pos = right_inst.get("position") or [0.0, 0.0, 0.0]

            # Candidate-radius gate: skip pairs whose instance centers
            # are farther apart than socket_candidate_radius_m.
            dx = float(left_pos[0]) - float(right_pos[0])
            dy = float(left_pos[1]) - float(right_pos[1])
            dz = float(left_pos[2]) - float(right_pos[2])
            inst_dist = math.sqrt(dx * dx + dy * dy + dz * dz)
            if inst_dist > config.socket_candidate_radius_m:
                continue

            # Emit at most one fact per instance pair.
            emitted = False

            # Find the minimum-distance socket pair.
            compatible = False
            min_dist = float("inf")
            min_sock_pair = (None, None)

            for ls in left_socks:
                if emitted:
                    break
                for rs in right_socks:
                    sdx = ls["position"][0] - rs["position"][0]
                    sdy = ls["position"][1] - rs["position"][1]
                    sdz = ls["position"][2] - rs["position"][2]
                    dist = math.sqrt(sdx * sdx + sdy * sdy + sdz * sdz)

                    if dist < min_dist:
                        min_dist = dist
                        min_sock_pair = (ls["id"], rs["id"])

                    if dist < config.socket_distance_threshold_m:
                        dot = (
                            ls["facing"][0] * rs["facing"][0]
                            + ls["facing"][1] * rs["facing"][1]
                            + ls["facing"][2] * rs["facing"][2]
                        )
                        if dot < -config.socket_facing_dot_threshold:
                            compatible = True
                            emitted = True
                            break
                        else:
                            facts.append({
                                "code": "socket_misaligned_facing",
                                "severity": "warning",
                                "message": (
                                    f"Sockets close but facings are misaligned "
                                    f"(dot={dot:.3f})."
                                ),
                                "paths": [
                                    left_inst.get("path", ""),
                                    right_inst.get("path", ""),
                                ],
                                "measured": {
                                    "distance_m": round(dist, 3),
                                    "facing_dot": round(dot, 3),
                                },
                            })
                            emitted = True
                            break
                if emitted:
                    break

            if compatible:
                continue

            if not emitted and min_sock_pair[0] is not None:
                facts.append({
                    "code": "socket_distance_exceeded",
                    "severity": "warning",
                    "message": (
                        f"Socket pair ({min_sock_pair[0]}, {min_sock_pair[1]}) "
                        f"distance {min_dist:.3f}m exceeds threshold."
                    ),
                    "paths": [
                        left_inst.get("path", ""),
                        right_inst.get("path", ""),
                    ],
                    "measured": {"distance_m": round(min_dist, 3)},
                })


def _check_duplicate_identity(
    instances: list[dict[str, Any]],
    facts: list[dict[str, Any]],
) -> None:
    seen: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        rp = str(inst.get("resource_path") or "")
        pos = inst.get("position") or [0.0, 0.0, 0.0]
        key = f"{rp}::{_pos_key(pos)}"
        seen.setdefault(key, []).append(inst)

    for key, group in seen.items():
        if len(group) > 1:
            facts.append({
                "code": "duplicate_preview_instance",
                "severity": "warning",
                "message": f"Duplicate instance ({len(group)}×) at same resource and position.",
                "paths": [g.get("path", "") for g in group],
                "measured": {"count": len(group)},
            })


def _check_piece_count(
    instances: list[dict[str, Any]],
    config: ValidationConfig,
    facts: list[dict[str, Any]],
) -> None:
    count = len(instances)
    if count > config.max_piece_count_warning:
        facts.append({
            "code": "piece_count_exceeded",
            "severity": "warning",
            "message": f"Preview contains {count} instances (max recommended: {config.max_piece_count_warning}).",
            "paths": [],
            "measured": {"count": count, "max_warning": config.max_piece_count_warning},
        })


def _check_total_footprint_bounds(
    instances: list[dict[str, Any]],
    footprint_map: dict[str, tuple[float, float, float, float] | None],
    facts: list[dict[str, Any]],
    unavailable: list[str],
) -> None:
    bounds_list = [v for v in footprint_map.values() if v is not None]
    if not bounds_list:
        unavailable.append("total_footprint_bounds")
        return

    min_x = min(b[0] for b in bounds_list)
    max_x = max(b[1] for b in bounds_list)
    min_z = min(b[2] for b in bounds_list)
    max_z = max(b[3] for b in bounds_list)
    facts.append({
        "code": "footprint_bounds",
        "severity": "info",
        "message": "Aggregate axis-aligned footprint bounds.",
        "paths": [],
        "measured": {
            "min_x": round(min_x, 3),
            "max_x": round(max_x, 3),
            "min_z": round(min_z, 3),
            "max_z": round(max_z, 3),
            "width_m": round(max_x - min_x, 3),
            "depth_m": round(max_z - min_z, 3),
        },
    })


def _check_malformed_nodes(
    snapshot: dict[str, Any],
    facts: list[dict[str, Any]],
) -> None:
    for diag in snapshot.get("diagnostics") or []:
        code = diag.get("code", "")
        if code in ("non_3d_preview_child", "non_scene_preview_child"):
            facts.append({
                "code": "malformed_preview_node",
                "severity": "info",
                "message": diag.get("message", code),
                "paths": [diag.get("path", "")] if diag.get("path") else [],
                "measured": {},
            })


# ── Helpers ─────────────────────────────────────────────────────────────────


def _assets_by_resource(project_root: Path) -> dict[str, GodotAsset]:
    """Build a casefolded resource-path → GodotAsset map from all sources."""
    # Avoid circular import: BUILTIN_ASSET_SOURCES lives in adapters.
    # We import late to keep validation.py free of bridge/conversation deps.
    from aura.godot_assets.adapters import BUILTIN_ASSET_SOURCES  # noqa: PLC0415

    result: dict[str, GodotAsset] = {}
    for source in BUILTIN_ASSET_SOURCES:
        if source.is_available(project_root):
            for asset in source.load(project_root).assets:
                result.setdefault(asset.resource_path.casefold(), asset)
    return result


def _lookup_asset(
    assets: dict[str, GodotAsset], inst: dict[str, Any]
) -> GodotAsset | None:
    rp = str(inst.get("resource_path") or "").casefold()
    return assets.get(rp)


def _compute_footprint_bounds(
    item: dict[str, Any], asset: GodotAsset
) -> tuple[float, float, float, float] | None:
    """Axis-aligned 2D footprint (min_x, max_x, min_z, max_z) from item + asset."""
    if asset.footprint_m is None:
        return None
    position = item.get("position") or []
    rotation = item.get("rotation_degrees") or []
    scale = item.get("scale") or []
    if len(position) != 3 or len(rotation) != 3 or len(scale) != 3:
        return None
    width = asset.footprint_m[0] * abs(float(scale[0]))
    depth = asset.footprint_m[1] * abs(float(scale[2]))
    radians = math.radians(float(rotation[1]))
    projected_width = abs(math.cos(radians)) * width + abs(math.sin(radians)) * depth
    projected_depth = abs(math.sin(radians)) * width + abs(math.cos(radians)) * depth
    x, z = float(position[0]), float(position[2])
    return (
        x - projected_width / 2.0,
        x + projected_width / 2.0,
        z - projected_depth / 2.0,
        z + projected_depth / 2.0,
    )


def _world_socket_position(
    item: dict[str, Any], local_pos: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Transform a socket's local position into world coordinates."""
    position = item.get("position") or [0.0, 0.0, 0.0]
    scale = item.get("scale") or [1.0, 1.0, 1.0]
    rx = float(position[0])
    ry = float(position[1])
    rz = float(position[2])
    sx = float(scale[0]) if len(scale) >= 1 else 1.0
    sy = float(scale[1]) if len(scale) >= 2 else 1.0
    sz = float(scale[2]) if len(scale) >= 3 else 1.0
    # Scale local position.
    lx = float(local_pos[0]) * sx
    ly = float(local_pos[1]) * sy
    lz = float(local_pos[2]) * sz
    # Rotate around Y.
    rotation = item.get("rotation_degrees") or [0.0, 0.0, 0.0]
    angle = math.radians(float(rotation[1]) if len(rotation) >= 2 else 0.0)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    wx = lx * cos_a - lz * sin_a
    wz = lx * sin_a + lz * cos_a
    return (wx + rx, ly + ry, wz + rz)


def _world_socket_facing(
    item: dict[str, Any], local_facing: tuple[float, float, float]
) -> tuple[float, float, float]:
    """Rotate a socket's local facing vector into world space (Y-rotation only)."""
    rotation = item.get("rotation_degrees") or [0.0, 0.0, 0.0]
    angle = math.radians(float(rotation[1]) if len(rotation) >= 2 else 0.0)
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    fx = float(local_facing[0]) * cos_a - float(local_facing[2]) * sin_a
    fz = float(local_facing[0]) * sin_a + float(local_facing[2]) * cos_a
    fy = float(local_facing[1])
    # Normalise.
    length = math.sqrt(fx * fx + fy * fy + fz * fz)
    if length < 1e-9:
        return (0.0, 0.0, 0.0)
    return (fx / length, fy / length, fz / length)


def _pos_key(pos: list[float]) -> str:
    """Rounded position string used for duplicate detection (1 cm precision)."""
    x = round(float(pos[0]) if len(pos) >= 1 else 0.0, 2)
    y = round(float(pos[1]) if len(pos) >= 2 else 0.0, 2)
    z = round(float(pos[2]) if len(pos) >= 3 else 0.0, 2)
    return f"{x},{y},{z}"


__all__ = [
    "ValidationConfig",
    "struct_validate_preview",
]
