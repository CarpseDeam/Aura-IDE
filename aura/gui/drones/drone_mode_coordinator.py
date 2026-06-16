from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, Signal

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


