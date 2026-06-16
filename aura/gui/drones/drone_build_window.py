"""Modeless chat window for building and editing Drones."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QDialog,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.bridge.qt_bridge import ConversationBridge
from aura.config import load_settings
from aura.conversation.dispatch import WorkerDispatchRequest
from aura.drones.build_spec_prompt import build_dispatch, revise_dispatch
from aura.drones.definition import slugify
from aura.gui.chat_view import ChatView
from aura.gui.input_panel import InputPanel
from aura.paths import data_dir

logger = logging.getLogger(__name__)


class DroneBuildWindow(QDialog):
    """Modeless chat window for building and editing Drones.

    Has its own internal ConversationBridge and chat surface, fully
    isolated from the project chat.
    """

    drone_built = Signal(str)  # drone_id
    geometry_saved = Signal(str)

    def __init__(
        self,
        workspace_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Drone")
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        self.resize(1000, 700)
        self.setMinimumSize(700, 500)

        self._workspace_root = workspace_root
        self._drone_id: str | None = None
        self._folder: Path | None = None

        self._geometry_restore_done = False

        # Geometry save timer (debounced)
        self._geometry_save_timer = QTimer(self)
        self._geometry_save_timer.setSingleShot(True)
        self._geometry_save_timer.setInterval(250)
        self._geometry_save_timer.timeout.connect(self._save_geometry)

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Chat view (fills available space)
        self._chat = ChatView()
        self._chat.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        layout.addWidget(self._chat)

        # Input panel (fixed at bottom)
        self._input = InputPanel(workspace_root, parent=self)
        self._input.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._input)

        # Internal bridge — fully isolated from the project chat bridge.
        settings = load_settings()
        self._bridge = ConversationBridge(
            parent_widget=self,
            provider=settings.provider,
        )
        self._bridge.set_workspace_root(workspace_root)

        # Wire bridge worker signals to chat
        self._bridge.workerReasoningDelta.connect(self._on_worker_reasoning)
        self._bridge.workerContentDelta.connect(self._on_worker_content)
        self._bridge.workerToolCallStart.connect(self._on_worker_tool_call_start)
        self._bridge.workerToolCallArgs.connect(self._on_worker_tool_args)
        self._bridge.workerToolResult.connect(self._on_worker_tool_result)
        self._bridge.workerTerminalOutput.connect(self._on_worker_terminal_output)
        self._bridge.workerApiError.connect(self._on_worker_api_error)
        self._bridge.workerFinished.connect(self._on_worker_finished)

        # Wire input submission
        self._input.sent.connect(self._on_input_sent)

        self._geometry_restore_done = True

    # -- public API ---------------------------------------------------------

    @property
    def drone_id(self) -> str | None:
        return self._drone_id

    @property
    def folder(self) -> Path | None:
        return self._folder

    def bind(self, drone_id: str, folder: Path) -> None:
        """Bind this window to an existing drone for editing."""
        self._drone_id = drone_id
        self._folder = folder
        name = folder.name.replace("-", " ").title()
        self.setWindowTitle(f"Edit Drone — {name}")

    def show_and_raise(self) -> None:
        """Show, raise, and activate the window."""
        self.show()
        self.raise_()
        self.activateWindow()

    def is_open(self) -> bool:
        return self.isVisible()

    # -- geometry save/restore ----------------------------------------------

    def _restore_geometry(self, geometry: str) -> None:
        if not geometry:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
        except Exception:
            logger.debug("Failed to restore DroneBuildWindow geometry", exc_info=True)

    def _schedule_geometry_save(self) -> None:
        if not self._geometry_restore_done:
            return
        self._geometry_save_timer.start()

    def _save_geometry(self) -> None:
        if not self._geometry_restore_done:
            return
        geometry = bytes(self.saveGeometry().toBase64()).decode("ascii")
        self.geometry_saved.emit(geometry)

    # -- events -------------------------------------------------------------

    def hideEvent(self, event) -> None:
        super().hideEvent(event)
        self._schedule_geometry_save()

    def moveEvent(self, event) -> None:
        super().moveEvent(event)
        self._schedule_geometry_save()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._schedule_geometry_save()

    def closeEvent(self, event: QCloseEvent) -> None:
        event.accept()

    # -- input handling -----------------------------------------------------

    def _on_input_sent(self, payload) -> None:
        text = payload.text.strip()
        if not text:
            return

        self._input.set_text("")
        self._chat.add_user(text)

        if self._drone_id is None:
            # New drone — compute target folder and dispatch
            slug = slugify(text)
            target = data_dir() / "drones" / slug
            data = build_dispatch(text, target)
            req = WorkerDispatchRequest(
                goal=data["goal"],
                files=data["files"],
                spec=data["spec"],
                acceptance=data["acceptance"],
                summary=data.get("summary", ""),
            )
            self._bridge.dispatch_drone_build(req)
            self.bind(slug, target)
            self.drone_built.emit(slug)
        else:
            # Editing existing drone
            data = revise_dispatch(self._folder, text)
            req = WorkerDispatchRequest(
                goal=data["goal"],
                files=data["files"],
                spec=data["spec"],
                acceptance=data["acceptance"],
                summary=data.get("summary", ""),
            )
            self._bridge.dispatch_drone_build(req)

    # -- bridge worker signal handlers --------------------------------------

    def _on_worker_reasoning(self, tool_id: str, text: str) -> None:
        self._chat.append_reasoning(text)

    def _on_worker_content(self, tool_id: str, text: str) -> None:
        self._chat.append_content(text)

    def _on_worker_tool_call_start(
        self, parent_id: str, worker_tool_id: str, name: str
    ) -> None:
        self._chat.add_tool_call(worker_tool_id, name)

    def _on_worker_tool_args(
        self, parent_id: str, worker_tool_id: str, fragment: str
    ) -> None:
        self._chat.append_tool_args(worker_tool_id, fragment)

    def _on_worker_tool_result(
        self,
        parent_id: str,
        worker_tool_id: str,
        name: str,
        ok: bool,
        result: str,
        extras: dict,
    ) -> None:
        self._chat.set_tool_result(worker_tool_id, ok, result)

    def _on_worker_terminal_output(
        self, parent_id: str, worker_tool_id: str, text: str
    ) -> None:
        self._chat.append_terminal_output(worker_tool_id, text)

    def _on_worker_api_error(self, tool_id: str, status: int, message: str) -> None:
        title = f"API Error {status}" if status > 0 else "Worker Error"
        self._chat.add_error(title, message)

    def _on_worker_finished(
        self, tool_id: str, ok: bool, summary: str, needs_followup: bool, status: str
    ) -> None:
        self._chat.assistant_done()
