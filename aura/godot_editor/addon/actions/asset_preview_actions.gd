@tool
extends RefCounted

const PreviewAlignment = preload("preview_alignment.gd")
const PreviewRevisionState = preload("preview_revision_state.gd")

const PREVIEW_ROOT_NAME := "AuraPreview"
const MAX_INITIAL_PLACEMENTS := 64
const MAX_REVISION_OPERATIONS := 128
const MAX_TOTAL_INSTANCES := 1024
const MAX_DUPLICATE_COUNT := 64
const MAX_SEMANTIC_REVISION_OPERATIONS := 1024

var _editor_interface: EditorInterface
var _undo_redo: EditorUndoRedoManager
var _alignment := PreviewAlignment.new()


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
	if not placements is Array or placements.is_empty() or placements.size() > MAX_INITIAL_PLACEMENTS:
		return {"ok": false, "error": "placements must contain between 1 and %d items" % MAX_INITIAL_PLACEMENTS}
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
	if names.size() > MAX_TOTAL_INSTANCES:
		_free_prepared_placements(prepared)
		return {"ok": false, "error": "placement would exceed the %d-instance preview limit" % MAX_TOTAL_INSTANCES}

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
	return {"ok": true, "result": {
		"applied": true,
		"preview_root": PREVIEW_ROOT_NAME,
		"request_operation_count": placements.size(),
		"instance_paths": paths,
		"added_paths": paths,
		"changed_paths": [],
		"removed_paths": [],
		"replaced_paths": [],
		"instance_count": names.size(),
	}}


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
	var removed_paths: Array[String] = []
	for child in children:
		removed_paths.append(PREVIEW_ROOT_NAME + "/" + str(child.name))
	_undo_redo.create_action(str(params.get("label", "Aura clear asset preview")), UndoRedo.MERGE_DISABLE, scene_root)
	for child in children:
		_undo_redo.add_do_method(preview, "remove_child", child)
		_undo_redo.add_undo_method(preview, "add_child", child, true)
		_undo_redo.add_undo_property(child, "owner", scene_root)
	_undo_redo.commit_action()
	return {"ok": true, "result": {
		"applied": true,
		"preview_root": PREVIEW_ROOT_NAME,
		"request_operation_count": children.size(),
		"removed_count": children.size(),
		"removed_paths": removed_paths,
		"added_paths": [],
		"changed_paths": [],
		"replaced_paths": [],
		"instance_count": 0,
	}}


func publish_scene(params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}
	if not scene_root is Node3D:
		return {"ok": false, "error": "scene publishing requires a Node3D scene root"}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview == null or not preview is Node3D or not bool(preview.get_meta("aura_preview_root", false)):
		return {"ok": false, "error": "a valid Aura-owned %s root is required" % PREVIEW_ROOT_NAME}
	return _publish_preview(scene_root, preview, params)


func _publish_preview(_scene_root: Node3D, preview: Node3D, params: Dictionary) -> Dictionary:
	var raw_path: Variant = params.get("path")
	if not raw_path is String:
		return {"ok": false, "error": "path is required and must be a string"}
	var resource_path := str(raw_path)
	if not _is_normalized_scene_path(resource_path):
		return {"ok": false, "error": "path must be a normalized res:// path ending in .tscn"}
	var raw_overwrite: Variant = params.get("overwrite", false)
	if not raw_overwrite is bool:
		return {"ok": false, "error": "overwrite must be a boolean"}
	if FileAccess.file_exists(resource_path) and not raw_overwrite:
		return {"ok": false, "error": "destination already exists; set overwrite to true: %s" % resource_path}
	if preview.get_child_count() == 0:
		return {"ok": false, "error": "%s is empty" % PREVIEW_ROOT_NAME}

	var checked_name := _published_root_name(params, resource_path)
	if not checked_name.get("ok", false):
		return checked_name
	var published_root := Node3D.new()
	published_root.name = checked_name["root_name"]
	for child in preview.get_children():
		var copied := child.duplicate(
			Node.DUPLICATE_SIGNALS
			| Node.DUPLICATE_GROUPS
			| Node.DUPLICATE_SCRIPTS
			| Node.DUPLICATE_USE_INSTANTIATION
		)
		if copied == null:
			published_root.free()
			return {"ok": false, "error": "failed to copy preview child: %s" % child.name}
		published_root.add_child(copied)
		copied.owner = published_root
		_assign_missing_owners(copied, published_root)

	var packed := PackedScene.new()
	var pack_error := packed.pack(published_root)
	if pack_error != OK:
		published_root.free()
		return {"ok": false, "error": "failed to pack published scene: %s" % error_string(pack_error)}
	var directory_error := DirAccess.make_dir_recursive_absolute(
		ProjectSettings.globalize_path(resource_path.get_base_dir())
	)
	if directory_error != OK and directory_error != ERR_ALREADY_EXISTS:
		published_root.free()
		return {"ok": false, "error": "failed to create destination directory: %s" % error_string(directory_error)}
	var save_error := ResourceSaver.save(packed, resource_path)
	var piece_count := preview.get_child_count()
	var root_name := str(published_root.name)
	published_root.free()
	if save_error != OK:
		return {"ok": false, "error": "failed to save published scene: %s" % error_string(save_error)}
	return {"ok": true, "result": {
		"path": resource_path,
		"root_name": root_name,
		"piece_count": piece_count,
	}}


