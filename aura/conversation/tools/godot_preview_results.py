"""Bounded factual continuation summaries for direct Godot preview edits."""

from __future__ import annotations

from aura.godot_assets.adapters import BUILTIN_ASSET_SOURCES
from aura.godot_assets.models import GodotAsset

_MAX_PATHS = 256
_MAX_RECORDS = 128
_MAX_WARNINGS = 32
_MAX_SOCKETS = 32


def compact_post_apply_summary(project_root, action: str, params: dict, result: dict, analyzed: dict) -> dict:
    added = list(result.get("added_paths") or result.get("instance_paths") or [])
    changed = list(result.get("changed_paths") or [])
    replaced = list(result.get("replaced_paths") or [])
    removed = list(result.get("removed_paths") or [])
    if action == "clear" and not removed:
        removed = list(result.get("instance_paths") or [])
    current_paths = set(added) | set(changed) | set(replaced)
    instances = list(analyzed.get("instances") or [])
    by_path = {str(item.get("path") or ""): item for item in instances}
    alignment_by_path = {
        str(item.get("path") or ""): item
        for item in list(result.get("alignment_facts") or [])[:_MAX_RECORDS]
        if isinstance(item, dict)
    }
    assets = _assets_by_identity(project_root)
    records: list[dict] = []
    socket_budget = _MAX_SOCKETS
    for path in [*added, *changed, *replaced]:
        if len(records) >= _MAX_RECORDS:
            break
        raw = by_path.get(str(path))
        if raw is None:
            continue
        record = _instance_record(raw)
        alignment = alignment_by_path.get(str(path))
        if alignment:
            record["placement"] = {
                key: alignment[key]
                for key in (
                    "method", "reference_path", "reference_anchor", "piece_anchor",
                    "calculated_position", "rotation_degrees_y", "scale", "offset", "offset_space",
                )
                if key in alignment
            }
        asset = assets.get((str(raw.get("domain") or "").casefold(), str(raw.get("asset_id") or "").casefold()))
        if asset is not None and asset.sockets and socket_budget > 0:
            sockets = [socket.to_dict() for socket in asset.sockets[:socket_budget]]
            if sockets:
                record["sockets"] = sockets
                socket_budget -= len(sockets)
        records.append(record)
    warnings, overall_bounds = _placement_facts(analyzed, current_paths)
    requested = len(params.get("placements") or []) if action == "instantiate" else len(params.get("operations") or []) if action == "apply" else int(result.get("removed_count") or 0)
    return {
        "available": True,
        "operation_count": int(result.get("request_operation_count", requested)),
        "total_instance_count": int(analyzed.get("instance_count", len(instances)) or len(instances)),
        **_bounded_paths("added", added), **_bounded_paths("changed", changed),
        **_bounded_paths("replaced", replaced), **_bounded_paths("removed", removed),
        "affected_instances": records,
        "affected_instance_count": len(current_paths),
        "affected_instances_truncated": len(records) < len(current_paths),
        "overall_preview_bounds": overall_bounds,
        "placement_warnings": warnings,
    }


def _placement_facts(analyzed: dict, current_paths: set) -> tuple[list[dict], dict]:
    validation = analyzed.get("structural_validation")
    facts = validation.get("facts") if isinstance(validation, dict) else []
    warnings: list[dict] = []
    bounds: dict = {}
    for fact in facts if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        if fact.get("code") == "footprint_bounds" and isinstance(fact.get("measured"), dict):
            bounds = dict(fact["measured"])
            continue
        paths = {str(path) for path in fact.get("paths", [])}
        if len(warnings) < _MAX_WARNINGS and paths.intersection(current_paths) and fact.get("severity") in {"warning", "error", "info"}:
            warnings.append({key: fact[key] for key in ("code", "severity", "message", "paths", "measured") if key in fact})
    return warnings, bounds


def _instance_record(raw: dict) -> dict:
    rotation = raw.get("rotation_degrees") or [0.0, 0.0, 0.0]
    return {
        "path": str(raw.get("path") or ""), "asset_id": str(raw.get("asset_id") or ""),
        "domain": str(raw.get("domain") or ""), "position": list(raw.get("position") or [0.0, 0.0, 0.0]),
        "rotation_degrees_y": float(rotation[1]) if isinstance(rotation, list) and len(rotation) == 3 else 0.0,
        "scale": list(raw.get("scale") or [1.0, 1.0, 1.0]),
    }


def _bounded_paths(prefix: str, paths: list) -> dict:
    normalized = [str(path) for path in paths]
    bounded = normalized[:_MAX_PATHS]
    return {f"{prefix}_paths": bounded, f"{prefix}_count": len(normalized), f"{prefix}_paths_truncated": len(bounded) < len(normalized)}


def _assets_by_identity(project_root) -> dict[tuple[str, str], GodotAsset]:
    result = {}
    for source in BUILTIN_ASSET_SOURCES:
        if source.is_available(project_root):
            for asset in source.load(project_root).assets:
                result.setdefault((asset.domain.casefold(), asset.id.casefold()), asset)
    return result


__all__ = ["compact_post_apply_summary"]
