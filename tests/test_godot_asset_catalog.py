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
    sv = result["structural_validation"]
    assert isinstance(sv, dict)
    assert sv["status"] in ("passed", "failed", "partial")
    footprint_facts = [f for f in sv["facts"] if f["code"] == "footprint_overlap"]
    assert len(footprint_facts) >= 1
    assert "overlap_x_m" in footprint_facts[0]["measured"]
    assert "overlap_z_m" in footprint_facts[0]["measured"]


def test_atomic_preview_apply_resolves_replacement_and_addition_assets(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = {
            "applied": True,
            "operation_count": 3,
            "changed_paths": ["AuraPreview/WestWall"],
            "added_paths": ["AuraPreview/wall_02"],
            "removed_paths": [],
            "replaced_paths": ["AuraPreview/Gate"],
            "instance_count": 3,
        }
        result = registry.execute(
            "edit_godot_asset_preview",
            {
                "action": "apply",
                "operations": [
                    {
                        "operation": "set_transform",
                        "node_path": "AuraPreview/WestWall",
                        "position": [4, 0, 0],
                    },
                    {"operation": "instantiate", "asset_id": "wall", "position": [8, 0, 0]},
                    {
                        "operation": "replace",
                        "node_path": "AuraPreview/Gate",
                        "asset_id": "wall",
                    },
                ],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    action, params = client_type.return_value.request.call_args.args
    assert action == "preview.apply"
    assert params["operations"][1]["resource_path"] == "res://assets/modules/wall.tscn"
    assert params["operations"][2]["catalog_identity"] == "ruins:wall"
    assert "position" not in params["operations"][2]


def test_atomic_preview_apply_rejects_nested_or_conflicting_targets(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    nested = registry.execute(
        "edit_godot_asset_preview",
        {
            "action": "apply",
            "operations": [
                {"operation": "remove", "node_path": "AuraPreview/Building/NestedWall"}
            ],
        },
        lambda _request: ApprovalDecision("approve"),
    )
    conflicting = registry.execute(
        "edit_godot_asset_preview",
        {
            "action": "apply",
            "operations": [
                {"operation": "remove", "node_path": "AuraPreview/Wall"},
                {
                    "operation": "set_transform",
                    "node_path": "AuraPreview/Wall",
                    "position": [1, 0, 0],
                },
            ],
        },
        lambda _request: ApprovalDecision("approve"),
    )

    assert nested.ok is False
    assert "direct AuraPreview child" in nested.payload["error"]
    assert conflicting.ok is False
    assert "more than one operation" in conflicting.payload["error"]


def test_preview_schema_exposes_one_bounded_relational_duplicate_operation(tmp_path: Path) -> None:
    schema = next(
        tool["function"]
        for tool in ToolRegistry(tmp_path, mode="worker").tool_defs()
        if tool["function"]["name"] == "edit_godot_asset_preview"
    )
    item = schema["parameters"]["properties"]["operations"]["items"]

    assert item["properties"]["operation"]["enum"] == [
        "set_transform", "instantiate", "remove", "replace", "duplicate"
    ]
    assert item["properties"]["count"]["minimum"] == 1
    assert item["properties"]["count"]["maximum"] == 16
    assert item["properties"]["offset_space"]["enum"] == ["local", "world"]


def test_preview_duplicate_normalizes_live_catalog_source_and_relative_inputs(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    snapshot = {
        "scene_open": True,
        "preview_exists": True,
        "diagnostics": [],
        "instances": [{
            "path": "AuraPreview/WestWall",
            "resource_path": "res://assets/modules/wall.tscn",
            "position": [2, 0, 3],
            "rotation_degrees": [0, 90, 0],
            "scale": [1, 1, 1],
        }],
    }
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.side_effect = [
            snapshot,
            {
                "applied": True,
                "operation_count": 3,
                "added_paths": [
                    "AuraPreview/WestWall_copy_01",
                    "AuraPreview/WestWall_copy_02",
                    "AuraPreview/WestWall_copy_03",
                ],
                "instance_count": 4,
            },
        ]
        result = registry.execute(
            "edit_godot_asset_preview",
            {
                "action": "apply",
                "operations": [{
                    "operation": "duplicate",
                    "node_path": "AuraPreview/WestWall",
                    "count": 3,
                    "offset": [4, 0, 0],
                }],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    calls = client_type.return_value.request.call_args_list
    assert calls[0].args == ("preview.snapshot", {})
    assert calls[1].args[0] == "preview.apply"
    duplicate = calls[1].args[1]["operations"][0]
    assert duplicate == {
        "operation": "duplicate",
        "node_path": "AuraPreview/WestWall",
        "count": 3,
        "offset": [4.0, 0.0, 0.0],
        "offset_space": "local",
        "catalog_identity": "ruins:wall",
        "resource_path": "res://assets/modules/wall.tscn",
    }


def test_preview_duplicate_rejects_invalid_count_before_live_lookup(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        for count in (0, 17, 1.5, True):
            result = registry.execute(
                "edit_godot_asset_preview",
                {
                    "action": "apply",
                    "operations": [{
                        "operation": "duplicate",
                        "node_path": "AuraPreview/Wall",
                        "count": count,
                        "offset": [1, 0, 0],
                    }],
                },
                lambda _request: ApprovalDecision("approve"),
            )
            assert result.ok is False
            assert "count" in result.payload["error"]
    client_type.assert_not_called()


def test_preview_duplicate_rejects_nested_source_before_live_lookup(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        result = registry.execute(
            "edit_godot_asset_preview",
            {
                "action": "apply",
                "operations": [{
                    "operation": "duplicate",
                    "node_path": "AuraPreview/Building/Wall",
                    "count": 2,
                    "offset": [1, 0, 0],
                }],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert "direct AuraPreview child" in result.payload["error"]
    client_type.assert_not_called()


def test_preview_duplicate_rejects_missing_or_non_catalog_source_before_apply(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    registry = ToolRegistry(root, mode="worker")
    snapshots = [
        {"scene_open": True, "preview_exists": True, "diagnostics": [], "instances": []},
        {
            "scene_open": True,
            "preview_exists": True,
            "diagnostics": [],
            "instances": [{
                "path": "AuraPreview/Wall",
                "resource_path": "res://handmade/not_catalogued.tscn",
                "position": [0, 0, 0],
                "rotation_degrees": [0, 0, 0],
                "scale": [1, 1, 1],
            }],
        },
    ]
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.side_effect = snapshots
        for expected in ("does not exist", "not a recognized catalog asset"):
            result = registry.execute(
                "edit_godot_asset_preview",
                {
                    "action": "apply",
                    "operations": [{
                        "operation": "duplicate",
                        "node_path": "AuraPreview/Wall",
                        "count": 2,
                        "offset": [1, 0, 0],
                    }],
                },
                lambda _request: ApprovalDecision("approve"),
            )
            assert result.ok is False
            assert expected in result.payload["error"]

    assert [call.args[0] for call in client_type.return_value.request.call_args_list] == [
        "preview.snapshot", "preview.snapshot"
    ]


def test_godot_duplicate_preparation_is_relative_deterministic_atomic_and_unsaved() -> None:
    content = Path("aura/godot_editor/addon/actions/asset_preview_actions.gd").read_text(
        encoding="utf-8"
    )

    assert "position_step = source.transform.basis * offset" in content
    assert 'candidate := "%s_copy_%02d"' in content
    assert '"path": PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name)' in content
    assert '_undo_redo.add_do_method(self, "_execute_revision"' in content
    assert '_undo_redo.add_undo_method(self, "_execute_revision"' in content
    assert "for index in range(operations.size() - 1, -1, -1)" in content
    assert "save_scene" not in content