func _is_normalized_scene_path(path: String) -> bool:
	return (
		not path.is_empty()
		and path.begins_with("res://")
		and path.ends_with(".tscn")
		and not path.contains("\\")
		and path == path.simplify_path()
		and path.get_file() != ".tscn"
	)


func _published_root_name(params: Dictionary, resource_path: String) -> Dictionary:
	var raw_name: Variant = params.get("root_name", resource_path.get_file().get_basename())
	if not raw_name is String:
		return {"ok": false, "error": "root_name must be a string"}
	var root_name := str(raw_name).strip_edges()
	if root_name.is_empty() or root_name.validate_node_name() != root_name:
		return {"ok": false, "error": "root_name must be a valid non-empty Godot node name"}
	return {"ok": true, "root_name": root_name}


func _assign_missing_owners(node: Node, published_root: Node) -> void:
	for child in node.get_children(true):
		if child.owner == null:
			child.owner = published_root
		_assign_missing_owners(child, published_root)


func apply_revision(params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}
	if not scene_root is Node3D:
		return {"ok": false, "error": "asset preview requires a Node3D scene root"}
	var operations = params.get("operations", [])
	var operation_limit := (
		MAX_SEMANTIC_REVISION_OPERATIONS
		if bool(params.get("semantic_builder", false))
		else MAX_REVISION_OPERATIONS
	)
	if not operations is Array or operations.is_empty() or operations.size() > operation_limit:
		return {"ok": false, "error": "operations must contain between 1 and %d items" % operation_limit}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview != null and (not preview is Node3D or not bool(preview.get_meta("aura_preview_root", false))):
		return {"ok": false, "error": "%s already exists but is not an Aura preview root" % PREVIEW_ROOT_NAME}

	var state := PreviewRevisionState.new()
	state.seed(preview)
	var prepared: Array[Dictionary] = []
	var creates_preview := preview == null
	for index in operations.size():
		var raw: Variant = operations[index]
		if raw is Dictionary:
			var raw_action := str(raw.get("operation", ""))
			var raw_path := str(raw.get("node_path", ""))
			if state.planned_paths.has(raw_path) and raw_action not in ["duplicate", "attach"]:
				_free_prepared_instances(prepared)
				return {"ok": false, "error": "operation %d cannot %s an unattached planned node: %s" % [index, raw_action, raw_path]}
		var checked := _prepare_revision_operation(raw, index, state)
		if not checked.get("ok", false):
			_free_prepared_instances(prepared)
			return checked
		if checked.has("operations"):
			prepared.append_array(checked["operations"])
		else:
			prepared.append(checked["operation"])
		state.register_checked(checked)
	if state.names.size() > MAX_TOTAL_INSTANCES:
		_free_prepared_instances(prepared)
		return {"ok": false, "error": "revision would exceed the %d-instance preview limit" % MAX_TOTAL_INSTANCES}
	if creates_preview:
		for operation in prepared:
			if operation["operation"] != "instantiate":
				_free_prepared_instances(prepared)
				return {"ok": false, "error": "only instantiate can create a missing AuraPreview root"}
		preview = Node3D.new()
		preview.name = PREVIEW_ROOT_NAME
		preview.set_meta("aura_preview_root", true)

	_undo_redo.create_action(str(params.get("label", "Aura revise asset preview")), UndoRedo.MERGE_DISABLE, scene_root)
	_undo_redo.add_do_method(self, "_execute_revision", scene_root, preview, prepared, creates_preview, true)
	_undo_redo.add_undo_method(self, "_execute_revision", scene_root, preview, prepared, creates_preview, false)
	for operation in prepared:
		if operation.has("new_node"):
			_undo_redo.add_do_reference(operation["new_node"])
	if creates_preview:
		_undo_redo.add_do_reference(preview)
	_undo_redo.commit_action()

	var changed: Array[String] = []
	var added: Array[String] = []
	var removed: Array[String] = []
	var replaced: Array[String] = []
	var alignment_facts: Array[Dictionary] = []
	for operation in prepared:
		match operation["operation"]:
			"set_transform": changed.append(operation["path"])
			"instantiate": added.append(operation["path"])
			"remove": removed.append(operation["path"])
			"replace": replaced.append(str(operation.get("new_path", operation["path"])))
		if operation.has("alignment_fact"):
			var fact: Dictionary = operation["alignment_fact"].duplicate(true)
			fact["path"] = str(operation.get("new_path", operation["path"]))
			alignment_facts.append(fact)
	return {"ok": true, "result": {
		"applied": true,
		"preview_root": PREVIEW_ROOT_NAME,
		"request_operation_count": operations.size(),
		"operation_count": prepared.size(),
		"changed_paths": changed,
		"added_paths": added,
		"removed_paths": removed,
		"replaced_paths": replaced,
		"instance_count": state.names.size(),
		"alignment_facts": alignment_facts,
	}}


