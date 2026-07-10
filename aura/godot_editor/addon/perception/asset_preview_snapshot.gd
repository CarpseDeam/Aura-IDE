@tool
extends RefCounted

const PREVIEW_ROOT_NAME := "AuraPreview"

var _editor_interface: EditorInterface


func _init(editor_interface: EditorInterface) -> void:
	_editor_interface = editor_interface


func capture(_params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": true, "result": {"scene_open": false, "preview_exists": false, "instances": [], "diagnostics": []}}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview == null:
		return {"ok": true, "result": {"scene_open": true, "scene_path": scene_root.scene_file_path, "preview_exists": false, "instances": [], "diagnostics": []}}
	if not preview is Node3D or not bool(preview.get_meta("aura_preview_root", false)):
		return {"ok": false, "error": "%s exists but is not an Aura preview root" % PREVIEW_ROOT_NAME}
	var instances: Array[Dictionary] = []
	var diagnostics: Array[Dictionary] = []
	for child in preview.get_children():
		if not child is Node3D:
			diagnostics.append({"severity": "warning", "code": "non_3d_preview_child", "path": PREVIEW_ROOT_NAME + "/" + str(child.name)})
			continue
		var resource_path := child.scene_file_path
		if resource_path.is_empty():
			diagnostics.append({"severity": "warning", "code": "non_scene_preview_child", "path": PREVIEW_ROOT_NAME + "/" + str(child.name)})
		instances.append({
			"path": PREVIEW_ROOT_NAME + "/" + str(child.name),
			"name": str(child.name),
			"resource_path": resource_path,
			"position": [child.position.x, child.position.y, child.position.z],
			"rotation_degrees": [child.rotation_degrees.x, child.rotation_degrees.y, child.rotation_degrees.z],
			"scale": [child.scale.x, child.scale.y, child.scale.z],
		})
	return {"ok": true, "result": {
		"scene_open": true,
		"scene_path": scene_root.scene_file_path,
		"preview_exists": true,
		"preview_root": PREVIEW_ROOT_NAME,
		"instance_count": instances.size(),
		"instances": instances,
		"diagnostics": diagnostics,
	}}
