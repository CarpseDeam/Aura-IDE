"""WebSocket client for Companion — connects to Relay, emits signals."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)


class CompanionWsClient(QObject):
    """WebSocket client that connects to Aura Relay.

    Runs in a dedicated QThread. Emits signals for connection state and messages.
    Phase 0: skeleton with signal stubs. Phase 1 fills in the WS logic.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)  # raw JSON string

    def __init__(self, url: str = "", device_token: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._token = device_token
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect_to_relay(self, url: str | None = None, token: str | None = None) -> None:
        """Initiate connection. Stub for Phase 0."""
        if url:
            self._url = url
        if token:
            self._token = token
        logger.info("[Companion] connect_to_relay stub: url=%s", self._url)

    def send(self, data: str) -> None:
        """Send a raw string over the websocket. Stub for Phase 0."""
        logger.debug("[Companion] send stub: %s", data[:200])

    def close(self) -> None:
        """Close the connection. Stub for Phase 0."""
        logger.info("[Companion] close stub")
        self._connected = False
