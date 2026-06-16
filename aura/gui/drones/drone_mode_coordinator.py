from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from aura.conversation.dispatch import WorkerDispatchRequest
from aura.drones.build_spec_prompt import build_dispatch, revise_dispatch
from aura.drones.definition import slugify
from aura.drones.store import DroneStore
from aura.paths import data_dir

logger = logging.getLogger(__name__)


class DroneModeCoordinator(QObject):
    """Thin adapter: new-drone and edit-drone verbs that dispatch to the Worker.

    No phase machine, no workspace lifecycle, no drone mode UI swap.
    """

    drone_mode_changed = Signal(bool)
    drone_list_changed = Signal()
    fresh_session_requested = Signal()

    def __init__(
        self,
        main_splitter=None,
        left_pane=None,
        bridge=None,
        chat=None,
        input_panel=None,
        status_bar=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._workspace_root: Path | None = None

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def is_drone_mode(self) -> bool:
        """Drone mode is removed; always returns False."""
        return False

    def active_drone_context(self) -> str:
        """Drone context is removed; always returns empty string."""
        return ""

    def exit_drone_mode(self, restore_project_chat: bool = True) -> None:
        """No-op: drone mode lifecycle removed."""

    def handle_drone_toggle(self) -> None:
        """No-op: drone mode no longer exists."""

    def create_new_drone(self, description: str) -> None:
        """Create a new Drone from a natural-language description.

        Slugifies the description to an id, calls build_dispatch to
        produce a Worker dispatch dict, then dispatches to the Worker.
        The Worker writes drone.json + entrypoint directly into the folder.
        """
        if not description.strip():
            return
        if self._bridge is None:
            logger.error("Cannot create drone: no bridge available")
            return
        slug = slugify(description)
        target_folder = data_dir() / "drones" / slug
        dispatch_dict = build_dispatch(description, target_folder)
        req = WorkerDispatchRequest(
            goal=dispatch_dict["goal"],
            files=dispatch_dict["files"],
            spec=dispatch_dict["spec"],
            acceptance=dispatch_dict["acceptance"],
            summary=dispatch_dict["summary"],
        )
        self._bridge.dispatch_drone_build(req)

    def edit_ready_drone_by_folder(self, drone_id: str, folder: Path, feedback: str = "") -> None:
        """Edit a Drone by folder path with natural-language feedback.

        Calls revise_dispatch to produce a Worker dispatch dict,
        then dispatches to the Worker.
        """
        if not feedback.strip() or self._bridge is None:
            return
        dispatch_dict = revise_dispatch(folder, feedback)
        req = WorkerDispatchRequest(
            goal=dispatch_dict["goal"],
            files=dispatch_dict["files"],
            spec=dispatch_dict["spec"],
            acceptance=dispatch_dict["acceptance"],
            summary=dispatch_dict["summary"],
        )
        self._bridge.dispatch_drone_build(req)

    def edit_ready_drone(self, drone_id: str, feedback: str = "") -> None:
        """Edit a Drone by id with natural-language feedback."""
        folder = DroneStore.drone_folder(drone_id)
        self.edit_ready_drone_by_folder(drone_id, folder, feedback)
