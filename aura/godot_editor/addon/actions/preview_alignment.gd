@tool
extends RefCounted

const LIMIT_METERS := 10000.0


func calculate(
	reference: Node3D,
	target_yaw_degrees: float,
	target_scale: Vector3,
	spec: Dictionary,
	trusted_geometry: Variant,
) -> Dictionary:
	if not trusted_geometry is Dictionary:
		return _failure("trusted alignment geometry is missing")
	var reference_geometry := _decode_geometry(trusted_geometry.get("reference"), "reference")
	if not reference_geometry.get("ok", false):
		return reference_geometry
	var piece_geometry := _decode_geometry(trusted_geometry.get("piece"), "piece")
	if not piece_geometry.get("ok", false):
		return piece_geometry
	var reference_anchor := _decode_anchor(spec.get("reference_anchor"), "reference_anchor")
	if not reference_anchor.get("ok", false):
		return reference_anchor
	var piece_anchor := _decode_anchor(spec.get("piece_anchor"), "piece_anchor")
	if not piece_anchor.get("ok", false):
		return piece_anchor
	var offset: Variant = _decode_vector(spec.get("offset", [0.0, 0.0, 0.0]))
	if offset == null or not offset.is_finite():
		return _failure("alignment offset must contain three finite numbers")
	var offset_space := str(spec.get("offset_space", "reference_local"))
	if offset_space not in ["reference_local", "world"]:
		return _failure("alignment offset_space must be reference_local or world")
	if not is_finite(target_yaw_degrees) or not target_scale.is_finite():
		return _failure("alignment target transform must be finite")

	var reference_local := _local_anchor(reference_geometry, reference_anchor["anchor"])
	var piece_local := _local_anchor(piece_geometry, piece_anchor["anchor"])
	var reference_basis := Basis(Vector3.UP, deg_to_rad(reference.rotation_degrees.y)).scaled_local(reference.scale)
	var target_basis := Basis(Vector3.UP, deg_to_rad(target_yaw_degrees)).scaled_local(target_scale)
	var reference_point := reference.position + reference_basis * reference_local
	var applied_offset: Vector3 = offset
	if offset_space == "reference_local":
		applied_offset = Basis(Vector3.UP, deg_to_rad(reference.rotation_degrees.y)) * offset
	var final_position: Vector3 = reference_point + applied_offset - target_basis * piece_local
	if not final_position.is_finite() or _outside_limit(final_position) or _outside_limit(offset):
		return _failure("calculated alignment exceeds the finite 10 km preview bound")
	return {
		"ok": true,
		"position": final_position,
		"fact": {
			"method": "calibrated_alignment",
			"reference_anchor": spec["reference_anchor"].duplicate(),
			"piece_anchor": spec["piece_anchor"].duplicate(),
			"calculated_position": [final_position.x, final_position.y, final_position.z],
			"rotation_degrees_y": target_yaw_degrees,
			"scale": [target_scale.x, target_scale.y, target_scale.z],
			"offset": [offset.x, offset.y, offset.z],
			"offset_space": offset_space,
		},
	}


func _decode_geometry(raw: Variant, label: String) -> Dictionary:
	if not raw is Dictionary or str(raw.get("catalog_identity", "")).is_empty():
		return _failure("%s trusted geometry lacks verified catalog identity" % label)
	var bounds: Variant = _decode_vector(raw.get("local_bounds_m"))
	var pivot: Variant = _decode_vector(raw.get("pivot_to_center_m"))
	if bounds == null or pivot == null or not bounds.is_finite() or not pivot.is_finite():
		return _failure("%s trusted geometry lacks finite bounds or pivot calibration" % label)
	if bounds.x <= 0.0 or bounds.y <= 0.0 or bounds.z <= 0.0:
		return _failure("%s trusted local bounds must be positive" % label)
	return {"ok": true, "bounds": bounds, "pivot": pivot}


func _decode_anchor(raw: Variant, label: String) -> Dictionary:
	if not raw is Array or raw.size() != 3:
		return _failure("%s must contain exactly three components" % label)
	var decoded := Vector3i.ZERO
	for axis in 3:
		if not raw[axis] is int or raw[axis] not in [-1, 0, 1]:
			return _failure("%s components must be -1, 0, or 1" % label)
		decoded[axis] = raw[axis]
	return {"ok": true, "anchor": decoded}


func _local_anchor(geometry: Dictionary, anchor: Vector3i) -> Vector3:
	var bounds: Vector3 = geometry["bounds"]
	var pivot: Vector3 = geometry["pivot"]
	return pivot + Vector3(bounds.x * anchor.x, bounds.y * anchor.y, bounds.z * anchor.z) * 0.5


func _decode_vector(raw: Variant) -> Variant:
	if not raw is Array or raw.size() != 3:
		return null
	for component in raw:
		if not component is int and not component is float:
			return null
	return Vector3(float(raw[0]), float(raw[1]), float(raw[2]))


func _outside_limit(value: Vector3) -> bool:
	return absf(value.x) > LIMIT_METERS or absf(value.y) > LIMIT_METERS or absf(value.z) > LIMIT_METERS


func _failure(message: String) -> Dictionary:
	return {"ok": false, "error": message}
