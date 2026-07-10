@tool
extends RefCounted

const SceneActions = preload("../actions/scene_actions.gd")
const SceneSnapshot = preload("../perception/scene_snapshot.gd")

var _editor_interface: EditorInterface
var _snapshot
var _actions


func _init(editor_interface: EditorInterface, undo_redo: EditorUndoRedoManager) -> void:
	_editor_interface = editor_interface
	_snapshot = SceneSnapshot.new(editor_interface)
	_actions = SceneActions.new(editor_interface, undo_redo)


func dispatch(action: String, params: Dictionary) -> Dictionary:
	match action:
		"ping":
			return {"ok": true, "result": {"bridge": "aura-godot-editor", "protocol": 1}}
		"scene.snapshot":
			return _snapshot.capture(params)
		"scene.select":
			return _actions.select_nodes(params)
		"scene.apply":
			return _actions.apply_operations(params)
		"scene.save":
			var error := _editor_interface.save_scene()
			return {"ok": error == OK, "result": {"saved": error == OK}, "error": error_string(error)}
		_:
			return {"ok": false, "error": "unsupported action: %s" % action}
