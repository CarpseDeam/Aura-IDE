"""Small authenticated client for Aura's in-editor Godot bridge."""

from __future__ import annotations

import json
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONFIG_PATH = Path(".aura/godot_editor_bridge.json")
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


class GodotEditorBridgeError(RuntimeError):
    """Raised when the editor bridge is unavailable or rejects a request."""


@dataclass(frozen=True)
class GodotEditorBridgeConfig:
    host: str
    port: int
    token: str


def load_bridge_config(project_root: Path) -> GodotEditorBridgeConfig:
    path = project_root.resolve() / CONFIG_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GodotEditorBridgeError(
            "Godot editor bridge is not installed. Run install_godot_editor_bridge first."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GodotEditorBridgeError(f"Invalid Godot editor bridge config: {exc}") from exc

    host = str(raw.get("host") or "127.0.0.1")
    if host not in {"127.0.0.1", "::1", "localhost"}:
        raise GodotEditorBridgeError("Godot editor bridge must use a loopback host")
    try:
        port = int(raw.get("port"))
    except (TypeError, ValueError) as exc:
        raise GodotEditorBridgeError("Godot editor bridge port is invalid") from exc
    token = str(raw.get("token") or "")
    if not 1024 <= port <= 65535 or len(token) < 24:
        raise GodotEditorBridgeError("Godot editor bridge config is incomplete")
    return GodotEditorBridgeConfig(host=host, port=port, token=token)


class GodotEditorBridgeClient:
    """Send one newline-delimited JSON request to the active Godot editor."""

    def __init__(self, project_root: Path, timeout: float = 3.0) -> None:
        self.project_root = project_root.resolve()
        self.timeout = timeout

    def request(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        config = load_bridge_config(self.project_root)
        request_id = uuid.uuid4().hex
        payload = {
            "protocol": 1,
            "request_id": request_id,
            "token": config.token,
            "action": action,
            "params": params or {},
        }
        wire = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with socket.create_connection((config.host, config.port), self.timeout) as peer:
                peer.settimeout(self.timeout)
                peer.sendall(wire)
                response_wire = _receive_line(peer)
        except (OSError, TimeoutError) as exc:
            raise GodotEditorBridgeError(
                "Godot editor bridge is offline. Open the project in Godot and enable "
                "Project Settings > Plugins > Aura Editor Bridge."
            ) from exc

        try:
            response = json.loads(response_wire.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GodotEditorBridgeError("Godot editor bridge returned invalid JSON") from exc
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            raise GodotEditorBridgeError("Godot editor bridge returned a mismatched response")
        if response.get("ok") is not True:
            raise GodotEditorBridgeError(str(response.get("error") or "Godot editor request failed"))
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise GodotEditorBridgeError("Godot editor bridge returned an invalid result")
        return result


def _receive_line(peer: socket.socket) -> bytes:
    chunks = bytearray()
    while len(chunks) <= MAX_RESPONSE_BYTES:
        chunk = peer.recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(chunks)))
        if not chunk:
            break
        chunks.extend(chunk)
        newline = chunks.find(b"\n")
        if newline >= 0:
            return bytes(chunks[:newline])
    if len(chunks) > MAX_RESPONSE_BYTES:
        raise GodotEditorBridgeError("Godot editor bridge response exceeded 8 MiB")
    raise GodotEditorBridgeError("Godot editor bridge closed before replying")


__all__ = [
    "GodotEditorBridgeClient",
    "GodotEditorBridgeConfig",
    "GodotEditorBridgeError",
    "load_bridge_config",
]
