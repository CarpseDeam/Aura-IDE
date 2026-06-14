from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.drones.workspaces.model import DroneWorkspace
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.theme import (
    BG_RAISED,
    DANGER,
    FG,
    FG_MUTED,
)

logger = logging.getLogger(__name__)


class _WorkspaceRow(QFrame):
    """A single clickable workspace row in the sidebar pane."""

    clicked = Signal(str)  # workspace_id
    discard_clicked = Signal(str)  # workspace_id

    def __init__(self, workspace: DroneWorkspace, parent=None) -> None:
        super().__init__(parent)
        self._workspace_id = workspace.workspace_id
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(
            f"""
            _WorkspaceRow {{
                background: transparent;
                border-radius: 6px;
                padding: 4px 6px;
            }}
            _WorkspaceRow:hover {{
                background: {BG_RAISED};
                border-radius: 6px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # Name + phase labels
        text_col = QVBoxLayout()
        text_col.setSpacing(1)

        name_label = QLabel(workspace.display_name)
        name_label.setStyleSheet(
            f"color: {FG}; font-weight: 600; font-size: 13px; background: transparent;"
        )
        text_col.addWidget(name_label)

        phase_label = QLabel(workspace.phase)
        phase_label.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        text_col.addWidget(phase_label)

        layout.addLayout(text_col, 1)

        # Discard button
        discard_btn = QPushButton("\u2715")
        discard_btn.setFixedSize(20, 20)
        discard_btn.setFlat(True)
        discard_btn.setStyleSheet(
            f"""
            QPushButton {{
                color: {DANGER};
                background: transparent;
                border: none;
                font-size: 12px;
            }}
            QPushButton:hover {{
                color: {DANGER};
                font-weight: 700;
            }}
            """
        )
        discard_btn.clicked.connect(lambda: self.discard_clicked.emit(self._workspace_id))
        layout.addWidget(discard_btn)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.clicked.emit(self._workspace_id)


class DroneWorkspacePane(QFrame):
    """Sidebar pane showing Drone Workspaces for the current project."""

    workspace_selected = Signal(str)  # workspace_id
    new_workspace_requested = Signal()
    discard_workspace_requested = Signal(str)  # workspace_id

    def __init__(self, project_root: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._project_root = project_root

        self.setObjectName("leftPane")
        self.setMinimumWidth(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header
        header = QLabel("DRONE WORKSPACES")
        header.setObjectName("paneTitleProjects")
        layout.addWidget(header)

        # Scroll area for workspace rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        self._rows_layout = QVBoxLayout(scroll_content)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

        # "New Drone" button
        new_btn = QPushButton("+ New Drone")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self.new_workspace_requested.emit)
        layout.addWidget(new_btn)

    def set_project_root(self, root: Path | None) -> None:
        """Update the project root and refresh the pane."""
        self._project_root = root
        self.refresh()

    def refresh(self) -> None:
        """Reload workspace data and rebuild row widgets."""
        self._clear_rows()

        if self._project_root is None:
            hint = QLabel("Open a project first")
            hint.setStyleSheet(
                f"color: {FG_MUTED}; font-size: 12px; padding: 8px; background: transparent;"
            )
            self._rows_layout.addWidget(hint)
            self._rows_layout.addStretch(1)
            return

        try:
            workspaces = DroneWorkspaceStore.list_workspaces(self._project_root)
        except Exception:
            logger.exception("Failed to list workspaces")
            workspaces = []

        for ws in workspaces:
            row = _WorkspaceRow(ws)
            row.clicked.connect(self.workspace_selected.emit)
            row.discard_clicked.connect(self.discard_workspace_requested.emit)
            self._rows_layout.addWidget(row)

        self._rows_layout.addStretch(1)

    def _clear_rows(self) -> None:
        """Remove all widgets from the rows layout."""
        while self._rows_layout.count() > 0:
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
