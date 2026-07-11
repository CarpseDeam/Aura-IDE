@tool
extends RefCounted

const MAX_REQUEST_BYTES := 1024 * 1024

var _listener := TCPServer.new()
var _clients: Array[Dictionary] = []
var _token := ""
var _router


func start(port: int, token: String, router) -> Error:
	_token = token
	_router = router
	return _listener.listen(port, "127.0.0.1")


func stop() -> void:
	for client in _clients:
		_cancel_client_pending(client)
		var peer: StreamPeerTCP = client["peer"]
		peer.disconnect_from_host()
	_clients.clear()
	_listener.stop()


func poll() -> void:
	while _listener.is_connection_available():
		_clients.append({"peer": _listener.take_connection(), "buffer": PackedByteArray()})
	for index in range(_clients.size() - 1, -1, -1):
		var client := _clients[index]
		var peer: StreamPeerTCP = client["peer"]
		peer.poll()
		if peer.get_status() != StreamPeerTCP.STATUS_CONNECTED:
			_cancel_client_pending(client)
			_clients.remove_at(index)
			continue
		if client.has("pending_id"):
			var pending: Dictionary = _router.poll_pending(int(client["pending_id"]))
			if pending.get("pending", false):
				continue
			_send_and_close(peer, _response_for_routed(str(client["request_id"]), pending))
			_clients.remove_at(index)
			continue
		var available := peer.get_available_bytes()
		if available > 0:
			var read_result := peer.get_data(available)
			if read_result[0] != OK:
				peer.disconnect_from_host()
				_clients.remove_at(index)
				continue
			var buffer: PackedByteArray = client["buffer"]
			buffer.append_array(read_result[1])
			client["buffer"] = buffer
		if client["buffer"].size() > MAX_REQUEST_BYTES:
			_send_and_close(peer, {"ok": false, "request_id": "", "error": "request exceeds 1 MiB"})
			_clients.remove_at(index)
			continue
		var newline: int = client["buffer"].find(10)
		if newline >= 0:
			var line: PackedByteArray = client["buffer"].slice(0, newline)
			var handled := _handle_line(line.get_string_from_utf8())
			if handled.get("pending", false):
				client["pending_id"] = int(handled["pending_id"])
				client["request_id"] = str(handled["request_id"])
				client["buffer"] = PackedByteArray()
			else:
				_send_and_close(peer, handled)
				_clients.remove_at(index)


func _handle_line(line: String) -> Dictionary:
	var request = JSON.parse_string(line)
	if not request is Dictionary:
		return {"ok": false, "request_id": "", "error": "request is not valid JSON"}
	var request_id := str(request.get("request_id", ""))
	if int(request.get("protocol", 0)) != 1:
		return {"ok": false, "request_id": request_id, "error": "unsupported protocol"}
	if str(request.get("token", "")) != _token:
		return {"ok": false, "request_id": request_id, "error": "authentication failed"}
	var action := str(request.get("action", ""))
	var params = request.get("params", {})
	if not params is Dictionary:
		return {"ok": false, "request_id": request_id, "error": "params must be an object"}
	var routed: Dictionary = _router.dispatch(action, params)
	if routed.get("pending", false):
		return {
			"pending": true,
			"pending_id": int(routed["pending_id"]),
			"request_id": request_id,
		}
	return _response_for_routed(request_id, routed)


func _response_for_routed(request_id: String, routed: Dictionary) -> Dictionary:
	if not routed.get("ok", false):
		return {"ok": false, "request_id": request_id, "error": str(routed.get("error", "request failed"))}
	return {"ok": true, "request_id": request_id, "result": routed.get("result", {})}


func _cancel_client_pending(client: Dictionary) -> void:
	if client.has("pending_id"):
		_router.cancel_pending(int(client["pending_id"]))


func _send_and_close(peer: StreamPeerTCP, response: Dictionary) -> void:
	var wire := (JSON.stringify(response) + "\n").to_utf8_buffer()
	peer.put_data(wire)
	peer.disconnect_from_host()
