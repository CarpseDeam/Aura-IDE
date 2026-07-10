@tool
extends RefCounted

const PREVIEW_ROOT_NAME := "AuraPreview"
const MAX_INSTANCES := 64

var _editor_interface: EditorInterface
var _undo_redo: EditorUndoRedoManager


func _init(editor_interface: EditorInterface, undo_redo: EditorUndoRedoManager) -> void:
	_editor_interface = editor_interface
	_undo_redo = undo_redo


func instantiate_assets(params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}
	if not scene_root is Node3D:
		return {"ok": false, "error": "asset preview requires a Node3D scene root"}
	var placements = params.get("placements", [])
	if not placements is Array or placements.is_empty() or placements.size() > MAX_INSTANCES:
		return {"ok": false, "error": "placements must contain between 1 and %d items" % MAX_INSTANCES}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview != null and (not preview is Node3D or not bool(preview.get_meta("aura_preview_root", false))):
		return {"ok": false, "error": "%s already exists but is not an Aura preview root" % PREVIEW_ROOT_NAME}

	var prepared: Array[Dictionary] = []
	var names: Dictionary = {}
	if preview != null:
		for child in preview.get_children():
			names[str(child.name)] = true
	for index in placements.size():
		var checked := _prepare_placement(placements[index], index, names)
		if not checked.get("ok", false):
			return checked
		prepared.append(checked["placement"])

	_undo_redo.create_action(str(params.get("label", "Aura place catalog assets")), UndoRedo.MERGE_DISABLE, scene_root)
	if preview == null:
		preview = Node3D.new()
		preview.name = PREVIEW_ROOT_NAME
		preview.set_meta("aura_preview_root", true)
		_undo_redo.add_do_method(scene_root, "add_child", preview, true)
		_undo_redo.add_do_property(preview, "owner", scene_root)
		_undo_redo.add_do_reference(preview)
		_undo_redo.add_undo_method(scene_root, "remove_child", preview)
	var paths: Array[String] = []
	for placement in prepared:
		var instance: Node3D = placement["instance"]
		_undo_redo.add_do_method(preview, "add_child", instance, true)
		_undo_redo.add_do_property(instance, "owner", scene_root)
		_undo_redo.add_do_property(instance, "position", placement["position"])
		_undo_redo.add_do_property(instance, "rotation_degrees", Vector3(0.0, placement["rotation_y"], 0.0))
		_undo_redo.add_do_property(instance, "scale", placement["scale"])
		_undo_redo.add_do_reference(instance)
		_undo_redo.add_undo_method(preview, "remove_child", instance)
		paths.append(PREVIEW_ROOT_NAME + "/" + str(instance.name))
	_undo_redo.commit_action()
	return {"ok": true, "result": {"applied": true, "preview_root": PREVIEW_ROOT_NAME, "instance_paths": paths, "instance_count": paths.size()}}


func clear_preview(params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview == null:
		return {"ok": true, "result": {"applied": false, "preview_root": PREVIEW_ROOT_NAME, "removed_count": 0}}
	if not bool(preview.get_meta("aura_preview_root", false)):
		return {"ok": false, "error": "%s is not an Aura preview root" % PREVIEW_ROOT_NAME}
	var children := preview.get_children()
	if children.is_empty():
		return {"ok": true, "result": {"applied": false, "preview_root": PREVIEW_ROOT_NAME, "removed_count": 0}}
	_undo_redo.create_action(str(params.get("label", "Aura clear asset preview")), UndoRedo.MERGE_DISABLE, scene_root)
	for child in children:
		_undo_redo.add_do_method(preview, "remove_child", child)
		_undo_redo.add_undo_method(preview, "add_child", child, true)
		_undo_redo.add_undo_property(child, "owner", scene_root)
	_undo_redo.commit_action()
	return {"ok": true, "result": {"applied": true, "preview_root": PREVIEW_ROOT_NAME, "removed_count": children.size()}}


func _prepare_placement(raw: Variant, index: int, names: Dictionary) -> Dictionary:
	if not raw is Dictionary:
		return {"ok": false, "error": "placement %d must be an object" % index}
	var resource_path := str(raw.get("resource_path", ""))
	if not resource_path.begins_with("res://") or not resource_path.ends_with(".tscn") or resource_path.contains(".."):
		return {"ok": false, "error": "placement %d has an unsafe scene resource path" % index}
	if not ResourceLoader.exists(resource_path, "PackedScene"):
		return {"ok": false, "error": "catalog scene does not exist: %s" % resource_path}
	var packed := ResourceLoader.load(resource_path, "PackedScene") as PackedScene
	if packed == null:
		return {"ok": false, "error": "catalog resource is not a PackedScene: %s" % resource_path}
	var instance := packed.instantiate()
	if not instance is Node3D:
		instance.free()
		return {"ok": false, "error": "catalog scene root must inherit Node3D: %s" % resource_path}
	var node_name := str(raw.get("name", "")).strip_edges()
	if node_name.is_empty():
		node_name = "Asset_%02d" % (index + 1)
	if node_name.validate_node_name() != node_name or names.has(node_name):
		instance.free()
		return {"ok": false, "error": "placement name is invalid or duplicated: %s" % node_name}
	instance.name = node_name
	names[node_name] = true
	var position: Variant = _vector3(raw.get("position", []), Vector3.ZERO)
	var scale: Variant = _vector3(raw.get("scale", []), Vector3.ONE)
	var rotation_y: Variant = raw.get("rotation_degrees_y", 0.0)
	if position == null or scale == null or (not rotation_y is float and not rotation_y is int):
		instance.free()
		return {"ok": false, "error": "placement %d transform is invalid" % index}
	if absf(position.x) > 10000.0 or absf(position.y) > 10000.0 or absf(position.z) > 10000.0:
		instance.free()
		return {"ok": false, "error": "placement %d exceeds the 10 km preview bound" % index}
	if scale.x < 0.01 or scale.y < 0.01 or scale.z < 0.01 or scale.x > 100.0 or scale.y > 100.0 or scale.z > 100.0:
		instance.free()
		return {"ok": false, "error": "placement %d scale must be between 0.01 and 100" % index}
	return {"ok": true, "placement": {"instance": instance, "position": position, "rotation_y": float(rotation_y), "scale": scale}}


func _vector3(raw: Variant, default_value: Vector3) -> Variant:
	if raw is Array and raw.is_empty():
		return default_value
	if not raw is Array or raw.size() != 3:
		return null
	for value in raw:
		if not value is float and not value is int:
			return null
	return Vector3(float(raw[0]), float(raw[1]), float(raw[2]))
