"""Handles send/stop/undo logic extracted from MainWindow.

Owns the message queue, vision fallback routing, and undo command
execution. Delegates to the bridge, chat view, and input panel.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox

from aura.config import PROVIDERS, AppSettings, ModelInfo, ThinkingMode
from aura.gui.input_panel import SendPayload
from aura.git_ops import undo_last_commit, restore_to_snapshot


class SendHandler(QObject):
    """Handles send/stop/undo logic extracted from MainWindow.

    Owns the message queue for queuing payloads while the bridge is busy,
    orchestrates vision fallback (local model image description), and
    processes the /undo git command.

    Signals:
        vision_done: Emitted (payload, descriptions, error) after the vision
            fallback thread completes, so the handler can finalise the send
            on the GUI thread.
    """

    vision_done = Signal(object, list, object)  # SendPayload, list[str], str|None

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

        # Queued messages sent while worker is running.
        self._message_queue: list[SendPayload] = []

        # Pending model/thinking stored while vision thread is running.
        self._pending_model: str = ""
        self._pending_thinking: ThinkingMode = "off"

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

    # ---- public API --------------------------------------------------------

    def handle_send(self, payload: SendPayload, model: str, thinking: ThinkingMode) -> None:
        """Process a send payload: intercept /undo, queue if busy, or send."""
        # Intercept /undo command
        if payload.text.strip().lower() == "/undo":
            self._chat.add_user("/undo")
            self._handle_undo()
            return

        if self._bridge.is_running():
            self._message_queue.append(payload)
            self._input.set_queued_messages(len(self._message_queue))
            return

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

        if image_atts and not native_vision and not self._settings.vision_enabled:
            self._chat.add_error(
                "Images not supported",
                "The selected model cannot read images. Enable local vision fallback or choose a vision-capable model.",
            )
            return

        if image_atts and not native_vision and self._settings.vision_enabled:
            # Fall back to local vision model for descriptive middleman
            self._input.set_placeholder("Analyzing images (local fallback)...")
            self._input.setEnabled(False)

            self._pending_model = model
            self._pending_thinking = thinking

            def _run_vision():
                nonlocal vision_error
                try:
                    from aura.vision import VisionClient

                    client = VisionClient(
                        endpoint=self._settings.vision_endpoint,
                        model=self._settings.vision_model,
                    )
                    for a in image_atts:
                        desc = client.describe(a.b64)
                        vision_descriptions.append(desc)
                except Exception as exc:
                    vision_error = (
                        f"Local vision model unavailable "
                        f"({self._settings.vision_model}): {exc}"
                    )

                # Marshal back to GUI thread to actually send the message
                self.vision_done.emit(payload, vision_descriptions, vision_error)

            threading.Thread(target=_run_vision, daemon=True).start()
            return  # Wait for _on_vision_done

        # Either no images, native vision supported, or local vision disabled
        self._finalize_send(payload, model, thinking, vision_descriptions, vision_error)

    def handle_stop(self) -> None:
        """Cancel the current bridge response and clear the message queue."""
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
        )
        return True

    # ---- undo --------------------------------------------------------------

    def _handle_undo(self) -> None:
        """Handle /undo command — restore to pre-worker snapshot or git reset."""
        ws_root = self._workspace_root
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
        )

    # ---- finalise send -----------------------------------------------------

    def _finalize_send(
        self,
        payload: SendPayload,
        model: str,
        thinking: ThinkingMode,
        vision_descriptions: list[str],
        vision_error: str | None,
    ) -> None:
        """Build the message parts, append to history, and send via the bridge."""
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
                    f"[Image {i + 1} description via local vision model:]\n{desc}"
                )
            vision_block = "\n\n---\n\n".join(vision_block_parts)

            if vision_error:
                vision_block += f"\n\n[Vision error: {vision_error}]"

            # Final text for the model
            final_text = f"{vision_block}\n\n[User's question:]\n{text}" if text else vision_block
            display_text = final_text
            self._bridge.history.append_user_text(final_text)
        elif vision_error and not vision_descriptions and image_atts:
            self._chat.add_error("Vision fallback failed", vision_error)
            return
        elif vision_error and not vision_descriptions:
            final_text = f"{text}\n\n[Note: {vision_error}]" if text else f"[Vision error: {vision_error}]"
            display_text = final_text
            self._bridge.history.append_user_text(final_text)
        else:
            # No images or vision disabled
            if image_atts:
                self._chat.add_error(
                    "Images not supported",
                    "The selected model cannot read images. Enable local vision fallback or choose a vision-capable model.",
                )
                return
            else:
                display_text = text
                self._bridge.history.append_user_text(text)

        self._chat.add_user(display_text, [a.b64 for a in image_atts] or None)
        self._chat.scroll_to_bottom(force=True)
        self._chat.begin_assistant()

        self._bridge.send(
            model=model,
            thinking=thinking,
            max_tool_rounds=self._settings.max_tool_rounds,
        )

    # ---- model info lookup -------------------------------------------------

    def _get_current_model_info(self, model: str) -> ModelInfo | None:
        """Look up metadata for the model from the provider used for chat sends."""
        cfg = PROVIDERS.get(self._settings.planner_provider)
        if not cfg:
            return None
        return cfg.models.get(model)

    # ---- message queue -----------------------------------------------------

    def _process_message_queue(self, model: str, thinking: ThinkingMode) -> None:
        """Send the next queued message, if any."""
        if not self._message_queue:
            return
        payload = self._message_queue.pop(0)
        self._input.set_queued_messages(len(self._message_queue))
        self.handle_send(payload, model, thinking)
