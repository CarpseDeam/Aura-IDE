from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from aura.conversation.tools.godot_preview_alignment import PreviewRevisionPreparer
from aura.godot_assets.models import GodotAsset


def _asset(asset_id: str = "wall") -> GodotAsset:
    return GodotAsset(
        id=asset_id, resource_path=f"res://{asset_id}.tscn", domain="ruins", kind="wall",
        tags=(), semantic_roles=(), footprint_m=(4.0, 1.0), height_m=4.0,
        local_bounds_m=(4.0, 4.0, 1.0), allowed_rotations_deg=(0.0, 90.0, 180.0, 270.0),
        sockets=(), weight=1.0, placement_mode="ground", source="fixture",
        semantic_source="fixture", calibration={"pivot_to_center_m": [0.5, 2.0, 0.0]},
    )


def _prepare(monkeypatch, operations: list[dict]) -> list[dict]:
    assets = {"wall": _asset()}
    monkeypatch.setattr(
        "aura.conversation.tools.godot_preview_alignment.resolve_godot_asset",
        lambda _root, asset_id, domain="": assets[asset_id],
    )
    client = Mock()
    client.return_value.request.return_value = {"instances": []}
    return PreviewRevisionPreparer(Path("."), client).prepare({"operations": operations})


def _relative(path: str) -> dict:
    return {
        "node_path": path, "reference_anchor": [1, -1, 0],
        "piece_anchor": [-1, -1, 0], "offset": [0.1, 0, 0],
        "offset_space": "reference_local",
    }


def test_same_batch_instantiate_can_reference_earlier_planned_piece(monkeypatch) -> None:
    prepared = _prepare(monkeypatch, [
        {"operation": "instantiate", "asset_id": "wall", "name": "First", "position": [0, 0, 0]},
        {"operation": "instantiate", "asset_id": "wall", "name": "Second", "relative_to": _relative("AuraPreview/First")},
    ])
    assert "position" not in prepared[1]
    assert prepared[1]["_alignment_geometry"]["piece"]["pivot_to_center_m"] == [0.5, 2.0, 0.0]


def test_named_duplicate_and_attachment_outputs_are_planned_references(monkeypatch) -> None:
    prepared = _prepare(monkeypatch, [
        {"operation": "instantiate", "asset_id": "wall", "name": "First", "position": [0, 0, 0]},
        {"operation": "duplicate", "node_path": "AuraPreview/First", "count": 1, "offset": [4, 0, 0], "name": "Second"},
        {"operation": "instantiate", "asset_id": "wall", "name": "Third", "relative_to": _relative("AuraPreview/Second")},
    ])
    assert prepared[2]["relative_to"]["node_path"] == "AuraPreview/Second"


def test_calibrated_sequence_and_replacement_outputs_can_be_referenced(monkeypatch) -> None:
    prepared = _prepare(monkeypatch, [
        {"operation": "instantiate", "asset_id": "wall", "name": "First", "position": [0, 0, 0]},
        {"operation": "duplicate", "node_path": "AuraPreview/First", "count": 2, "alignment_step": {"reference_anchor": [1, -1, 0], "piece_anchor": [-1, -1, 0]}},
        {"operation": "replace", "node_path": "AuraPreview/Existing", "asset_id": "wall", "name": "Replacement", "relative_to": _relative("AuraPreview/First_copy_02")},
        {"operation": "instantiate", "asset_id": "wall", "name": "AfterReplacement", "relative_to": _relative("AuraPreview/Replacement")},
    ])
    assert prepared[1]["_output_names"] == ["First_copy_01", "First_copy_02"]
    assert prepared[3]["relative_to"]["node_path"] == "AuraPreview/Replacement"


def test_forward_reference_fails_clearly(monkeypatch) -> None:
    with pytest.raises(ValueError, match="forward references"):
        _prepare(monkeypatch, [
            {"operation": "instantiate", "asset_id": "wall", "name": "First", "relative_to": _relative("AuraPreview/Later")},
            {"operation": "instantiate", "asset_id": "wall", "name": "Later", "position": [0, 0, 0]},
        ])


def test_relative_position_and_duplicate_modes_are_mutually_exclusive(monkeypatch) -> None:
    with pytest.raises(ValueError, match="both relative_to and position"):
        _prepare(monkeypatch, [
            {"operation": "instantiate", "asset_id": "wall", "name": "First", "position": [0, 0, 0]},
            {"operation": "instantiate", "asset_id": "wall", "name": "Second", "position": [4, 0, 0], "relative_to": _relative("AuraPreview/First")},
        ])
    with pytest.raises(ValueError, match="exactly one"):
        _prepare(monkeypatch, [
            {"operation": "instantiate", "asset_id": "wall", "name": "First", "position": [0, 0, 0]},
            {"operation": "duplicate", "node_path": "AuraPreview/First", "count": 1, "offset": [4, 0, 0], "alignment_step": {"reference_anchor": [1, 0, 0], "piece_anchor": [-1, 0, 0]}},
        ])