func _prepare_revision_operation(
	raw: Variant,
	index: int,
	state_or_names,
	nodes: Dictionary = {},
	targeted: Dictionary = {},
	discarded: Dictionary = {},
	reads: Dictionary = {},
) -> Dictionary:
	var state = state_or_names
	if state_or_names is Dictionary:
		state = PreviewRevisionState.new()
		state.names = state_or_names
		state.nodes = nodes
		state.targeted = targeted
		state.discarded = discarded
		state.reads = reads
	if not raw is Dictionary:
		return {"ok": false, "error": "operation %d must be an object" % index}
	var action := str(raw.get("operation", ""))
	if action == "instantiate":
		var placement_raw: Dictionary = raw.duplicate(true)
		var alignment_fact: Dictionary = {}
		if raw.has("relative_to"):
			var reference_checked: Dictionary = state.resolve_reference(str(raw["relative_to"].get("node_path", "")))
			if not reference_checked.get("ok", false):
				return reference_checked
			var aligned: Dictionary = _alignment.calculate(reference_checked["node"], float(raw.get("rotation_degrees_y", 0.0)), _vector3(raw.get("scale", []), Vector3.ONE), raw["relative_to"], raw.get("_alignment_geometry"))
			if not aligned.get("ok", false):
				return aligned
			placement_raw["position"] = _vector_array(aligned["position"])
			alignment_fact = aligned["fact"]
		var placed := _prepare_placement(placement_raw, index, state.names)
		if not placed.get("ok", false):
			return placed
		var placement: Dictionary = placed["placement"]
		var instantiate_operation := {
			"operation": action,
			"path": PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name),
			"new_node": placement["instance"],
			"new_position": placement["position"],
			"new_rotation": Vector3(0.0, placement["rotation_y"], 0.0),
			"new_scale": placement["scale"],
		}
		if raw.has("relative_to"):
			instantiate_operation["alignment_fact"] = alignment_fact
			instantiate_operation["alignment_fact"]["reference_path"] = str(raw["relative_to"]["node_path"])
		return {"ok": true, "operation": instantiate_operation}

	var path := str(raw.get("node_path", ""))
	if not path.begins_with(PREVIEW_ROOT_NAME + "/") or path.count("/") != 1:
		return {"ok": false, "error": "operation %d must target one direct preview child" % index}
	var access_checked: Dictionary = state.validate_access(path, action, index)
	if not access_checked.get("ok", false):
		return access_checked
	var target: Node = state.nodes.get(path)
	if target == null or not target is Node3D:
		return {"ok": false, "error": "preview target does not exist or is not Node3D: %s" % path}
	var target_3d := target as Node3D
	if action == "duplicate":
		return _prepare_duplicate_operation(raw, index, target_3d, state.names)
	if action == "attach":
		return _prepare_attach_operation(raw, index, target_3d, state.names)
	var base := {
		"operation": action,
		"path": path,
		"old_node": target_3d,
		"old_owner": target_3d.owner,
		"old_index": target_3d.get_index(),
		"old_position": target_3d.position,
		"old_rotation": target_3d.rotation_degrees,
		"old_scale": target_3d.scale,
	}
	if action == "remove":
		state.names.erase(str(target_3d.name))
		state.discarded[path] = true
		return {"ok": true, "operation": base}
	if action == "set_transform":
		var transform := _revision_transform(raw, target_3d.position, target_3d.rotation_degrees.y, target_3d.scale)
		if not transform.get("ok", false):
			return transform
		if raw.has("relative_to"):
			var reference_checked: Dictionary = state.resolve_reference(str(raw["relative_to"].get("node_path", "")))
			if not reference_checked.get("ok", false):
				return reference_checked
			var aligned: Dictionary = _alignment.calculate(reference_checked["node"], transform["transform"]["new_rotation"].y, transform["transform"]["new_scale"], raw["relative_to"], raw.get("_alignment_geometry"))
			if not aligned.get("ok", false):
				return aligned
			transform["transform"]["new_position"] = aligned["position"]
			base["alignment_fact"] = aligned["fact"]
			base["alignment_fact"]["reference_path"] = str(raw["relative_to"]["node_path"])
		base.merge(transform["transform"])
		return {"ok": true, "operation": base}
	if action == "replace":
		state.names.erase(str(target_3d.name))
		state.discarded[path] = true
		var replacement_raw: Dictionary = raw.duplicate()
		if str(replacement_raw.get("name", "")).is_empty():
			replacement_raw["name"] = str(target_3d.name)
		if not replacement_raw.has("position") and not replacement_raw.has("relative_to"):
			replacement_raw["position"] = [target_3d.position.x, target_3d.position.y, target_3d.position.z]
		if not replacement_raw.has("rotation_degrees_y"):
			replacement_raw["rotation_degrees_y"] = target_3d.rotation_degrees.y
		if not replacement_raw.has("scale"):
			replacement_raw["scale"] = [target_3d.scale.x, target_3d.scale.y, target_3d.scale.z]
		if replacement_raw.has("relative_to"):
			var reference_checked: Dictionary = state.resolve_reference(str(replacement_raw["relative_to"].get("node_path", "")))
			if not reference_checked.get("ok", false):
				return reference_checked
			var replacement_scale: Variant = _vector3(replacement_raw.get("scale", []), target_3d.scale)
			var aligned: Dictionary = _alignment.calculate(reference_checked["node"], float(replacement_raw["rotation_degrees_y"]), replacement_scale, replacement_raw["relative_to"], replacement_raw.get("_alignment_geometry"))
			if not aligned.get("ok", false):
				return aligned
			replacement_raw["position"] = _vector_array(aligned["position"])
			base["alignment_fact"] = aligned["fact"]
			base["alignment_fact"]["reference_path"] = str(replacement_raw["relative_to"]["node_path"])
		var replaced := _prepare_placement(replacement_raw, index, state.names)
		if not replaced.get("ok", false):
			state.names[str(target_3d.name)] = true
			return replaced
		var placement: Dictionary = replaced["placement"]
		base["new_node"] = placement["instance"]
		base["new_position"] = placement["position"]
		base["new_rotation"] = Vector3(0.0, placement["rotation_y"], 0.0)
		base["new_scale"] = placement["scale"]
		base["new_path"] = PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name)
		return {"ok": true, "operation": base}
	return {"ok": false, "error": "unsupported revision operation: %s" % action}


