from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from aura.conversation.tools._godot_asset_preview_mixin import (
    _catalog_socket,
    _preview_bridge_error,
)
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_assets import inspect_godot_assets, resolve_godot_asset
from aura.godot_assets.models import GodotAssetSocket
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


def _corner(asset_id: str = "corner") -> dict:
    return {
        **_wall(asset_id),
        "path": "res://assets/modules/corner.tscn",
        "kind": "wall_corner",
        "sockets": [
            {"id": "left", "position": [0.0, 0.0, -1.0], "facing": [0.0, 0.0, -1.0]},
            {"id": "right", "position": [2.0, 0.0, 0.0], "facing": [1.0, 0.0, 0.0]},
        ],
    }


def _attachment_project(tmp_path: Path, source: dict | None = None) -> tuple[Path, Path]:
    root, catalog = _project(tmp_path, [source or _wall(), _corner()])
    (root / "assets/modules/corner.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Corner" type="Node3D"]\n', encoding="utf-8"
    )
    return root, catalog


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


def test_preview_schema_exposes_relational_duplicate_and_attach_operations(tmp_path: Path) -> None:
    schema = next(
        tool["function"]
        for tool in ToolRegistry(tmp_path, mode="worker").tool_defs()
        if tool["function"]["name"] == "edit_godot_asset_preview"
    )
    item = schema["parameters"]["properties"]["operations"]["items"]

    assert item["properties"]["operation"]["enum"] == [
        "set_transform", "instantiate", "remove", "replace", "duplicate", "attach"
    ]
    assert item["properties"]["count"]["minimum"] == 1
    assert item["properties"]["count"]["maximum"] == 16
    assert item["properties"]["offset_space"]["enum"] == ["local", "world"]
    assert {"source_socket", "target_socket", "asset_id", "domain", "name", "scale"} <= set(
        item["properties"]
    )
    assert "resource_path" not in item["properties"]
    assert "source_socket_position" not in item["properties"]
    assert "target_socket_facing" not in item["properties"]


def test_preview_apply_prepares_named_sequential_construction_without_live_snapshot(
    tmp_path: Path,
) -> None:
    terminal = {**_wall("terminal"), "path": "res://assets/modules/terminal.tscn"}
    root, _catalog = _project(tmp_path, [_wall(), _corner(), terminal])
    for scene_name in ("corner", "terminal"):
        (root / f"assets/modules/{scene_name}.tscn").write_text(
            f'[gd_scene format=3]\n\n[node name="{scene_name.title()}" type="Node3D"]\n',
            encoding="utf-8",
        )
    public_operations = [
        {
            "operation": "instantiate", "asset_id": "wall", "name": "RearWall01",
            "position": [2, 0, 3], "rotation_degrees_y": 90, "scale": [2, 1, 3],
        },
        {
            "operation": "duplicate", "node_path": "AuraPreview/RearWall01",
            "count": 1, "offset": [8, 0, 0], "name": "RearWall02",
        },
        {
            "operation": "duplicate", "node_path": "AuraPreview/RearWall02",
            "count": 1, "offset": [8, 0, 0], "name": "RearWall03",
        },
        {
            "operation": "attach", "node_path": "AuraPreview/RearWall03",
            "source_socket": "right", "asset_id": "corner", "target_socket": "left",
            "name": "RearCorner",
        },
        {
            "operation": "attach", "node_path": "AuraPreview/RearCorner",
            "source_socket": "right", "asset_id": "wall", "target_socket": "left",
            "name": "SideWall01", "scale": [1.5, 2, 0.75],
        },
        {
            "operation": "duplicate", "node_path": "AuraPreview/SideWall01",
            "count": 1, "offset": [6, 0, 0], "name": "SideWall02",
        },
        {
            "operation": "attach", "node_path": "AuraPreview/SideWall02",
            "source_socket": "right", "asset_id": "terminal", "target_socket": "left",
            "name": "Terminal",
        },
    ]
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = {
            "applied": True,
            "added_paths": [f"AuraPreview/{name}" for name in (
                "RearWall01", "RearWall02", "RearWall03", "RearCorner",
                "SideWall01", "SideWall02", "Terminal",
            )],
        }
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            {"action": "apply", "operations": public_operations},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    client_type.return_value.request.assert_called_once()
    action, params = client_type.return_value.request.call_args.args
    assert action == "preview.apply"
    prepared = params["operations"]
    assert [item.get("name") for item in prepared] == [
        "RearWall01", "RearWall02", "RearWall03", "RearCorner",
        "SideWall01", "SideWall02", "Terminal",
    ]
    assert prepared[1]["catalog_identity"] == "ruins:wall"
    assert prepared[2]["catalog_identity"] == "ruins:wall"
    assert prepared[3]["source_catalog_identity"] == "ruins:wall"
    assert prepared[4]["source_catalog_identity"] == "ruins:corner"
    assert prepared[5]["catalog_identity"] == "ruins:wall"
    assert prepared[6]["source_catalog_identity"] == "ruins:wall"
    assert prepared[3]["source_socket_position"] == [2.0, 0.0, 0.0]
    assert prepared[3]["target_socket_position"] == [0.0, 0.0, -1.0]
    assert all("resource_path" not in item for item in public_operations)
    assert all("source_socket_position" not in item for item in public_operations)


