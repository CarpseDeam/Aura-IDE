@tool
extends RefCounted

const PREVIEW_ROOT_NAME := "AuraPreview"
const CONTROLLED_MODES := {"overview": true, "top_down": true}

var _editor_interface: EditorInterface
var _active_job: Dictionary = {}
var _finished_jobs: Dictionary = {}
var _next_job_id := 1


func _init(editor_interface: EditorInterface) -> void:
	_editor_interface = editor_interface


func capture(params: Dictionary) -> Dictionary:
	if not _active_job.is_empty():
		return {"ok": false, "error": "another preview capture is already in progress"}
	var prepared := _prepare_capture(params)
	if not prepared.get("ok", false):
		return prepared
	var job: Dictionary = prepared["job"]
	job["id"] = _next_job_id
	_next_job_id += 1
	_active_job = job
	_arm_next_view()
	return {"ok": true, "pending": true, "pending_id": int(job["id"])}


func poll_capture(pending_id: int) -> Dictionary:
	if _finished_jobs.has(pending_id):
		var result: Dictionary = _finished_jobs[pending_id]
		_finished_jobs.erase(pending_id)
		return result
	if not _active_job.is_empty() and int(_active_job["id"]) == pending_id:
		return {"pending": true}
	return {"ok": false, "error": "preview capture request is no longer available"}


func cancel_capture(pending_id: int) -> void:
	if not _active_job.is_empty() and int(_active_job["id"]) == pending_id:
		_finish_with_error("preview capture client disconnected")
	_finished_jobs.erase(pending_id)


func _prepare_capture(params: Dictionary) -> Dictionary:
	var scene_root := _editor_interface.get_edited_scene_root()
	if scene_root == null:
		return {"ok": false, "error": "no scene is open"}
	var preview := scene_root.get_node_or_null(NodePath(PREVIEW_ROOT_NAME))
	if preview == null or not preview is Node3D:
		return {"ok": false, "error": "AuraPreview not found or not a Node3D"}

	var raw_id: Variant = params.get("capture_set_id")
	if raw_id != null and not (raw_id is String):
		return {"ok": false, "error": "capture_set_id must be a string"}
	var capture_set_id := str(raw_id if raw_id != null else "").strip_edges()
	if capture_set_id.is_empty():
		capture_set_id = str(Time.get_unix_time_from_system()).replace(".", "_")
	if capture_set_id.contains("..") or capture_set_id.contains("/") or capture_set_id.contains("\\"):
		return {"ok": false, "error": "invalid capture_set_id"}

	var raw_width: Variant = params.get("width", 1280)
	if raw_width != null and not (raw_width is int or raw_width is float):
		return {"ok": false, "error": "width must be a number"}
	var width := clampi(int(raw_width), 64, 1920)
	var raw_height: Variant = params.get("height", 720)
	if raw_height != null and not (raw_height is int or raw_height is float):
		return {"ok": false, "error": "height must be a number"}
	var height := clampi(int(raw_height), 64, 1080)

	var raw_modes: Variant = params.get("modes")
	if raw_modes != null and not raw_modes is Array:
		return {"ok": false, "error": "modes must be an array"}
	var modes: Array = ["current_editor"] if raw_modes == null else raw_modes
	if modes.is_empty():
		modes = ["current_editor"]
	if modes.size() > 4:
		return {"ok": false, "error": "too many capture modes (max 4)"}
	var accepted_modes := {"current_editor": true, "overview": true, "top_down": true}
	var seen_modes := {}
	for raw_mode in modes:
		var mode := str(raw_mode)
		if not accepted_modes.has(mode):
			return {"ok": false, "error": "unsupported capture mode: %s" % mode}
		if seen_modes.has(mode):
			return {"ok": false, "error": "duplicate capture mode: %s" % mode}
		seen_modes[mode] = true

	var dir := DirAccess.open("res://")
	if dir == null:
		return {"ok": false, "error": "cannot create capture directory"}
	var dir_err := dir.make_dir_recursive(".aura/tmp/godot_previews/%s" % capture_set_id)
	if dir_err != OK:
		return {"ok": false, "error": "cannot create capture directory"}
	var viewport := _editor_interface.get_editor_viewport_3d()
	if viewport == null:
		return {"ok": false, "error": "no 3D editor viewport is available"}

	var preview_bounds := _compute_preview_bounds(preview)
	var scene_path := scene_root.scene_file_path
	return {"ok": true, "job": {
		"viewport": viewport,
		"camera": viewport.get_camera_3d(),
		"modes": modes.duplicate(),
		"mode_index": 0,
		"captures": [],
		"controlled_hashes": {},
		"capture_set_id": capture_set_id,
		"out_prefix": "res://.aura/tmp/godot_previews/%s/" % capture_set_id,
		"width": width,
		"height": height,
		"preview_bounds": preview_bounds,
		"scene_path": scene_path,
		"scene_fingerprint": _scene_fingerprint(scene_root, preview),
		"camera_state": {},
		"viewport_update_mode": null,
		"post_draws_remaining": 1,
	}}


