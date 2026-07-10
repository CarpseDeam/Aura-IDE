from __future__ import annotations

import json
from pathlib import Path

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_assets import inspect_godot_assets


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
