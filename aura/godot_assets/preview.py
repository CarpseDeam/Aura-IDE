"""Semantic enrichment and conservative structural checks for Aura preview scenes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.godot_assets.adapters import BUILTIN_ASSET_SOURCES
from aura.godot_assets.models import GodotAsset
from aura.godot_assets.validation import struct_validate_preview


def analyze_preview_snapshot(project_root: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Attach catalog semantics and flag obvious footprint collisions."""
    assets = _assets_by_resource(project_root.resolve())
    diagnostics = list(snapshot.get("diagnostics") or [])
    enriched: list[dict[str, Any]] = []
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
        enriched.append(item)

    # Build a temporary snapshot with enriched instances for validation.
    enriched_snapshot = dict(snapshot)
    enriched_snapshot["instances"] = enriched
    enriched_snapshot["diagnostics"] = diagnostics

    validation_result = struct_validate_preview(project_root, enriched_snapshot)

    # Flatten structured facts into the flat diagnostics list for backward
    # compatibility, then append any unrecognized-preview diagnostics.
    for fact in validation_result.get("facts", []):
        flat: dict[str, Any] = {
            "severity": fact.get("severity", "info"),
            "code": fact.get("code", ""),
        }
        paths = fact.get("paths", [])
        if paths:
            flat["paths"] = list(paths)
        if len(paths) == 1:
            flat["path"] = str(paths[0])
        # Preserve overlap_m for backward compatibility with existing tests.
        if fact["code"] == "footprint_overlap":
            m = fact.get("measured", {})
            ox = m.get("overlap_x_m", 0)
            oz = m.get("overlap_z_m", 0)
            flat["overlap_m"] = [ox, oz]
            flat["note"] = fact.get("message", "")
        diagnostics.append(flat)

    result = dict(snapshot)
    result["instances"] = enriched
    result["diagnostics"] = diagnostics
    result["diagnostic_count"] = len(diagnostics)
    result["structural_validation"] = validation_result
    return result


def _assets_by_resource(project_root: Path) -> dict[str, GodotAsset]:
    result: dict[str, GodotAsset] = {}
    for source in BUILTIN_ASSET_SOURCES:
        if source.is_available(project_root):
            for asset in source.load(project_root).assets:
                result.setdefault(asset.resource_path.casefold(), asset)
    return result


__all__ = ["analyze_preview_snapshot"]