@pytest.mark.parametrize(
    ("operations", "expected"),
    [
        (
            [
                {"operation": "instantiate", "asset_id": "wall", "name": "Same"},
                {"operation": "instantiate", "asset_id": "wall", "name": "Same"},
            ],
            "duplicate new preview name",
        ),
        (
            [
                {"operation": "instantiate", "asset_id": "wall", "name": "Anchor"},
                {
                    "operation": "duplicate", "node_path": "AuraPreview/Anchor",
                    "count": 2, "offset": [4, 0, 0], "name": "Copies",
                },
            ],
            "name only when count is 1",
        ),
        (
            [
                {"operation": "instantiate", "asset_id": "wall", "name": "Anchor"},
                {"operation": "remove", "node_path": "AuraPreview/Anchor"},
            ],
            "cannot remove an unattached planned node",
        ),
    ],
)
def test_preview_apply_rejects_bad_named_plans_before_bridge(
    tmp_path: Path, operations: list[dict], expected: str
) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            {"action": "apply", "operations": operations},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert expected in result.payload["error"]
    client_type.assert_not_called()


def test_preview_apply_rejects_forward_planned_path_before_apply(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = {
            "scene_open": True, "preview_exists": True, "instances": [], "diagnostics": [],
        }
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            {
                "action": "apply",
                "operations": [
                    {
                        "operation": "duplicate", "node_path": "AuraPreview/Later",
                        "count": 1, "offset": [4, 0, 0], "name": "TooEarly",
                    },
                    {"operation": "instantiate", "asset_id": "wall", "name": "Later"},
                ],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert "source does not exist" in result.payload["error"]
    client_type.return_value.request.assert_called_once_with("preview.snapshot", {})


def test_old_preview_plugin_attach_error_requests_reinstall_and_reload() -> None:
    message = _preview_bridge_error(ValueError("unsupported revision operation: attach"))

    assert "install_godot_editor_bridge" in message
    assert "disable and re-enable Aura Editor Bridge" in message


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


def _attach_args(**overrides) -> dict:
    operation = {
        "operation": "attach",
        "node_path": "AuraPreview/WestWall",
        "source_socket": "right",
        "asset_id": "corner",
        "target_socket": "left",
    }
    operation.update(overrides)
    return {"action": "apply", "operations": [operation]}


def _wall_snapshot(*, resource_path: str = "res://assets/modules/wall.tscn", **extra) -> dict:
    instance = {"path": "AuraPreview/WestWall", "resource_path": resource_path, **extra}
    return {
        "scene_open": True,
        "preview_exists": True,
        "diagnostics": [],
        "instances": [instance],
    }


def test_preview_attach_normalizes_live_source_and_catalog_sockets(tmp_path: Path) -> None:
    root, _catalog = _attachment_project(tmp_path)
    snapshot = _wall_snapshot(
        position=[2, 0, 3], rotation_degrees=[0, 90, 0], scale=[2, 1, 1]
    )
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.side_effect = [
            snapshot,
            {"applied": True, "added_paths": ["AuraPreview/CornerPiece"]},
        ]
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            _attach_args(name="CornerPiece", scale=[2, 1, 3], domain="ruins"),
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    calls = client_type.return_value.request.call_args_list
    assert calls[0].args == ("preview.snapshot", {})
    operation = calls[1].args[1]["operations"][0]
    assert operation == {
        "operation": "attach",
        "node_path": "AuraPreview/WestWall",
        "source_catalog_identity": "ruins:wall",
        "source_resource_path": "res://assets/modules/wall.tscn",
        "source_socket_position": [2.0, 0.0, 0.0],
        "source_socket_facing": [1.0, 0.0, 0.0],
        "catalog_identity": "ruins:corner",
        "asset_id": "corner",
        "resource_path": "res://assets/modules/corner.tscn",
        "target_socket_position": [0.0, 0.0, -1.0],
        "target_socket_facing": [0.0, 0.0, -1.0],
        "allowed_rotations_deg": [],
        "scale": [2.0, 1.0, 3.0],
        "name": "CornerPiece",
    }
    assert "position_offset" not in operation


def test_preview_attach_rejects_nested_source_before_live_lookup(tmp_path: Path) -> None:
    root, _catalog = _attachment_project(tmp_path)
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            _attach_args(node_path="AuraPreview/Building/Wall"),
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert "direct AuraPreview child" in result.payload["error"]
    client_type.assert_not_called()


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        ({"instances": [], "diagnostics": []}, "does not exist"),
        (_wall_snapshot(resource_path="res://handmade/not_catalogued.tscn"), "not a recognized catalog asset"),
        (
            _wall_snapshot(
                resource_path="res://handmade/not_catalogued.tscn",
                asset_id="wall",
                domain="ruins",
            ),
            "catalog identity is inconsistent",
        ),
    ],
)
def test_preview_attach_rejects_missing_non_catalog_and_mismatched_sources(
    tmp_path: Path, snapshot: dict, expected: str
) -> None:
    root, _catalog = _attachment_project(tmp_path)
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = snapshot
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            _attach_args(),
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert expected in result.payload["error"]
    client_type.return_value.request.assert_called_once_with("preview.snapshot", {})


