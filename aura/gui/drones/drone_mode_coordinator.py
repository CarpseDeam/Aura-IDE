from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QFrame, QSplitter

from aura.drones.build_spec import DroneBuildBrief
from aura.drones.build_prompt import build_drone_architect_prompt
from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.drones.drone_workspace_pane import DroneWorkspacePane

logger = logging.getLogger(__name__)


class DroneModeCoordinator(QObject):
    """Owns entering/exiting Drone mode, swapping the sidebar, and
    routing Drone mode chat messages."""

    drone_mode_changed = Signal(bool)

    def __init__(
        self,
        main_splitter: QSplitter,
        left_pane: QFrame,
        bridge,
        chat,
        input_panel,
        status_bar,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._main_splitter = main_splitter
        self._left_pane = left_pane
        self._bridge = bridge
        self._chat = chat
        self._input = input_panel
        self._status_bar = status_bar

        # Workspace pane placed into the splitter during drone mode
        self._workspace_pane = DroneWorkspacePane(parent=parent)
        self._workspace_pane.workspace_selected.connect(self._on_workspace_selected)
        self._workspace_pane.new_workspace_requested.connect(self._on_new_workspace)
        self._workspace_pane.discard_workspace_requested.connect(
            self._on_discard_workspace
        )

        self._drone_mode: bool = False
        self._workspace_root: Path | None = None
        self._active_workspace: DroneWorkspace | None = None

    # ---- public API --------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        """Update the workspace root and refresh the pane."""
        self._workspace_root = root
        self._workspace_pane.set_project_root(root)

    def is_drone_mode(self) -> bool:
        return self._drone_mode

    def enter_drone_mode(self) -> None:
        """Switch to drone mode: swap sidebar and update UI."""
        if self._drone_mode:
            return
        self._drone_mode = True
        self._workspace_pane.refresh()

        # Swap left pane for workspace pane in the splitter
        idx = self._main_splitter.indexOf(self._left_pane)
        if idx >= 0:
            self._left_pane.hide()
            self._main_splitter.replaceWidget(idx, self._workspace_pane)
            self._workspace_pane.show()

        self._input.set_drone_architect_mode(True)
        self._status_bar.set_drone_architect_mode(True)
        self.drone_mode_changed.emit(True)
        self._chat.add_info(
            "Drone Workspaces",
            "Drone mode active. Select a workspace or create a new Drone.",
        )

    def exit_drone_mode(self) -> None:
        """Exit drone mode: restore normal sidebar without adding chat messages."""
        if not self._drone_mode:
            return
        self._drone_mode = False

        # Swap workspace pane back to left pane
        idx = self._main_splitter.indexOf(self._workspace_pane)
        if idx >= 0:
            self._workspace_pane.hide()
            self._main_splitter.replaceWidget(idx, self._left_pane)
            self._left_pane.show()

        self._input.set_drone_architect_mode(False)
        self._status_bar.set_drone_architect_mode(False)
        self.drone_mode_changed.emit(False)
        # Caller adds info cards if needed; we stay silent here.

    def handle_message(
        self, payload, model: str, thinking
    ) -> None:
        """Route a user message in drone mode.

        Ensures an active workspace exists, wraps the message as a
        drone build brief, and sends the compiled prompt to the bridge.
        """
        if self._bridge.is_running():
            self._chat.add_info("Busy", "Wait for the current response to finish.")
            return

        text = payload.text.strip() if hasattr(payload, "text") else str(payload)

        # Ensure an active workspace
        if self._active_workspace is None:
            workspaces = DroneWorkspaceStore.list_workspaces(self._workspace_root)
            if workspaces:
                self._active_workspace = workspaces[0]
                DroneWorkspaceStore.set_active_workspace(
                    self._workspace_root, self._active_workspace
                )
            else:
                name = " ".join(text.split()[:5]) if text else "New Drone"
                self._active_workspace = DroneWorkspaceStore.create_workspace(
                    self._workspace_root, display_name=name
                )
                DroneWorkspaceStore.set_active_workspace(
                    self._workspace_root, self._active_workspace
                )
                self._workspace_pane.refresh()

        # Wrap the user message as a drone build brief
        brief = DroneBuildBrief(
            response_type="brief",
            message="",
            ready_to_build=True,
            build_brief=text,
        )
        compiled_prompt = build_drone_architect_prompt(brief)

        # Persist the brief on the workspace
        self._active_workspace.build_brief = text
        DroneWorkspaceStore.save_workspace(self._active_workspace)

        # Show user text and send the compiled prompt
        self._chat.add_user(text)
        self._bridge.history.append_user_text(compiled_prompt)
        self._chat.begin_assistant()
        self._bridge.send(model=model, thinking=thinking)

    # ---- private helpers ---------------------------------------------------

    def _on_workspace_selected(self, workspace_id: str) -> None:
        """Load and activate a workspace when the user clicks a row."""
        if self._workspace_root is None:
            return
        ws = DroneWorkspaceStore.load_workspace(self._workspace_root, workspace_id)
        if ws is None:
            return
        self._active_workspace = ws
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        self._chat.add_info(
            "Drone Workspace",
            f"Active workspace: {ws.display_name} (phase: {ws.phase})",
        )

    def _on_new_workspace(self) -> None:
        """Create a new workspace with a default name."""
        if self._workspace_root is None:
            return
        ws = DroneWorkspaceStore.create_workspace(
            self._workspace_root, display_name="New Drone"
        )
        self._active_workspace = ws
        DroneWorkspaceStore.set_active_workspace(self._workspace_root, ws)
        self._workspace_pane.refresh()
        self._chat.add_info(
            "Drone Workspace",
            f"Created new workspace '{ws.display_name}'. Describe what you want to build.",
        )

    def _on_discard_workspace(self, workspace_id: str) -> None:
        """Discard a workspace by ID."""
        if self._workspace_root is None:
            return
        ws = DroneWorkspaceStore.load_workspace(self._workspace_root, workspace_id)
        if ws is None:
            return
        DroneWorkspaceStore.discard_workspace(ws)
        if self._active_workspace is not None and self._active_workspace.workspace_id == workspace_id:
            self._active_workspace = None
        self._workspace_pane.refresh()