func _register_prepared_outputs(
	checked: Dictionary,
	nodes: Dictionary,
	targeted: Dictionary,
	planned_paths: Dictionary,
	discarded: Dictionary,
) -> void:
	# Compatibility boundary for focused bridge harnesses; production owns one state object.
	var state := PreviewRevisionState.new()
	state.nodes = nodes
	state.targeted = targeted
	state.planned_paths = planned_paths
	state.discarded = discarded
	state.register_checked(checked)


func _prepare_duplicate_operation(
	raw: Dictionary,
	index: int,
	source: Node3D,
	names: Dictionary,
) -> Dictionary:
	var count: Variant = raw.get("count")
	if not count is int or count < 1 or count > MAX_DUPLICATE_COUNT:
		return {"ok": false, "error": "duplicate count must be between 1 and %d" % MAX_DUPLICATE_COUNT}
	var has_offset := raw.has("offset")
	var has_alignment := raw.has("alignment_step")
	if has_offset == has_alignment:
		return {"ok": false, "error": "duplicate requires exactly one of offset or alignment_step"}
	var offset: Variant = _vector3(raw.get("offset", null), Vector3.ZERO) if has_offset else Vector3.ZERO
	if has_offset and offset == null:
		return {"ok": false, "error": "duplicate offset must contain three numbers"}
	var offset_space := str(raw.get("offset_space", "local"))
	if has_offset and offset_space != "local" and offset_space != "world":
		return {"ok": false, "error": "duplicate offset_space must be local or world"}
	var requested_name := str(raw.get("name", "")).strip_edges()
	if count > 1 and not requested_name.is_empty():
		return {"ok": false, "error": "duplicate name is allowed only when count is 1"}
	var resource_path := str(raw.get("resource_path", ""))
	if resource_path.is_empty() or source.scene_file_path != resource_path:
		return {"ok": false, "error": "duplicate source is not the prepared catalog asset: %s" % source.name}

	var position_step: Vector3 = offset
	if has_offset and offset_space == "local":
		position_step = source.transform.basis.orthonormalized() * offset
	var output_names: Variant = raw.get("_output_names", [])
	if not output_names is Array or (not output_names.is_empty() and output_names.size() != count):
		return {"ok": false, "error": "duplicate planned output names are invalid"}
	var duplicated: Array[Dictionary] = []
	var previous := source
	for copy_index in count:
		var copy_name := requested_name
		if not output_names.is_empty():
			copy_name = str(output_names[copy_index])
		if copy_name.is_empty():
			copy_name = _next_duplicate_name(str(source.name), names)
		var next_position := source.position + position_step * float(copy_index + 1)
		var alignment_fact: Dictionary = {}
		if has_alignment:
			var aligned := _alignment.calculate(previous, source.rotation_degrees.y, source.scale, raw["alignment_step"], raw.get("_alignment_geometry"))
			if not aligned.get("ok", false):
				_free_prepared_instances(duplicated)
				return aligned
			next_position = aligned["position"]
			alignment_fact = aligned["fact"]
			alignment_fact["reference_path"] = PREVIEW_ROOT_NAME + "/" + str(previous.name)
		var placement_raw := {
			"resource_path": resource_path,
			"name": copy_name,
			"position": [
				next_position.x, next_position.y, next_position.z,
			],
			"rotation_degrees_y": source.rotation_degrees.y,
			"scale": [source.scale.x, source.scale.y, source.scale.z],
		}
		var placed := _prepare_placement(placement_raw, index, names)
		if not placed.get("ok", false):
			_free_prepared_instances(duplicated)
			return placed
		var placement: Dictionary = placed["placement"]
		var duplicate_operation := {
			"operation": "instantiate",
			"path": PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name),
			"new_node": placement["instance"],
			"new_position": placement["position"],
			"new_rotation": Vector3(0.0, placement["rotation_y"], 0.0),
			"new_scale": placement["scale"],
		}
		if has_alignment:
			duplicate_operation["alignment_fact"] = alignment_fact
		duplicated.append(duplicate_operation)
		previous = placement["instance"]
		previous.position = placement["position"]
		previous.rotation_degrees = Vector3(0.0, placement["rotation_y"], 0.0)
		previous.scale = placement["scale"]
	return {"ok": true, "operations": duplicated}


