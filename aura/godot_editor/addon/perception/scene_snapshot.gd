@tool
extends RefCounted

const DEFAULT_MAX_NODES := 500
const MAX_VALUE_CHARS := 1200

var _editor_interface: EditorInterface


func _init(editor_interface: EditorInterface) -> void:
	_editor_interface = editor_interface


func capture(params: Dictionary) -> Dictionary:
	var root := _editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": true, "result": {"scene_open": false, "nodes": [], "selected": []}}
	var include_properties := bool(params.get("include_properties", true))
	var max_nodes := clampi(int(params.get("max_nodes", DEFAULT_MAX_NODES)), 1, 2000)
	var selected_nodes := _editor_interface.get_selection().get_selected_nodes()
	var selected_ids: Dictionary = {}
	var selected_paths: Array[String] = []
	for selected in selected_nodes:
		selected_ids[selected.get_instance_id()] = true
		selected_paths.append(str(root.get_path_to(selected)))

	var nodes: Array[Dictionary] = []
	var queue: Array[Node] = [root]
	while not queue.is_empty() and nodes.size() < max_nodes:
		var node := queue.pop_front()
		nodes.append(_describe_node(root, node, selected_ids, include_properties))
		for child in node.get_children():
			queue.append(child)
	return {
		"ok": true,
		"result": {
			"scene_open": true,
			"scene_path": root.scene_file_path,
			"root_type": root.get_class(),
			"selected": selected_paths,
			"nodes": nodes,
			"truncated": not queue.is_empty(),
		},
	}


func _describe_node(root: Node, node: Node, selected_ids: Dictionary, include_properties: bool) -> Dictionary:
	var path := str(root.get_path_to(node))
	var owner_path = null
	if node.owner != null and (root.is_ancestor_of(node.owner) or node.owner == root):
		owner_path = str(root.get_path_to(node.owner))
	var result := {
		"path": path,
		"name": str(node.name),
		"type": node.get_class(),
		"script": node.get_script().resource_path if node.get_script() != null else "",
		"owner": owner_path,
		"child_count": node.get_child_count(),
		"selected": selected_ids.has(node.get_instance_id()),
	}
	if node is Node3D:
		result["transform"] = _encode_value(node.transform)
	elif node is Node2D:
		result["transform"] = _encode_value(node.transform)
	if include_properties:
		result["properties"] = _editor_properties(node)
	return result


func _editor_properties(node: Node) -> Dictionary:
	var properties: Dictionary = {}
	for descriptor in node.get_property_list():
		var usage := int(descriptor.get("usage", 0))
		var name := str(descriptor.get("name", ""))
		if name.is_empty() or usage & PROPERTY_USAGE_EDITOR == 0:
			continue
		if usage & (PROPERTY_USAGE_CATEGORY | PROPERTY_USAGE_GROUP | PROPERTY_USAGE_SUBGROUP) != 0:
			continue
		properties[name] = _encode_value(node.get(name))
	return properties


func _encode_value(value: Variant) -> Dictionary:
	var text := var_to_str(value)
	if text.length() > MAX_VALUE_CHARS:
		text = text.left(MAX_VALUE_CHARS) + "…"
	return {"type": type_string(typeof(value)), "value_text": text}
