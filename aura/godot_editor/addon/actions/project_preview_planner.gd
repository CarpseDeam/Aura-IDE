@tool
extends RefCounted

const PROJECT_SETTING := "aura/editor_bridge/preview_planner"
const INTERFACE_VERSION := 1
const INTERFACE_METHOD := &"preview_planner_interface"
const INSPECT_METHOD := &"inspect_preview_contract"
const PLAN_METHOD := &"plan_preview_revision"
const REQUIRED_CAPABILITIES := ["inspect_contract", "plan_revision"]
const REVISION_OPERATIONS := ["instantiate", "remove", "set_transform", "replace", "duplicate", "attach"]

var _preview_snapshot
var _preview_actions


func _init(preview_snapshot, preview_actions) -> void:
	_preview_snapshot = preview_snapshot
	_preview_actions = preview_actions


func inspect_contract(params: Dictionary) -> Dictionary:
	var prepared := _prepare_request(params)
	if not prepared.get("ok", false):
		return prepared
	var planner_result: Variant = prepared["adapter"].call(INSPECT_METHOD, prepared["request"])
	var checked := _planner_result(planner_result, "contract inspection")
	if not checked.get("ok", false):
		return checked
	return {"ok": true, "result": checked["planner_result"]}


func plan_and_apply(params: Dictionary) -> Dictionary:
	var raw_label: Variant = params.get("label", "Aura semantic preview revision")
	if not raw_label is String:
		return {"ok": false, "error": "preview planner label must be a string"}
	var prepared := _prepare_request(params)
	if not prepared.get("ok", false):
		return prepared
	var planner_result: Variant = prepared["adapter"].call(PLAN_METHOD, prepared["request"])
	var checked := _planner_result(planner_result, "revision planning")
	if not checked.get("ok", false):
		return checked
	var plan: Dictionary = checked["planner_result"]
	if not bool(plan["ok"]):
		var semantic_failure := plan.duplicate(true)
		semantic_failure["applied"] = false
		semantic_failure["saved"] = false
		return {"ok": true, "result": semantic_failure}
	var operations_check := _revision_operations(plan.get("operations"))
	if not operations_check.get("ok", false):
		return operations_check
	var revision: Variant = _preview_actions.apply_revision({
		"label": raw_label,
		"operations": plan["operations"],
	})
	if not revision is Dictionary or not revision.has("ok") or not revision["ok"] is bool:
		return {"ok": false, "error": "preview revision owner returned an invalid result"}
	var summary := plan.duplicate(true)
	summary.erase("operations")
	if not bool(revision["ok"]):
		summary["ok"] = false
		summary["error"] = "preview revision rejected: %s" % str(revision.get("error", "request failed"))
		summary["applied"] = false
		summary["saved"] = false
		return {"ok": true, "result": summary}
	var revision_result: Variant = revision.get("result")
	if not revision_result is Dictionary:
		return {"ok": false, "error": "preview revision owner returned an invalid result payload"}
	var response := summary.duplicate(true)
	for key in revision_result:
		response[key] = revision_result[key]
	response["ok"] = true
	response["semantic"] = summary
	response["revision"] = revision_result.duplicate(true)
	response["saved"] = false
	return {"ok": true, "result": response}


func _prepare_request(params: Dictionary) -> Dictionary:
	var adapter_result := _project_adapter()
	if not adapter_result.get("ok", false):
		return adapter_result
	var raw_request: Variant = params.get("request", {})
	if not raw_request is Dictionary:
		return {"ok": false, "error": "preview planner request must be an object"}
	var snapshot: Variant = _preview_snapshot.capture({})
	if not snapshot is Dictionary or not snapshot.has("ok") or not snapshot["ok"] is bool:
		return {"ok": false, "error": "preview snapshot owner returned an invalid result"}
	if not bool(snapshot["ok"]):
		return {"ok": false, "error": str(snapshot.get("error", "preview snapshot failed"))}
	var snapshot_result: Variant = snapshot.get("result")
	if not snapshot_result is Dictionary:
		return {"ok": false, "error": "preview snapshot owner returned an invalid result payload"}
	var request: Dictionary = raw_request.duplicate(true)
	request["snapshot"] = snapshot_result
	return {"ok": true, "adapter": adapter_result["adapter"], "request": request}


func _project_adapter() -> Dictionary:
	if not ProjectSettings.has_setting(PROJECT_SETTING):
		return {"ok": false, "error": "project preview planner is not declared in %s" % PROJECT_SETTING}
	var raw_path: Variant = ProjectSettings.get_setting(PROJECT_SETTING)
	if not raw_path is String:
		return {"ok": false, "error": "project preview planner declaration must be a res:// GDScript path"}
	var path := str(raw_path)
	if (
		path.is_empty()
		or not path.begins_with("res://")
		or not path.ends_with(".gd")
		or path.contains("\\")
		or path != path.simplify_path()
	):
		return {"ok": false, "error": "project preview planner declaration must be a normalized res:// GDScript path"}
	var script: Variant = ResourceLoader.load(path, "Script", ResourceLoader.CACHE_MODE_REUSE)
	if not script is Script or not script.can_instantiate():
		return {"ok": false, "error": "project preview planner is missing or invalid: %s" % path}
	if str(script.get_instance_base_type()) != "RefCounted":
		return {"ok": false, "error": "project preview planner must extend RefCounted: %s" % path}
	var adapter: Variant = script.new()
	if adapter == null:
		return {"ok": false, "error": "project preview planner could not be instantiated: %s" % path}
	for method in [INTERFACE_METHOD, INSPECT_METHOD, PLAN_METHOD]:
		if not adapter.has_method(method):
			return {"ok": false, "error": "project preview planner is incompatible; missing %s" % method}
	var interface: Variant = adapter.call(INTERFACE_METHOD)
	if not interface is Dictionary or int(interface.get("version", 0)) != INTERFACE_VERSION:
		return {"ok": false, "error": "project preview planner interface version is incompatible"}
	var capabilities: Variant = interface.get("capabilities", [])
	if not capabilities is Array:
		return {"ok": false, "error": "project preview planner capabilities are invalid"}
	for capability in REQUIRED_CAPABILITIES:
		if not capability in capabilities:
			return {"ok": false, "error": "project preview planner is incompatible; missing capability %s" % capability}
	return {"ok": true, "adapter": adapter}


func _planner_result(raw: Variant, purpose: String) -> Dictionary:
	if not raw is Dictionary:
		return {"ok": false, "error": "project preview planner %s must return a dictionary" % purpose}
	if not raw.has("ok") or not raw["ok"] is bool:
		return {"ok": false, "error": "project preview planner %s must return an explicit boolean ok value" % purpose}
	return {"ok": true, "planner_result": raw}


func _revision_operations(raw: Variant) -> Dictionary:
	if not raw is Array or raw.is_empty():
		return {"ok": false, "error": "successful project preview plan must contain a non-empty low-level operations array"}
	for index in raw.size():
		var operation: Variant = raw[index]
		if not operation is Dictionary:
			return {"ok": false, "error": "project preview plan operation %d must be a dictionary" % index}
		if str(operation.get("operation", "")) not in REVISION_OPERATIONS:
			return {"ok": false, "error": "project preview plan operation %d is not a supported low-level preview revision" % index}
	return {"ok": true}
