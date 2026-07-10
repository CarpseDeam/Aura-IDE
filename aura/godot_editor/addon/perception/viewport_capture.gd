@tool
extends RefCounted

const PREVIEW_ROOT_NAME := "AuraPreview"

var _editor_interface: EditorInterface


func _init(editor_interface: EditorInterface) -> void:
	_editor_interface = editor_interface


func capture(params: Dictionary) -> Dictionary:
	# Validate scene is open
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}

	# Validate AuraPreview Node3D child exists
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview == null or not preview is Node3D:
		return {"ok": false, "error": "AuraPreview not found or not a Node3D"}

	# Validate capture_set_id (type-checked)
	var raw_id := params.get("capture_set_id")
	if raw_id != null and not (raw_id is String):
		return {"ok": false, "error": "capture_set_id must be a string"}
	var capture_set_id := str(raw_id if raw_id != null else "").strip_edges()
	if capture_set_id.is_empty():
		capture_set_id = str(Time.get_unix_time_from_system()).replace(".", "_")
	if capture_set_id.contains("..") or capture_set_id.contains("/") or capture_set_id.contains("\\"):
		return {"ok": false, "error": "invalid capture_set_id"}

	# Validate dimensions (type-checked, clamped)
	var raw_width := params.get("width", 1280)
	if raw_width != null and not (raw_width is int or raw_width is float):
		return {"ok": false, "error": "width must be a number"}
	var width := clampi(int(raw_width), 64, 1920)
	var raw_height := params.get("height", 720)
	if raw_height != null and not (raw_height is int or raw_height is float):
		return {"ok": false, "error": "height must be a number"}
	var height := clampi(int(raw_height), 64, 1080)

	# Validate modes (type-checked)
	var raw_modes := params.get("modes")
	if raw_modes == null:
		var modes: Array = ["current_editor"]
	elif raw_modes is Array:
		var modes: Array = raw_modes
	else:
		return {"ok": false, "error": "modes must be an array"}
	if modes.is_empty():
		modes = ["current_editor"]
	if modes.size() > 4:
		return {"ok": false, "error": "too many capture modes (max 4)"}
	var accepted_modes := {"current_editor": true, "overview": true, "top_down": true}
	for raw_mode in modes:
		var mode := str(raw_mode)
		if not accepted_modes.has(mode):
			return {"ok": false, "error": "unsupported capture mode: %s" % mode}

	# Create output directory
	var dir := DirAccess.open("res://")
	if dir == null:
		return {"ok": false, "error": "cannot create capture directory"}
	var dir_err := dir.make_dir_recursive(".aura/tmp/godot_previews/%s" % capture_set_id)
	if dir_err != OK:
		return {"ok": false, "error": "cannot create capture directory"}
	var out_prefix := "res://.aura/tmp/godot_previews/%s/" % capture_set_id

	# Get 3D editor viewport
	var viewport := _editor_interface.get_editor_viewport_3d()
	if viewport == null:
		return {"ok": false, "error": "no 3D editor viewport is available"}

	# Compute AuraPreview world-space bounds
	var preview_bounds := _compute_preview_bounds(preview)
	var preview_center := preview_bounds.get_center()
	var preview_size := preview_bounds.size
	var max_dim := maxf(preview_size.x, maxf(preview_size.y, preview_size.z))

	# Scene fingerprint (computed from live preview children, not saved file content)
	var scene_path := scene_root.scene_file_path
	var scene_fingerprint: int = 0
	if not scene_path.is_empty() and preview.get_child_count() > 0:
		var parts: Array[String] = []
		for child in preview.get_children():
			if not child is Node3D:
				continue
			var n: Node3D = child
			var name := n.name
			var rp := n.scene_file_path if n.scene_file_path else ""
			var pos_str := str(n.position)
			var rot_str := str(n.rotation_degrees)
			var scale_str := str(n.scale)
			parts.append("%s|%s|%s|%s|%s" % [name, rp, pos_str, rot_str, scale_str])
		parts.sort()
		var combined := scene_path + "::" + ";".join(parts)
		scene_fingerprint = hash(combined)

	var captures: Array[Dictionary] = []
	for raw_mode in modes:
		var mode := str(raw_mode)
		var capture_result := _capture_single(viewport, mode, capture_set_id, out_prefix, \
				width, height, preview_center, max_dim)
		if not capture_result.get("ok", false):
			return capture_result
		captures.append(capture_result["capture"])

	return {"ok": true, "result": {
		"capture_set_id": capture_set_id,
		"scene_path": scene_path,
		"scene_fingerprint": scene_fingerprint,
		"preview_bounds": {
			"min": [preview_bounds.position.x, preview_bounds.position.y, preview_bounds.position.z],
			"max": [preview_bounds.end.x, preview_bounds.end.y, preview_bounds.end.z],
			"size": [preview_size.x, preview_size.y, preview_size.z],
		},
		"captures": captures,
	}}


