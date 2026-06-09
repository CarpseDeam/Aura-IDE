"""WebSocket client for Companion — connects to Relay safely from a worker thread.

``CompanionWsClient`` is a thin facade that lives on the UI thread. It owns a
``QThread`` and a separate ``_WsWorker`` QObject (parentless) that is moved into
that thread. The worker runs the asyncio loop. Outbound signals (connected,
disconnected, message_received) use Qt signals from worker→UI thread. Inbound
communication (send, shutdown) uses ``asyncio.run_coroutine_threadsafe`` directly
on the worker's event loop — no Qt queued signals into the worker.
"""
from __future__ import annotations

import asyncio
import json
import logging

import websockets
from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot

logger = logging.getLogger(__name__)


class _WsWorker(QObject):
    """Runs the asyncio WS loop on the worker thread.

    No Qt parent — it is moved into the worker thread on its own.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)
    error = Signal(str)

    def __init__(self, url: str, device_id: str, desktop_secret: str) -> None:
        super().__init__()  # No parent — required for moveToThread
        self._url = url
        self._device_id = device_id
        self._desktop_secret = desktop_secret
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._should_run = True
        self._reconnect_delay = 1.0
        self._sleep_task: asyncio.Task | None = None

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
                    hello = {
                        "type": "hello",
                        "device_id": self._device_id or "unknown",
                        "device_type": "desktop",
                    }
                    if self._desktop_secret:
                        hello["secret"] = self._desktop_secret
                    await ws.send(json.dumps(hello))
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
                self.error.emit(str(exc))
            finally:
                self._ws = None
                self.disconnected.emit()
            if not self._should_run:
                break
            logger.info("[CompanionWsClient] reconnecting in %.1fs", self._reconnect_delay)
            try:
                self._sleep_task = asyncio.create_task(asyncio.sleep(self._reconnect_delay))
                await self._sleep_task
            except asyncio.CancelledError:
                break
            finally:
                self._sleep_task = None
            self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    async def _send_async(self, data: str) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(data)
            except Exception as exc:
                logger.warning("[CompanionWsClient] send failed: %s", exc)

    def shutdown(self) -> None:
        """Stop the loop and close the socket. Safe to call from any thread, idempotent."""
        self._should_run = False
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        def _do_shutdown() -> None:
            ws = self._ws
            if ws is not None:
                loop.create_task(ws.close())
            task = self._sleep_task
            if task is not None and not task.done():
                task.cancel()

        try:
            loop.call_soon_threadsafe(_do_shutdown)
        except Exception:
            pass


class CompanionWsClient(QObject):
    """Public facade — lives on the UI thread; owns the worker thread.

    Forwards signals from the worker to manager-facing signals.
    """

    connected = Signal()
    disconnected = Signal()
    message_received = Signal(str)  # raw JSON string
    error = Signal(str)

    def __init__(self, url: str = "", device_id: str = "", desktop_secret: str = "", parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._url = url
        self._device_id = device_id
        self._desktop_secret = desktop_secret
        self._thread: QThread | None = None
        self._worker: _WsWorker | None = None
        self._is_connected: bool = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def connect_to_relay(self, url: str | None = None) -> None:
        if url:
            self._url = url
        if self._thread is not None and self._thread.isRunning():
            logger.warning("[CompanionWsClient] already connecting")
            return

        self._worker = _WsWorker(self._url, self._device_id, self._desktop_secret)
        self._thread = QThread()
        self._worker.moveToThread(self._thread)

        # Wire signals BEFORE starting the thread.
        self._worker.connected.connect(self._on_worker_connected)
        self._worker.disconnected.connect(self._on_worker_disconnected)
        self._worker.message_received.connect(self.message_received)
        self._worker.error.connect(self.error)

        self._thread.started.connect(self._worker.run)
        self._thread.start()

    @Slot()
    def _on_worker_connected(self) -> None:
        self._is_connected = True
        self.connected.emit()

    @Slot()
    def _on_worker_disconnected(self) -> None:
        self._is_connected = False
        self.disconnected.emit()

    def send(self, data: str) -> None:
        worker = self._worker
        if worker is not None:
            loop = worker._loop
            if loop is not None:
                asyncio.run_coroutine_threadsafe(worker._send_async(data), loop)
            else:
                logger.warning("[CompanionWsClient] send: loop not ready")

    def close(self) -> None:
        worker = self._worker
        thread = self._thread
        if worker is not None:
            worker.shutdown()
        if thread is not None:
            thread.quit()
            if not thread.wait(5000):
                logger.warning("[CompanionWsClient] worker thread did not stop within 5 s — not clearing references")
                return
        self._thread = None
        self._worker = None
        self._is_connected = False