func _next_duplicate_name(source_name: String, names: Dictionary) -> String:
	var suffix := 1
	var candidate := "%s_copy_%02d" % [source_name, suffix]
	while names.has(candidate):
		suffix += 1
		candidate = "%s_copy_%02d" % [source_name, suffix]
	return candidate


func _prepare_attach_operation(
	raw: Dictionary,
	index: int,
	source: Node3D,
	names: Dictionary,
) -> Dictionary:
	var source_resource_path := str(raw.get("source_resource_path", ""))
	var source_identity := str(raw.get("source_catalog_identity", ""))
	if source_identity.is_empty() or source_resource_path.is_empty() or source.scene_file_path != source_resource_path:
		return {"ok": false, "error": "attach source is not the prepared catalog asset: %s" % source.name}
	var target_identity := str(raw.get("catalog_identity", ""))
	var target_asset_id := str(raw.get("asset_id", ""))
	if target_identity.is_empty() or target_asset_id.is_empty():
		return {"ok": false, "error": "attach target catalog identity is missing"}
	var source_socket_position: Variant = _vector3(raw.get("source_socket_position", null), Vector3.ZERO)
	var source_socket_facing: Variant = _vector3(raw.get("source_socket_facing", null), Vector3.ZERO)
	var target_socket_position: Variant = _vector3(raw.get("target_socket_position", null), Vector3.ZERO)
	var target_socket_facing: Variant = _vector3(raw.get("target_socket_facing", null), Vector3.ZERO)
	var target_scale: Variant = _vector3(raw.get("scale", []), Vector3.ONE)
	if (
		source_socket_position == null
		or source_socket_facing == null
		or target_socket_position == null
		or target_socket_facing == null
		or target_scale == null
	):
		return {"ok": false, "error": "attach socket vectors or scale are invalid"}
	if (
		not source_socket_position.is_finite()
		or not source_socket_facing.is_finite()
		or not target_socket_position.is_finite()
		or not target_socket_facing.is_finite()
		or not target_scale.is_finite()
	):
		return {"ok": false, "error": "attach socket vectors or scale must be finite"}
	var source_horizontal := Vector3(source_socket_facing.x, 0.0, source_socket_facing.z)
	var target_horizontal := Vector3(target_socket_facing.x, 0.0, target_socket_facing.z)
	if source_horizontal.length_squared() < 0.000000000001 or target_horizontal.length_squared() < 0.000000000001:
		return {"ok": false, "error": "attach socket facing must have a usable horizontal component"}

	var source_socket_transform := source.transform
	var source_socket_point: Vector3 = source_socket_transform * source_socket_position
	var source_facing_parent := source_socket_transform.basis.orthonormalized() * source_horizontal.normalized()
	var desired_target_facing := -Vector3(source_facing_parent.x, 0.0, source_facing_parent.z).normalized()
	var normalized_target_facing := target_horizontal.normalized()
	var source_heading := atan2(-desired_target_facing.z, desired_target_facing.x)
	var target_heading := atan2(-normalized_target_facing.z, normalized_target_facing.x)
	var target_yaw := wrapf(source_heading - target_heading, -PI, PI)
	var target_basis := Basis(Vector3.UP, target_yaw)
	var target_socket_transform := Transform3D(target_basis.scaled_local(target_scale), Vector3.ZERO)
	var target_socket_offset: Vector3 = target_socket_transform * target_socket_position
	var target_position: Vector3 = source_socket_point - target_socket_offset
	var target_rotation_degrees := rad_to_deg(target_yaw)
	if is_zero_approx(target_rotation_degrees):
		target_rotation_degrees = 0.0
	var requested_name := str(raw.get("name", "")).strip_edges()
	if requested_name.is_empty():
		requested_name = _next_asset_name(target_asset_id, names)
	var placement_raw := {
		"resource_path": str(raw.get("resource_path", "")),
		"name": requested_name,
		"position": [target_position.x, target_position.y, target_position.z],
		"rotation_degrees_y": target_rotation_degrees,
		"scale": [target_scale.x, target_scale.y, target_scale.z],
		"allowed_rotations_deg": raw.get("allowed_rotations_deg", []),
	}
	var placed := _prepare_placement(placement_raw, index, names)
	if not placed.get("ok", false):
		return placed
	var placement: Dictionary = placed["placement"]
	return {"ok": true, "operation": {
		"operation": "instantiate",
		"path": PREVIEW_ROOT_NAME + "/" + str(placement["instance"].name),
		"new_node": placement["instance"],
		"new_position": placement["position"],
		"new_rotation": Vector3(0.0, placement["rotation_y"], 0.0),
		"new_scale": placement["scale"],
	}}


