@tool
extends RefCounted

const PREVIEW_ROOT_NAME := "AuraPreview"

var names: Dictionary = {}
var nodes: Dictionary = {}
var targeted: Dictionary = {}
var discarded: Dictionary = {}
var reads: Dictionary = {}
var planned_paths: Dictionary = {}


func seed(preview: Node3D) -> void:
	if preview == null:
		return
	for child in preview.get_children():
		names[str(child.name)] = true
		nodes[PREVIEW_ROOT_NAME + "/" + str(child.name)] = child


func validate_access(path: String, action: String, index: int) -> Dictionary:
	if action in ["duplicate", "attach"]:
		if discarded.has(path):
			return _failure("operation %d cannot %s from a removed or replaced source: %s" % [index, action, path])
		if targeted.has(path):
			return _failure("operation %d cannot %s from a source already targeted for mutation: %s" % [index, action, path])
		reads[path] = true
	else:
		if targeted.has(path):
			return _failure("preview target appears more than once: %s" % path)
		if reads.has(path):
			return _failure("operation %d cannot mutate a path already read earlier in the batch: %s" % [index, path])
		targeted[path] = true
	return {"ok": true}


func register_checked(checked: Dictionary) -> void:
	var outputs: Array[Dictionary] = []
	if checked.has("operations"):
		outputs.assign(checked["operations"])
	else:
		var operation: Dictionary = checked["operation"]
		if operation.has("new_node"):
			outputs.append(operation)
	for operation in outputs:
		var node: Node3D = operation["new_node"]
		node.position = operation["new_position"]
		node.rotation_degrees = operation["new_rotation"]
		node.scale = operation["new_scale"]
		var output_path := str(operation.get("new_path", operation["path"]))
		nodes[output_path] = node
		planned_paths[output_path] = true
		targeted.erase(output_path)
		if operation["operation"] == "replace" and output_path != operation["path"]:
			nodes.erase(operation["path"])
	if not checked.has("operations"):
		var op: Dictionary = checked["operation"]
		if op["operation"] == "remove":
			discarded[op["path"]] = true


func resolve_reference(path: String) -> Dictionary:
	if discarded.has(path) and not planned_paths.has(path):
		return _failure("alignment reference was removed or replaced: %s" % path)
	if targeted.has(path) and not planned_paths.has(path):
		return _failure("alignment reference was already targeted for mutation: %s" % path)
	var node: Variant = nodes.get(path)
	if node == null or not node is Node3D:
		return _failure("alignment reference does not exist (forward references are not allowed): %s" % path)
	if not planned_paths.has(path):
		reads[path] = true
	return {"ok": true, "node": node}


func _failure(message: String) -> Dictionary:
	return {"ok": false, "error": message}