func _arm_next_view() -> void:
	if _active_job.is_empty():
		return
	var modes: Array = _active_job["modes"]
	var index := int(_active_job["mode_index"])
	if index >= modes.size():
		_finish_success()
		return
	var mode := str(modes[index])
	if CONTROLLED_MODES.has(mode):
		var camera: Camera3D = _active_job["camera"]
		if camera == null or not is_instance_valid(camera):
			_finish_with_error("no camera available for %s capture" % mode)
			return
		_active_job["camera_state"] = _snapshot_camera(camera)
		var pre_draw_callback := Callable(self, "_on_frame_pre_draw")
		if not RenderingServer.frame_pre_draw.is_connected(pre_draw_callback):
			RenderingServer.frame_pre_draw.connect(pre_draw_callback, CONNECT_ONE_SHOT)
		# Mutating the camera marks an otherwise-idle editor viewport dirty and
		# guarantees another render is scheduled. frame_pre_draw reapplies the
		# same state after the editor viewport controller has run.
		_apply_controlled_camera(camera, mode)
	else:
		var viewport: SubViewport = _active_job["viewport"]
		_active_job["viewport_update_mode"] = viewport.render_target_update_mode
		_active_job["post_draws_remaining"] = 2
		viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
		_connect_post_draw()


func _on_frame_pre_draw() -> void:
	if _active_job.is_empty():
		return
	var modes: Array = _active_job["modes"]
	var mode := str(modes[int(_active_job["mode_index"])])
	var camera: Camera3D = _active_job["camera"]
	if camera == null or not is_instance_valid(camera):
		_finish_with_error("no camera available for %s capture" % mode)
		return
	# The editor's 3D viewport controller writes projection properties during its
	# process step. Apply Aura's state at frame_pre_draw so those exact values are
	# the ones consumed by RenderingServer for the frame we read back.
	_apply_controlled_camera(camera, mode)
	_connect_post_draw()


func _connect_post_draw() -> void:
	var callback := Callable(self, "_on_frame_post_draw")
	if not RenderingServer.frame_post_draw.is_connected(callback):
		RenderingServer.frame_post_draw.connect(callback, CONNECT_ONE_SHOT)


func _on_frame_post_draw() -> void:
	if _active_job.is_empty():
		return
	var remaining := int(_active_job.get("post_draws_remaining", 1))
	if remaining > 1:
		_active_job["post_draws_remaining"] = remaining - 1
		var viewport: SubViewport = _active_job["viewport"]
		viewport.render_target_update_mode = SubViewport.UPDATE_ONCE
		_connect_post_draw()
		return
	var modes: Array = _active_job["modes"]
	var mode := str(modes[int(_active_job["mode_index"])])
	var result := _acquire_rendered_view(mode)
	var camera_restored := _restore_controlled_camera()
	_restore_viewport_update_mode()
	if not result.get("ok", false):
		_finish_with_error(str(result.get("error", "preview capture failed")))
		return
	var capture_entry: Dictionary = result["capture"]
	if CONTROLLED_MODES.has(mode):
		capture_entry["camera_state_restored"] = camera_restored
		if not camera_restored:
			_finish_with_error("failed to restore the editor camera after %s capture" % mode)
			return
		var digest := str(capture_entry["sha256"])
		var hashes: Dictionary = _active_job["controlled_hashes"]
		if hashes.has(digest):
			_finish_with_error(
				"controlled capture %s repeated the rendered frame from %s" % [mode, hashes[digest]]
			)
			return
		hashes[digest] = mode
	var captures: Array = _active_job["captures"]
	captures.append(capture_entry)
	_active_job["mode_index"] = int(_active_job["mode_index"]) + 1
	if int(_active_job["mode_index"]) >= modes.size():
		_finish_success()
	else:
		# Leave the RenderingServer callback before arming and dirtying the next
		# editor view. This keeps multi-mode scheduling on the editor's main loop.
		call_deferred("_arm_next_view")


