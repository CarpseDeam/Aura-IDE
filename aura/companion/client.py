"""WebSocket client for Companion — connects to Relay safely from a worker thread.

Threading model
---------------
``CompanionWsClient`` is a thin facade that lives on the UI thread. It owns a
``QThread`` and a separate ``_WsWorker`` QObject (parentless) that is moved into
that thread. The worker runs the asyncio loop. All cross-thread communication
happens via signals — no shared mutable state, no parent on a moved QObject.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from PySide6.QtCore import QMetaObject, QObject, Qt, QThread, Signal, Slot

logger = logging.getLogger(__name__)


class _WsWorker(QObject):
    """Runs the asyncio WS loop on the worker thread.

    No Qt parent — it is moved into the worker thread on its own.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)
    send_request = Signal(str)  # internal: UI -> worker

    def __init__(self, url: str, token: str) -> None:
        super().__init__()  # No parent — required for moveToThread
        self._url = url
        self._token = token
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._should_run = True
        self._reconnect_delay = 1.0
        self.send_request.connect(self._on_send_request, Qt.ConnectionType.QueuedConnection)

    @Slot()
    def run(self) -> None:
        """Entry point — runs on the worker thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._ws_loop())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None

    async def _ws_loop(self) -> None:
        while self._should_run:
            try:
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1.0
                    await ws.send(json.dumps({
                        "type": "hello",
                        "device_id": self._token or "unknown",
                        "device_type": "desktop",
                        "token": self._token or "",
                    }))
                    welcome_raw = await ws.recv()
                    try:
                        welcome = json.loads(welcome_raw)
                    except json.JSONDecodeError:
                        welcome = {}
                    logger.info("[CompanionWsClient] connected — welcome: %s", welcome.get("type"))
                    self.connected.emit()
                    async for raw in ws:
                        if not self._should_run:
                            break
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
            logger.info("[CompanionWsClient] reconnecting in %.1fs", self._reconnect_delay)
            try:
                await asyncio.sleep(self._reconnect_delay)
            except asyncio.CancelledError:
                break
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    @Slot(str)
    def _on_send_request(self, data: str) -> None:
        if self._loop and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._send_async(data), self._loop)

    async def _send_async(self, data: str) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(data)
            except Exception as exc:
                logger.warning("[CompanionWsClient] send failed: %s", exc)

    @Slot()
    def shutdown(self) -> None:
        """Stop the loop and close the socket. Safe to call from any thread."""
        self._should_run = False
        loop = self._loop
        ws = self._ws
        if loop and ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except Exception:
                pass


class CompanionWsClient(QObject):
    """Public facade — lives on the UI thread; owns the worker thread.

    Forwards signals from the worker to manager-facing signals.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)  # raw JSON string

    def __init__(self, url: str = "", device_token: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._token = device_token
        self._thread: QThread | None = None
        self._worker: _WsWorker | None = None
        self._is_connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def connect_to_relay(self, url: str | None = None, token: str | None = None) -> None:
        if url:
            self._url = url
        if token:
            self._token = token
        if self._thread is not None and self._thread.isRunning():
            logger.warning("[CompanionWsClient] already connecting")
            return

        self._worker = _WsWorker(self._url, self._token)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Wire signals BEFORE starting the thread.
        self._worker.connected.connect(self._on_worker_connected)
        self._worker.disconnected.connect(self._on_worker_disconnected)
        self._worker.message_received.connect(self.message_received)

        self._thread.started.connect(self._worker.run)
        # When the worker run() returns, quit the thread; after thread quits, clean up.
        self._worker.disconnected.connect(self._maybe_quit_thread)
        self._thread.start()

    @Slot()
    def _on_worker_connected(self) -> None:
        self._is_connected = True
        self.connected.emit()

    @Slot()
    def _on_worker_disconnected(self) -> None:
        self._is_connected = False
        self.disconnected.emit()

    @Slot()
    def _maybe_quit_thread(self) -> None:
        # Worker emits disconnected on every reconnect cycle, so do NOT quit
        # the thread here — only when shutdown() is called.
        pass

    def send(self, data: str) -> None:
        worker = self._worker
        if worker is not None:
            # Queued connection: worker reads on its own thread.
            worker.send_request.emit(data)

    def close(self) -> None:
        worker = self._worker
        if worker is not None:
            # Trigger shutdown on the worker thread via a queued slot.
            QMetaObject.invokeMethod(worker, "shutdown", Qt.ConnectionType.QueuedConnection)
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None
        self._is_connected = False
