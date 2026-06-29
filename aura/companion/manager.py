"""CompanionManager — lifecycle, signal routing, and event dispatch."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from PySide6.QtCore import QObject, QThread, Signal, Slot

from aura.companion.auth import (
    create_device_token,
    generate_pairing_code,
    generate_ticket,
    get_device_display_name,
    get_device_id,
    invalidate_pairing_code,
    pairing_code_expiry,
    validate_pairing_code,
)
from aura.companion.client import CompanionWsClient
from aura.companion.commands import CommandContext
from aura.companion.commands.conversations import (
    handle_conversation_create,
    handle_conversation_history,
    handle_conversation_list,
    handle_conversation_select,
)
from aura.companion.commands.drones import handle_drone_list_recent, handle_drone_status
from aura.companion.commands.projects import handle_project_list_recent
from aura.companion.commands.receipts import handle_receipt_list_recent
from aura.companion.defaults import DEFAULT_HOSTED_COMPANION_WEB_URL
from aura.companion.local_relay import (
    LocalRelayError,
    ensure_local_relay,
    is_local_relay_url,
    normalize_relay_url,
    relay_port,
    stop_managed_relay,
)
from aura.companion.protocol import make_envelope
from aura.companion.replies import build_reply_envelope
from aura.companion.router import CompanionCommandRouter
from aura.companion.state import CompanionState
from aura.settings import AppSettings, resolve_role_default_model
from aura.version import __version__

logger = logging.getLogger(__name__)


class _RelayConnectWorker(QObject):
    finished = Signal(int, str)
    failed = Signal(int, str)

    def __init__(self, generation: int, url: str) -> None:
        super().__init__()
        self._generation = generation
        self._url = url

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self._generation, ensure_local_relay(self._url))
        except LocalRelayError as exc:
            self.failed.emit(self._generation, str(exc))
        except Exception as exc:
            logger.exception("[Companion] local relay startup failed")
            self.failed.emit(self._generation, str(exc))


class CompanionManager(QObject):
    """Manages the Companion (mobile web control plane) connection lifecycle.

    Owns the WebSocket client, routes incoming commands, and forwards
    desktop events to the phone via Relay.
    """

    connection_status_changed = Signal(str)  # "disabled", "connecting", "connected", "error"
    connection_error = Signal(str)           # exact error string from the ws worker
    message_received = Signal(dict)
    pairing_code_available = Signal(str)       # Emitted when a pairing code is generated
    pairing_code_invalidated = Signal()        # Emitted when a pairing code is cancelled
    pairing_complete = Signal(str)             # Emitted when a phone pairs successfully (param: device_name)
    conversation_selected_by_companion = Signal(Path, Path)  # project_root_path, conversation_path

    def __init__(self, settings: AppSettings | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._settings = settings or AppSettings()
        self._state = CompanionState()
        self._ws_client: CompanionWsClient | None = None
        self._retiring_clients: set[CompanionWsClient] = set()
        self._relay_workers: list[tuple[QThread, _RelayConnectWorker]] = []
        self._connection_generation = 0
        self._bridge: Any = None
        self._drone_runner: Any = None
        self._project_store: Any = None
        self._router = CompanionCommandRouter()
        self._register_command_handlers()

    def _register_command_handlers(self) -> None:
        self._router.register(
            "project.list_recent",
            lambda msg: handle_project_list_recent(msg, self._make_command_context()),
        )
        self._router.register(
            "conversation.list",
            lambda msg: handle_conversation_list(msg, self._make_command_context()),
        )
        self._router.register(
            "conversation.create",
            lambda msg: handle_conversation_create(msg, self._make_command_context()),
        )
        self._router.register(
            "conversation.select",
            lambda msg: handle_conversation_select(msg, self._make_command_context()),
        )
        self._router.register(
            "conversation.history",
            lambda msg: handle_conversation_history(msg, self._make_command_context()),
        )
        self._router.register(
            "drone.list_recent",
            lambda msg: handle_drone_list_recent(msg, self._make_command_context()),
        )
        self._router.register(
            "drone.status",
            lambda msg: handle_drone_status(msg, self._make_command_context()),
        )
        self._router.register(
            "receipt.list_recent",
            lambda msg: handle_receipt_list_recent(msg, self._make_command_context()),
        )
        # Manager-internal handlers (stay in CompanionManager for this pass)
        self._router.register("chat.send", self._handle_chat_send)
        self._router.register("chat.cancel", self._handle_chat_cancel)
        self._router.register("pair.verify", self._handle_pair_verify)
        self._router.register("pair.cancel", self._handle_pair_cancel)
        self._router.register("companion.verify", self._handle_companion_verify)

    def _make_command_context(self) -> CommandContext:
        return CommandContext(
            state=self._state,
            settings=self._settings,
            send_fn=self.send_event,
            bridge=self._bridge,
            drone_runner=self._drone_runner,
            project_store=self._project_store,
            on_conversation_selected=self.conversation_selected_by_companion.emit,
        )

    # ── Lifecycle ────────────────────────────────────────────

    def start(self) -> None:
        if not self._settings.companion_enabled:
            self.connection_status_changed.emit("disabled")
            return
        self._connection_generation += 1
        self.connection_status_changed.emit("connecting")
        logger.info("[Companion] starting — relay: %s", self._settings.companion_relay_url)
        self._connect(self._connection_generation)

    def stop(self) -> None:
        self._connection_generation += 1
        self._close_current_client()
        self._stop_managed_relay_async()
        self.connection_status_changed.emit("disabled")
        logger.info("[Companion] stopped")

    def update_settings(self, settings: AppSettings) -> None:
        previous = self._settings
        was_enabled = previous.companion_enabled
        enabled_changed = was_enabled != settings.companion_enabled
        relay_changed = self._normalized_relay_url(previous.companion_relay_url) != self._normalized_relay_url(
            settings.companion_relay_url
        )
        self._settings = settings

        if enabled_changed and not settings.companion_enabled:
            self.stop()
            return

        if enabled_changed and settings.companion_enabled:
            self.start()
            return

        if settings.companion_enabled and relay_changed:
            self._connection_generation += 1
            self._close_current_client()
            if is_local_relay_url(previous.companion_relay_url) and not is_local_relay_url(settings.companion_relay_url):
                self._stop_managed_relay_async()
            self.connection_status_changed.emit("connecting")
            self._connect(self._connection_generation)

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
            except (TypeError, RuntimeError) as exc:
                logger.debug("[Companion] bridge disconnect skipped: %s", exc)
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
        self._state.workspace_root = path

    def set_current_project(self, project_id: str, project_name: str = "") -> None:
        self._state.current_project_id = project_id
        self._state.paired_project_name = project_name

    def set_current_conversation(self, conversation_id: str) -> None:
        self._state.current_conversation_id = conversation_id
        self._state.conversation_loaded = bool(conversation_id)

    # ── Send ────────────────────────────────────────────────

    def send_event(self, event: dict) -> None:
        if self._ws_client and self._ws_client.is_connected:
            self._ws_client.send(json.dumps(event))

    def _reply_to_sender(
        self,
        msg: dict,
        msg_type: str,
        payload: dict,
        *,
        project_id: str = "",
        conversation_id: str = "",
    ) -> None:
        env = build_reply_envelope(msg, msg_type, payload, project_id=project_id, conversation_id=conversation_id)
        if env:
            self.send_event(env)

    # ── Pairing ─────────────────────────────────────────────

    def generate_new_pairing_code(self) -> str:
        """Generate a new pairing code and emit signal."""
        # Invalidate any existing code
        if self._state.current_pairing_code:
            invalidate_pairing_code(self._state.current_pairing_code)

        code = generate_pairing_code()
        self._state.current_pairing_code = code
        self.pairing_code_available.emit(code)
        logger.info("[Companion] Pairing code generated: %s", code)
        return code

    def start_pairing(self) -> dict:
        """Create a fresh pairing code and return a structured payload.

        Returns a dict with: code, expires_at (unix), ticket, desktop_name,
        pair_url (the URL the phone opens to auto-fill the pair screen).
        The pair URL points at the configured companion web URL with a
        time-limited ticket — see AppSettings.companion_web_url.
        """
        code = self.generate_new_pairing_code()
        expires_at = pairing_code_expiry(code) or 0.0
        desktop_id = get_device_id()
        desktop_name = self._settings.companion_display_name or get_device_display_name()
        web_url = (self._settings.companion_web_url or "http://localhost:5173").rstrip("/")

        ticket = generate_ticket(
            desktop_id=desktop_id,
            pairing_code=code,
            desktop_name=desktop_name,
            project_id=self._state.current_project_id,
            conversation_id=self._state.current_conversation_id,
        )

        # Register the ticket with the relay so it can resolve context
        self.send_event(make_envelope("ticket.register", {
            "ticket": ticket,
            "desktop_id": desktop_id,
            "desktop_name": desktop_name,
            "code": code,
            "project_id": self._state.current_project_id,
            "conversation_id": self._state.current_conversation_id,
        }))

        runtime_relay_url = self._state.active_relay_url or normalize_relay_url(self._settings.companion_relay_url)

        # Hosted web + localhost relay → phones can't reach localhost, skip relay param
        is_hosted_web = web_url.rstrip("/") == DEFAULT_HOSTED_COMPANION_WEB_URL.rstrip("/")
        if is_hosted_web and is_local_relay_url(runtime_relay_url):
            params = urlencode({"ticket": ticket})
        else:
            params = urlencode({"ticket": ticket, "relay": runtime_relay_url})
        pair_url = f"{web_url}/pair?{params}"
        return {
            "code": code,
            "expires_at": expires_at,
            "ticket": ticket,
            "desktop_name": desktop_name,
            "pair_url": pair_url,
            "relay_url": runtime_relay_url,
        }

    def cancel_pairing(self) -> None:
        """Invalidate the active pairing code (user closed the pair dialog)."""
        if self._state.current_pairing_code:
            invalidate_pairing_code(self._state.current_pairing_code)
            self._state.current_pairing_code = ""
            self.pairing_code_invalidated.emit()

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
        self._state.current_pairing_code = ""

        token = create_device_token(
            desktop_id=get_device_id(),
            device_name=phone_name,
            role="phone"
        )

        # Resolve safe context to deliver to the phone after pairing
        project_name = ""
        if self._state.current_project_id and self._project_store:
            try:
                project = self._project_store.load_project(self._state.current_project_id)
                if project:
                    project_name = project.name
            except Exception as exc:
                logger.debug("[Companion] could not resolve project name for pairing context: %s", exc)

        safe_context = {
            "project_id": self._state.current_project_id,
            "project_name": project_name,
            "conversation_id": self._state.current_conversation_id,
        }
        self._state.paired_context = safe_context

        # Send confirmation back through relay with safe context
        self.send_event(make_envelope("pair.confirmed", {
            "token": token,
            "desktop_id": get_device_id(),
            "desktop_name": get_device_display_name(),
            "phone_id": phone_id,
            "device_name": phone_name,
            **safe_context,
        }, in_response_to=original_msg_id))

        # Emit signal for UI
        self.pairing_complete.emit(phone_name)
        logger.info("[Companion] Phone paired: %s | token issued", phone_name)

    def _handle_pair_cancel(self, msg: dict) -> None:
        """Cancel active pairing."""
        if self._state.current_pairing_code:
            invalidate_pairing_code(self._state.current_pairing_code)
            self._state.current_pairing_code = ""
        self.pairing_code_invalidated.emit()
        logger.info("[Companion] Pairing cancelled")

    def _handle_companion_verify(self, msg: dict) -> None:
        """Respond to phone connection verification — lightweight, read-only."""
        desktop_name = self._settings.companion_display_name or get_device_display_name()
        self._reply_to_sender(msg, "companion.verify_ack", {
            "desktop_id": get_device_id(),
            "desktop_name": desktop_name,
            "project_id": self._state.current_project_id,
            "conversation_id": self._state.current_conversation_id,
        })

    # ── Internal ────────────────────────────────────────────

    @staticmethod
    def _normalized_relay_url(url: str) -> str:
        return normalize_relay_url(url)

    def _connect(self, generation: int | None = None) -> None:
        if generation is None:
            self._connection_generation += 1
            generation = self._connection_generation
        url = self._settings.companion_relay_url
        if not url:
            logger.warning("[Companion] no relay URL configured")
            self.connection_error.emit("Companion relay URL is missing.")
            self.connection_status_changed.emit("error")
            return
        url = normalize_relay_url(url)
        self._state.active_relay_url = url
        if is_local_relay_url(url):
            self.connection_status_changed.emit("starting_local_relay")
            self._start_local_relay_worker(generation, url)
            return

        self._connect_client(generation, url)

    def _connect_client(self, generation: int, url: str) -> None:
        if generation != self._connection_generation or not self._settings.companion_enabled:
            return
        self._state.active_relay_url = url

        device_id = get_device_id()
        desktop_secret = os.environ.get("AURA_COMPANION_DESKTOP_SECRET", "")

        client = CompanionWsClient(url, device_id, desktop_secret, self)
        self._ws_client = client
        client.connected.connect(lambda c=client: self._on_connected(c))
        client.disconnected.connect(lambda c=client: self._on_disconnected(c))
        client.error.connect(lambda err, c=client: self._on_client_error(c, err))
        client.message_received.connect(lambda raw, c=client: self._on_raw_message(c, raw))
        client.connect_to_relay()

    def _start_local_relay_worker(self, generation: int, url: str) -> None:
        thread = QThread()
        worker = _RelayConnectWorker(generation, url)
        worker.moveToThread(thread)
        self._relay_workers.append((thread, worker))
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_local_relay_ready)
        worker.failed.connect(self._on_local_relay_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread, w=worker: self._forget_relay_worker(t, w))
        thread.start()

    def _forget_relay_worker(self, thread: QThread, worker: _RelayConnectWorker) -> None:
        self._relay_workers = [(t, w) for t, w in self._relay_workers if t is not thread and w is not worker]

    def _on_local_relay_ready(self, generation: int, url: str) -> None:
        if generation != self._connection_generation or not self._settings.companion_enabled:
            return
        self._state.active_relay_url = url
        self.connection_status_changed.emit("connecting")
        self._connect_client(generation, url)

    def _on_local_relay_failed(self, generation: int, error: str) -> None:
        if generation != self._connection_generation or not self._settings.companion_enabled:
            return
        logger.warning("[Companion] local relay unavailable: %s", error)
        self.connection_error.emit(error)
        self.connection_status_changed.emit("error")

    def _close_current_client(self) -> None:
        client = self._ws_client
        self._ws_client = None
        if client is None:
            return
        self._retiring_clients.add(client)
        client.closed.connect(lambda c=client: self._on_retired_client_closed(c))
        client.close()

    def _on_retired_client_closed(self, client: CompanionWsClient) -> None:
        self._retiring_clients.discard(client)
        client.deleteLater()

    def _stop_managed_relay_async(self) -> None:
        threading.Thread(target=stop_managed_relay, name="AuraCompanionRelayStop", daemon=True).start()

    def _on_client_error(self, client: CompanionWsClient, error_str: str) -> None:
        if client is not self._ws_client:
            return
        self.connection_error.emit(self._friendly_connection_error(error_str))

    def _on_connected(self, client: CompanionWsClient | None = None) -> None:
        if client is not None and client is not self._ws_client:
            return
        self.connection_status_changed.emit("connected")
        logger.info("[Companion] connected to Relay")
        # Send desktop.online event
        self.send_event(make_envelope("desktop.online", {
            "display_name": get_device_display_name(),
            "aura_version": __version__,
            "capabilities": ["chat.send", "project.list_recent", "conversation.*"],
        }, desktop_id=get_device_id()))

    def _on_disconnected(self, client: CompanionWsClient | None = None) -> None:
        if client is not None and client is not self._ws_client:
            return
        self.connection_status_changed.emit("error")
        logger.warning("[Companion] disconnected from Relay")

    def _friendly_connection_error(self, error_str: str) -> str:
        lowered = error_str.lower()
        if is_local_relay_url(self._state.active_relay_url) and "invalidstatus" in lowered and "404" in lowered:
            port = relay_port(self._state.active_relay_url)
            return (
                f"Port {port} is already in use by another service. Close that service or change the "
                "Companion relay port in Advanced / Self-hosting."
            )
        if "invalidstatus" in lowered:
            return "Could not connect to the Companion relay. Check the relay URL in Advanced / Self-hosting."
        return error_str

    def _on_raw_message(self, client: CompanionWsClient, raw: str) -> None:
        """Handle an incoming message from Relay."""
        if client is not self._ws_client:
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[Companion] invalid JSON: %s", raw[:200])
            return
        self.message_received.emit(msg)
        self._router.dispatch(msg)

    # ── Bridge chat handlers ────────────────────────────────

    def _handle_chat_send(self, msg: dict) -> None:
        payload = msg.get("payload", {})
        sender_phone_id = msg.get("sender_device_id", "")
        chat_id = msg.get("id", "")
        text = (payload.get("text", "") or "").strip()

        def reply_error(message: str) -> None:
            self._reply_to_sender(msg, "chat.error", {"message": message})

        if self._bridge is None:
            reply_error("Companion is not connected to a conversation session.")
            return

        if not text:
            reply_error("Message text is empty.")
            return

        project_id = (
            msg.get("project_id")
            or payload.get("project_id", "")
            or self._state.current_project_id
        )
        conversation_id = (
            msg.get("conversation_id")
            or payload.get("conversation_id", "")
            or self._state.current_conversation_id
        )
        if not project_id or not conversation_id:
            reply_error("Open or create a conversation in Aura Desktop, then try again.")
            return

        self._state.current_project_id = project_id
        self._state.current_conversation_id = conversation_id
        self._state.conversation_loaded = True

        if self._bridge.is_running():
            reply_error("A conversation is already in progress on the desktop.")
            return

        self._state.pending_chat_id = chat_id
        self._state.pending_chat_phone_id = sender_phone_id
        self._bridge.history.append_user_text(text)
        model = resolve_role_default_model(self._settings.planner_provider, "planner")
        thinking = self._settings.default_planner_thinking
        self._bridge.send(model=model, thinking=thinking, max_tool_rounds=self._settings.max_tool_rounds)

    def _handle_chat_cancel(self, msg: dict) -> None:
        if self._bridge and self._bridge.is_running():
            self._bridge.request_cancel()

    # │ Read-only command handlers are now in aura/companion/commands/
    # │ and dispatched via self._router in _on_raw_message.

    def complete_conversation_select(self, success: bool, error_text: str = "") -> None:
        """Called by MainWindow after attempting to load a companion-requested thread."""
        msg = self._state.pending_select_msg
        self._state.pending_select_msg = None
        if msg is None:
            return
        payload = msg.get("payload", {})
        thread_id = payload.get("thread_id", "")
        project_id = payload.get("project_id", self._state.current_project_id)
        if success:
            self._state.current_project_id = project_id
            self._state.current_conversation_id = thread_id
            self._state.conversation_loaded = True
            self._reply_to_sender(msg, "conversation.selected", {
                "project_id": project_id,
                "thread_id": thread_id,
            })
        else:
            self._reply_to_sender(msg, "conversation.selected", {
                "project_id": project_id,
                "thread_id": thread_id,
                "status": "error",
                "error": error_text,
            })



    def _on_bridge_content_delta(self, text: str) -> None:
        self.send_event(make_envelope("chat.message.delta", {
            "role": "assistant",
            "type": "content",
            "text": text,
        }, desktop_id=self._state.pending_chat_phone_id, in_response_to=self._state.pending_chat_id))

    def _on_bridge_reasoning_delta(self, text: str) -> None:
        self.send_event(make_envelope("chat.message.delta", {
            "role": "assistant",
            "type": "reasoning",
            "text": text,
        }, desktop_id=self._state.pending_chat_phone_id, in_response_to=self._state.pending_chat_id))

    def _on_bridge_stream_done(self, finish_reason: str, full_message: dict) -> None:
        content = full_message.get("content", "") if isinstance(full_message, dict) else ""
        if isinstance(content, list):
            texts = [p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"]
            content = " ".join(texts)
        self.send_event(make_envelope("chat.message.complete", {
            "role": "assistant",
            "text": content or "…",
            "finish_reason": finish_reason,
        }, desktop_id=self._state.pending_chat_phone_id, in_response_to=self._state.pending_chat_id))
        self._state.pending_chat_id = ""
        self._state.pending_chat_phone_id = ""

    def _on_bridge_api_error(self, status_code: int, message: str) -> None:
        self.send_event(make_envelope("chat.error", {
            "message": f"API error ({status_code}): {message}",
        }, desktop_id=self._state.pending_chat_phone_id, in_response_to=self._state.pending_chat_id))
        self._state.pending_chat_id = ""
        self._state.pending_chat_phone_id = ""

    def _on_bridge_finished(self) -> None:
        if self._state.pending_chat_id:
            self.send_event(make_envelope("chat.message.complete", {
                "role": "assistant",
                "text": "",
                "finish_reason": "cancelled",
            }, desktop_id=self._state.pending_chat_phone_id, in_response_to=self._state.pending_chat_id))
            self._state.pending_chat_id = ""
            self._state.pending_chat_phone_id = ""
