from __future__ import annotations

from dataclasses import replace

import pytest

from aura.godot_assets.alignment import CalibratedGeometry, validate_anchor
from aura.godot_assets.models import GodotAsset


def _asset(**changes) -> GodotAsset:
    asset = GodotAsset(
        id="wall", resource_path="res://wall.tscn", domain="ruins", kind="wall",
        tags=(), semantic_roles=(), footprint_m=(4.0, 2.0), height_m=6.0,
        local_bounds_m=(4.0, 6.0, 2.0), allowed_rotations_deg=(0.0, 90.0, 180.0, 270.0),
        sockets=(), weight=1.0, placement_mode="ground", source="fixture",
        semantic_source="fixture", calibration={"pivot_to_center_m": [-1.0, 2.0, 0.5]},
    )
    return replace(asset, **changes)


@pytest.mark.parametrize("anchor", [(-1, 0, 1), [0, 0, 0], [1, -1, 0]])
def test_anchor_validation_accepts_plane_selectors(anchor) -> None:
    assert validate_anchor(anchor, "anchor") == tuple(anchor)


@pytest.mark.parametrize("anchor", [[], [0, 0], [0, 0, 0, 0], [2, 0, 0], [0.0, 0, 0], [True, 0, 0]])
def test_anchor_validation_rejects_every_other_shape(anchor) -> None:
    with pytest.raises(ValueError, match="-1, 0, or 1|exactly three"):
        validate_anchor(anchor, "anchor")


def test_asymmetric_pivot_calibration_selects_min_center_and_max() -> None:
    geometry = CalibratedGeometry.from_asset(_asset(), role="target")
    assert geometry.local_anchor((-1, -1, -1)) == pytest.approx((-3.0, -1.0, -0.5))
    assert geometry.local_anchor((0, 0, 0)) == pytest.approx((-1.0, 2.0, 0.5))
    assert geometry.local_anchor((1, 1, 1)) == pytest.approx((1.0, 5.0, 1.5))


@pytest.mark.parametrize(
    ("changes", "missing"),
    [
        ({"local_bounds_m": None}, "local_bounds_m"),
        ({"calibration": {}}, "pivot_to_center_m"),
    ],
)
def test_alignment_requires_audited_bounds_and_pivot(changes, missing: str) -> None:
    with pytest.raises(ValueError, match=missing):
        CalibratedGeometry.from_asset(_asset(**changes), role="reference")
