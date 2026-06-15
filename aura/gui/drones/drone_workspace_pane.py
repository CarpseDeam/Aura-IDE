from __future__ import annotations

import json
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

from aura.drones.store import DroneStore
from aura.drones.workspaces.model import DroneThread, DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.paths import candidate_dir
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.theme import (
    BG_RAISED,
    DANGER,
    FG,
    FG_MUTED,
)

logger = logging.getLogger(__name__)


class _WorkspaceRow(QFrame):
    """A clickable workspace row with expand/collapse and status badge."""

    clicked = Signal(str)  # workspace_id
    discard_clicked = Signal(str)  # workspace_id
    toggled = Signal(str)  # workspace_id (expand/collapse)

    def __init__(
        self,
        workspace: DroneWorkspace,
        display_name: str,
        expanded: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._workspace_id = workspace.workspace_id
        self._expanded = expanded
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style(active=False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(4)

        # Expand/collapse button
        self._expand_btn = QPushButton("\u25bc" if expanded else "\u25b6")
        self._expand_btn.setFixedSize(18, 18)
        self._expand_btn.setFlat(True)
        self._expand_btn.setStyleSheet(
            "QPushButton { color: %s; background: transparent; border: none; font-size: 10px; }"
            "QPushButton:hover { color: %s; }"
            % (FG_MUTED, FG)
        )
        self._expand_btn.clicked.connect(self._on_expand_clicked)
        layout.addWidget(self._expand_btn)

        # Name + phase labels
        text_col = QVBoxLayout()
        text_col.setSpacing(1)

        name_label = QLabel(display_name)
        name_label.setStyleSheet(
            f"color: {FG}; font-weight: 600; font-size: 13px; background: transparent;"
        )
        text_col.addWidget(name_label)

        phase_label = QLabel(_status_for_phase(workspace.phase))
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

    def _on_expand_clicked(self) -> None:
        self.toggled.emit(self._workspace_id)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.clicked.emit(self._workspace_id)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._expand_btn.setText("\u25bc" if expanded else "\u25b6")

    def set_active(self, active: bool) -> None:
        self._update_style(active)

    def _update_style(self, active: bool) -> None:
        if active:
            self.setStyleSheet(
                f"""
                _WorkspaceRow {{
                    background: {BG_RAISED};
                    border-left: 3px solid #5294E2;
                    border-radius: 6px;
                    padding: 4px 6px 4px 3px;
                }}
                _WorkspaceRow:hover {{
                    background: {BG_RAISED};
                    border-radius: 6px;
                }}
                """
            )
        else:
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


class _ThreadRow(QFrame):
    """A clickable thread row indented under its parent workspace."""

    clicked = Signal(str, str)  # workspace_id, thread_id

    def __init__(self, workspace_id: str, thread: DroneThread, parent=None) -> None:
        super().__init__(parent)
        self._workspace_id = workspace_id
        self._thread_id = thread.id
        self._thread_title = thread.title
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_style(active=False)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(24, 3, 6, 3)
        layout.setSpacing(4)

        title_label = QLabel(thread.title)
        title_label.setStyleSheet(
            f"color: {FG}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(title_label, 1)

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.clicked.emit(self._workspace_id, self._thread_id)

    def set_active(self, active: bool) -> None:
        self._update_style(active)

    def _update_style(self, active: bool) -> None:
        if active:
            self.setStyleSheet(
                f"""
                _ThreadRow {{
                    background: {BG_RAISED};
                    border-left: 2px solid #5294E2;
                    border-radius: 4px;
                    padding: 2px 4px 2px 22px;
                }}
                _ThreadRow:hover {{
                    background: {BG_RAISED};
                    border-radius: 4px;
                }}
                """
            )
        else:
            self.setStyleSheet(
                f"""
                _ThreadRow {{
                    background: transparent;
                    border-radius: 4px;
                    padding: 2px 4px 2px 24px;
                }}
                _ThreadRow:hover {{
                    background: {BG_RAISED};
                    border-radius: 4px;
                }}
                """
            )


class DroneWorkspacePane(QFrame):
    """Sidebar pane showing Drones being built with thread children under each workspace."""

    workspace_selected = Signal(str)  # workspace_id
    thread_selected = Signal(str, str)  # workspace_id, thread_id
    back_to_project_requested = Signal()
    edit_ready = Signal(str)  # drone_id
    new_workspace_requested = Signal()
    discard_workspace_requested = Signal(str)  # workspace_id

    def __init__(self, project_root: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._active_workspace_id: str | None = None
        self._active_thread_id: str | None = None
        self._expanded_workspace_ids: set[str] = set()

        self.setObjectName("leftPane")
        self.setMinimumWidth(180)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Header
        header = QLabel("DRONES")
        header.setObjectName("paneTitleProjects")
        layout.addWidget(header)

        # "New Drone" button
        new_btn = QPushButton("+ New Drone")
        new_btn.setObjectName("primary")
        new_btn.clicked.connect(self.new_workspace_requested.emit)
        layout.addWidget(new_btn)

        # Back to Project button
        back_btn = QPushButton("← Back to Project")
        back_btn.setFlat(True)
        back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        back_btn.setStyleSheet(
            f"QPushButton {{ color: {FG}; background: transparent; border: none; font-size: 12px; text-align: left; padding: 2px 0; }}"
            f"QPushButton:hover {{ color: #5294E2; }}"
        )
        back_btn.clicked.connect(self.back_to_project_requested.emit)
        layout.addWidget(back_btn)

        # Scroll area for workspace rows
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        scroll_content = QWidget()
        self._rows_layout = QVBoxLayout(scroll_content)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(2)

        scroll.setWidget(scroll_content)
        layout.addWidget(scroll, 1)

    def set_project_root(self, root: Path | None) -> None:
        """Update the project root and refresh the pane."""
        self._project_root = root
        self.refresh()

    def set_active(self, workspace_id: str | None, thread_id: str | None = None) -> None:
        """Set the active workspace and optionally active thread, then refresh."""
        self._active_workspace_id = workspace_id
        self._active_thread_id = thread_id
        if workspace_id is not None:
            self._expanded_workspace_ids.add(workspace_id)
        self.refresh()

    def set_active_workspace_id(self, workspace_id: str | None) -> None:
        """Backward-compatible setter for workspace highlight."""
        self._active_workspace_id = workspace_id
        if workspace_id is not None:
            self._expanded_workspace_ids.add(workspace_id)

    def refresh(self) -> None:
        """Reload workspace data and rebuild tree row widgets."""
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
        workspaces = [
            ws
            for ws in workspaces
            if ws.phase != WorkspacePhase.DISCARDED.value
        ]

        if not workspaces:
            hint = QLabel("No Drones yet.\nDescribe the Drone\nyou want to build.")
            hint.setStyleSheet(
                f"color: {FG_MUTED}; font-size: 12px; padding: 8px; background: transparent;"
            )
            hint.setWordWrap(True)
            self._rows_layout.addWidget(hint)
            self._rows_layout.addStretch(1)
            return

        rows: list[_WorkspaceRow] = []
        thread_rows: list[_ThreadRow] = []

        for ws in workspaces:
            display_name = _resolve_workspace_name(
                self._project_root, ws, workspace_id=ws.workspace_id,
            )
            expanded = ws.workspace_id in self._expanded_workspace_ids
            row = _WorkspaceRow(ws, display_name, expanded=expanded)

            row.clicked.connect(self.workspace_selected.emit)
            row.discard_clicked.connect(self.discard_workspace_requested.emit)
            row.toggled.connect(self._on_workspace_toggled)

            self._rows_layout.addWidget(row)
            rows.append(row)

            # Load thread children if expanded
            if expanded:
                try:
                    threads = DroneWorkspaceStore.list_threads(
                        self._project_root, ws.workspace_id
                    )
                except Exception:
                    logger.exception("Failed to list threads for %s", ws.workspace_id)
                    threads = []

                for thread in threads:
                    trow = _ThreadRow(ws.workspace_id, thread)
                    trow.clicked.connect(self._on_thread_row_clicked)
                    self._rows_layout.addWidget(trow)
                    thread_rows.append(trow)

        # Apply active highlighting
        for row in rows:
            row.set_active(row._workspace_id == self._active_workspace_id)

        for trow in thread_rows:
            trow.set_active(trow._thread_id == self._active_thread_id)

        self._rows_layout.addStretch(1)

    def _on_workspace_toggled(self, workspace_id: str) -> None:
        """Toggle expanded state for a workspace and refresh the tree."""
        if workspace_id in self._expanded_workspace_ids:
            self._expanded_workspace_ids.discard(workspace_id)
        else:
            self._expanded_workspace_ids.add(workspace_id)
        self.refresh()

    def _on_thread_row_clicked(self, workspace_id: str, thread_id: str) -> None:
        self.thread_selected.emit(workspace_id, thread_id)

    def _clear_rows(self) -> None:
        """Remove all widgets from the rows layout."""
        while self._rows_layout.count() > 0:
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()


def _resolve_workspace_name(
    project_root: Path, workspace: DroneWorkspace, workspace_id: str | None = None,
) -> str:
    """Resolve the best display name for a workspace.

    Priority: candidate drone.json name -> installed Drone name -> display_name -> workspace_id.
    """
    wid = workspace_id or workspace.workspace_id
    # 1. Candidate drone.json — use edit_source_folder if set
    try:
        if workspace.edit_source_folder:
            drone_json = Path(workspace.edit_source_folder) / "drone.json"
        else:
            drone_json = candidate_dir(project_root, wid) / "drone.json"
        if drone_json.exists():
            data = json.loads(drone_json.read_text(encoding="utf-8"))
            name = data.get("name")
            if name:
                return str(name)
    except Exception as exc:
        logger.debug("Failed to read candidate drone.json for %s: %s", wid, exc)
    # 2. Fallback
    return workspace.display_name or wid


def _status_for_phase(phase: str) -> str:
    if phase == WorkspacePhase.WORKSHOP.value:
        return "Draft"
    if phase in (WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value):
        return "Building"
    if phase == WorkspacePhase.READY.value:
        return "Ready"
    return "Draft"
