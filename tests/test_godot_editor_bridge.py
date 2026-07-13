from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.godot_editor.client import GodotEditorBridgeClient, GodotEditorBridgeError, load_bridge_config
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
    assert (root / "addons/aura_bridge/actions/project_preview_planner.gd").is_file()
    assert (root / "addons/aura_bridge/perception/asset_preview_snapshot.gd").is_file()
    assert (root / "addons/aura_bridge/perception/api_introspection.gd").is_file()
    assert ADDON_SETTING not in (root / "project.godot").read_text(encoding="utf-8")
    config = load_bridge_config(root)
    assert config.host == "127.0.0.1"
    assert config.port == 18991
    assert len(config.token) >= 24
    assert (root / "addons/aura_bridge/perception/viewport_capture.gd").is_file()


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
        "inspect_godot_api",
        "inspect_godot_asset_preview",
        "capture_godot_asset_preview",
        "edit_godot_editor",
        "edit_godot_asset_preview",
        "install_godot_editor_bridge",
    } <= worker_names
    assert "inspect_godot_editor" in planner_names
    assert "inspect_godot_api" in planner_names
    assert "inspect_godot_asset_preview" in planner_names
    assert "capture_godot_asset_preview" in planner_names
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


def test_capability_reporting_includes_preview_capture(tmp_path: Path) -> None:
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

    def serve() -> None:
        with server:
            peer, _address = server.accept()
            with peer:
                wire = b""
                while not wire.endswith(b"\n"):
                    wire += peer.recv(4096)
                request = json.loads(wire)
                response = {
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": {
                        "bridge": "aura-godot-editor",
                        "protocol": 1,
                        "bridge_version": 10,
                        "capabilities": [
                            "scene.snapshot",
                            "scene.select",
                            "scene.apply",
                            "scene.save",
                            "preview.snapshot",
                            "preview.instantiate",
                            "preview.clear",
                            "preview.apply",
                            "preview.publish_scene",
                            "preview.capture",
                            "preview.planner.inspect",
                            "preview.planner.apply",
                            "api.describe",
                        ],
                    },
                }
                peer.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    result = GodotEditorBridgeClient(root).request("ping", {})
    thread.join(timeout=2)

    assert result["bridge"] == "aura-godot-editor"
    assert result["bridge_version"] == 10
    assert "preview.apply" in result["capabilities"]
    assert "preview.publish_scene" in result["capabilities"]
    assert "api.describe" in result["capabilities"]
    assert "preview.capture" in result["capabilities"]
    assert "preview.planner.inspect" in result["capabilities"]
    assert "preview.planner.apply" in result["capabilities"]


def test_project_preview_planner_routes_through_focused_owner() -> None:
    router = Path("aura/godot_editor/addon/protocol/request_router.gd").read_text(
        encoding="utf-8"
    )
    owner = Path("aura/godot_editor/addon/actions/project_preview_planner.gd").read_text(
        encoding="utf-8"
    )

    assert '"preview.planner.inspect"' in router
    assert '"preview.planner.apply"' in router
    assert "_project_preview_planner.inspect_contract(params)" in router
    assert "_project_preview_planner.plan_and_apply(params)" in router
    assert 'const PROJECT_SETTING := "aura/editor_bridge/preview_planner"' in owner
    assert 'const INSPECT_METHOD := &"inspect_preview_contract"' in owner
    assert 'const PLAN_METHOD := &"plan_preview_revision"' in owner
    assert owner.count("_preview_snapshot.capture({})") == 1
    assert owner.count("_preview_actions.apply_revision({") == 1
    assert "ResourceSaver" not in owner
    assert "save_scene" not in owner


