"""WebSocket client for Companion — connects to Relay via websockets in a QThread."""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from PySide6.QtCore import QObject, QThread, Signal

logger = logging.getLogger(__name__)


class CompanionWsClient(QObject):
    """WebSocket client that connects to Aura Relay.

    Runs the asyncio event loop in a dedicated QThread.
    Emits signals for connection state and messages.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)  # raw JSON string

    def __init__(self, url: str = "", device_token: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._token = device_token
        self._ws = None
        self._thread: QThread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._should_run = False
        self._reconnect_delay = 1.0

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    def connect_to_relay(self, url: str | None = None, token: str | None = None) -> None:
        """Initiate connection to Relay in a background thread."""
        if url:
            self._url = url
        if token:
            self._token = token
        if self._thread and self._thread.isRunning():
            logger.warning("[CompanionWsClient] already connecting")
            return
        self._should_run = True
        self._thread = QThread()
        self.moveToThread(self._thread)
        self._thread.started.connect(self._run_loop)
        self._thread.start()

    def _run_loop(self) -> None:
        """Run the asyncio event loop in this thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_loop())

    async def _ws_loop(self) -> None:
        """Main WebSocket loop with auto-reconnect."""
        while self._should_run:
            try:
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    # Send handshake
                    await ws.send(json.dumps({
                        "type": "hello",
                        "device_id": self._token.split(":")[0] if self._token else "unknown",
                        "device_type": "desktop",
                        "token": self._token or "",
                    }))
                    # Wait for welcome
                    welcome_raw = await ws.recv()
                    welcome = json.loads(welcome_raw)
                    logger.info("[CompanionWsClient] connected — welcome: %s", welcome.get("type"))
                    self.connected.emit()
                    # Message loop
                    async for raw in ws:
                        self.message_received.emit(raw)
            except websockets.ConnectionClosed:
                logger.warning("[CompanionWsClient] connection closed")
            except Exception as exc:
                logger.error("[CompanionWsClient] connection error: %s", exc)
            finally:
                self._ws = None
                self.disconnected.emit()
                if not self._should_run:
                    break
                # Reconnect with exponential backoff
                logger.info("[CompanionWsClient] reconnecting in %.1fs", self._reconnect_delay)
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def send(self, data: str) -> None:
        """Send a raw string over the websocket."""
        if self._loop and self._should_run:
            asyncio.run_coroutine_threadsafe(self._send_async(data), self._loop)

    async def _send_async(self, data: str) -> None:
        if self._ws and not self._ws.closed:
            await self._ws.send(data)

    def close(self) -> None:
        """Close the connection and stop the thread."""
        self._should_run = False
        if self._loop and self._ws:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        if self._thread:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._loop = None
        self._ws = None