func _acquire_rendered_view(mode: String) -> Dictionary:
	var viewport: SubViewport = _active_job["viewport"]
	var texture: ViewportTexture = viewport.get_texture()
	if texture == null:
		return {"ok": false, "error": "%s capture returned null texture" % mode}
	var image: Image = texture.get_image()
	if image == null or image.is_empty():
		return {"ok": false, "error": "%s capture returned null image" % mode}
	var width := int(_active_job["width"])
	var height := int(_active_job["height"])
	if image.get_width() != width or image.get_height() != height:
		image.resize(width, height, Image.INTERPOLATE_LANCZOS)
	var file_path := str(_active_job["out_prefix"]) + mode + ".png"
	var save_err: Error = image.save_png(file_path)
	if save_err != OK:
		return {"ok": false, "error": "failed to save %s capture" % mode}
	var capture_entry: Dictionary = {
		"view": mode,
		"path": file_path,
		"width": width,
		"height": height,
		"sha256": FileAccess.get_sha256(file_path),
		"render_frame": Engine.get_frames_drawn(),
	}
	if CONTROLLED_MODES.has(mode):
		var camera: Camera3D = _active_job["camera"]
		capture_entry["camera_transform"] = _transform_metadata(camera.global_transform)
		capture_entry["camera_projection"] = _projection_metadata(camera)
	return {"ok": true, "capture": capture_entry}


func _apply_controlled_camera(camera: Camera3D, mode: String) -> void:
	var bounds: AABB = _active_job["preview_bounds"]
	var center := bounds.get_center()
	var size := bounds.size
	var max_dim := maxf(size.x, maxf(size.y, size.z))
	camera.h_offset = 0.0
	camera.v_offset = 0.0
	camera.frustum_offset = Vector2.ZERO
	camera.keep_aspect = Camera3D.KEEP_HEIGHT
	if mode == "overview":
		camera.projection = Camera3D.PROJECTION_PERSPECTIVE
		camera.fov = 55.0
		camera.near = 0.05
		camera.far = maxf(4000.0, max_dim * 20.0 + 100.0)
		var half_extent := maxf(max_dim * 0.5, 0.5)
		var distance := half_extent / tan(deg_to_rad(camera.fov * 0.5)) * 1.35
		var direction := Vector3(1.0, 0.8, 1.0).normalized()
		camera.global_transform = Transform3D(Basis(), center + direction * distance).looking_at(center, Vector3.UP)
	else:
		camera.projection = Camera3D.PROJECTION_ORTHOGONAL
		var aspect := float(_active_job["width"]) / float(_active_job["height"])
		camera.size = maxf(maxf(size.x, size.z * aspect) * 1.2, 2.0)
		camera.near = 0.05
		camera.far = maxf(4000.0, max_dim * 20.0 + 100.0)
		var eye := center + Vector3.UP * (maxf(max_dim, 1.0) * 2.0 + 2.0)
		camera.global_transform = Transform3D(Basis(), eye).looking_at(center, Vector3.FORWARD)


func _snapshot_camera(camera: Camera3D) -> Dictionary:
	return {
		"global_transform": camera.global_transform,
		"projection": camera.projection,
		"fov": camera.fov,
		"size": camera.size,
		"near": camera.near,
		"far": camera.far,
		"keep_aspect": camera.keep_aspect,
		"frustum_offset": camera.frustum_offset,
		"h_offset": camera.h_offset,
		"v_offset": camera.v_offset,
	}


func _restore_controlled_camera() -> bool:
	if _active_job.is_empty() or (_active_job.get("camera_state", {}) as Dictionary).is_empty():
		return true
	var camera: Camera3D = _active_job["camera"]
	var state: Dictionary = _active_job["camera_state"]
	var restored := false
	if camera != null and is_instance_valid(camera):
		camera.projection = state["projection"]
		camera.fov = state["fov"]
		camera.size = state["size"]
		camera.near = state["near"]
		camera.far = state["far"]
		camera.keep_aspect = state["keep_aspect"]
		camera.frustum_offset = state["frustum_offset"]
		camera.h_offset = state["h_offset"]
		camera.v_offset = state["v_offset"]
		camera.global_transform = state["global_transform"]
		restored = (
			camera.global_transform == state["global_transform"]
			and camera.projection == state["projection"]
			and camera.fov == state["fov"]
			and camera.size == state["size"]
			and camera.near == state["near"]
			and camera.far == state["far"]
			and camera.keep_aspect == state["keep_aspect"]
			and camera.frustum_offset == state["frustum_offset"]
			and camera.h_offset == state["h_offset"]
			and camera.v_offset == state["v_offset"]
		)
	_active_job["camera_state"] = {}
	return restored


