"""Semantic enrichment and conservative structural checks for Aura preview scenes."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from aura.godot_assets.adapters import BUILTIN_ASSET_SOURCES
from aura.godot_assets.models import GodotAsset


def analyze_preview_snapshot(project_root: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach catalog semantics and flag obvious footprint collisions."""
    assets = _assets_by_resource(project_root.resolve())
    diagnostics = list(snapshot.get("diagnostics") or [])
    enriched: list[dict[str, Any]] = []
    footprints: list[tuple[dict[str, Any], tuple[float, float, float, float]]] = []
    for raw in snapshot.get("instances") or []:
        item = dict(raw)
        resource_path = str(item.get("resource_path") or "")
        asset = assets.get(resource_path.casefold())
        if asset is None:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "unrecognized_preview_asset",
                    "path": item.get("path", ""),
                    "resource_path": resource_path,
                }
            )
        else:
            item["asset_id"] = asset.id
            item["domain"] = asset.domain
            item["kind"] = asset.kind
            item["semantic_roles"] = list(asset.semantic_roles)
            bounds = _footprint_bounds(item, asset)
            if bounds is not None:
                footprints.append((item, bounds))
            position = item.get("position") or [0.0, 0.0, 0.0]
            if len(position) == 3 and abs(float(position[1])) > 0.05 and asset.placement_mode == "ground":
                diagnostics.append(
                    {
                        "severity": "info",
                        "code": "ground_asset_elevated",
                        "path": item.get("path", ""),
                        "height_m": float(position[1]),
                    }
                )
        enriched.append(item)

    for index, (left, left_bounds) in enumerate(footprints):
        for right, right_bounds in footprints[index + 1 :]:
            overlap_x = min(left_bounds[1], right_bounds[1]) - max(left_bounds[0], right_bounds[0])
            overlap_z = min(left_bounds[3], right_bounds[3]) - max(left_bounds[2], right_bounds[2])
            if overlap_x > 0.05 and overlap_z > 0.05:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "footprint_overlap",
                        "paths": [left.get("path", ""), right.get("path", "")],
                        "overlap_m": [round(overlap_x, 3), round(overlap_z, 3)],
                        "note": "Catalog-footprint approximation; inspect visually before revising.",
                    }
                )

    result = dict(snapshot)
    result["instances"] = enriched
    result["diagnostics"] = diagnostics
    result["diagnostic_count"] = len(diagnostics)
    result["structural_validation"] = "catalog_footprint_approximation"
    return result


def _assets_by_resource(project_root: Path) -> dict[str, GodotAsset]:
    result: dict[str, GodotAsset] = {}
    for source in BUILTIN_ASSET_SOURCES:
        if source.is_available(project_root):
            for asset in source.load(project_root).assets:
                result.setdefault(asset.resource_path.casefold(), asset)
    return result


def _footprint_bounds(
    item: dict[str, Any], asset: GodotAsset
) -> tuple[float, float, float, float] | None:
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


__all__ = ["analyze_preview_snapshot"]
