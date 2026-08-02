"""Handles send/stop/undo logic extracted from MainWindow.

Owns the message queue, vision fallback routing, and undo command
execution. Delegates to the bridge, chat view, and input panel.
"""

from __future__ import annotations

import logging
import re
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

_log = logging.getLogger(__name__)

from dataclasses import dataclass

from aura.config import PROVIDERS, AppSettings, ModelInfo, ThinkingMode, has_usable_provider_configuration
from aura.conversation.task_router import TaskLane, TaskRoute, classify_user_request
from aura.gui.input_panel import Attachment
from aura.git_ops import (
    recent_commit_log,
    restore_to_snapshot,
    undo_last_commit,
    working_tree_diff,
    working_tree_status,
)
from aura.gui.input_panel import SendPayload


def _extract_snapshot_sha(text: str) -> str | None:
    """Pull an explicit commit sha out of a restore request, if present."""
    match = re.search(r"\b([0-9a-f]{7,40})\b", str(text or "").lower())
    return match.group(1) if match else None


@dataclass
class QueuedItem:
    """A captured send request waiting for the current production run to finish.

    Each item preserves its own submission-time state so that changing controls
    after queueing does not alter an already queued request. The deterministic
    ``TaskRoute`` computed at submit time travels with the item so a queued
    message never loses or reuses another message's route.
    """
    text: str
    attachments: list[Attachment]
    model: str
    thinking: ThinkingMode
    route: TaskRoute