func _restore_viewport_update_mode() -> void:
	if _active_job.is_empty() or _active_job.get("viewport_update_mode") == null:
		return
	var viewport: SubViewport = _active_job["viewport"]
	if viewport != null and is_instance_valid(viewport):
		viewport.render_target_update_mode = int(_active_job["viewport_update_mode"])
	_active_job["viewport_update_mode"] = null


func _finish_success() -> void:
	var preview_bounds: AABB = _active_job["preview_bounds"]
	var preview_size := preview_bounds.size
	var result := {"ok": true, "result": {
		"capture_set_id": _active_job["capture_set_id"],
		"scene_path": _active_job["scene_path"],
		"scene_fingerprint": _active_job["scene_fingerprint"],
		"preview_bounds": {
			"min": [preview_bounds.position.x, preview_bounds.position.y, preview_bounds.position.z],
			"max": [preview_bounds.end.x, preview_bounds.end.y, preview_bounds.end.z],
			"size": [preview_size.x, preview_size.y, preview_size.z],
		},
		"captures": _active_job["captures"],
	}}
	var job_id := int(_active_job["id"])
	_active_job = {}
	_finished_jobs[job_id] = result


func _finish_with_error(message: String) -> void:
	if _active_job.is_empty():
		return
	_restore_controlled_camera()
	_restore_viewport_update_mode()
	var pre_draw_callback := Callable(self, "_on_frame_pre_draw")
	if RenderingServer.frame_pre_draw.is_connected(pre_draw_callback):
		RenderingServer.frame_pre_draw.disconnect(pre_draw_callback)
	var callback := Callable(self, "_on_frame_post_draw")
	if RenderingServer.frame_post_draw.is_connected(callback):
		RenderingServer.frame_post_draw.disconnect(callback)
	var job_id := int(_active_job["id"])
	_active_job = {}
	_finished_jobs[job_id] = {"ok": false, "error": message}


func _compute_preview_bounds(preview: Node3D) -> AABB:
	var bounds: AABB
	var first := true
	var stack: Array[Node] = preview.get_children()
	while stack.size() > 0:
		var node: Node = stack.pop_back()
		if node is VisualInstance3D:
			var vi := node as VisualInstance3D
			var child_aabb := vi.global_transform * vi.get_aabb()
			bounds = child_aabb if first else bounds.merge(child_aabb)
			first = false
		elif node is Node3D:
			var small_box := AABB(node.global_position - Vector3(0.1, 0.1, 0.1), Vector3(0.2, 0.2, 0.2))
			bounds = small_box if first else bounds.merge(small_box)
			first = false
		for child in node.get_children():
			stack.append(child)
	if first:
		bounds = AABB(preview.global_position - Vector3.ONE, Vector3.ONE * 2.0)
	return bounds


func _scene_fingerprint(scene_root: Node, preview: Node3D) -> int:
	var scene_path := scene_root.scene_file_path
	if scene_path.is_empty() or preview.get_child_count() == 0:
		return 0
	var parts: Array[String] = []
	for child in preview.get_children():
		if not child is Node3D:
			continue
		var node := child as Node3D
		parts.append("%s|%s|%s|%s|%s" % [node.name, node.scene_file_path, node.position, node.rotation_degrees, node.scale])
	parts.sort()
	return hash(scene_path + "::" + ";".join(parts))


func _transform_metadata(transform: Transform3D) -> Dictionary:
	var basis := transform.basis
	return {
		"origin": [transform.origin.x, transform.origin.y, transform.origin.z],
		"basis_x": [basis.x.x, basis.x.y, basis.x.z],
		"basis_y": [basis.y.x, basis.y.y, basis.y.z],
		"basis_z": [basis.z.x, basis.z.y, basis.z.z],
	}


func _projection_metadata(camera: Camera3D) -> Dictionary:
	return {
		"projection": "orthogonal" if camera.projection == Camera3D.PROJECTION_ORTHOGONAL else "perspective",
		"fov": camera.fov,
		"size": camera.size,
		"near": camera.near,
		"far": camera.far,
		"keep_aspect": camera.keep_aspect,
	}