func _next_asset_name(asset_id: String, names: Dictionary) -> String:
	var stem := asset_id.validate_node_name()
	if stem.is_empty():
		stem = "Asset"
	var suffix := 1
	var candidate := "%s_%02d" % [stem, suffix]
	while names.has(candidate):
		suffix += 1
		candidate = "%s_%02d" % [stem, suffix]
	return candidate


func _revision_transform(raw: Dictionary, position: Vector3, rotation_y: float, scale: Vector3) -> Dictionary:
	var decoded_position: Variant = _vector3(raw.get("position", []), position)
	var decoded_scale: Variant = _vector3(raw.get("scale", []), scale)
	var decoded_rotation: Variant = raw.get("rotation_degrees_y", rotation_y)
	if decoded_position == null or decoded_scale == null or (not decoded_rotation is int and not decoded_rotation is float):
		return {"ok": false, "error": "revision transform is invalid"}
	if not decoded_position.is_finite() or not decoded_scale.is_finite() or not is_finite(float(decoded_rotation)):
		return {"ok": false, "error": "revision transform must be finite"}
	if absf(decoded_position.x) > 10000.0 or absf(decoded_position.y) > 10000.0 or absf(decoded_position.z) > 10000.0:
		return {"ok": false, "error": "revision position exceeds the 10 km preview bound"}
	if decoded_scale.x < 0.01 or decoded_scale.y < 0.01 or decoded_scale.z < 0.01 or decoded_scale.x > 100.0 or decoded_scale.y > 100.0 or decoded_scale.z > 100.0:
		return {"ok": false, "error": "revision scale must be between 0.01 and 100"}
	var allowed_rotations: Variant = raw.get("allowed_rotations_deg", [])
	if not allowed_rotations is Array or not _yaw_is_allowed(float(decoded_rotation), allowed_rotations):
		return {"ok": false, "error": "revision rotation is not catalog-approved"}
	return {"ok": true, "transform": {
		"new_position": decoded_position,
		"new_rotation": Vector3(0.0, float(decoded_rotation), 0.0),
		"new_scale": decoded_scale,
	}}


