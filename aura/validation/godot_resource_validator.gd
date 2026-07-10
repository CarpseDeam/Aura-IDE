extends SceneTree


func _init() -> void:
	var failed := false
	for resource_path in OS.get_cmdline_user_args():
		if not resource_path.begins_with("res://"):
			push_error("Aura validator rejected non-project path: %s" % resource_path)
			failed = true
			continue
		var resource := ResourceLoader.load(resource_path)
		if resource == null:
			push_error("Aura could not load Godot resource: %s" % resource_path)
			failed = true
			continue
		if resource is PackedScene and not _validate_scene_types(resource, resource_path):
			failed = true
	quit(1 if failed else 0)


func _validate_scene_types(scene: PackedScene, resource_path: String) -> bool:
	var valid := true
	var state := scene.get_state()
	for node_index in range(state.get_node_count()):
		var node_type := state.get_node_type(node_index)
		if not node_type.is_empty() and not ClassDB.class_exists(node_type):
			push_error(
				"Aura found unknown node type %s in %s" % [node_type, resource_path]
			)
			valid = false
	return valid
