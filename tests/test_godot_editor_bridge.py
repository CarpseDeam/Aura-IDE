from __future__ import annotations

import json
import socket
import threading
from pathlib import Path
from unittest.mock import patch

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_editor.client import GodotEditorBridgeClient, load_bridge_config
from aura.godot_editor.installer import ADDON_SETTING, enable_plugin_setting, install_editor_bridge


def _project(tmp_path: Path) -> Path:
    (tmp_path / "project.godot").write_text(
        '[application]\nconfig/name="Bridge Test"\n', encoding="utf-8"
    )
    return tmp_path


def test_installer_copies_modular_addon_for_normal_godot_activation(tmp_path: Path) -> None:
    root = _project(tmp_path)

    result = install_editor_bridge(root, 18991)

    assert result.plugin_enabled is False
    assert (root / "addons/aura_bridge/plugin.cfg").is_file()
    assert (root / "addons/aura_bridge/transport/bridge_server.gd").is_file()
    assert (root / "addons/aura_bridge/perception/scene_snapshot.gd").is_file()
    assert (root / "addons/aura_bridge/actions/scene_actions.gd").is_file()
    assert (root / "addons/aura_bridge/actions/asset_preview_actions.gd").is_file()
    assert (root / "addons/aura_bridge/perception/asset_preview_snapshot.gd").is_file()
    assert ADDON_SETTING not in (root / "project.godot").read_text(encoding="utf-8")
    config = load_bridge_config(root)
    assert config.host == "127.0.0.1"
    assert config.port == 18991
    assert len(config.token) >= 24


def test_installer_can_enable_plugin_for_headless_setup(tmp_path: Path) -> None:
    root = _project(tmp_path)

    result = install_editor_bridge(root, enable_plugin=True)

    assert result.plugin_enabled is True
    assert ADDON_SETTING in (root / "project.godot").read_text(encoding="utf-8")


def test_enable_plugin_setting_preserves_existing_plugins() -> None:
    content = (
        "[editor_plugins]\n\n"
        'enabled=PackedStringArray("res://addons/other/plugin.cfg")\n\n'
        "[rendering]\nrenderer/rendering_method=\"gl_compatibility\"\n"
    )

    updated = enable_plugin_setting(content)

    assert '"res://addons/other/plugin.cfg"' in updated
    assert f'"{ADDON_SETTING}"' in updated
    assert updated.count("[rendering]") == 1
    assert enable_plugin_setting(updated) == updated


def test_client_uses_authenticated_newline_json_protocol(tmp_path: Path) -> None:
    root = _project(tmp_path)
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    token = "t" * 32
    (root / ".aura").mkdir()
    (root / ".aura/godot_editor_bridge.json").write_text(
        json.dumps({"host": "127.0.0.1", "port": port, "token": token}), encoding="utf-8"
    )
    observed: dict = {}

    def serve() -> None:
        with server:
            peer, _address = server.accept()
            with peer:
                wire = b""
                while not wire.endswith(b"\n"):
                    wire += peer.recv(4096)
                request = json.loads(wire)
                observed.update(request)
                response = {
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": {"scene_open": True, "nodes": []},
                }
                peer.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    result = GodotEditorBridgeClient(root).request("scene.snapshot", {"max_nodes": 10})
    thread.join(timeout=2)

    assert result["scene_open"] is True
    assert observed["token"] == token
    assert observed["action"] == "scene.snapshot"
    assert observed["params"] == {"max_nodes": 10}


def test_registry_exposes_live_editor_tools_by_role(tmp_path: Path) -> None:
    worker_names = {
        tool["function"]["name"] for tool in ToolRegistry(tmp_path, mode="worker").tool_defs()
    }
    planner_names = {
        tool["function"]["name"] for tool in ToolRegistry(tmp_path, mode="planner").tool_defs()
    }

    assert {
        "inspect_godot_editor",
        "inspect_godot_asset_preview",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "install_godot_editor_bridge",
    } <= worker_names
    assert "inspect_godot_editor" in planner_names
    assert "inspect_godot_asset_preview" in planner_names
    assert "edit_godot_editor" not in planner_names
    assert "edit_godot_asset_preview" not in planner_names
    assert "install_godot_editor_bridge" not in planner_names


def test_missing_bridge_directs_model_to_bundled_installer(tmp_path: Path) -> None:
    result = ToolRegistry(tmp_path, mode="planner").execute(
        "inspect_godot_editor", {}, lambda _request: ApprovalDecision("approve")
    )

    assert result.ok is False
    assert result.payload["bridge_installed"] is False
    assert result.payload["suggested_next_tool"] == "install_godot_editor_bridge"
    assert "do not author a new addon" in result.payload["suggested_next_action"]


def test_live_editor_edit_is_approval_gated(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="worker")
    fake_client = patch("aura.conversation.tools._godot_editor_mixin.GodotEditorBridgeClient")
    with fake_client as client_type:
        client_type.return_value.request.return_value = {
            "applied": True,
            "operation_count": 1,
            "changed_nodes": ["Camera3D"],
        }
        result = registry.execute(
            "edit_godot_editor",
            {
                "action": "apply",
                "operations": [
                    {
                        "action": "set_property",
                        "node_path": "Camera3D",
                        "property": "fov",
                        "value_text": "70.0",
                    }
                ],
            },
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    assert result.payload["applied"] is True
    client_type.return_value.request.assert_called_once()