func _execute_revision(
	scene_root: Node,
	preview: Node3D,
	operations: Array[Dictionary],
	creates_preview: bool,
	forward: bool,
) -> void:
	if forward:
		if creates_preview and preview.get_parent() == null:
			scene_root.add_child(preview, true)
			preview.owner = scene_root
		for operation in operations:
			_apply_prepared_operation(scene_root, preview, operation, true)
	else:
		for index in range(operations.size() - 1, -1, -1):
			_apply_prepared_operation(scene_root, preview, operations[index], false)
		if creates_preview and preview.get_parent() == scene_root:
			scene_root.remove_child(preview)


func _apply_prepared_operation(scene_root: Node, preview: Node3D, operation: Dictionary, forward: bool) -> void:
	var action: String = operation["operation"]
	if action == "set_transform":
		var node: Node3D = operation["old_node"]
		node.position = operation["new_position"] if forward else operation["old_position"]
		node.rotation_degrees = operation["new_rotation"] if forward else operation["old_rotation"]
		node.scale = operation["new_scale"] if forward else operation["old_scale"]
	elif action == "instantiate":
		var node: Node3D = operation["new_node"]
		if forward:
			node.position = operation["new_position"]
			node.rotation_degrees = operation["new_rotation"]
			node.scale = operation["new_scale"]
			_mount_owned_child(preview, node, scene_root)
		elif node.get_parent() == preview:
			_detach_owned_child(preview, node)
	elif action == "remove":
		var node: Node3D = operation["old_node"]
		if forward and node.get_parent() == preview:
			_detach_owned_child(preview, node)
		elif not forward:
			_mount_owned_child(preview, node, operation["old_owner"], operation["old_index"])
	elif action == "replace":
		var old_node: Node3D = operation["old_node"]
		var new_node: Node3D = operation["new_node"]
		if forward:
			new_node.position = operation["new_position"]
			new_node.rotation_degrees = operation["new_rotation"]
			new_node.scale = operation["new_scale"]
			if old_node.get_parent() == preview:
				_detach_owned_child(preview, old_node)
			_mount_owned_child(preview, new_node, scene_root, operation["old_index"])
		else:
			if new_node.get_parent() == preview:
				_detach_owned_child(preview, new_node)
			_mount_owned_child(preview, old_node, operation["old_owner"], operation["old_index"])


