"""CompanionManager — lifecycle, signal routing, and event dispatch for Companion."""
from __future__ import annotations

import json
import logging
from typing import Any

from PySide6.QtCore import QObject, Signal, QTimer

from aura.companion.auth import get_device_id
from aura.companion.client import CompanionWsClient
from aura.companion.protocol import make_envelope, parse_command
from aura.companion.settings import default_display_name, default_relay_url
from aura.settings import AppSettings

logger = logging.getLogger(__name__)


class CompanionManager(QObject):
    """Manages the Companion (mobile web control plane) connection lifecycle.

    Owns the WebSocket client, routes incoming commands, and forwards
    desktop events to the phone via Relay.

    Signals:
        connection_status_changed(str): "disabled", "connecting", "connected", "error"
        message_received(dict): raw incoming JSON command from phone
    """

    connection_status_changed = Signal(str)
    message_received = Signal(dict)

    def __init__(self, settings: AppSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings or AppSettings()
        self._ws_client: CompanionWsClient | None = None
        self._reconnect_timer: QTimer | None = None
        self._bridge: Any = None      # will be ConversationBridge in Phase 2
        self._drone_runner: Any = None  # will be DroneRunner in Phase 3
        self._project_store: Any = None  # will be ProjectStore in Phase 3
        self._workspace_root: str = ""
        self._current_project_id: str = ""

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        """Start the Companion connection if enabled.

        Call from MainWindow.__init__ after settings are loaded.
        """
        if not self._settings.companion_enabled:
            self.connection_status_changed.emit("disabled")
            return
        self.connection_status_changed.emit("connecting")
        logger.info("[Companion] starting — relay: %s", self._settings.companion_relay_url)
        self._connect()

    def stop(self) -> None:
        """Stop the Companion connection.

        Call from MainWindow.closeEvent or when toggling off.
        """
        self._stop_reconnect_timer()
        if self._ws_client:
            self._ws_client.close()
            self._ws_client = None
        self.connection_status_changed.emit("disabled")
        logger.info("[Companion] stopped")

    def update_settings(self, settings: AppSettings) -> None:
        """Apply new settings and reconnect if needed."""
        self.stop()
        self._settings = settings
        if settings.companion_enabled:
            self.start()

    # ── Bridge / Runner / Store wiring ──────────────────────

    def set_bridge(self, bridge: Any) -> None:
        """Set the ConversationBridge reference (Phase 2)."""
        self._bridge = bridge

    def set_drone_runner(self, runner: Any) -> None:
        """Set the DroneRunner reference (Phase 3)."""
        self._drone_runner = runner

    def set_project_store(self, store: Any) -> None:
        """Set the ProjectStore reference (Phase 3)."""
        self._project_store = store

    def set_workspace_root(self, path: str) -> None:
        self._workspace_root = path

    # ── Send ────────────────────────────────────────────────

    def send_event(self, event: dict) -> None:
        """Send an event to the phone via Relay."""
        if self._ws_client and self._ws_client.is_connected:
            self._ws_client.send(json.dumps(event))

    # ── Pairing (Phase 4 stub) ──────────────────────────────

    def generate_pairing_code(self) -> str:
        """Generate a pairing code (Phase 4)."""
        from aura.companion.auth import generate_pairing_code as _gen
        return _gen()

    # ── Internal ────────────────────────────────────────────

    def _connect(self) -> None:
        """Initiate the WebSocket connection."""
        url = self._settings.companion_relay_url or default_relay_url()
        token = self._settings.companion_device_token or ""
        display_name = self._settings.companion_display_name or default_display_name()

        self._ws_client = CompanionWsClient(url, token, self)
        self._ws_client.connected.connect(lambda: self.connection_status_changed.emit("connected"))
        self._ws_client.disconnected.connect(self._on_disconnected)
        self._ws_client.message_received.connect(self._on_raw_message)
        self._ws_client.connect_to_relay()

    def _stop_reconnect_timer(self) -> None:
        if self._reconnect_timer and self._reconnect_timer.isActive():
            self._reconnect_timer.stop()
            self._reconnect_timer = None

    def _on_disconnected(self) -> None:
        self.connection_status_changed.emit("error")
        logger.warning("[Companion] disconnected — will retry")
        # Phase 1: add reconnect logic with backoff

    def _on_raw_message(self, raw: str) -> None:
        """Handle an incoming message from Relay."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Companion] invalid JSON from Relay: %s", raw[:200])
            return
        self.message_received.emit(msg)
        # Phase 1: command dispatch routing will go here
