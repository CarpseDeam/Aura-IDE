"""Session manager — tracks connected devices and routes messages."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)


@dataclass
class DeviceSession:
    """Data stored per connected device."""
    ws: WebSocket
    device_type: str = "desktop"
    display_name: str = ""
    device_name: str = ""
    last_seen: str = ""
    authenticated: bool = False
    token_payload: dict | None = None


class SessionManager:
    """Tracks WebSocket connections for desktops and phones."""

    def __init__(self) -> None:
        self._sessions: dict[str, DeviceSession] = {}

    def register(self, device_id: str, ws: WebSocket, device_type: str = "desktop",
                 display_name: str = "") -> None:
        """Register a device connection."""
        self._sessions[device_id] = DeviceSession(
            ws=ws,
            device_type=device_type,
            display_name=display_name or device_id,
            last_seen=datetime.now().isoformat(),
        )
        logger.info("[Relay] device registered: %s (%s)", device_id, device_type)

    def unregister(self, device_id: str) -> None:
        """Remove a device connection."""
        self._sessions.pop(device_id, None)
        logger.info("[Relay] device unregistered: %s", device_id)

    def is_online(self, device_id: str) -> bool:
        return device_id in self._sessions

    def get_ws(self, device_id: str) -> WebSocket | None:
        entry = self._sessions.get(device_id)
        return entry.ws if entry else None

    async def send_to(self, device_id: str, data: str) -> bool:
        """Send a raw JSON string to a connected device."""
        ws = self.get_ws(device_id)
        if ws is None:
            return False
        try:
            await ws.send_text(data)
            return True
        except Exception as exc:
            logger.warning("[Relay] send_to %s failed: %s", device_id, exc)
            self.unregister(device_id)
            return False

    def list_online(self, device_type: str | None = None) -> list[dict]:
        """List connected devices, optionally filtered by type."""
        result = []
        for did, session in self._sessions.items():
            if device_type and session.device_type != device_type:
                continue
            result.append({
                "device_id": did,
                "display_name": session.display_name,
                "device_type": session.device_type,
                "last_seen": session.last_seen,
            })
        return result

    @property
    def online_count(self) -> int:
        return len(self._sessions)

    def set_authenticated(self, device_id: str, token_payload: dict) -> None:
        """Mark a device as authenticated with its JWT payload."""
        session = self._sessions.get(device_id)
        if session:
            session.authenticated = True
            session.token_payload = token_payload

    def is_authenticated(self, device_id: str) -> bool:
        """Check if a device has completed pairing."""
        session = self._sessions.get(device_id)
        return session is not None and session.authenticated

    def set_device_name(self, device_id: str, name: str) -> None:
        """Set a friendly name for a device."""
        session = self._sessions.get(device_id)
        if session:
            session.device_name = name

    def get_device_name(self, device_id: str) -> str:
        """Get the friendly name of a device."""
        session = self._sessions.get(device_id)
        return session.device_name if session else ""
