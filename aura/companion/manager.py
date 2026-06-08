"""CompanionManager — lifecycle, signal routing, and event dispatch."""
from __future__ import annotations

import json
import logging
from typing import Any

from aura.version import __version__

from PySide6.QtCore import QObject, Signal

from aura.companion.auth import generate_pairing_code, get_device_display_name, get_device_id, invalidate_pairing_code, validate_pairing_code
from aura.companion.client import CompanionWsClient
from pathlib import Path

from aura.companion.protocol import (
    ActiveRunSummary,
    CompanionProject,
    CompanionThread,
    ReceiptSummary,
    make_envelope,
)
from aura.drones.store import RunHistoryStore
from aura.projects.store import ProjectStore
from aura.settings import AppSettings, resolve_role_default_model
from relay.auth import create_device_token

logger = logging.getLogger(__name__)


class CompanionManager(QObject):
    """Manages the Companion (mobile web control plane) connection lifecycle.

    Owns the WebSocket client, routes incoming commands, and forwards
    desktop events to the phone via Relay.
    """

    connection_status_changed = Signal(str)  # "disabled", "connecting", "connected", "error"
    message_received = Signal(dict)
    pairing_code_available = Signal(str)       # Emitted when a pairing code is generated
    pairing_code_invalidated = Signal()        # Emitted when a pairing code is cancelled
    pairing_complete = Signal(str)             # Emitted when a phone pairs successfully (param: device_name)

    def __init__(self, settings: AppSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings or AppSettings()
        self._ws_client: CompanionWsClient | None = None
        self._bridge: Any = None
        self._drone_runner: Any = None
        self._project_store: Any = None
        self._workspace_root: str = ""
        self._current_project_id: str = ""
        self._pending_chat_id: str = ""
        self._pending_chat_phone_id: str = ""
        self._current_pairing_code: str = ""

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        if not self._settings.companion_enabled:
            self.connection_status_changed.emit("disabled")
            return
        self.connection_status_changed.emit("connecting")
        logger.info("[Companion] starting — relay: %s", self._settings.companion_relay_url)
        self._connect()

    def stop(self) -> None:
        if self._ws_client:
            self._ws_client.close()
            self._ws_client = None
        self.connection_status_changed.emit("disabled")
        logger.info("[Companion] stopped")

    def update_settings(self, settings: AppSettings) -> None:
        was_enabled = self._settings.companion_enabled
        self._settings = settings
        if was_enabled:
            self.stop()
        if settings.companion_enabled:
            self.start()

    # ── Bridge / Runner / Store wiring ──────────────────────

    def set_bridge(self, bridge: Any) -> None:
        old = self._bridge
        if old is not None:
            try:
                old.contentDelta.disconnect(self._on_bridge_content_delta)
                old.reasoningDelta.disconnect(self._on_bridge_reasoning_delta)
                old.streamDone.disconnect(self._on_bridge_stream_done)
                old.apiError.disconnect(self._on_bridge_api_error)
                old.finished.disconnect(self._on_bridge_finished)
            except (TypeError, RuntimeError):
                pass
        self._bridge = bridge
        if bridge is not None:
            bridge.contentDelta.connect(self._on_bridge_content_delta)
            bridge.reasoningDelta.connect(self._on_bridge_reasoning_delta)
            bridge.streamDone.connect(self._on_bridge_stream_done)
            bridge.apiError.connect(self._on_bridge_api_error)
            bridge.finished.connect(self._on_bridge_finished)

    def set_drone_runner(self, runner: Any) -> None:
        self._drone_runner = runner

    def set_project_store(self, store: Any) -> None:
        self._project_store = store

    def set_workspace_root(self, path: str) -> None:
        self._workspace_root = path

    # ── Send ────────────────────────────────────────────────

    def send_event(self, event: dict) -> None:
        if self._ws_client and self._ws_client.is_connected:
            self._ws_client.send(json.dumps(event))

    # ── Pairing ─────────────────────────────────────────────

    def generate_new_pairing_code(self) -> str:
        """Generate a new pairing code and emit signal."""
        # Invalidate any existing code
        if self._current_pairing_code:
            invalidate_pairing_code(self._current_pairing_code)

        code = generate_pairing_code()
        self._current_pairing_code = code
        self.pairing_code_available.emit(code)
        logger.info("[Companion] Pairing code generated: %s", code)
        return code

    def _handle_pair_verify(self, msg: dict) -> None:
        """Desktop receives a pair.verify from relay — validate code and respond."""
        payload = msg.get("payload", {})
        code = payload.get("code", "")
        phone_id = payload.get("phone_id", "")
        phone_name = payload.get("device_name", "Phone")
        original_msg_id = payload.get("original_msg_id", msg.get("id", ""))

        if not code:
            self.send_event(make_envelope("pair.error", {
                "message": "No pairing code provided",
            }, in_response_to=original_msg_id))
            return

        if not validate_pairing_code(code):
            self.send_event(make_envelope("pair.error", {
                "message": "Invalid or expired pairing code",
            }, in_response_to=original_msg_id))
            return

        # Clear used code
        self._current_pairing_code = ""

        token = create_device_token(
            desktop_id=get_device_id(),
            device_name=phone_name,
            role="phone"
        )

        # Send confirmation back through relay
        self.send_event(make_envelope("pair.confirmed", {
            "token": token,
            "desktop_id": get_device_id(),
            "desktop_name": get_device_display_name(),
            "phone_id": phone_id,
            "device_name": phone_name,
        }, in_response_to=original_msg_id))

        # Emit signal for UI
        self.pairing_complete.emit(phone_name)
        logger.info("[Companion] Phone paired: %s | token issued", phone_name)

    def _handle_pair_cancel(self, msg: dict) -> None:
        """Cancel active pairing."""
        if self._current_pairing_code:
            invalidate_pairing_code(self._current_pairing_code)
            self._current_pairing_code = ""
        self.pairing_code_invalidated.emit()
        logger.info("[Companion] Pairing cancelled")

    # ── Internal ────────────────────────────────────────────

    def _connect(self) -> None:
        url = self._settings.companion_relay_url
        if not url:
            logger.warning("[Companion] no relay URL configured")
            self.connection_status_changed.emit("error")
            return
        # Ensure ws:// prefix
        if not url.startswith("ws"):
            url = f"ws://{url}"
        # Ensure /ws path
        if not url.endswith("/ws"):
            url = url.rstrip("/") + "/ws"
        # Use device_id as token for now (Phase 4 upgrades to JWT)
        device_id = get_device_id()
        self._ws_client = CompanionWsClient(url, device_id, self)
        self._ws_client.connected.connect(self._on_connected)
        self._ws_client.disconnected.connect(self._on_disconnected)
        self._ws_client.message_received.connect(self._on_raw_message)
        self._ws_client.connect_to_relay()

    def _on_connected(self) -> None:
        self.connection_status_changed.emit("connected")
        logger.info("[Companion] connected to Relay")
        # Send desktop.online event
        self.send_event(make_envelope("desktop.online", {
            "display_name": get_device_display_name(),
            "aura_version": __version__,
            "capabilities": ["chat.send", "project.list_recent", "conversation.*"],
        }, desktop_id=get_device_id()))

    def _on_disconnected(self) -> None:
        self.connection_status_changed.emit("error")
        logger.warning("[Companion] disconnected from Relay")

    def _on_raw_message(self, raw: str) -> None:
        """Handle an incoming message from Relay."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Companion] invalid JSON: %s", raw[:200])
            return
        self.message_received.emit(msg)
        # Phase 2: route chat commands through bridge
        msg_type = msg.get("type", "")
        if msg_type == "chat.send":
            self._handle_chat_send(msg)
        elif msg_type == "chat.cancel":
            self._handle_chat_cancel(msg)
        elif msg_type == "project.list_recent":
            self._handle_project_list_recent(msg)
        elif msg_type == "conversation.list":
            self._handle_conversation_list(msg)
        elif msg_type == "conversation.select":
            self._handle_conversation_select(msg)
        elif msg_type == "drone.list_recent":
            self._handle_drone_list_recent(msg)
        elif msg_type == "drone.status":
            self._handle_drone_status(msg)
        elif msg_type == "receipt.list_recent":
            self._handle_receipt_list_recent(msg)
        elif msg_type == "pair.verify":
            self._handle_pair_verify(msg)
        elif msg_type == "pair.cancel":
            self._handle_pair_cancel(msg)

    # ── Bridge chat handlers ────────────────────────────────

    def _handle_chat_send(self, msg: dict) -> None:
        if self._bridge is None:
            self.send_event(make_envelope("chat.error", {
                "message": "Companion is not connected to a conversation session.",
            }, in_response_to=msg.get("id", "")))
            return
        text = msg.get("payload", {}).get("text", "")
        if not text:
            return
        if self._bridge.is_running():
            self.send_event(make_envelope("chat.error", {
                "message": "A conversation is already in progress on the desktop.",
            }, in_response_to=msg.get("id", "")))
            return
        self._pending_chat_id = msg.get("id", "")
        self._pending_chat_phone_id = msg.get("sender_device_id", "")
        self._bridge.history.append_user_text(text)
        model = resolve_role_default_model(self._settings.planner_provider, "planner")
        thinking = self._settings.default_planner_thinking
        self._bridge.send(model=model, thinking=thinking, max_tool_rounds=self._settings.max_tool_rounds)

    def _handle_chat_cancel(self, msg: dict) -> None:
        if self._bridge and self._bridge.is_running():
            self._bridge.request_cancel()

    # ── Phase 3 command handlers ───────────────────────────────

    def _handle_project_list_recent(self, msg: dict) -> None:
        """List recent projects."""
        if not self._workspace_root:
            self.send_event(make_envelope("project.list_result", {
                "projects": [],
            }, in_response_to=msg.get("id", "")))
            return
        try:
            store = ProjectStore()
            projects = store.list_projects()
            projects.sort(key=lambda p: p.updated_at, reverse=True)
            dtos = []
            for p in projects[:20]:
                thread_count = 0
                try:
                    threads = store.list_threads(p)
                    thread_count = len(threads)
                except Exception as exc:
                    logger.debug("[Companion] thread count for %s: %s", p.id, exc)
                dtos.append(CompanionProject(
                    id=p.id,
                    name=p.name,
                    updated_at=p.updated_at,
                    thread_count=thread_count,
                ).to_dict())
            self.send_event(make_envelope("project.list_result", {
                "projects": dtos,
            }, in_response_to=msg.get("id", "")))
        except Exception as exc:
            logger.error("[Companion] project.list_recent error: %s", exc)
            self.send_event(make_envelope("project.list_result", {
                "projects": [],
                "error": str(exc),
            }, in_response_to=msg.get("id", "")))

    def _handle_conversation_list(self, msg: dict) -> None:
        """List threads for the current project, or a specified project."""
        payload = msg.get("payload", {})
        project_id = payload.get("project_id", self._current_project_id)
        if not project_id or not self._workspace_root:
            self.send_event(make_envelope("conversation.list_result", {
                "threads": [],
            }, in_response_to=msg.get("id", "")))
            return
        try:
            store = ProjectStore()
            project = store.load_project(project_id)
            if not project:
                self.send_event(make_envelope("conversation.list_result", {
                    "threads": [],
                    "error": "Project not found",
                }, in_response_to=msg.get("id", "")))
                return
            threads = store.list_threads(project)
            threads.sort(key=lambda t: t.updated_at, reverse=True)
            dtos = []
            for t in threads[:50]:
                dtos.append(CompanionThread(
                    id=t.id,
                    title=t.title or "Untitled",
                    updated_at=t.updated_at,
                    is_current=(t.id == self._current_project_id),
                ).to_dict())
            self.send_event(make_envelope("conversation.list_result", {
                "threads": dtos,
            }, in_response_to=msg.get("id", "")))
        except Exception as exc:
            logger.error("[Companion] conversation.list error: %s", exc)
            self.send_event(make_envelope("conversation.list_result", {
                "threads": [],
                "error": str(exc),
            }, in_response_to=msg.get("id", "")))

    def _handle_conversation_select(self, msg: dict) -> None:
        """Select a thread as the active conversation."""
        payload = msg.get("payload", {})
        thread_id = payload.get("thread_id", "")
        project_id = payload.get("project_id", self._current_project_id)
        if not thread_id or not project_id:
            self.send_event(make_envelope("conversation.selected", {
                "error": "Missing thread_id or project_id",
            }, in_response_to=msg.get("id", "")))
            return
        self._current_project_id = project_id
        self.send_event(make_envelope("conversation.selected", {
            "project_id": project_id,
            "thread_id": thread_id,
        }, in_response_to=msg.get("id", "")))

    def _handle_drone_list_recent(self, msg: dict) -> None:
        """List recent drone runs."""
        if not self._workspace_root:
            self.send_event(make_envelope("drone.list_result", {
                "runs": [],
            }, in_response_to=msg.get("id", "")))
            return
        try:
            root = Path(self._workspace_root)
            runs = RunHistoryStore.list_runs(root, limit=20)
            summaries = []
            for r in runs:
                summaries.append(ActiveRunSummary(
                    run_id=r.get("run_id", ""),
                    kind="drone",
                    label=r.get("drone_name", r.get("drone_id", "Drone")),
                    status=r.get("status", "unknown"),
                    started_at=r.get("started_at"),
                ).to_dict())
            self.send_event(make_envelope("drone.list_result", {
                "runs": summaries,
            }, in_response_to=msg.get("id", "")))
        except Exception as exc:
            logger.error("[Companion] drone.list_recent error: %s", exc)
            self.send_event(make_envelope("drone.list_result", {
                "runs": [],
                "error": str(exc),
            }, in_response_to=msg.get("id", "")))

    def _handle_drone_status(self, msg: dict) -> None:
        """Report current drone runner status."""
        if self._drone_runner is not None:
            try:
                state = self._drone_runner.run_state()
                from datetime import datetime
                started_at_str = datetime.fromtimestamp(state.started_at).isoformat() if state.started_at else None
                summary = ActiveRunSummary(
                    run_id=state.run_id,
                    kind="drone",
                    label=state.drone.name if hasattr(state, 'drone') and state.drone else "Drone",
                    status=state.status,
                    started_at=started_at_str,
                ).to_dict()
                self.send_event(make_envelope("drone.status_result", {
                    "running": True,
                    "run": summary,
                }, in_response_to=msg.get("id", "")))
                return
            except Exception as exc:
                logger.error("[Companion] drone.status error: %s", exc)
        self.send_event(make_envelope("drone.status_result", {
            "running": False,
            "run": None,
        }, in_response_to=msg.get("id", "")))

    def _handle_receipt_list_recent(self, msg: dict) -> None:
        """List recent receipts (same data as drone runs, ReceiptSummary DTO)."""
        if not self._workspace_root:
            self.send_event(make_envelope("receipt.list_result", {
                "receipts": [],
            }, in_response_to=msg.get("id", "")))
            return
        try:
            root = Path(self._workspace_root)
            runs = RunHistoryStore.list_runs(root, limit=20)
            receipts = []
            for r in runs:
                receipts.append(ReceiptSummary(
                    run_id=r.get("run_id", ""),
                    kind="drone",
                    label=r.get("drone_name", r.get("drone_id", "Drone")),
                    status=r.get("status", "unknown"),
                    completed_at=r.get("ended_at", r.get("started_at", "")),
                    summary=r.get("summary", ""),
                ).to_dict())
            self.send_event(make_envelope("receipt.list_result", {
                "receipts": receipts,
            }, in_response_to=msg.get("id", "")))
        except Exception as exc:
            logger.error("[Companion] receipt.list_recent error: %s", exc)
            self.send_event(make_envelope("receipt.list_result", {
                "receipts": [],
                "error": str(exc),
            }, in_response_to=msg.get("id", "")))

    def _on_bridge_content_delta(self, text: str) -> None:
        self.send_event(make_envelope("chat.message.delta", {
            "role": "assistant",
            "type": "content",
            "text": text,
        }, desktop_id=self._pending_chat_phone_id, in_response_to=self._pending_chat_id))

    def _on_bridge_reasoning_delta(self, text: str) -> None:
        self.send_event(make_envelope("chat.message.delta", {
            "role": "assistant",
            "type": "reasoning",
            "text": text,
        }, desktop_id=self._pending_chat_phone_id, in_response_to=self._pending_chat_id))

    def _on_bridge_stream_done(self, finish_reason: str, full_message: dict) -> None:
        content = full_message.get("content", "") if isinstance(full_message, dict) else ""
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(texts)
        self.send_event(make_envelope("chat.message.complete", {
            "role": "assistant",
            "text": content or "…",
            "finish_reason": finish_reason,
        }, desktop_id=self._pending_chat_phone_id, in_response_to=self._pending_chat_id))
        self._pending_chat_id = ""
        self._pending_chat_phone_id = ""

    def _on_bridge_api_error(self, status_code: int, message: str) -> None:
        self.send_event(make_envelope("chat.error", {
            "message": f"API error ({status_code}): {message}",
        }, desktop_id=self._pending_chat_phone_id, in_response_to=self._pending_chat_id))
        self._pending_chat_id = ""
        self._pending_chat_phone_id = ""

    def _on_bridge_finished(self) -> None:
        if self._pending_chat_id:
            self.send_event(make_envelope("chat.message.complete", {
                "role": "assistant",
                "text": "",
                "finish_reason": "cancelled",
            }, desktop_id=self._pending_chat_phone_id, in_response_to=self._pending_chat_id))
            self._pending_chat_id = ""
            self._pending_chat_phone_id = ""
