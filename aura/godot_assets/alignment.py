"""Trusted calibrated geometry used by generic direct-piece alignment."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from aura.godot_assets.models import GodotAsset

Anchor = tuple[int, int, int]


@dataclass(frozen=True)
class CalibratedGeometry:
    """Audited root-relative bounds for one verified catalog asset."""

    asset_identity: str
    local_bounds_m: tuple[float, float, float]
    pivot_to_center_m: tuple[float, float, float]

    @classmethod
    def from_asset(cls, asset: GodotAsset, *, role: str) -> "CalibratedGeometry":
        identity = f"{asset.domain}:{asset.id}"
        bounds = _finite_triplet(asset.local_bounds_m)
        if bounds is None or any(component <= 0.0 for component in bounds):
            raise ValueError(
                f"{role} asset {identity} lacks exact positive local_bounds_m"
            )
        calibration = asset.calibration
        pivot = _finite_triplet(
            calibration.get("pivot_to_center_m")
            if isinstance(calibration, dict)
            else None
        )
        if pivot is None:
            raise ValueError(
                f"{role} asset {identity} lacks valid pivot_to_center_m calibration"
            )
        return cls(identity, bounds, pivot)

    def local_anchor(self, anchor: Anchor) -> tuple[float, float, float]:
        checked = validate_anchor(anchor, "anchor")
        return tuple(
            self.pivot_to_center_m[axis]
            + 0.5 * self.local_bounds_m[axis] * checked[axis]
            for axis in range(3)
        )

    def to_bridge_dict(self) -> dict[str, Any]:
        return {
            "catalog_identity": self.asset_identity,
            "local_bounds_m": list(self.local_bounds_m),
            "pivot_to_center_m": list(self.pivot_to_center_m),
        }


def validate_anchor(value: Any, label: str) -> Anchor:
    """Accept exactly three authored plane selectors: -1, 0, or 1."""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise ValueError(f"{label} must contain exactly three anchor components")
    result: list[int] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, int):
            raise ValueError(f"{label} components must be -1, 0, or 1")
        if component not in {-1, 0, 1}:
            raise ValueError(f"{label} components must be -1, 0, or 1")
        result.append(component)
    return result[0], result[1], result[2]


def _finite_triplet(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    if any(
        isinstance(component, bool)
        or not isinstance(component, (int, float))
        or not math.isfinite(float(component))
        for component in value
    ):
        return None
    return float(value[0]), float(value[1]), float(value[2])


__all__ = ["Anchor", "CalibratedGeometry", "validate_anchor"]