def test_project_preview_planner_runtime_contract_is_atomic_and_read_only(tmp_path: Path) -> None:
    executable = (
        os.environ.get("GODOT_BIN")
        or shutil.which("godot")
        or (r"C:\Tools\Godot\Godot.exe" if Path(r"C:\Tools\Godot\Godot.exe").is_file() else None)
    )
    if not executable:
        pytest.skip("Godot is required for the project preview planner runtime harness")

    project = tmp_path / "planner_project"
    actions = project / "addons/aura_bridge/actions"
    actions.mkdir(parents=True)
    shutil.copyfile(
        "aura/godot_editor/addon/actions/project_preview_planner.gd",
        actions / "project_preview_planner.gd",
    )
    (project / "project.godot").write_text(
        'config_version=5\n\n[aura]\neditor_bridge/preview_planner="res://fake_adapter.gd"\n',
        encoding="utf-8",
    )
    (project / "fake_adapter.gd").write_text(
        """extends RefCounted

func preview_planner_interface() -> Dictionary:
    return {"version": 1, "capabilities": ["inspect_contract", "plan_revision"]}

func inspect_preview_contract(request: Dictionary):
    return {"ok": true, "kind": "contract", "snapshot_count": request["snapshot"]["instances"].size()}

func plan_preview_revision(request: Dictionary):
    var operation := str(request.get("operations", [{}])[0].get("operation", ""))
    if operation == "reject":
        return {"ok": false, "error": "semantic rejection", "diagnostic": {"field": "wall", "valid_candidates": ["court_east"]}}
    if operation == "bad_result":
        return []
    if operation == "missing_revision":
        return {"ok": true, "handles": ["court"]}
    return {"ok": true, "operations": [{"operation": "instantiate"}], "handles": ["court"], "diagnostics": [{"code": "semantic_summary"}]}
""",
        encoding="utf-8",
    )
    (project / "incompatible_adapter.gd").write_text(
        """extends RefCounted

func preview_planner_interface() -> Dictionary:
    return {"version": 1, "capabilities": ["inspect_contract", "plan_revision"]}
""",
        encoding="utf-8",
    )
    (project / "test_planner.gd").write_text(
        """extends SceneTree

const Planner = preload("res://addons/aura_bridge/actions/project_preview_planner.gd")

class FakeSnapshot:
    extends RefCounted
    var calls := 0
    func capture(_params: Dictionary) -> Dictionary:
        calls += 1
        return {"ok": true, "result": {"instances": [{"name": "existing"}]}}

class FakeActions:
    extends RefCounted
    var calls := 0
    var last_label := ""
    func apply_revision(params: Dictionary) -> Dictionary:
        calls += 1
        last_label = str(params.get("label", ""))
        return {"ok": true, "result": {"applied": true, "operation_count": params["operations"].size(), "added_paths": ["AuraPreview/new"]}}

func _require(value: bool, message: String) -> bool:
    if value:
        return true
    push_error(message)
    quit(1)
    return false

func _init() -> void:
    var snapshot := FakeSnapshot.new()
    var actions := FakeActions.new()
    var planner = Planner.new(snapshot, actions)

    var inspected: Dictionary = planner.inspect_contract({})
    if not _require(inspected.get("ok", false), "contract inspection failed"): return
    if not _require(inspected["result"]["snapshot_count"] == 1, "contract did not receive snapshot"): return
    if not _require(snapshot.calls == 1 and actions.calls == 0, "contract inspection mutated preview"): return

    var built: Dictionary = planner.plan_and_apply({"label": "One semantic revision", "request": {"operations": [{"operation": "build"}]}})
    if not _require(built.get("ok", false) and built["result"].get("ok", false), "semantic build failed"): return
    if not _require(snapshot.calls == 2 and actions.calls == 1, "semantic build did not snapshot and apply once"): return
    if not _require(actions.last_label == "One semantic revision", "UndoRedo label was not preserved"): return
    if not _require(built["result"]["handles"] == ["court"], "semantic summary was lost"): return
    if not _require(built["result"]["revision"]["operation_count"] == 1, "revision result was lost"): return

    var rejected: Dictionary = planner.plan_and_apply({"request": {"operations": [{"operation": "reject"}]}})
    if not _require(rejected.get("ok", false) and not rejected["result"].get("ok", true), "semantic rejection was not returned"): return
    if not _require(rejected["result"]["diagnostic"]["valid_candidates"] == ["court_east"], "structured diagnostic was lost"): return
    if not _require(actions.calls == 1, "rejected semantic plan was applied"): return

    var malformed: Dictionary = planner.plan_and_apply({"request": {"operations": [{"operation": "bad_result"}]}})
    if not _require(not malformed.get("ok", true) and "dictionary" in malformed.get("error", ""), "non-dictionary result was accepted"): return
    var no_revision: Dictionary = planner.plan_and_apply({"request": {"operations": [{"operation": "missing_revision"}]}})
    if not _require(not no_revision.get("ok", true) and "operations array" in no_revision.get("error", ""), "missing revision was accepted"): return
    if not _require(actions.calls == 1, "malformed plan was applied"): return

    ProjectSettings.set_setting("aura/editor_bridge/preview_planner", "res://incompatible_adapter.gd")
    var incompatible: Dictionary = Planner.new(snapshot, actions).inspect_contract({})
    if not _require(not incompatible.get("ok", true) and "incompatible" in incompatible.get("error", ""), "incompatible adapter did not fail clearly"): return
    ProjectSettings.set_setting("aura/editor_bridge/preview_planner", null)
    var missing: Dictionary = Planner.new(snapshot, actions).inspect_contract({})
    if not _require(not missing.get("ok", true) and "not declared" in missing.get("error", ""), "missing adapter did not fail clearly"): return
    quit(0)
""",
        encoding="utf-8",
    )

    completed = subprocess.run(
        [str(executable), "--headless", "--path", str(project), "--script", "res://test_planner.gd"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_preview_capture_metadata_shape_is_rejected_by_client_when_malformed(tmp_path: Path) -> None:
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

    def serve() -> None:
        with server:
            peer, _address = server.accept()
            with peer:
                wire = b""
                while not wire.endswith(b"\n"):
                    wire += peer.recv(4096)
                request = json.loads(wire)
                response = {
                    "ok": True,
                    "request_id": request["request_id"],
                    "result": "not-a-dict",
                }
                peer.sendall(json.dumps(response).encode() + b"\n")

    thread = threading.Thread(target=serve)
    thread.start()
    with pytest.raises(GodotEditorBridgeError, match="invalid result"):
        GodotEditorBridgeClient(root).request("preview.capture", {})
    thread.join(timeout=2)


def test_viewport_capture_gdscript_patterns() -> None:
    """Verify the GDScript uses correct Godot 4 APIs, not invalid ones."""
    gd_path = Path("aura/godot_editor/addon/perception/viewport_capture.gd")
    content = gd_path.read_text(encoding="utf-8")
    # Must use VisualInstance3D for bounds (not bare Node3D cast)
    assert "VisualInstance3D" in content, "Must use VisualInstance3D for aabb bounds"
    assert "vi.get_aabb()" in content, "Must use VisualInstance3D.get_aabb() for local bounds"
    # Must use camera.global_transform for controlled capture (not set_camera_transform)
    assert "camera.global_transform" in content, "Must use Camera3D.global_transform for camera manipulation"
    # Must NOT use set_camera_transform (not available on SubViewport)
    assert "set_camera_transform" not in content, "SubViewport has no set_camera_transform method"
    # Must compute scene_fingerprint from live preview state (not file content)
    assert "scene_file_path" in content, "Fingerprint must reference scene_file_path"
    # Must have robust fallback for non-VisualInstance3D nodes
    assert "global_position" in content, "Fallback bounds must use global_position"


def test_controlled_capture_waits_for_a_completed_render_before_readback() -> None:
    content = Path("aura/godot_editor/addon/perception/viewport_capture.gd").read_text(
        encoding="utf-8"
    )

    arm = content.index("func _arm_next_view")
    connect_pre_draw = content.index("RenderingServer.frame_pre_draw.connect", arm)
    pre_draw = content.index("func _on_frame_pre_draw", connect_pre_draw)
    apply_camera = content.index("_apply_controlled_camera(camera, mode)", pre_draw)
    connect_post_draw = content.index("RenderingServer.frame_post_draw.connect", apply_camera)
    callback = content.index("func _on_frame_post_draw", connect_post_draw)
    readback = content.index("texture.get_image()", callback)

    assert connect_pre_draw < pre_draw < apply_camera < connect_post_draw < callback < readback
    assert "RenderingServer.force_draw" not in content
    assert 'call_deferred("_arm_next_view")' in content
    assert "viewport.render_target_update_mode = SubViewport.UPDATE_ONCE" in content
    assert '"post_draws_remaining"] = 2' in content
    assert "_restore_viewport_update_mode()" in content


def test_controlled_views_have_distinct_camera_intent_and_stale_frame_guard() -> None:
    content = Path("aura/godot_editor/addon/perception/viewport_capture.gd").read_text(
        encoding="utf-8"
    )

    assert "Camera3D.PROJECTION_PERSPECTIVE" in content
    assert "Vector3(1.0, 0.8, 1.0).normalized()" in content
    assert "Camera3D.PROJECTION_ORTHOGONAL" in content
    assert "Vector3.UP *" in content
    assert 'capture_entry["camera_transform"]' in content
    assert 'capture_entry["camera_projection"]' in content
    assert 'hashes.has(digest)' in content
    assert "repeated the rendered frame" in content


def test_controlled_capture_restores_complete_camera_state_on_success_and_error() -> None:
    content = Path("aura/godot_editor/addon/perception/viewport_capture.gd").read_text(
        encoding="utf-8"
    )
    snapshot = content[content.index("func _snapshot_camera") : content.index("func _finish_success")]
    for property_name in (
        "global_transform",
        "projection",
        "fov",
        "size",
        "near",
        "far",
        "keep_aspect",
        "frustum_offset",
        "h_offset",
        "v_offset",
    ):
        assert f'"{property_name}"' in snapshot
        assert f'camera.{property_name} = state["{property_name}"]' in snapshot

    post_draw = content[content.index("func _on_frame_post_draw") : content.index("func _acquire_rendered_view")]
    error_finish = content[content.index("func _finish_with_error") : content.index("func _compute_preview_bounds")]
    assert post_draw.index("_acquire_rendered_view") < post_draw.index("_restore_controlled_camera")
    assert "_restore_controlled_camera()" in error_finish
    assert 'capture_entry["camera_state_restored"] = camera_restored' in content
    assert "failed to restore the editor camera" in content


def test_bridge_holds_async_capture_request_until_render_job_finishes() -> None:
    server = Path("aura/godot_editor/addon/transport/bridge_server.gd").read_text(
        encoding="utf-8"
    )
    router = Path("aura/godot_editor/addon/protocol/request_router.gd").read_text(
        encoding="utf-8"
    )

    assert 'client["pending_id"]' in server
    assert "_router.poll_pending" in server
    assert "_router.cancel_pending" in server
    assert "func poll_pending" in router
    assert "func cancel_pending" in router


def test_preview_publish_scene_is_routed_through_preview_actions() -> None:
    router = Path("aura/godot_editor/addon/protocol/request_router.gd").read_text(
        encoding="utf-8"
    )
    actions = Path("aura/godot_editor/addon/actions/asset_preview_actions.gd").read_text(
        encoding="utf-8"
    )

    assert '"preview.publish_scene"' in router
    assert "_asset_preview_actions.publish_scene(params)" in router
    assert "func publish_scene(params: Dictionary)" in actions
    assert "PackedScene.new()" in actions
    assert "ResourceSaver.save(packed, resource_path)" in actions
    assert "save_scene" not in actions


def test_viewport_capture_rejects_malformed_width() -> None:
    """GDScript-level: string width should return type error (verified via content pattern)."""
    gd_path = Path("aura/godot_editor/addon/perception/viewport_capture.gd")
    content = gd_path.read_text(encoding="utf-8")
    assert 'raw_width != null and not (raw_width is int or raw_width is float)' in content or \
           'raw_width is int or raw_width is float' in content, \
        "Must type-check width before clamping"


def test_viewport_capture_rejects_non_string_capture_id() -> None:
    """GDScript-level: non-string capture_set_id should return type error."""
    gd_path = Path("aura/godot_editor/addon/perception/viewport_capture.gd")
    content = gd_path.read_text(encoding="utf-8")
    assert 'capture_set_id must be a string' in content, \
        "Must reject non-string capture_set_id with clear error"


def test_viewport_capture_fingerprint_uses_live_state() -> None:
    """Scene fingerprint must use live child data, not saved file content."""
    gd_path = Path("aura/godot_editor/addon/perception/viewport_capture.gd")
    content = gd_path.read_text(encoding="utf-8")
    # Must sort child data for deterministic fingerprint
    assert "parts.sort()" in content, "Must sort child data for deterministic fingerprint"
    # Must iterate preview children for the fingerprint (not file I/O)
    assert "preview.get_child_count" in content, "Fingerprint must check preview child count"
    assert "preview.get_children()" in content, "Fingerprint must iterate preview children"


def test_capture_tool_rejects_bridge_offline(tmp_path: Path) -> None:
    """capture_godot_asset_preview returns ok=False when bridge is offline."""
    registry = ToolRegistry(tmp_path, mode="planner")
    result = registry.execute(
        "capture_godot_asset_preview",
        {},
        lambda _request: ApprovalDecision("approve"),
    )
    assert result.ok is False
    assert "error" in result.payload
    assert "not installed" in str(result.payload["error"]).lower() or \
           "bridge is not installed" in str(result.payload["error"]).lower()


def test_inspect_godot_api_uses_live_classdb_route(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="planner")
    with patch("aura.conversation.tools._godot_editor_mixin.GodotEditorBridgeClient") as client_type:
        client_type.return_value.request.return_value = {
            "mode": "class",
            "class_name": "Node3D",
            "methods": [{"name": "to_local", "arguments": []}],
            "properties": [],
            "signals": [],
            "integer_constants": [],
            "enums": [],
        }
        result = registry.execute(
            "inspect_godot_api",
            {"class_name": "Node3D", "member_query": "local"},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    assert result.payload["source"] == "live_godot_classdb"
    client_type.return_value.request.assert_called_once_with(
        "api.describe",
        {
            "class_name": "Node3D",
            "member_query": "local",
            "include_inherited": False,
            "max_items": 50,
        },
    )


def test_inspect_godot_api_falls_back_to_configured_executable(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="planner")
    with (
        patch("aura.conversation.tools._godot_editor_mixin.GodotEditorBridgeClient") as client_type,
        patch(
            "aura.conversation.tools._godot_editor_mixin.query_godot_api_offline",
            return_value={"mode": "class_search", "engine_classes": ["Node3D"]},
        ) as offline_query,
    ):
        client_type.return_value.request.side_effect = GodotEditorBridgeError("offline")
        result = registry.execute(
            "inspect_godot_api",
            {"member_query": "node3d", "max_items": 10},
            lambda _request: ApprovalDecision("approve"),
        )

    assert result.ok is True
    assert result.payload["source"] == "configured_godot_classdb"
    offline_query.assert_called_once()