class SendHandler(QObject):
    """Handles send/stop/undo logic extracted from MainWindow.

    Owns the message queue for queuing payloads while the bridge is busy,
    orchestrates screenshot decompiler for image attachments, and
    processes the /undo git command.

    Signals:
        vision_done: Emitted (payload, descriptions, error) after the vision
            fallback thread completes, so the handler can finalise the send
            on the GUI thread.
    """

    vision_done = Signal(object, list, object)  # SendPayload, list[str], str|None
    drone_bay_requested = Signal()  # /drone command → open/toggle Drone Workbay
    answer_only_research_started = Signal()

    def __init__(
        self,
        bridge,
        chat,
        input_panel,
        settings: AppSettings,
        workspace_root: Path | None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._chat = chat
        self._input = input_panel
        self._settings = settings
        self._workspace_root = workspace_root

        # Queued messages sent while the bridge is running.
        self._message_queue: list[QueuedItem] = []

        # Pending model/thinking/route stored while vision thread is running.
        self._pending_model: str = ""
        self._pending_thinking: ThinkingMode = "off"
        self._pending_route: TaskRoute | None = None

        # Route of the most recently sent production message, so a retry of the
        # retained user turn keeps the same deterministic classification.
        self._last_sent_route: TaskRoute | None = None

        # Wire our own signal so _on_vision_done runs on the GUI thread.
        self.vision_done.connect(self._on_vision_done)

    # ---- public helpers (called externally from MainWindow) -----------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Update the workspace root path (called when user changes root)."""
        self._workspace_root = root

    def update_settings(self, settings: AppSettings) -> None:
        """Use the latest settings object after Settings is accepted."""
        self._settings = settings

    def clear_queue(self) -> None:
        """Clear any queued messages (called on new/open conversation)."""
        self._message_queue.clear()

    def process_message_queue(self, model: str, thinking: ThinkingMode) -> None:
        """Send the next queued message, if any."""
        self._process_message_queue(model, thinking)

    # ---- public API --------------------------------------------------------

    def handle_send(
        self,
        payload: SendPayload,
        model: str,
        thinking: ThinkingMode,
        route: TaskRoute | None = None,
    ) -> None:
        """Process a send payload: route built-ins, queue if busy, or send.

        ``route`` is the deterministic ``TaskRoute`` already selected for this
        payload (for example by a dequeued ``QueuedItem`` or the vision path).
        When omitted it is computed here with ``classify_user_request`` — the
        normal GUI path always supplies one.
        """
        if route is None:
            route = classify_user_request(payload.text)
        # Guard: no workspace selected
        if self._workspace_root is None:
            self._chat.add_error(
                "No workspace",
                "Open a project first. Try the Demo Project to test Aura safely, or open an existing project folder.",
            )
            return

        # Guard: no provider configured
        if not has_usable_provider_configuration(self._settings.provider):
            self._chat.add_error(
                "No AI provider configured",
                "Configure an AI provider in Settings → API Keys to start chatting. "
                "DeepSeek, OpenAI, Anthropic, Gemini, and OpenRouter are supported.\n\n"
                "You can also open/browse a project folder before configuring AI.",
            )
            return

        # Drone mode checks removed — drone lifecycle removed.

        if route.lane == TaskLane.built_in_action:
            self._chat.add_user(payload.text)
            self._handle_built_in_action(route.action, payload.text)
            return

        if self._bridge.is_running():
            item = QueuedItem(
                text=payload.text,
                attachments=list(payload.attachments),
                model=model,
                thinking=thinking,
                route=route,
            )
            self._message_queue.append(item)
            self._input.set_queued_messages(len(self._message_queue))
            return

        if route.lane == TaskLane.research and route.action == "web_research":
            self.answer_only_research_started.emit()

        # Check if the current model supports native vision
        m_info = self._get_current_model_info(model)
        native_vision = m_info.supports_vision if m_info else False

        # Prepare history append: image attachments go via multimodal content array.
        text = payload.text
        # Add text refs from non-image attachments to the text body so the model knows.
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]

        # --- Vision routing ---
        vision_descriptions: list[str] = []
        vision_error: str | None = None

        if image_atts and not native_vision:
            # Screenshot decompiler for non-vision models
            self._input.set_placeholder("Decompiling image (structural)...")
            self._input.setEnabled(False)

            self._pending_model = model
            self._pending_thinking = thinking
            self._pending_route = route

            def _run_vision():
                nonlocal vision_error
                try:
                    from aura.perception.decompiler import describe

                    for a in image_atts:
                        desc = describe(a.b64, context=payload.text)
                        vision_descriptions.append(desc)
                except Exception as exc:
                    vision_error = (
                        f"Screenshot decompiler failed: {exc}"
                    )

                # Marshal back to GUI thread to actually send the message
                self.vision_done.emit(payload, vision_descriptions, vision_error)

            threading.Thread(target=_run_vision, daemon=True).start()
            return  # Wait for _on_vision_done

        # Either no images or native vision supported
        self._finalize_send(
            payload, model, thinking, vision_descriptions, vision_error, route=route
        )

    def handle_stop(self) -> None:
        """Cancel the current bridge response, clear the message queue, but
        preserve the current draft and attachments in the composer."""
        self._bridge.request_cancel()
        self._message_queue.clear()
        self._input.set_queued_messages(0)

    def handle_retry_last(
        self,
        model: str,
        thinking: ThinkingMode,
        replay_cb=None,
    ) -> bool:
        """Rerun the most recent user turn after discarding its response."""
        if self._bridge.is_running():
            return False

        rewound = self._bridge.history.rewind_to_last_user_turn()
        if not rewound:
            self._chat.add_error("Retry", "No user message to retry.")
            return False

        self._message_queue.clear()
        self._input.set_queued_messages(0)
        self._chat.reset()
        if replay_cb is not None:
            replay_cb()
        self._chat.begin_assistant()
        self._bridge.send(
            model=model,
            thinking=thinking,
            max_tool_rounds=self._settings.max_tool_rounds,
            route=self._last_sent_route,
        )
        return True

    # ---- drone construction --------------------------------------------------

    # ---- undo --------------------------------------------------------------
    def _handle_built_in_action(self, action: str, text: str = "") -> None:
        """Run deterministic built-in actions without model or Worker dispatch."""
        if action == "undo":
            self._handle_undo()
            return
        if action == "restore_snapshot":
            self._handle_restore_snapshot(text)
            return
        if action == "git_status":
            self._handle_git_status()
            return
        if action == "git_diff":
            self._handle_git_diff()
            return
        if action == "git_log":
            self._handle_git_log()
            return
        if action == "drone_enter_mode":
            self.drone_bay_requested.emit()
            return
        self._chat.add_error("Built-in action", f"Unsupported action: {action}")

    def _handle_restore_snapshot(self, text: str = "") -> None:
        """Restore an explicitly named snapshot, or list the ones available."""
        if self._bridge.is_running():
            self._chat.add_error(
                "Restore snapshot",
                "Stop the running task before undoing or restoring a snapshot.",
            )
            return

        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Restore snapshot", "No workspace root set.")
            return

        sha = _extract_snapshot_sha(text)
        if sha:
            reply = QMessageBox.question(
                self._chat,
                "Restore Snapshot",
                f"This will discard ALL changes made after {sha}. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                self._chat.add_info("Restore snapshot", "Cancelled.")
                return
            ok, message = restore_to_snapshot(ws_root, sha)
            if ok:
                self._bridge.clear_pre_worker_snapshot()
                self._chat.add_info("Restore snapshot", message)
            else:
                self._chat.add_error("Restore snapshot", message)
            return

        # No sha given — show what can actually be restored to.
        lines = ["Name the snapshot to restore, e.g. `restore snapshot a1b2c3d`."]
        pre_worker = self._bridge.get_pre_worker_snapshot()
        if pre_worker:
            lines.append(
                f"\nPre-worker snapshot: {pre_worker} (or use /undo to return to it)."
            )
        ok, log_text, message = recent_commit_log(ws_root)
        if ok and log_text.strip():
            lines.append(f"\nRecent commits:\n{log_text.strip()}")
        elif not ok:
            lines.append(f"\nCould not read git history: {message}")
        self._chat.add_info("Restore snapshot", "\n".join(lines))

    def _handle_undo(self) -> None:
        """Handle /undo command — restore to pre-worker snapshot or git reset."""
        ws_root = self._workspace_root
        if self._bridge.is_running():
            self._chat.add_error(
                "Undo",
                "Stop the running task before undoing or restoring a snapshot.",
            )
            return
        if ws_root is None:
            self._chat.add_error("Undo", "No workspace root set.")
            return

        # Check for pre-worker snapshot first (more reliable)
        snapshot_sha = self._bridge.get_pre_worker_snapshot()
        if snapshot_sha is not None:
            # Confirm destructive restore
            reply = QMessageBox.question(
                self._chat,  # parent widget (ChatView is a QWidget)
                "Restore to Pre-Worker State",
                "This will discard ALL changes since the worker started "
                "(including any intervening commits). Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                ok, message = restore_to_snapshot(ws_root, snapshot_sha)
                self._bridge.clear_pre_worker_snapshot()
                if ok:
                    self._chat.add_info("Undo", message)
                else:
                    self._chat.add_error("Undo", message)
        else:
            # Fall back to simple undo_last_commit
            ok, message = undo_last_commit(ws_root)
            if ok:
                self._chat.add_info("Undo", message)
            else:
                self._chat.add_error("Undo", message)

    def _handle_git_status(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git status", "No workspace root set.")
            return

        ok, status, message = working_tree_status(ws_root)
        if ok:
            self._chat.add_info("Git status", status or "Working tree clean.")
        else:
            self._chat.add_error("Git status", message)

    def _handle_git_diff(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git diff", "No workspace root set.")
            return

        ok, diff, message = working_tree_diff(ws_root)
        if ok:
            self._chat.add_info("Git diff", diff or "No unstaged changes.")
        else:
            self._chat.add_error("Git diff", message)

    def _handle_git_log(self) -> None:
        ws_root = self._workspace_root
        if ws_root is None:
            self._chat.add_error("Git log", "No workspace root set.")
            return

        ok, log_text, message = recent_commit_log(ws_root)
        if ok:
            self._chat.add_info("Git log", log_text or "No commits found.")
        else:
            self._chat.add_error("Git log", message)

    # ---- vision done slot --------------------------------------------------

    def _on_vision_done(self, payload: SendPayload, descriptions: list[str], error: str | None) -> None:
        """Called when the vision fallback thread completes."""
        self._input.setEnabled(True)
        self._input.set_placeholder("")
        self._finalize_send(
            payload,
            self._pending_model,
            self._pending_thinking,
            descriptions,
            error,
            route=self._pending_route,
        )

    # ---- finalise send -----------------------------------------------------

    def _finalize_send(
        self,
        payload: SendPayload,
        model: str,
        thinking: ThinkingMode,
        vision_descriptions: list[str],
        vision_error: str | None,
        route: TaskRoute | None = None,
    ) -> None:
        """Build the message parts, append to history, and send via the bridge.

        The deterministic route selected at send time travels with the message
        into ``ConversationBridge.send`` so production context composition
        treats this turn's classification as authoritative.
        """
        self._last_sent_route = route
        image_atts = [a for a in payload.attachments if a.kind == "image" and a.b64]
        text = payload.text
        text_refs = [a.text_ref for a in payload.attachments if a.text_ref]
        if text_refs:
            ref_block = "\n".join(text_refs)
            text = f"{text}\n\n{ref_block}".strip() if text else ref_block

        # Determine if we should send a native multimodal payload
        m_info = self._get_current_model_info(model)
        native_vision = m_info.supports_vision if m_info else False

        if native_vision and image_atts:
            # Construct native multimodal parts
            parts = []
            if text:
                parts.append({"type": "text", "text": text})
            for a in image_atts:
                parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{a.b64}"},
                })
            self._bridge.history.append_user_multimodal(parts)
            display_text = text
        elif vision_descriptions:
            # Build vision block from local fallback
            vision_block_parts = []
            for i, desc in enumerate(vision_descriptions):
                vision_block_parts.append(
                    f"[Image {i + 1} structural decompile:]\n{desc}"
                )
            vision_block = "\n\n---\n\n".join(vision_block_parts)

            if vision_error:
                vision_block += f"\n\n[Vision error: {vision_error}]"

            # Final text for the model
            final_text = f"{vision_block}\n\n[User's question:]\n{text}" if text else vision_block
            display_text = final_text
            history_text = final_text
            self._bridge.history.append_user_text(history_text)
        elif vision_error and not vision_descriptions and image_atts:
            self._chat.add_error("Vision fallback failed", vision_error)
            return
        elif vision_error and not vision_descriptions:
            final_text = f"{text}\n\n[Note: {vision_error}]" if text else f"[Vision error: {vision_error}]"
            display_text = final_text
            history_text = final_text
            self._bridge.history.append_user_text(history_text)
        else:
            # No images
            display_text = text
            history_text = text
            self._bridge.history.append_user_text(history_text)

        self._chat.add_user(display_text, [a.b64 for a in image_atts] or None)
        self._chat.scroll_to_bottom(force=True)
        self._chat.begin_assistant()

        _log.info(
            "send_start model=%s thinking=%s workspace_root=%s",
            model, thinking, self._workspace_root,
        )
        self._bridge.send(
            model=model,
            thinking=thinking,
            max_tool_rounds=self._settings.max_tool_rounds,
            route=route,
        )

    # ---- model info lookup -------------------------------------------------

    def _get_current_model_info(self, model: str) -> ModelInfo | None:
        """Look up metadata for the model from the provider used for chat sends."""
        cfg = PROVIDERS.get(self._settings.provider)
        if not cfg:
            return None
        return cfg.models.get(model)

    # ---- message queue -----------------------------------------------------

    def _process_message_queue(self, model: str, thinking: ThinkingMode) -> None:
        """Send the next queued message, if any."""
        if self._bridge.is_running():
            return
        if not self._message_queue:
            return
        item = self._message_queue.pop(0)
        self._input.set_queued_messages(len(self._message_queue))
        # Reconstruct a SendPayload from the queued item, which captured its
        # own model and thinking at queue time.
        payload = SendPayload(text=item.text, attachments=list(item.attachments))
        self.handle_send(payload, item.model, item.thinking, route=item.route)
