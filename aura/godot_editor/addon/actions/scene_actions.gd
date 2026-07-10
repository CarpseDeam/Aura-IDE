@tool
extends RefCounted

var _editor_interface: EditorInterface
var _undo_redo: EditorUndoRedoManager


func _init(editor_interface: EditorInterface, undo_redo: EditorUndoRedoManager) -> void:
	_editor_interface = editor_interface
	_undo_redo = undo_redo


func select_nodes(params: Dictionary) -> Dictionary:
	var root := _editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "error": "no scene is open"}
	var paths = params.get("paths", [])
	if not paths is Array:
		return {"ok": false, "error": "paths must be an array"}
	var nodes: Array[Node] = []
	for raw_path in paths:
		var found := _find_node(root, str(raw_path))
		if found == null:
			return {"ok": false, "error": "node does not exist: %s" % raw_path}
		nodes.append(found)
	var selection := _editor_interface.get_selection()
	selection.clear()
	for node in nodes:
		selection.add_node(node)
	if nodes.size() == 1:
		_editor_interface.edit_node(nodes[0])
	return {"ok": true, "result": {"selected": paths}}


func apply_operations(params: Dictionary) -> Dictionary:
	var root := _editor_interface.get_edited_scene_root()
	if root == null:
		return {"ok": false, "error": "no scene is open"}
	var operations = params.get("operations", [])
	if not operations is Array or operations.is_empty():
		return {"ok": false, "error": "operations must be a non-empty array"}
	var prepared: Array[Dictionary] = []
	for operation in operations:
		if not operation is Dictionary:
			return {"ok": false, "error": "each operation must be an object"}
		var checked := _prepare_operation(root, operation)
		if not checked.get("ok", false):
			return checked
		prepared.append(checked["operation"])

	_undo_redo.create_action(str(params.get("label", "Aura scene edit")), UndoRedo.MERGE_DISABLE, root)
	var changed: Array[String] = []
	for operation in prepared:
		if operation["action"] == "set_property":
			var node: Node = operation["node"]
			_undo_redo.add_do_property(node, operation["property"], operation["value"])
			_undo_redo.add_undo_property(node, operation["property"], node.get(operation["property"]))
			changed.append(operation["path"])
		elif operation["action"] == "create_node":
			var parent: Node = operation["parent"]
			var created: Node = operation["node"]
			_undo_redo.add_do_method(parent, "add_child", created, true)
			_undo_redo.add_do_property(created, "owner", root)
			for property in operation["properties"]:
				_undo_redo.add_do_property(created, property, operation["properties"][property])
			_undo_redo.add_do_reference(created)
			_undo_redo.add_undo_method(parent, "remove_child", created)
			changed.append(operation["path"])
	_undo_redo.commit_action()
	return {"ok": true, "result": {"applied": true, "changed_nodes": changed, "operation_count": prepared.size()}}


func _prepare_operation(root: Node, operation: Dictionary) -> Dictionary:
	var action := str(operation.get("action", ""))
	if action == "set_property":
		var path := str(operation.get("node_path", ""))
		var node := _find_node(root, path)
		if node == null:
			return {"ok": false, "error": "node does not exist: %s" % path}
		var property := str(operation.get("property", ""))
		if not _has_property(node, property):
			return {"ok": false, "error": "property does not exist on %s: %s" % [path, property]}
		var decoded := _decode_value(operation.get("value_text", ""))
		if not decoded.get("ok", false):
			return decoded
		return {"ok": true, "operation": {"action": action, "path": path, "node": node, "property": property, "value": decoded["value"]}}
	if action == "create_node":
		var parent_path := str(operation.get("parent", "."))
		var parent := _find_node(root, parent_path)
		if parent == null:
			return {"ok": false, "error": "parent node does not exist: %s" % parent_path}
		var type_name := str(operation.get("type", ""))
		if not ClassDB.class_exists(type_name) or not ClassDB.is_parent_class(type_name, "Node") or not ClassDB.can_instantiate(type_name):
			return {"ok": false, "error": "type is not an instantiable Node: %s" % type_name}
		var name := str(operation.get("name", ""))
		if name.is_empty() or name.validate_node_name() != name:
			return {"ok": false, "error": "invalid or empty node name: %s" % name}
		if parent.has_node(NodePath(name)):
			return {"ok": false, "error": "node already exists beneath %s: %s" % [parent_path, name]}
		var created = ClassDB.instantiate(type_name)
		created.name = name
		var decoded_properties: Dictionary = {}
		var properties = operation.get("properties", {})
		if not properties is Dictionary:
			return {"ok": false, "error": "create_node properties must be an object"}
		for property in properties:
			if not _has_property(created, str(property)):
				return {"ok": false, "error": "property does not exist on %s: %s" % [type_name, property]}
			var decoded := _decode_value(properties[property])
			if not decoded.get("ok", false):
				return decoded
			decoded_properties[property] = decoded["value"]
		var path := name if parent_path == "." else parent_path + "/" + name
		return {"ok": true, "operation": {"action": action, "path": path, "parent": parent, "node": created, "properties": decoded_properties}}
	return {"ok": false, "error": "unsupported scene operation: %s" % action}


func _find_node(root: Node, path: String) -> Node:
	if path == "." or path.is_empty():
		return root
	return root.get_node_or_null(NodePath(path))


func _has_property(object: Object, property: String) -> bool:
	for descriptor in object.get_property_list():
		if str(descriptor.get("name", "")) == property:
			return true
	return false


func _decode_value(value_text: Variant) -> Dictionary:
	if not value_text is String or value_text.is_empty():
		return {"ok": false, "error": "value_text must be a non-empty Godot Variant string"}
	var value = str_to_var(value_text)
	if value == null and value_text.strip_edges() != "null":
		return {"ok": false, "error": "value_text is not a valid Godot Variant: %s" % value_text}
	return {"ok": true, "value": value}
