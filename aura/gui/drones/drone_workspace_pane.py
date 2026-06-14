from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from aura.config import ProviderId
from aura.drones.workspaces.model import DroneThread, DroneWorkspace, WorkspacePhase
from aura.drones.workspaces.store import DroneWorkspaceStore
from aura.gui.left_pane import _models_with_default
from aura.gui.theme import (
    BG_RAISED,
    BORDER,
    DANGER,
    FG,
    FG_DIM,
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

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        self.clicked.emit(self._workspace_id)


class _ThreadRow(QFrame):
    """A single clickable thread row in the sidebar pane."""

    clicked = Signal(str)  # thread_id
    rename_requested = Signal(str, str)  # thread_id, new_title

    def __init__(self, thread: DroneThread, active: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._thread_id = thread.id
        self._inline_edit: QLineEdit | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        bg = BG_RAISED if active else "transparent"
        self.setStyleSheet(
            f"""
            _ThreadRow {{
                background: {bg};
                border-radius: 4px;
                padding: 2px 4px;
            }}
            _ThreadRow:hover {{
                background: {BG_RAISED};
                border-radius: 4px;
            }}
            """
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 2, 6, 2)
        layout.setSpacing(4)

        self._title_label = QLabel(thread.title or "Untitled")
        self._title_label.setStyleSheet(
            f"color: {FG}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(self._title_label, 1)

    def mousePressEvent(self, event) -> None:
        if self._title_label.geometry().contains(event.pos()):
            self._start_inline_edit()
        else:
            super().mousePressEvent(event)
            self.clicked.emit(self._thread_id)

    def set_active(self, active: bool) -> None:
        bg = BG_RAISED if active else "transparent"
        self.setStyleSheet(
            f"""
            _ThreadRow {{
                background: {bg};
                border-radius: 4px;
                padding: 2px 4px;
            }}
            _ThreadRow:hover {{
                background: {BG_RAISED};
                border-radius: 4px;
            }}
            """
        )

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        rename_action = menu.addAction("Rename")
        rename_action.triggered.connect(self._start_inline_edit)
        menu.exec(event.globalPos())

    def _start_inline_edit(self) -> None:
        """Replace the title label with a QLineEdit for inline renaming."""
        if self._inline_edit is not None:
            return
        self._title_label.hide()
        edit = QLineEdit(self._title_label.text())
        edit.selectAll()
        edit.setStyleSheet(
            f"color: {FG}; font-size: 12px; background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 3px; padding: 1px 4px;"
        )
        layout = self.layout()
        if layout:
            layout.replaceWidget(self._title_label, edit)
        edit.setFocus()
        self._inline_edit = edit

        edit.returnPressed.connect(self._finish_inline_edit)
        edit.editingFinished.connect(self._finish_inline_edit)
        edit.installEventFilter(self)

    def _finish_inline_edit(self) -> None:
        """Commit the rename from the active inline edit, restore the label."""
        edit = self._inline_edit
        if edit is None:
            return
        new_title = edit.text().strip()
        layout = self.layout()
        if layout:
            layout.replaceWidget(edit, self._title_label)
        edit.deleteLater()
        self._inline_edit = None
        self._title_label.show()
        if new_title and new_title != self._title_label.text():
            self._title_label.setText(new_title)
            self.rename_requested.emit(self._thread_id, new_title)

    def eventFilter(self, obj, event) -> bool:
        if (
            obj is self._inline_edit
            and event.type() == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            # Cancel: restore label without emitting rename_requested
            edit = self._inline_edit
            self._inline_edit = None
            try:
                edit.returnPressed.disconnect(self._finish_inline_edit)
                edit.editingFinished.disconnect(self._finish_inline_edit)
            except (TypeError, RuntimeError):
                pass
            layout = self.layout()
            if layout:
                layout.replaceWidget(edit, self._title_label)
            edit.deleteLater()
            self._title_label.show()
            return True
        return super().eventFilter(obj, event)


class DroneWorkspacePane(QFrame):
    """Sidebar pane showing Drones being built for the current project."""

    workspace_selected = Signal(str)  # workspace_id
    new_workspace_requested = Signal()
    discard_workspace_requested = Signal(str)  # workspace_id
    new_thread_requested = Signal()
    thread_selected = Signal(str)  # thread_id
    thread_rename_requested = Signal(str, str)  # thread_id, new_title
    planner_model_changed = Signal(str)
    worker_model_changed = Signal(str)

    def __init__(self, project_root: Path | None = None, parent=None) -> None:
        super().__init__(parent)
        self._project_root = project_root
        self._active_workspace_id: str | None = None
        self._active_thread_id: str | None = None

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

        # --- Threads section ---
        self._threads_section = QFrame()
        self._threads_section.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        threads_layout = QVBoxLayout(self._threads_section)
        threads_layout.setContentsMargins(0, 0, 0, 0)
        threads_layout.setSpacing(3)

        threads_sep = QFrame()
        threads_sep.setFrameShape(QFrame.Shape.HLine)
        threads_sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        threads_layout.addWidget(threads_sep)

        threads_header = QLabel("THREADS")
        threads_header.setObjectName("paneTitleProjects")
        threads_layout.addWidget(threads_header)

        new_thread_btn = QPushButton("+ New Thread")
        new_thread_btn.clicked.connect(self.new_thread_requested.emit)
        threads_layout.addWidget(new_thread_btn)

        self._threads_rows_layout = QVBoxLayout()
        self._threads_rows_layout.setContentsMargins(0, 0, 0, 0)
        self._threads_rows_layout.setSpacing(2)
        threads_layout.addLayout(self._threads_rows_layout)

        self._threads_section.setVisible(False)
        layout.addWidget(self._threads_section)

        # --- Model Config section ---
        self._model_config_footer = QFrame()
        self._model_config_footer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        footer_layout = QVBoxLayout(self._model_config_footer)
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(4)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"QFrame {{ color: {BORDER}; }}")
        footer_layout.addWidget(sep)

        model_label = QLabel("Model Configuration")
        model_label.setObjectName("paneTitleModel")
        footer_layout.addWidget(model_label)

        # Planner model
        planner_model_row = QHBoxLayout()
        planner_model_row.setSpacing(4)
        planner_model_label = QLabel("Planner:")
        planner_model_label.setStyleSheet(f"color: {FG_DIM};")
        planner_model_row.addWidget(planner_model_label)
        self._planner_model_combo = QComboBox()
        self._planner_model_combo.currentIndexChanged.connect(
            lambda: self.planner_model_changed.emit(self.current_planner_model())
        )
        planner_model_row.addWidget(self._planner_model_combo, 1)
        footer_layout.addLayout(planner_model_row)

        # Worker model
        worker_model_row = QHBoxLayout()
        worker_model_row.setSpacing(4)
        worker_model_label = QLabel("Worker:")
        worker_model_label.setStyleSheet(f"color: {FG_DIM};")
        worker_model_row.addWidget(worker_model_label)
        self._worker_model_combo = QComboBox()
        self._worker_model_combo.currentIndexChanged.connect(
            lambda: self.worker_model_changed.emit(self.current_worker_model())
        )
        worker_model_row.addWidget(self._worker_model_combo, 1)
        footer_layout.addLayout(worker_model_row)

        layout.addWidget(self._model_config_footer)

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

    def _clear_thread_rows(self) -> None:
        """Remove all widgets from the thread rows layout."""
        while self._threads_rows_layout.count() > 0:
            item = self._threads_rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def set_active_workspace_id(self, workspace_id: str | None) -> None:
        """Set the active workspace ID to show/hide the thread section."""
        self._active_workspace_id = workspace_id
        self._threads_section.setVisible(workspace_id is not None)

    def set_active_thread(
        self, thread_id: str | None, threads: list[DroneThread] | None = None
    ) -> None:
        """Update the thread list display and highlight active thread."""
        self._active_thread_id = thread_id
        self._clear_thread_rows()
        if not threads:
            return
        for t in threads:
            active = t.id == thread_id
            row = _ThreadRow(t, active=active)
            row.clicked.connect(self.thread_selected.emit)
            row.rename_requested.connect(self.thread_rename_requested.emit)
            self._threads_rows_layout.addWidget(row)

    def populate_models(self, planner_provider: ProviderId, worker_provider: ProviderId) -> None:
        """Populate planner and worker model combos from provider specs."""
        # Planner
        self._planner_model_combo.blockSignals(True)
        self._planner_model_combo.clear()
        for mid, info in _models_with_default(planner_provider).items():
            self._planner_model_combo.addItem(info.label, mid)
        self._planner_model_combo.blockSignals(False)

        # Worker
        self._worker_model_combo.blockSignals(True)
        self._worker_model_combo.clear()
        for mid, info in _models_with_default(worker_provider).items():
            self._worker_model_combo.addItem(info.label, mid)
        self._worker_model_combo.blockSignals(False)

    def current_planner_model(self) -> str:
        return self._planner_model_combo.currentData()

    def current_worker_model(self) -> str:
        return self._worker_model_combo.currentData()

    def set_planner_model(self, model: str) -> None:
        idx = self._planner_model_combo.findData(model)
        if idx >= 0:
            self._planner_model_combo.setCurrentIndex(idx)

    def set_worker_model(self, model: str) -> None:
        idx = self._worker_model_combo.findData(model)
        if idx >= 0:
            self._worker_model_combo.setCurrentIndex(idx)


def _status_for_phase(phase: str) -> str:
    if phase == WorkspacePhase.WORKSHOP.value:
        return "Draft"
    if phase in (WorkspacePhase.BUILDING.value, WorkspacePhase.ITERATING.value):
        return "Building"
    if phase in (
        WorkspacePhase.READINESS_RUNNING.value,
        WorkspacePhase.INSTALLING.value,
        WorkspacePhase.AWAITING_DECISION.value,
    ):
        return "Testing"
    if phase == WorkspacePhase.READINESS_FAILED.value:
        return "Needs Fix"
    if phase == WorkspacePhase.INSTALLED.value:
        return "Ready"
    return "Draft"
