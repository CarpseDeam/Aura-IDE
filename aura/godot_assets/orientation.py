"""Resolve verified yaw policies from a project's orientation catalog."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

ORIENTATION_CATALOG_PATH = Path("assets/ruins/catalog/placeable_asset_orientations.json")

_REQUIRED_PROFILE_FIELDS = {
    "valid_yaw_degrees",
    "yaw_mode",
    "continuous_yaw_range_degrees",
    "free_yaw_safe",
    "natural_forward_local",
    "attachment_facing_local",
    "natural_run_local",
    "natural_corner_normals_local",
    "orientation_source",
    "pitch_fixed",
    "roll_fixed",
    "mirroring_allowed",
    "axis_stretching_allowed",
    "scale_policy",
}
_ORIENTATION_SOURCES = {
    "corner",
    "free_standing",
    "room_axis",
    "route_direction",
    "supporting_surface",
    "wall_crown",
    "wall_face",
    "wall_run",
}


def load_orientation_catalog(
    project_root: Path, diagnostics: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Load and validate the existing V_Ruins orientation metadata root."""
    path = project_root / ORIENTATION_CATALOG_PATH
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        diagnostics.append(_diagnostic("invalid_orientation_catalog", f"could not read orientation catalog: {exc}"))
        return None
    if not isinstance(parsed, dict) or parsed.get("schema_version") != 1:
        diagnostics.append(_diagnostic("invalid_orientation_catalog", "orientation catalog must use schema version 1"))
        return None
    for field in ("profiles", "kind_profiles", "tag_overrides", "unknown_profile"):
        if field not in parsed or not isinstance(parsed[field], dict if field != "unknown_profile" else str):
            diagnostics.append(_diagnostic("invalid_orientation_catalog", f"orientation catalog lacks valid {field}"))
            return None
    return parsed


def resolve_orientation(
    entry: dict[str, Any],
    catalog: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]],
    *,
    asset_id: str,
    profile_id: str = "",
) -> dict[str, Any] | None:
    """Return one validated orientation profile, matching the Godot policy format."""
    if catalog is None:
        diagnostics.append(
            _diagnostic(
                "incomplete_orientation_metadata",
                "orientation metadata is unavailable; direct yaw is restricted to the authored orientation",
                asset_id,
            )
        )
        return None

    explicit = entry.get("orientation")
    architecture = entry.get("architecture")
    if not isinstance(explicit, dict) and isinstance(architecture, dict):
        explicit = architecture.get("orientation")
    if isinstance(explicit, dict):
        orientation = dict(explicit)
        resolved_profile = str(orientation.get("profile_id") or "explicit")
    else:
        resolved_profile = profile_id.strip()
        if not resolved_profile:
            kind = str(entry.get("kind") or "")
            resolved_profile = str(
                catalog["kind_profiles"].get(kind, catalog["unknown_profile"])
            )
        raw_profile = catalog["profiles"].get(resolved_profile)
        if not isinstance(raw_profile, dict):
            diagnostics.append(
                _diagnostic(
                    "invalid_orientation_metadata",
                    f"orientation profile does not exist: {resolved_profile}",
                    asset_id,
                )
            )
            return None
        orientation = dict(raw_profile)
        for raw_tag in entry.get("tags", []):
            override = catalog["tag_overrides"].get(str(raw_tag))
            if isinstance(override, dict):
                orientation.update(override)
            elif override is not None:
                diagnostics.append(
                    _diagnostic(
                        "invalid_orientation_metadata",
                        f"orientation tag override must be an object: {raw_tag}",
                        asset_id,
                    )
                )
                return None
    orientation["profile_id"] = resolved_profile
    error = _validate_orientation(orientation)
    if error:
        diagnostics.append(_diagnostic("invalid_orientation_metadata", error, asset_id))
        return None
    return orientation


def verified_allowed_rotations(
    orientation: dict[str, Any] | None,
    diagnostics: list[dict[str, Any]],
    *,
    asset_id: str,
) -> tuple[float, ...]:
    """Derive the finite yaw candidates accepted by the generic preview editor."""
    if orientation is None:
        return (0.0,)
    if orientation.get("yaw_mode") == "free":
        diagnostics.append(
            _diagnostic(
                "incomplete_orientation_metadata",
                "continuous free yaw is not represented by the generic editor; direct yaw is restricted to verified candidates",
                asset_id,
            )
        )
    return tuple(float(value) for value in orientation["valid_yaw_degrees"])


def _validate_orientation(metadata: dict[str, Any]) -> str:
    missing = sorted(_REQUIRED_PROFILE_FIELDS - metadata.keys())
    if missing:
        return f"orientation metadata lacks {missing[0]}"
    yaw_mode = metadata.get("yaw_mode")
    if yaw_mode not in {"cardinal", "fixed", "free"}:
        return "orientation yaw_mode is invalid"
    if metadata.get("orientation_source") not in _ORIENTATION_SOURCES:
        return "orientation source is invalid"
    yaws = metadata.get("valid_yaw_degrees")
    if not isinstance(yaws, list) or not yaws:
        return "orientation requires verified yaw candidates"
    if any(not _finite_number(value) for value in yaws):
        return "orientation yaw candidates must be finite numbers"
    if not metadata.get("pitch_fixed") or not metadata.get("roll_fixed"):
        return "unverified pitch or roll is forbidden"
    if metadata.get("mirroring_allowed"):
        return "negative-scale mirroring is not verified"
    if metadata.get("scale_policy") not in {"authored", "verified_axis_stretch"}:
        return "orientation scale policy is invalid"
    for field in (
        "natural_forward_local",
        "attachment_facing_local",
        "natural_run_local",
    ):
        value = metadata.get(field)
        if value is not None and not _valid_direction(value):
            return f"orientation {field} must be a non-zero finite vector or null"
    corner_normals = metadata.get("natural_corner_normals_local")
    if not isinstance(corner_normals, list) or any(
        not _valid_direction(value) for value in corner_normals
    ):
        return "orientation corner normals must be finite direction vectors"
    continuous = metadata.get("continuous_yaw_range_degrees")
    if yaw_mode == "free":
        if (
            metadata.get("free_yaw_safe") is not True
            or not isinstance(continuous, list)
            or len(continuous) != 2
            or not all(_finite_number(value) for value in continuous)
            or not math.isclose(float(continuous[0]), 0.0, abs_tol=1e-6)
            or not math.isclose(float(continuous[1]), 360.0, abs_tol=1e-6)
        ):
            return "free yaw requires an explicitly verified continuous range"
    elif metadata.get("free_yaw_safe") or continuous is not None:
        return "cardinal or fixed yaw cannot claim a continuous free-yaw range"
    return ""


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _valid_direction(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) == 3
        and all(_finite_number(component) for component in value)
        and math.sqrt(sum(float(component) ** 2 for component in value)) > 1e-6
    )


def _diagnostic(code: str, message: str, asset_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"severity": "warning", "code": code, "message": message}
    if asset_id:
        result["asset_id"] = asset_id
    return result


__all__ = [
    "ORIENTATION_CATALOG_PATH",
    "load_orientation_catalog",
    "resolve_orientation",
    "verified_allowed_rotations",
]
