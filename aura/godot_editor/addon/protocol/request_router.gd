@tool
extends RefCounted

const SceneActions = preload("../actions/scene_actions.gd")
const SceneSnapshot = preload("../perception/scene_snapshot.gd")
const AssetPreviewActions = preload("../actions/asset_preview_actions.gd")
const AssetPreviewSnapshot = preload("../perception/asset_preview_snapshot.gd")
const ApiIntrospection = preload("../perception/api_introspection.gd")
const ViewportCapture = preload("../perception/viewport_capture.gd")

var _editor_interface: EditorInterface
var _snapshot
var _actions
var _asset_preview_actions
var _asset_preview_snapshot
var _api_introspection
var _viewport_capture


func _init(editor_interface: EditorInterface, undo_redo: EditorUndoRedoManager) -> void:
	_editor_interface = editor_interface
	_snapshot = SceneSnapshot.new(editor_interface)
	_actions = SceneActions.new(editor_interface, undo_redo)
	_asset_preview_actions = AssetPreviewActions.new(editor_interface, undo_redo)
	_asset_preview_snapshot = AssetPreviewSnapshot.new(editor_interface)
	_api_introspection = ApiIntrospection.new()
	_viewport_capture = ViewportCapture.new(editor_interface)


func dispatch(action: String, params: Dictionary) -> Dictionary:
	match action:
		"ping":
			return {"ok": true, "result": {
				"bridge": "aura-godot-editor",
				"protocol": 1,
				"bridge_version": 5,
				"capabilities": ["scene.snapshot", "scene.select", "scene.apply", "scene.save", "preview.snapshot", "preview.instantiate", "preview.clear", "preview.apply", "preview.capture", "api.describe"],
			}}
		"scene.snapshot":
			return _snapshot.capture(params)
		"scene.select":
			return _actions.select_nodes(params)
		"scene.apply":
			return _actions.apply_operations(params)
		"scene.save":
			var error := _editor_interface.save_scene()
			return {"ok": error == OK, "result": {"saved": error == OK}, "error": error_string(error)}
		"preview.snapshot":
			return _asset_preview_snapshot.capture(params)
		"preview.instantiate":
			return _asset_preview_actions.instantiate_assets(params)
		"preview.clear":
			return _asset_preview_actions.clear_preview(params)
		"preview.apply":
			return _asset_preview_actions.apply_revision(params)
		"preview.capture":
			return _viewport_capture.capture(params)
		"api.describe":
			return _api_introspection.describe(params)
		_:
			return {"ok": false, "error": "unsupported action: %s" % action}
