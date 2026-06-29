from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox

from aura.companion import CompanionManager
from aura.config import APP_NAME

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

logger = logging.getLogger(__name__)


class MainWindowCompanionController(QObject):
    """Owns the companion (mobile control plane) lifecycle for MainWindow."""

    def __init__(self, window: MainWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._window = window

        self._companion = CompanionManager(window._settings)
        self._companion.connection_status_changed.connect(self._on_companion_status)
        self._companion.connection_error.connect(self._on_companion_error)
        self._companion.message_received.connect(self._on_companion_message)
        self._companion.conversation_selected_by_companion.connect(self._on_companion_thread_selected)
        self._companion.set_bridge(window._bridge)
        self._companion.set_workspace_root(str(window._workspace_root))

        self._last_status: str = ""
        self._last_error: str = ""

        self._companion.start()

    @property
    def companion_manager(self):
        return self._companion

    def update_settings(self, settings) -> None:
        self._companion.update_settings(settings)

    def set_drone_runner(self, runner) -> None:
        self._companion.set_drone_runner(runner)

    def set_workspace_root(self, path: str) -> None:
        self._companion.set_workspace_root(path)

    def set_current_project(self, project_id: str, project_name: str) -> None:
        self._companion.set_current_project(project_id, project_name)

    def stop(self) -> None:
        self._companion.stop()

    def set_current_conversation(self, conversation_id: str) -> None:
        self._companion.set_current_conversation(conversation_id)

    def sync_context(self, project_id: str, thread_id: str) -> None:
        if project_id:
            self._companion.set_current_project(project_id)
        self._companion.set_current_conversation(thread_id or "")

    def _on_companion_status(self, status: str) -> None:
        self._last_status = status
        logger.info("[MainWindow] Companion status: %s", status)
        window = self._window
        if hasattr(window, '_edge_rail') and window._edge_rail is not None:
            window._edge_rail.set_companion_status(status)

    def _on_companion_error(self, error: str) -> None:
        self._last_status = "error"
        self._last_error = error
        window = self._window
        if hasattr(window, '_edge_rail') and window._edge_rail is not None:
            window._edge_rail.set_companion_status("error", error)

    def _on_companion_message(self, msg: dict) -> None:
        logger.debug("[MainWindow] Companion msg: %s", msg.get("type"))

    def sync_edge_rail_status(self) -> None:
        window = self._window
        if hasattr(window, '_edge_rail') and window._edge_rail is not None:
            if self._last_status == "error":
                window._edge_rail.set_companion_status("error", self._last_error)
            else:
                window._edge_rail.set_companion_status(self._last_status)

    def _on_companion_thread_selected(self, project_root: Path, conversation_path: Path) -> None:
        if self._window._bridge.is_running():
            self._companion.complete_conversation_select(
                False, "Desktop is busy — wait for the current response to finish, or click Stop."
            )
            return
        if self._window._workspace_root is not None and self._window._workspace_root.resolve() != project_root.resolve():
            self._window._workspace_controller._on_project_selected(project_root, restore_last=False)
        try:
            self._window._persistence.load_and_apply(conversation_path)
            self._window._send_handler.clear_queue()
            self._window._input.set_queued_messages(0)
            self._window._reset_session_usage()
            self._companion.complete_conversation_select(True)
        except Exception as _err:
            QMessageBox.warning(self._window, APP_NAME, f"Could not open conversation:\n{_err}")
            self._companion.complete_conversation_select(False, str(_err))