func _compute_preview_bounds(preview: Node3D) -> AABB:
	var bounds: AABB
	var first := true
	var stack: Array[Node] = preview.get_children()
	while stack.size() > 0:
		var node := stack.pop_back()
		if node is VisualInstance3D:
			var child_aabb := node.get_transformed_aabb()
			if first:
				bounds = child_aabb
				first = false
			else:
				bounds = bounds.merge(child_aabb)
		elif node is Node3D and not node is VisualInstance3D:
			var small_box := AABB(node.global_position - Vector3(0.1, 0.1, 0.1), Vector3(0.2, 0.2, 0.2))
			if first:
				bounds = small_box
				first = false
			else:
				bounds = bounds.merge(small_box)
		# Recurse into children
		for c in node.get_children():
			stack.append(c)
	if first:
		# No visualizable children — box at preview global position
		bounds = AABB(preview.global_position - Vector3(1, 1, 1), Vector3(2, 2, 2))
	return bounds


func _capture_single(viewport, mode: String, capture_set_id: String, out_prefix: String, \
		width: int, height: int, preview_center: Vector3, max_dim: float) -> Dictionary:
	var is_controlled := mode != "current_editor"
	var camera: Camera3D
	var stored_transform: Transform3D
	var new_transform: Transform3D

	if is_controlled:
		camera = viewport.get_camera_3d()
		if camera == null:
			return {"ok": false, "error": "no camera available for %s capture" % mode}
		stored_transform = camera.global_transform

		if mode == "overview":
			var eye := preview_center + Vector3(max_dim * 0.6, max_dim * 2.0 + 2.0, max_dim * 0.8)
			new_transform = Transform3D(Basis(), eye).looking_at(preview_center, Vector3.UP)
		elif mode == "top_down":
			var eye := preview_center + Vector3(0, max_dim * 2.0 + 2.0, 0)
			new_transform = Transform3D(Basis(), eye).looking_at(preview_center, Vector3.FORWARD)
		else:
			return {"ok": false, "error": "unsupported capture mode: %s" % mode}

		camera.global_transform = new_transform
		RenderingServer.force_draw()

	# Capture viewport texture
	var texture := viewport.get_texture()
	if texture == null:
		if is_controlled:
			camera.global_transform = stored_transform
		return {"ok": false, "error": "%s capture returned null texture" % mode}

	var image := texture.get_image()
	if image == null or image.is_empty():
		if is_controlled:
			camera.global_transform = stored_transform
		return {"ok": false, "error": "%s capture returned null image" % mode}

	# Resize if different from requested dimensions
	if image.get_width() != width or image.get_height() != height:
		image.resize(width, height, Image.INTERPOLATE_LANCZOS)

	# Save as PNG
	var file_name := mode + ".png"
	var file_path := out_prefix + file_name
	var save_err := image.save_png(file_path)
	if save_err != OK:
		if is_controlled:
			camera.global_transform = stored_transform
		return {"ok": false, "error": "failed to save capture"}

	# Restore camera
	if is_controlled:
		camera.global_transform = stored_transform

	# SHA-256 checksum
	var sha256 := FileAccess.get_sha256(file_path)

	# Build capture entry
	var capture_entry: Dictionary = {
		"view": mode,
		"path": out_prefix + file_name,
		"width": width,
		"height": height,
		"sha256": sha256,
	}

	if is_controlled:
		var basis := new_transform.basis
		capture_entry["camera_transform"] = {
			"origin": [new_transform.origin.x, new_transform.origin.y, new_transform.origin.z],
			"basis_x": [basis.x.x, basis.x.y, basis.x.z],
			"basis_y": [basis.y.x, basis.y.y, basis.y.z],
			"basis_z": [basis.z.x, basis.z.y, basis.z.z],
		}

	return {"ok": true, "capture": capture_entry}
