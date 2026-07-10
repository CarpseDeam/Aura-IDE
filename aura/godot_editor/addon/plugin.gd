@tool
extends EditorPlugin

const BridgeServer = preload("transport/bridge_server.gd")
const RequestRouter = preload("protocol/request_router.gd")

var _bridge_server


func _enter_tree() -> void:
	var config := _read_config()
	if config.is_empty():
		push_warning("Aura Editor Bridge: res://.aura/godot_editor_bridge.json is missing or invalid")
		return
	var router = RequestRouter.new(get_editor_interface(), get_undo_redo())
	_bridge_server = BridgeServer.new()
	var error: Error = _bridge_server.start(int(config["port"]), str(config["token"]), router)
	if error != OK:
		push_error("Aura Editor Bridge could not listen on 127.0.0.1:%d (error %d)" % [config["port"], error])
		_bridge_server = null
		return
	set_process(true)
	print("Aura Editor Bridge listening on 127.0.0.1:%d" % config["port"])


func _exit_tree() -> void:
	set_process(false)
	if _bridge_server != null:
		_bridge_server.stop()
		_bridge_server = null


func _process(_delta: float) -> void:
	if _bridge_server != null:
		_bridge_server.poll()


func _read_config() -> Dictionary:
	var file := FileAccess.open("res://.aura/godot_editor_bridge.json", FileAccess.READ)
	if file == null:
		return {}
	var parsed = JSON.parse_string(file.get_as_text())
	if not parsed is Dictionary:
		return {}
	var host := str(parsed.get("host", ""))
	var port := int(parsed.get("port", 0))
	var token := str(parsed.get("token", ""))
	if host != "127.0.0.1" or port < 1024 or port > 65535 or token.length() < 24:
		return {}
	return {"port": port, "token": token}
