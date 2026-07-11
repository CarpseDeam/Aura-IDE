@tool
extends RefCounted

const DEFAULT_MAX_ITEMS := 50
const MAX_ITEMS := 200


func describe(params: Dictionary) -> Dictionary:
	var target_class := str(params.get("class_name", "")).strip_edges()
	var query := str(params.get("member_query", "")).strip_edges().to_lower()
	var max_items := clampi(int(params.get("max_items", DEFAULT_MAX_ITEMS)), 1, MAX_ITEMS)
	var include_inherited := bool(params.get("include_inherited", false))
	if target_class.is_empty():
		return _search_classes(query, max_items)
	if not ClassDB.class_exists(target_class):
		return {"ok": false, "error": "Godot ClassDB class does not exist: %s" % target_class}
	var no_inheritance := not include_inherited
	return {"ok": true, "result": {
		"mode": "class",
		"class_name": target_class,
		"parent_class": str(ClassDB.get_parent_class(target_class)),
		"can_instantiate": ClassDB.can_instantiate(target_class),
		"enabled": ClassDB.is_class_enabled(target_class),
		"api_type": int(ClassDB.class_get_api_type(target_class)),
		"inheritors": Array(ClassDB.get_inheriters_from_class(target_class)).slice(0, max_items),
		"include_inherited": include_inherited,
		"member_query": query,
		"methods": _methods(target_class, query, no_inheritance, max_items),
		"properties": _properties(target_class, query, no_inheritance, max_items),
		"signals": _signals(target_class, query, no_inheritance, max_items),
		"integer_constants": _constants(target_class, query, no_inheritance, max_items),
		"enums": _enums(target_class, query, no_inheritance, max_items),
	}}


func _search_classes(query: String, max_items: int) -> Dictionary:
	var matches: Array[String] = []
	for raw_name in ClassDB.get_class_list():
		var candidate := str(raw_name)
		if query.is_empty() or query in candidate.to_lower():
			matches.append(candidate)
			if matches.size() >= max_items:
				break
	var script_classes: Array[Dictionary] = []
	for descriptor in ProjectSettings.get_global_class_list():
		var script_name := str(descriptor.get("class", ""))
		if query.is_empty() or query in script_name.to_lower():
			script_classes.append({
				"class_name": script_name,
				"base": str(descriptor.get("base", "")),
				"path": str(descriptor.get("path", "")),
				"language": str(descriptor.get("language", "")),
			})
			if script_classes.size() >= max_items:
				break
	return {"ok": true, "result": {
		"mode": "class_search",
		"query": query,
		"engine_classes": matches,
		"script_classes": script_classes,
		"returned_count": matches.size() + script_classes.size(),
		"truncated": matches.size() >= max_items,
	}}


func _methods(target_class: String, query: String, no_inheritance: bool, max_items: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for raw in ClassDB.class_get_method_list(target_class, no_inheritance):
		var name := str(raw.get("name", ""))
		if not _matches(name, query):
			continue
		var args: Array[Dictionary] = []
		for descriptor in raw.get("args", []):
			args.append(_typed_descriptor(descriptor))
		var defaults: Array[String] = []
		for value in raw.get("default_args", []):
			defaults.append(var_to_str(value))
		result.append({
			"name": name,
			"return": _typed_descriptor(raw.get("return", {})),
			"arguments": args,
			"default_arguments": defaults,
			"flags": int(raw.get("flags", 0)),
		})
		if result.size() >= max_items:
			break
	return result


func _properties(target_class: String, query: String, no_inheritance: bool, max_items: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for descriptor in ClassDB.class_get_property_list(target_class, no_inheritance):
		var name := str(descriptor.get("name", ""))
		if _matches(name, query):
			var detail := _typed_descriptor(descriptor)
			detail["default_value_text"] = var_to_str(
				ClassDB.class_get_property_default_value(target_class, name)
			)
			detail["getter"] = str(ClassDB.class_get_property_getter(target_class, name))
			detail["setter"] = str(ClassDB.class_get_property_setter(target_class, name))
			result.append(detail)
			if result.size() >= max_items:
				break
	return result


func _signals(target_class: String, query: String, no_inheritance: bool, max_items: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for raw in ClassDB.class_get_signal_list(target_class, no_inheritance):
		var name := str(raw.get("name", ""))
		if not _matches(name, query):
			continue
		var args: Array[Dictionary] = []
		for descriptor in raw.get("args", []):
			args.append(_typed_descriptor(descriptor))
		result.append({"name": name, "arguments": args})
		if result.size() >= max_items:
			break
	return result


func _constants(target_class: String, query: String, no_inheritance: bool, max_items: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for raw_name in ClassDB.class_get_integer_constant_list(target_class, no_inheritance):
		var name := str(raw_name)
		if _matches(name, query):
			result.append({"name": name, "value": ClassDB.class_get_integer_constant(target_class, name)})
			if result.size() >= max_items:
				break
	return result


func _enums(target_class: String, query: String, no_inheritance: bool, max_items: int) -> Array[Dictionary]:
	var result: Array[Dictionary] = []
	for raw_name in ClassDB.class_get_enum_list(target_class, no_inheritance):
		var name := str(raw_name)
		if not _matches(name, query):
			continue
		var values: Array[Dictionary] = []
		for raw_constant in ClassDB.class_get_enum_constants(target_class, name, no_inheritance):
			var constant_name := str(raw_constant)
			values.append({
				"name": constant_name,
				"value": ClassDB.class_get_integer_constant(target_class, constant_name),
			})
		result.append({"name": name, "values": values})
		if result.size() >= max_items:
			break
	return result


func _typed_descriptor(raw: Variant) -> Dictionary:
	if not raw is Dictionary:
		return {}
	var type_id := int(raw.get("type", TYPE_NIL))
	return {
		"name": str(raw.get("name", "")),
		"type": type_string(type_id),
		"type_id": type_id,
		"class_name": str(raw.get("class_name", "")),
		"hint": int(raw.get("hint", 0)),
		"hint_string": str(raw.get("hint_string", "")),
		"usage": int(raw.get("usage", 0)),
	}


func _matches(name: String, query: String) -> bool:
	return query.is_empty() or query in name.to_lower()