func _detach_owned_child(parent: Node, child: Node) -> void:
	if child.get_parent() != parent:
		return
	child.owner = null
	parent.remove_child(child)


func _mount_owned_child(parent: Node, child: Node, child_owner: Node, index: int = -1) -> void:
	if child.get_parent() != parent:
		child.owner = null
		parent.add_child(child, true)
	if index >= 0:
		parent.move_child(child, mini(index, parent.get_child_count() - 1))
	child.owner = child_owner


func _free_prepared_instances(prepared: Array[Dictionary]) -> void:
	for operation in prepared:
		if operation.has("new_node"):
			var node: Node = operation["new_node"]
			if is_instance_valid(node) and node.get_parent() == null:
				node.free()


func _free_prepared_placements(prepared: Array[Dictionary]) -> void:
	for placement in prepared:
		var node: Node = placement.get("instance")
		if node != null and is_instance_valid(node) and node.get_parent() == null:
			node.free()


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
	if not position.is_finite() or not scale.is_finite() or not is_finite(float(rotation_y)):
		instance.free()
		return {"ok": false, "error": "placement %d transform must be finite" % index}
	if absf(position.x) > 10000.0 or absf(position.y) > 10000.0 or absf(position.z) > 10000.0:
		instance.free()
		return {"ok": false, "error": "placement %d exceeds the 10 km preview bound" % index}
	if scale.x < 0.01 or scale.y < 0.01 or scale.z < 0.01 or scale.x > 100.0 or scale.y > 100.0 or scale.z > 100.0:
		instance.free()
		return {"ok": false, "error": "placement %d scale must be between 0.01 and 100" % index}
	var allowed_rotations: Variant = raw.get("allowed_rotations_deg", [])
	if not allowed_rotations is Array:
		instance.free()
		return {"ok": false, "error": "placement %d allowed rotations are invalid" % index}
	if not allowed_rotations.is_empty():
		if not _yaw_is_allowed(float(rotation_y), allowed_rotations):
			instance.free()
			return {"ok": false, "error": "placement %d rotation is not catalog-approved" % index}
	return {"ok": true, "placement": {"instance": instance, "position": position, "rotation_y": float(rotation_y), "scale": scale}}


func _yaw_is_allowed(rotation_y: float, allowed_rotations: Array) -> bool:
	if allowed_rotations.is_empty():
		return true
	for allowed in allowed_rotations:
		if (allowed is int or allowed is float) and is_finite(float(allowed)) and is_equal_approx(fposmod(rotation_y, 360.0), fposmod(float(allowed), 360.0)):
			return true
	return false


func _vector3(raw: Variant, default_value: Vector3) -> Variant:
	if raw is Array and raw.is_empty():
		return default_value
	if not raw is Array or raw.size() != 3:
		return null
	for value in raw:
		if not value is float and not value is int:
			return null
	return Vector3(float(raw[0]), float(raw[1]), float(raw[2]))


func _vector_array(value: Vector3) -> Array[float]:
	return [value.x, value.y, value.z]
