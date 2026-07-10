from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_assets import inspect_godot_assets
from aura.godot_assets.preview import analyze_preview_snapshot


def _project(tmp_path: Path, entries: list[dict]) -> tuple[Path, Path]:
    (tmp_path / "project.godot").write_text('[application]\nconfig/name="Assets"\n', encoding="utf-8")
    scene = tmp_path / "assets/modules/wall.tscn"
    scene.parent.mkdir(parents=True)
    scene.write_text('[gd_scene format=3]\n\n[node name="Wall" type="Node3D"]\n', encoding="utf-8")
    catalog = tmp_path / "assets/ruins/catalog/ruin_pieces.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    (catalog.parent / "calibrations.json").write_text(
        json.dumps({"wall": {"position_offset": [1.0, 0.0, 0.0]}}), encoding="utf-8"
    )
    return tmp_path, catalog


def _wall(asset_id: str = "wall") -> dict:
    return {
        "id": asset_id,
        "path": "res://assets/modules/wall.tscn",
        "kind": "wall_straight",
        "footprint_m": [4.0, 1.0],
        "height_m": 4.0,
        "tags": ["ruins", "wall", "cover"],
        "sockets": [
            {"id": "left", "position": [-2.0, 0.0, 0.0], "facing": [-1.0, 0.0, 0.0]},
            {"id": "right", "position": [2.0, 0.0, 0.0], "facing": [1.0, 0.0, 0.0]},
        ],
        "weight": 1.0,
    }


def test_inspection_returns_generic_asset_semantics_and_calibration(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])

    result = inspect_godot_assets(root)

    assert result["ok"] is True
    assert result["read_only"] is True
    assert result["sources"] == ["ruinlab_json"]
    assert result["catalog_asset_count"] == 1
    asset = result["assets"][0]
    assert asset["domain"] == "ruins"
    assert asset["kind"] == "wall_straight"
    assert asset["semantic_roles"] == ["barrier", "cover"]
    assert asset["placement_mode"] == "socket"
    assert asset["calibration"]["position_offset"] == [1.0, 0.0, 0.0]


def test_inspection_filters_by_generic_role_without_mutating_catalog(tmp_path: Path) -> None:
    debris = {
        **_wall("debris"),
        "kind": "rubble",
        "tags": ["ruins", "rubble", "scatter"],
        "sockets": [],
    }
    root, catalog = _project(tmp_path, [_wall(), debris])
    before = catalog.read_bytes()

    result = inspect_godot_assets(root, semantic_roles=["cover"], max_items=1)

    assert result["matched_asset_count"] == 1
    assert result["assets"][0]["id"] == "wall"
    assert catalog.read_bytes() == before


def test_invalid_and_duplicate_assets_are_excluded_with_diagnostics(tmp_path: Path) -> None:
    missing = {**_wall("missing"), "path": "res://assets/modules/missing.tscn"}
    root, _catalog = _project(tmp_path, [_wall(), _wall(), missing, {"id": "broken"}])

    result = inspect_godot_assets(root)

    assert result["catalog_asset_count"] == 1
    codes = {diagnostic["code"] for diagnostic in result["diagnostics"]}
    assert {"duplicate_id", "missing_asset", "missing_required_field"} <= codes


def test_query_is_deterministic_and_bounded(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall("wall_b"), _wall("wall_a")])

    first = inspect_godot_assets(root, query="wall", max_items=1)
    second = inspect_godot_assets(root, query="wall", max_items=1)

    assert first == second
    assert first["truncated"] is True
    assert first["matched_asset_count"] == 2
    assert [asset["id"] for asset in first["assets"]] == ["wall_a"]


def test_tool_is_read_only_and_available_to_planner(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    planner = ToolRegistry(root, mode="planner")
    names = {tool["function"]["name"] for tool in planner.tool_defs()}

    result = planner.execute(
        "inspect_godot_assets",
        {"semantic_roles": ["barrier"]},
        lambda _request: ApprovalDecision("approve"),
    )

    assert "inspect_godot_assets" in names
    assert result.ok is True
    assert result.payload["assets"][0]["id"] == "wall"


def test_preview_instancing_resolves_catalog_id_and_is_approval_gated(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = {
            "applied": True,
            "preview_root": "AuraPreview",
            "instance_paths": ["AuraPreview/WestWall"],
            "instance_count": 1,
        }
        result = registry.execute(
            "edit_godot_asset_preview",
            {
                "action": "instantiate",
                "placements": [{
                    "asset_id": "wall", "domain": "ruins", "name": "WestWall",
                    "position": [2, 0, 3], "rotation_degrees_y": 90,
                }],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    request = client_type.return_value.request.call_args.args
    assert request[0] == "preview.instantiate"
    placement = request[1]["placements"][0]
    assert placement["resource_path"] == "res://assets/modules/wall.tscn"
    assert placement["catalog_identity"] == "ruins:wall"
    assert placement["position"] == [2.0, 0.0, 3.0]


def test_preview_instancing_rejects_non_catalog_asset_before_bridge(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        result = registry.execute(
            "edit_godot_asset_preview",
            {"action": "instantiate", "placements": [{"asset_id": "not_catalogued"}]},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert "not present in a recognized catalog" in result.payload["error"]
    client_type.assert_not_called()


def test_preview_analysis_enriches_assets_and_flags_overlap(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    snapshot = {
        "scene_open": True, "preview_exists": True, "diagnostics": [],
        "instances": [
            {"path": "AuraPreview/A", "resource_path": "res://assets/modules/wall.tscn",
             "position": [0, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1]},
            {"path": "AuraPreview/B", "resource_path": "res://assets/modules/wall.tscn",
             "position": [1, 0, 0], "rotation_degrees": [0, 0, 0], "scale": [1, 1, 1]},
        ],
    }

    result = analyze_preview_snapshot(root, snapshot)

    assert result["instances"][0]["asset_id"] == "wall"
    assert result["instances"][0]["semantic_roles"] == ["barrier", "cover"]
    overlaps = [item for item in result["diagnostics"] if item["code"] == "footprint_overlap"]
    assert overlaps[0]["paths"] == ["AuraPreview/A", "AuraPreview/B"]

    # Structural-validation payload is now a dict (not the old string).
    sv = result["structural_validation"]
    assert isinstance(sv, dict)
    assert sv["status"] in ("passed", "failed", "partial")
    footprint_facts = [f for f in sv["facts"] if f["code"] == "footprint_overlap"]
    assert len(footprint_facts) >= 1
    assert "overlap_x_m" in footprint_facts[0]["measured"]
    assert "overlap_z_m" in footprint_facts[0]["measured"]
