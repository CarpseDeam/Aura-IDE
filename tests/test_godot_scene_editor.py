from __future__ import annotations

from pathlib import Path

import pytest

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_scene_editor import GodotSceneEditError, edit_godot_scene, scene_node_paths

SCENE = """[gd_scene format=3]

[node name="Main" type="Node2D"]

[node name="Player" type="CharacterBody2D" parent="."]
position = Vector2(10, 20)

[node name="Sprite" type="Sprite2D" parent="Player"]
visible = true
"""


def test_add_node_and_set_properties_atomically() -> None:
    result = edit_godot_scene(
        SCENE,
        [
            {
                "action": "add_node",
                "name": "Camera",
                "type": "Camera2D",
                "parent": "Player",
                "properties": {"enabled": "true"},
            },
            {
                "action": "set_property",
                "node_path": "Player",
                "property": "position",
                "value": "Vector2(30, 40)",
            },
            {
                "action": "set_property",
                "node_path": "Player/Camera",
                "property": "zoom",
                "value": "Vector2(2, 2)",
            },
        ],
    )

    assert scene_node_paths(result.content) == [".", "Player", "Player/Sprite", "Player/Camera"]
    assert "position = Vector2(30, 40)" in result.content
    assert "enabled = true" in result.content
    assert "zoom = Vector2(2, 2)" in result.content
    assert len(result.operations) == 3


def test_remove_node_requires_recursive_for_subtree() -> None:
    with pytest.raises(GodotSceneEditError, match="has children"):
        edit_godot_scene(SCENE, [{"action": "remove_node", "node_path": "Player"}])

    result = edit_godot_scene(
        SCENE,
        [{"action": "remove_node", "node_path": "Player", "recursive": True}],
    )

    assert scene_node_paths(result.content) == ["."]
    assert "CharacterBody2D" not in result.content
    assert "Sprite2D" not in result.content


def test_scene_tool_uses_approval_gated_write_and_reports_operations(tmp_path: Path) -> None:
    scene = tmp_path / "scenes" / "main.tscn"
    scene.parent.mkdir()
    scene.write_text(SCENE, encoding="utf-8")
    registry = ToolRegistry(tmp_path, mode="worker")
    requests = []

    def approve(request):
        requests.append(request)
        return ApprovalDecision(action="approve")

    result = registry.execute(
        "edit_godot_scene",
        {
            "path": "scenes/main.tscn",
            "operations": [
                {
                    "action": "set_property",
                    "node_path": "Player/Sprite",
                    "property": "visible",
                    "value": "false",
                }
            ],
        },
        approve,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["applied_tool"] == "edit_godot_scene"
    assert result.payload["operation_count"] == 1
    assert "visible = false" in scene.read_text(encoding="utf-8")
    assert len(requests) == 1
    assert requests[0].rel_path == "scenes/main.tscn"


def test_scene_tool_is_exposed_to_workers_but_not_planners(tmp_path: Path) -> None:
    worker_names = {
        tool["function"]["name"]
        for tool in ToolRegistry(tmp_path, mode="worker").tool_defs()
    }
    planner_names = {
        tool["function"]["name"]
        for tool in ToolRegistry(tmp_path, mode="planner").tool_defs()
    }

    assert "edit_godot_scene" in worker_names
    assert "edit_godot_scene" not in planner_names


def test_scene_tool_refuses_live_preview_main_scene_disk_edits(tmp_path: Path) -> None:
    scene = tmp_path / "scenes" / "ruin_preview.tscn"
    scene.parent.mkdir()
    scene.write_text(SCENE, encoding="utf-8")
    (tmp_path / "project.godot").write_text(
        '[application]\nrun/main_scene="res://scenes/ruin_preview.tscn"\n\n'
        '[aura]\neditor_bridge/preview_planner="res://scripts/live/planner.gd"\n',
        encoding="utf-8",
    )
    registry = ToolRegistry(tmp_path, mode="worker")

    result = registry.execute(
        "edit_godot_scene",
        {
            "path": "scenes/ruin_preview.tscn",
            "operations": [{"action": "remove_node", "node_path": "Player", "recursive": True}],
        },
        lambda request: ApprovalDecision(action="approve"),
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "live_preview_scene_file_edit_forbidden"
    assert result.payload["suggested_next_tool"] == "build_live_ruin"
    assert scene.read_text(encoding="utf-8") == SCENE
