"""Handoff flow controller — extracted from MainWindow for a focused lifecycle."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from aura.config import APP_NAME, ThinkingMode
from aura.gui.input_panel import SendPayload
from aura.handoff import extract_handoff_text, generate_handoff_prompt, save_handoff

logger = logging.getLogger(__name__)


class MainWindowHandoffController(QObject):
    """Owns the pending-handoff flag and coordinates the handoff lifecycle.

    Avoids importing MainWindow; instead receives the objects and callbacks
    it needs via the constructor.
    """

    def __init__(
        self,
        bridge,
        send_handler,
        chat,
        input_panel,
        persistence,
        get_workspace_root: Callable[[], Path | None],
        get_model: Callable[[], str],
        get_thinking: Callable[[], ThinkingMode],
        reset_session_usage: Callable[[], None],
        parent_widget,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._send_handler = send_handler
        self._chat = chat
        self._input_panel = input_panel
        self._persistence = persistence
        self._get_workspace_root = get_workspace_root
        self._get_model = get_model
        self._get_thinking = get_thinking
        self._reset_session_usage = reset_session_usage
        self._parent_widget = parent_widget

        self._pending_handoff: bool = False

    @property
    def pending(self) -> bool:
        return self._pending_handoff

    def request_handoff(self) -> None:
        """Initiate the handoff generation flow."""
        if self._bridge.is_running():
            QMessageBox.information(
                self._parent_widget,
                APP_NAME,
                "Please wait for the current response to finish, or click Stop before generating a handoff.",
            )
            return

        workspace_root = self._get_workspace_root()
        if not workspace_root or not workspace_root.exists():
            QMessageBox.information(
                self._parent_widget,
                APP_NAME,
                "Set a workspace root before generating a handoff.",
            )
            return

        prompt_text = generate_handoff_prompt()
        payload = SendPayload(text=prompt_text, attachments=[])
        self._pending_handoff = True
        self._send_handler.handle_send(payload, self._get_model(), self._get_thinking())

    def finalize_handoff(self, full_message: dict) -> None:
        """Complete a pending handoff — save, start fresh conversation, inject context."""
        if not self._pending_handoff:
            return
        self._pending_handoff = False

        handoff_text = extract_handoff_text(full_message)
        if not handoff_text.strip():
            self._chat.add_error(
                "Handoff",
                "Handoff response was empty. Please try again.",
            )
            return

        workspace_root = self._get_workspace_root()
        if workspace_root is None:
            self._chat.add_error(
                "Handoff",
                "No workspace root set. Cannot save handoff.",
            )
            return

        try:
            save_handoff(workspace_root, handoff_text)
        except Exception as exc:
            self._chat.add_error(
                "Handoff",
                f"Could not save handoff: {exc}",
            )
            return

        # Start a fresh conversation
        self._persistence.new_conversation()
        self._send_handler.clear_queue()
        self._input_panel.set_queued_messages(0)
        self._reset_session_usage()

        # Add handoff to bridge history as prior context (no API call)
        self._bridge.history.append_user_text(
            f"[Handoff from previous conversation — use as context for the next user request]\n\n{handoff_text}"
        )

        # Show local-only assistant message
        self._chat.begin_assistant()
        self._chat.append_content("Context loaded. What do you need?")
        self._chat.assistant_done()

    def clear_on_error(self) -> None:
        """Reset pending handoff state on API error."""
        self._pending_handoff = False