@pytest.mark.parametrize(
    ("source_socket", "target_socket", "expected"),
    [
        ("missing", "left", "source asset wall has no socket"),
        ("right", "missing", "target asset corner has no socket"),
    ],
)
def test_preview_attach_rejects_missing_catalog_sockets(
    tmp_path: Path, source_socket: str, target_socket: str, expected: str
) -> None:
    root, _catalog = _attachment_project(tmp_path)
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = _wall_snapshot()
        result = ToolRegistry(root, mode="worker").execute(
            "edit_godot_asset_preview",
            _attach_args(source_socket=source_socket, target_socket=target_socket),
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is False
    assert expected in result.payload["error"]


def test_preview_attach_reports_duplicated_and_malformed_catalog_sockets(tmp_path: Path) -> None:
    root, _catalog = _project(tmp_path, [_wall()])
    asset = resolve_godot_asset(root, "wall", domain="ruins")
    duplicated = replace(asset, sockets=(asset.sockets[0], asset.sockets[0]))
    malformed = replace(
        asset,
        sockets=(GodotAssetSocket("broken", (0.0, 0.0), (1.0, 0.0, 0.0)),),
    )

    with pytest.raises(ValueError, match="duplicated socket ID left"):
        _catalog_socket(duplicated, "left", "attach source asset wall")
    with pytest.raises(ValueError, match="socket broken is malformed"):
        _catalog_socket(malformed, "broken", "attach target asset wall")


def test_preview_attach_rejects_vertical_facing_and_manual_transform(tmp_path: Path) -> None:
    vertical = _wall()
    vertical["sockets"] = [
        {"id": "top", "position": [0, 1, 0], "facing": [0, 1, 0]},
    ]
    root, _catalog = _attachment_project(tmp_path, vertical)
    registry = ToolRegistry(root, mode="worker")
    with patch(
        "aura.conversation.tools._godot_asset_preview_mixin.GodotEditorBridgeClient"
    ) as client_type:
        client_type.return_value.request.return_value = _wall_snapshot()
        vertical_result = registry.execute(
            "edit_godot_asset_preview",
            _attach_args(source_socket="top"),
            lambda _request: ApprovalDecision("approve"),
        )
        manual_result = registry.execute(
            "edit_godot_asset_preview",
            _attach_args(source_socket="top", position=[10, 0, 0]),
            lambda _request: ApprovalDecision("approve"),
        )

    assert vertical_result.ok is False
    assert "no usable horizontal facing" in vertical_result.payload["error"]
    assert manual_result.ok is False
    assert "derives position and rotation from sockets" in manual_result.payload["error"]


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


def test_godot_duplicate_preparation_is_deterministic_atomic_and_unsaved() -> None:
    content = Path("aura/godot_editor/addon/actions/asset_preview_actions.gd").read_text(
        encoding="utf-8"
    )

    assert 'candidate := "%s_copy_%02d"' in content
    assert '"path": PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name)' in content
    assert '_undo_redo.add_do_method(self, "_execute_revision"' in content
    assert '_undo_redo.add_undo_method(self, "_execute_revision"' in content
    assert "for index in range(operations.size() - 1, -1, -1)" in content
    assert "save_scene" not in content


def test_godot_duplicate_offsets_ignore_scale_and_preserve_inherited_scale(
    tmp_path: Path,
) -> None:
    executable = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not executable:
        pytest.skip("GODOT_BIN or godot on PATH is required for runtime transform validation")

    project = tmp_path / "godot_duplicate_runtime"
    actions_dir = project / "addons/aura_bridge/actions"
    actions_dir.mkdir(parents=True)
    shutil.copyfile(
        "aura/godot_editor/addon/actions/asset_preview_actions.gd",
        actions_dir / "asset_preview_actions.gd",
    )
    (project / "project.godot").write_text(
        '[application]\nconfig/name="Aura Duplicate Runtime Test"\n', encoding="utf-8"
    )
    (project / "asset.tscn").write_text(
        '[gd_scene format=3]\n\n[node name="Wall" type="Node3D"]\n', encoding="utf-8"
    )
    (project / "test_duplicate.gd").write_text(
        """extends SceneTree

const Actions = preload("res://addons/aura_bridge/actions/asset_preview_actions.gd")

func _initialize() -> void:
    var packed := load("res://asset.tscn") as PackedScene
    var source := packed.instantiate() as Node3D
    source.name = "Wall"
    source.rotation_degrees = Vector3(0.0, 90.0, 0.0)
    source.scale = Vector3(2.0, 3.0, 4.0)
    var scene_root := Node3D.new()
    var preview := Node3D.new()
    scene_root.add_child(preview)
    preview.add_child(source)
    var actions = Actions.new(null, null)
    var checked: Dictionary = actions._prepare_duplicate_operation({
        "count": 2,
        "offset": [2.0, 0.0, 0.0],
        "offset_space": "local",
        "resource_path": "res://asset.tscn",
    }, 0, source, {"Wall": true})
    if not checked.get("ok", false):
        push_error(str(checked.get("error", "duplicate preparation failed")))
        quit(1)
        return
    var operations: Array[Dictionary] = checked["operations"]
    var unit_source := packed.instantiate() as Node3D
    unit_source.name = "UnitWall"
    unit_source.rotation_degrees = Vector3(0.0, 90.0, 0.0)
    var unit_checked: Dictionary = actions._prepare_duplicate_operation({
        "count": 1,
        "offset": [2.0, 0.0, 0.0],
        "offset_space": "local",
        "resource_path": "res://asset.tscn",
    }, 0, unit_source, {"UnitWall": true})
    var world_checked: Dictionary = actions._prepare_duplicate_operation({
        "count": 1,
        "offset": [2.0, 0.0, 0.0],
        "offset_space": "world",
        "resource_path": "res://asset.tscn",
    }, 0, source, {"Wall": true})
    if not unit_checked.get("ok", false) or not world_checked.get("ok", false):
        push_error("unit or world duplicate preparation failed")
        quit(1)
        return
    var unit_operations: Array[Dictionary] = unit_checked["operations"]
    var world_operations: Array[Dictionary] = world_checked["operations"]
    actions._execute_revision(scene_root, preview, operations, false, true)
    var result := {
        "unit_step": [unit_operations[0]["new_position"].x, unit_operations[0]["new_position"].y, unit_operations[0]["new_position"].z],
        "scaled_step": [operations[0]["new_position"].x, operations[0]["new_position"].y, operations[0]["new_position"].z],
        "world_step": [world_operations[0]["new_position"].x, world_operations[0]["new_position"].y, world_operations[0]["new_position"].z],
        "positions": [
            [operations[0]["new_position"].x, operations[0]["new_position"].y, operations[0]["new_position"].z],
            [operations[1]["new_position"].x, operations[1]["new_position"].y, operations[1]["new_position"].z],
        ],
        "scales": [
            [operations[0]["new_scale"].x, operations[0]["new_scale"].y, operations[0]["new_scale"].z],
            [operations[1]["new_scale"].x, operations[1]["new_scale"].y, operations[1]["new_scale"].z],
        ],
        "paths": [operations[0]["path"], operations[1]["path"]],
        "forward_child_count": preview.get_child_count(),
    }
    actions._execute_revision(scene_root, preview, operations, false, false)
    result["reversed_child_count"] = preview.get_child_count()
    print("AURA_DUPLICATE_RUNTIME:" + JSON.stringify(result))
    actions._free_prepared_instances(unit_operations)
    actions._free_prepared_instances(world_operations)
    unit_source.free()
    scene_root.free()
    quit()
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [executable, "--headless", "--path", str(project), "--script", "res://test_duplicate.gd"],
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    payload_line = next(
        line for line in output.splitlines() if line.startswith("AURA_DUPLICATE_RUNTIME:")
    )
    payload = json.loads(payload_line.split(":", 1)[1])

    assert payload["unit_step"] == pytest.approx([0.0, 0.0, -2.0], abs=1e-5)
    assert payload["scaled_step"] == pytest.approx(payload["unit_step"], abs=1e-5)
    assert payload["world_step"] == [2.0, 0.0, 0.0]
    assert payload["positions"][0] == pytest.approx([0.0, 0.0, -2.0], abs=1e-5)
    assert payload["positions"][1] == pytest.approx([0.0, 0.0, -4.0], abs=1e-5)
    assert payload["scales"] == [[2.0, 3.0, 4.0], [2.0, 3.0, 4.0]]
    assert payload["paths"] == ["AuraPreview/Wall_copy_01", "AuraPreview/Wall_copy_02"]
    assert payload["forward_child_count"] == 3
    assert payload["reversed_child_count"] == 1


def test_godot_socket_attachment_math_naming_validation_and_undo(tmp_path: Path) -> None:
    executable = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not executable:
        pytest.skip("GODOT_BIN or godot on PATH is required for runtime attachment validation")
    project = tmp_path / "godot_attach_runtime"
    actions_dir = project / "addons/aura_bridge/actions"
    actions_dir.mkdir(parents=True)
    shutil.copyfile(
        "aura/godot_editor/addon/actions/asset_preview_actions.gd",
        actions_dir / "asset_preview_actions.gd",
    )
    (project / "project.godot").write_text(
        '[application]\nconfig/name="Aura Attach Runtime Test"\n', encoding="utf-8"
    )
    for scene_name in ("source", "target"):
        (project / f"{scene_name}.tscn").write_text(
            f'[gd_scene format=3]\n\n[node name="{scene_name.title()}" type="Node3D"]\n',
            encoding="utf-8",
        )
    (project / "test_attach.gd").write_text(
        """extends SceneTree
const Actions = preload("res://addons/aura_bridge/actions/asset_preview_actions.gd")

func _params(asset_id: String, source_position: Array, source_facing: Array,
        target_position: Array, target_facing: Array,
        target_scale: Array = [1.0, 1.0, 1.0], allowed: Array = []) -> Dictionary:
    return {
        "source_catalog_identity": "ruins:wall",
        "source_resource_path": "res://source.tscn",
        "source_socket_position": source_position,
        "source_socket_facing": source_facing,
        "catalog_identity": "ruins:" + asset_id,
        "asset_id": asset_id,
        "resource_path": "res://target.tscn",
        "target_socket_position": target_position,
        "target_socket_facing": target_facing,
        "scale": target_scale,
        "allowed_rotations_deg": allowed,
    }

func _vector(value: Vector3) -> Array:
    return [value.x, value.y, value.z]

func _initialize() -> void:
    var source := (load("res://source.tscn") as PackedScene).instantiate() as Node3D
    source.name = "Wall"
    var scene_root := Node3D.new()
    var preview := Node3D.new()
    preview.name = "AuraPreview"
    scene_root.add_child(preview)
    preview.add_child(source)
    var actions = Actions.new(null, null)
    var straight: Dictionary = actions._prepare_attach_operation(
        _params("straight", [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [-2.0, 0.0, 0.0], [-1.0, 0.0, 0.0]),
        0, source, {"Wall": true})
    var corner: Dictionary = actions._prepare_attach_operation(
        _params("corner", [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 0.0, -1.0]),
        0, source, {"Wall": true, "corner_01": true})
    source.scale = Vector3(2.0, 3.0, 4.0)
    var scaled_source: Dictionary = actions._prepare_attach_operation(
        _params("scaled_source", [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [-2.0, 0.0, 0.0], [-1.0, 0.0, 0.0]),
        0, source, {"Wall": true})
    source.scale = Vector3.ONE
    var scaled_target: Dictionary = actions._prepare_attach_operation(
        _params("scaled_target", [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 0.0, -1.0], [2.0, 1.0, 3.0]),
        0, source, {"Wall": true})
    var disallowed: Dictionary = actions._prepare_attach_operation(
        _params("corner", [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 0.0, -1.0], [1.0, 1.0, 1.0], [0.0]),
        0, source, {"Wall": true})
    var failed_preparation_child_count := preview.get_child_count()
    if not straight.get("ok", false) or not corner.get("ok", false) or not scaled_source.get("ok", false) or not scaled_target.get("ok", false):
        push_error("attachment preparation failed")
        quit(1)
        return
    var straight_op: Dictionary = straight["operation"]
    var corner_op: Dictionary = corner["operation"]
    var source_socket_point: Vector3 = source.transform * Vector3(2.0, 0.0, 0.0)
    var straight_transform := Transform3D(
        Basis(Vector3.UP, deg_to_rad(straight_op["new_rotation"].y)).scaled(straight_op["new_scale"]),
        straight_op["new_position"])
    var straight_socket_point: Vector3 = straight_transform * Vector3(-2.0, 0.0, 0.0)
    var source_facing: Vector3 = source.transform.basis.orthonormalized() * Vector3.RIGHT
    var target_facing: Vector3 = straight_transform.basis.orthonormalized() * Vector3.LEFT
    var prepared_ops: Array[Dictionary] = [straight_op]
    actions._execute_revision(scene_root, preview, prepared_ops, false, true)
    var forward_count := preview.get_child_count()
    actions._execute_revision(scene_root, preview, prepared_ops, false, false)
    var straight_undo_count := preview.get_child_count()
    var scaled_target_ops: Array[Dictionary] = [scaled_target["operation"]]
    actions._execute_revision(scene_root, preview, scaled_target_ops, false, true)
    var live_scaled_target: Node3D = scaled_target["operation"]["new_node"]
    var live_source_socket: Vector3 = source.transform * Vector3(2.0, 0.0, 0.0)
    var live_target_socket: Vector3 = live_scaled_target.transform * Vector3(0.0, 0.0, -1.0)
    var live_source_facing: Vector3 = source.transform.basis.orthonormalized() * Vector3.RIGHT
    var live_target_facing: Vector3 = live_scaled_target.transform.basis.orthonormalized() * Vector3(0.0, 0.0, -1.0)
    var result := {
        "straight_position": _vector(straight_op["new_position"]),
        "straight_rotation_y": straight_op["new_rotation"].y,
        "socket_distance": source_socket_point.distance_to(straight_socket_point),
        "facing_dot": source_facing.normalized().dot(target_facing.normalized()),
        "corner_position": _vector(corner_op["new_position"]),
        "corner_rotation_y": corner_op["new_rotation"].y,
        "corner_name": corner_op["new_node"].name,
        "corner_path": corner_op["path"],
        "scaled_source_position": _vector(scaled_source["operation"]["new_position"]),
        "scaled_source_facing": _vector((Basis(Vector3.UP, deg_to_rad(scaled_source["operation"]["new_rotation"].y)) * Vector3.LEFT).normalized()),
        "scaled_target_position": _vector(live_scaled_target.position),
        "scaled_target_rotation_y": live_scaled_target.rotation_degrees.y,
        "scaled_target_socket_distance": live_source_socket.distance_to(live_target_socket),
        "scaled_target_facing_dot": live_source_facing.normalized().dot(live_target_facing.normalized()),
        "scaled_target_scale": _vector(live_scaled_target.scale),
        "disallowed_ok": disallowed.get("ok", false),
        "disallowed_error": disallowed.get("error", ""),
        "child_count_after_failed_preparation": failed_preparation_child_count,
        "forward_child_count": forward_count,
        "undo_child_count": straight_undo_count,
    }
    actions._execute_revision(scene_root, preview, scaled_target_ops, false, false)
    result["scaled_target_undo_child_count"] = preview.get_child_count()
    print("AURA_ATTACH_RUNTIME:" + JSON.stringify(result))
    var all_prepared: Array[Dictionary] = [straight_op, corner_op, scaled_source["operation"], scaled_target["operation"]]
    actions._free_prepared_instances(all_prepared)
    scene_root.free()
    quit()
""",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [executable, "--headless", "--path", str(project), "--script", "res://test_attach.gd"],
        cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    payload_line = next(
        line for line in output.splitlines() if line.startswith("AURA_ATTACH_RUNTIME:")
    )
    payload = json.loads(payload_line.split(":", 1)[1])
    assert payload["straight_position"] == pytest.approx([4.0, 0.0, 0.0], abs=1e-5)
    assert payload["straight_rotation_y"] == pytest.approx(0.0, abs=1e-5)
    assert payload["socket_distance"] == pytest.approx(0.0, abs=1e-5)
    assert payload["facing_dot"] == pytest.approx(-1.0, abs=1e-5)
    assert payload["corner_position"] == pytest.approx([3.0, 0.0, 0.0], abs=1e-5)
    assert payload["corner_rotation_y"] == pytest.approx(90.0, abs=1e-5)
    assert payload["corner_name"] == "corner_02"
    assert payload["corner_path"] == "AuraPreview/corner_02"
    assert payload["scaled_source_position"] == pytest.approx([6.0, 0.0, 0.0], abs=1e-5)
    assert payload["scaled_source_facing"] == pytest.approx([-1.0, 0.0, 0.0], abs=1e-5)
    assert payload["scaled_target_position"] == pytest.approx([5.0, 0.0, 0.0], abs=1e-5)
    assert payload["scaled_target_rotation_y"] == pytest.approx(90.0, abs=1e-5)
    assert payload["scaled_target_socket_distance"] == pytest.approx(0.0, abs=1e-5)
    assert payload["scaled_target_facing_dot"] == pytest.approx(-1.0, abs=1e-5)
    assert payload["scaled_target_scale"] == [2.0, 1.0, 3.0]
    assert payload["disallowed_ok"] is False
    assert "rotation is not catalog-approved" in payload["disallowed_error"]
    assert payload["child_count_after_failed_preparation"] == 1
    assert payload["forward_child_count"] == 2
    assert payload["undo_child_count"] == 1
    assert payload["scaled_target_undo_child_count"] == 1


def test_godot_named_sequential_burst_uses_planned_nodes_and_undoes_atomically(
    tmp_path: Path,
) -> None:
    executable = os.environ.get("GODOT_BIN") or shutil.which("godot")
    if not executable:
        pytest.skip("GODOT_BIN or godot on PATH is required for runtime burst validation")
    project = tmp_path / "godot_named_burst_runtime"
    actions_dir = project / "addons/aura_bridge/actions"
    actions_dir.mkdir(parents=True)
    shutil.copyfile(
        "aura/godot_editor/addon/actions/asset_preview_actions.gd",
        actions_dir / "asset_preview_actions.gd",
    )
    (project / "project.godot").write_text(
        '[application]\nconfig/name="Aura Named Burst Runtime Test"\n', encoding="utf-8"
    )
    for scene_name in ("wall", "corner", "terminal"):
        (project / f"{scene_name}.tscn").write_text(
            f'[gd_scene format=3]\n\n[node name="{scene_name.title()}" type="Node3D"]\n',
            encoding="utf-8",
        )
    (project / "test_named_burst.gd").write_text(
        r'''extends SceneTree
const Actions = preload("res://addons/aura_bridge/actions/asset_preview_actions.gd")

func _attach(source_path: String, source_identity: String, source_resource: String,
        source_position: Array, source_facing: Array, asset_id: String,
        target_resource: String, target_position: Array, target_facing: Array,
        name: String, scale: Array) -> Dictionary:
    return {
        "operation": "attach",
        "node_path": source_path,
        "source_catalog_identity": source_identity,
        "source_resource_path": source_resource,
        "source_socket_position": source_position,
        "source_socket_facing": source_facing,
        "catalog_identity": "ruins:" + asset_id,
        "asset_id": asset_id,
        "resource_path": target_resource,
        "target_socket_position": target_position,
        "target_socket_facing": target_facing,
        "allowed_rotations_deg": [],
        "name": name,
        "scale": scale,
    }

func _prepare_batch(actions, raw_operations: Array, names: Dictionary,
        nodes: Dictionary, targeted: Dictionary, planned_paths: Dictionary,
        prepared: Array[Dictionary]) -> Dictionary:
    for index in raw_operations.size():
        var raw: Dictionary = raw_operations[index]
        if planned_paths.has(str(raw.get("node_path", ""))) and str(raw.get("operation", "")) not in ["duplicate", "attach"]:
            return {"ok": false, "error": "incompatible planned operation"}
        var checked: Dictionary = actions._prepare_revision_operation(
            raw, index, names, nodes, targeted)
        if not checked.get("ok", false):
            return checked
        if checked.has("operations"):
            prepared.append_array(checked["operations"])
        else:
            prepared.append(checked["operation"])
        actions._register_prepared_outputs(checked, nodes, targeted, planned_paths)
    return {"ok": true}

func _point(node: Node3D, socket_position: Vector3) -> Vector3:
    return node.transform * socket_position

func _facing(node: Node3D, socket_facing: Vector3) -> Vector3:
    return (node.transform.basis.orthonormalized() * socket_facing).normalized()

func _initialize() -> void:
    var actions = Actions.new(null, null)
    var scene_root := Node3D.new()
    var preview := Node3D.new()
    preview.name = "AuraPreview"
    preview.set_meta("aura_preview_root", true)
    scene_root.add_child(preview)
    var names := {}
    var nodes := {}
    var targeted := {}
    var planned_paths := {}
    var prepared: Array[Dictionary] = []
    var raw_operations: Array = [
        {
            "operation": "instantiate", "catalog_identity": "ruins:wall",
            "resource_path": "res://wall.tscn", "name": "RearWall01",
            "position": [2.0, 0.0, 3.0], "rotation_degrees_y": 90.0,
            "scale": [2.0, 1.0, 3.0], "allowed_rotations_deg": [],
        },
        {
            "operation": "duplicate", "node_path": "AuraPreview/RearWall01",
            "count": 1, "offset": [8.0, 0.0, 0.0], "offset_space": "local",
            "catalog_identity": "ruins:wall", "resource_path": "res://wall.tscn",
            "name": "RearWall02",
        },
        {
            "operation": "duplicate", "node_path": "AuraPreview/RearWall02",
            "count": 1, "offset": [8.0, 0.0, 0.0], "offset_space": "local",
            "catalog_identity": "ruins:wall", "resource_path": "res://wall.tscn",
            "name": "RearWall03",
        },
        _attach("AuraPreview/RearWall03", "ruins:wall", "res://wall.tscn",
            [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], "corner", "res://corner.tscn",
            [0.0, 0.0, -1.0], [0.0, 0.0, -1.0], "RearCorner", [2.0, 1.0, 3.0]),
        _attach("AuraPreview/RearCorner", "ruins:corner", "res://corner.tscn",
            [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], "wall", "res://wall.tscn",
            [-2.0, 0.0, 0.0], [-1.0, 0.0, 0.0], "SideWall01", [1.5, 2.0, 0.75]),
        {
            "operation": "duplicate", "node_path": "AuraPreview/SideWall01",
            "count": 1, "offset": [6.0, 0.0, 0.0], "offset_space": "local",
            "catalog_identity": "ruins:wall", "resource_path": "res://wall.tscn",
            "name": "SideWall02",
        },
        _attach("AuraPreview/SideWall02", "ruins:wall", "res://wall.tscn",
            [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], "terminal", "res://terminal.tscn",
            [-2.0, 0.0, 0.0], [-1.0, 0.0, 0.0], "Terminal", [0.5, 1.0, 2.0]),
    ]
    var prepared_check := _prepare_batch(
        actions, raw_operations, names, nodes, targeted, planned_paths, prepared)
    if not prepared_check.get("ok", false):
        push_error(str(prepared_check.get("error", "burst preparation failed")))
        quit(1)
        return

    var bad_names := {}
    var bad_nodes := {}
    var bad_targeted := {}
    var bad_paths := {}
    var bad_prepared: Array[Dictionary] = []
    var bad_operations: Array = [
        {
            "operation": "instantiate", "catalog_identity": "ruins:wall",
            "resource_path": "res://wall.tscn", "name": "WouldMutate",
            "position": [0.0, 0.0, 0.0], "rotation_degrees_y": 0.0,
            "scale": [1.0, 1.0, 1.0], "allowed_rotations_deg": [],
        },
        _attach("AuraPreview/WouldMutate", "ruins:wall", "res://wall.tscn",
            [2.0, 0.0, 0.0], [1.0, 0.0, 0.0], "missing", "res://missing.tscn",
            [-2.0, 0.0, 0.0], [-1.0, 0.0, 0.0], "BadFinal", [1.0, 1.0, 1.0]),
    ]
    var bad_check := _prepare_batch(
        actions, bad_operations, bad_names, bad_nodes, bad_targeted, bad_paths, bad_prepared)
    var failed_child_count := preview.get_child_count()
    actions._free_prepared_instances(bad_prepared)

    var before_names: Array[String] = []
    for child in preview.get_children():
        before_names.append(str(child.name))
    actions._execute_revision(scene_root, preview, prepared, false, true)
    var expected_paths := [
        "AuraPreview/RearWall01", "AuraPreview/RearWall02", "AuraPreview/RearWall03",
        "AuraPreview/RearCorner", "AuraPreview/SideWall01", "AuraPreview/SideWall02",
        "AuraPreview/Terminal",
    ]
    var all_paths_exist := true
    var returned_paths: Array[String] = []
    for operation in prepared:
        if operation["operation"] == "instantiate":
            returned_paths.append(str(operation["path"]))
            all_paths_exist = all_paths_exist and scene_root.has_node(NodePath(operation["path"]))
    var rear_1: Node3D = nodes["AuraPreview/RearWall01"]
    var rear_2: Node3D = nodes["AuraPreview/RearWall02"]
    var rear_3: Node3D = nodes["AuraPreview/RearWall03"]
    var corner: Node3D = nodes["AuraPreview/RearCorner"]
    var side_1: Node3D = nodes["AuraPreview/SideWall01"]
    var side_2: Node3D = nodes["AuraPreview/SideWall02"]
    var terminal: Node3D = nodes["AuraPreview/Terminal"]
    var pairs := [
        [rear_1, Vector3(2, 0, 0), Vector3(1, 0, 0), rear_2, Vector3(-2, 0, 0), Vector3(-1, 0, 0)],
        [rear_2, Vector3(2, 0, 0), Vector3(1, 0, 0), rear_3, Vector3(-2, 0, 0), Vector3(-1, 0, 0)],
        [rear_3, Vector3(2, 0, 0), Vector3(1, 0, 0), corner, Vector3(0, 0, -1), Vector3(0, 0, -1)],
        [corner, Vector3(2, 0, 0), Vector3(1, 0, 0), side_1, Vector3(-2, 0, 0), Vector3(-1, 0, 0)],
        [side_1, Vector3(2, 0, 0), Vector3(1, 0, 0), side_2, Vector3(-2, 0, 0), Vector3(-1, 0, 0)],
        [side_2, Vector3(2, 0, 0), Vector3(1, 0, 0), terminal, Vector3(-2, 0, 0), Vector3(-1, 0, 0)],
    ]
    var distances: Array[float] = []
    var facing_dots: Array[float] = []
    for pair in pairs:
        distances.append(_point(pair[0], pair[1]).distance_to(_point(pair[3], pair[4])))
        facing_dots.append(_facing(pair[0], pair[2]).dot(_facing(pair[3], pair[5])))
    var result := {
        "bad_ok": bad_check.get("ok", false),
        "bad_error": bad_check.get("error", ""),
        "failed_child_count": failed_child_count,
        "before_names": before_names,
        "forward_child_count": preview.get_child_count(),
        "expected_paths": expected_paths,
        "returned_paths": returned_paths,
        "all_paths_exist": all_paths_exist,
        "distances": distances,
        "facing_dots": facing_dots,
        "rear_rotation": rear_1.rotation_degrees.y,
        "rear_scale": [rear_1.scale.x, rear_1.scale.y, rear_1.scale.z],
        "corner_scale": [corner.scale.x, corner.scale.y, corner.scale.z],
        "side_scale": [side_1.scale.x, side_1.scale.y, side_1.scale.z],
        "terminal_scale": [terminal.scale.x, terminal.scale.y, terminal.scale.z],
    }
    actions._execute_revision(scene_root, preview, prepared, false, false)
    var after_names: Array[String] = []
    for child in preview.get_children():
        after_names.append(str(child.name))
    result["undo_child_count"] = preview.get_child_count()
    result["after_names"] = after_names
    print("AURA_NAMED_BURST_RUNTIME:" + JSON.stringify(result))
    actions._free_prepared_instances(prepared)
    scene_root.free()
    quit()
''',
        encoding="utf-8",
    )
    completed = subprocess.run(
        [executable, "--headless", "--path", str(project), "--script", "res://test_named_burst.gd"],
        cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )
    output = completed.stdout + "\n" + completed.stderr
    assert completed.returncode == 0, output
    payload_line = next(
        line for line in output.splitlines() if line.startswith("AURA_NAMED_BURST_RUNTIME:")
    )
    payload = json.loads(payload_line.split(":", 1)[1])
    assert payload["bad_ok"] is False
    assert "catalog scene does not exist" in payload["bad_error"]
    assert payload["failed_child_count"] == 0
    assert payload["forward_child_count"] == 7
    assert payload["all_paths_exist"] is True
    assert payload["returned_paths"] == payload["expected_paths"]
    assert payload["distances"] == pytest.approx([0.0] * 6, abs=1e-5)
    assert payload["facing_dots"] == pytest.approx([-1.0] * 6, abs=1e-5)
    assert payload["rear_rotation"] == pytest.approx(90.0, abs=1e-5)
    assert payload["rear_scale"] == [2.0, 1.0, 3.0]
    assert payload["corner_scale"] == [2.0, 1.0, 3.0]
    assert payload["side_scale"] == [1.5, 2.0, 0.75]
    assert payload["terminal_scale"] == [0.5, 1.0, 2.0]
    assert payload["before_names"] == payload["after_names"] == []
    assert payload["undo_child_count"] == 0
