"""The strip above the canvas: which workflow, and what may be done to it.

Choosing, creating, renaming, and deleting a workflow all live here, next to
the one switch that is not part of any workflow at all: whether Aura may use
it. That switch is this user's private decision about this workspace, so the
bar shows it as a personal choice and says so, rather than letting it look
like a property of a file that might be shared.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QWidget,
)

from aura.agents.graph_local_state import AVAILABILITY_LABEL, AVAILABILITY_NOTE
from aura.gui.theme import FG, FG_DIM, SUCCESS, WARN

SCOPE_LABELS: dict[str, str] = {"project": "Project", "personal": "Personal"}


@dataclass(frozen=True)
class WorkflowRow:
    """One workflow as the picker lists it."""

    graph_id: str
    scope: str
    name: str
    valid: bool = True
    errors: tuple[str, ...] = ()

    @property
    def scope_label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope.title())

    @property
    def label(self) -> str:
        if not self.valid:
            return f"{self.scope_label} · {self.name} — could not be loaded"
        return f"{self.scope_label} · {self.name}"


class WorkflowBar(QWidget):
    """Workflow selection, lifecycle, and the private availability switch."""

    workflow_selected = Signal(str)  # graph id
    create_requested = Signal(str)  # scope key
    rename_requested = Signal()
    delete_requested = Signal()
    availability_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[WorkflowRow, ...] = ()
        self._mutations_enabled = True
        self._loading = False

        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        label = QLabel("Workflow")
        label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; background: transparent;")
        row.addWidget(label)

        self.picker = QComboBox()
        self.picker.setMinimumWidth(220)
        self.picker.currentIndexChanged.connect(lambda _index: self._emit_selection())
        row.addWidget(self.picker)

        self.new_button = QToolButton()
        self.new_button.setText("New")
        self.new_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.new_button)
        project = QAction("New project workflow", menu)
        project.setToolTip("Lives in this project and travels with it.")
        project.triggered.connect(lambda: self._request_create("project"))
        menu.addAction(project)
        personal = QAction("New personal workflow", menu)
        personal.setToolTip("Stays on this computer, in every project you open.")
        personal.triggered.connect(lambda: self._request_create("personal"))
        menu.addAction(personal)
        self.new_button.setMenu(menu)
        self.new_menu = menu
        row.addWidget(self.new_button)

        self.rename_button = QPushButton("Rename")
        self.rename_button.clicked.connect(self._request_rename)
        row.addWidget(self.rename_button)

        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._request_delete)
        row.addWidget(self.delete_button)

        row.addStretch(1)

        self.status = QLabel("")
        self.status.setStyleSheet("font-size: 11px; background: transparent;")
        row.addWidget(self.status)

        self.available = QCheckBox(AVAILABILITY_LABEL)
        self.available.setToolTip(AVAILABILITY_NOTE)
        self.available.setStyleSheet(f"color: {FG}; background: transparent;")
        self.available.toggled.connect(self._emit_availability)
        row.addWidget(self.available)

    # ---- what the page sets ------------------------------------------------

    def set_rows(self, rows: tuple[WorkflowRow, ...], current_id: str) -> None:
        """Replace the picker's contents and select *current_id* if it is there."""
        self._rows = tuple(rows)
        self._loading = True
        try:
            self.picker.clear()
            if not rows:
                self.picker.addItem("No workflows yet", "")
            for item in rows:
                self.picker.addItem(item.label, item.graph_id)
                if item.errors:
                    self.picker.setItemData(
                        self.picker.count() - 1,
                        "\n".join(item.errors),
                        Qt.ItemDataRole.ToolTipRole,
                    )
            index = self.picker.findData(current_id) if current_id else -1
            self.picker.setCurrentIndex(index if index >= 0 else 0)
        finally:
            self._loading = False
        self._update_actions()

    def set_available(self, available: bool) -> None:
        self._loading = True
        try:
            self.available.setChecked(bool(available))
        finally:
            self._loading = False

    def set_status(self, text: str, *, ok: bool = True) -> None:
        self.status.setStyleSheet(
            f"color: {SUCCESS if ok else WARN}; font-size: 11px; background: transparent;"
        )
        self.status.setText(text)

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self._update_actions()

    def current_graph_id(self) -> str:
        return str(self.picker.currentData() or "")

    def _update_actions(self) -> None:
        has_workflow = bool(self.current_graph_id())
        self.new_button.setEnabled(self._mutations_enabled)
        self.rename_button.setEnabled(self._mutations_enabled and has_workflow)
        self.delete_button.setEnabled(self._mutations_enabled and has_workflow)
        self.available.setEnabled(self._mutations_enabled and has_workflow)

    # ---- user intent -------------------------------------------------------

    def _emit_selection(self) -> None:
        self._update_actions()
        if not self._loading:
            self.workflow_selected.emit(self.current_graph_id())

    def _request_create(self, scope: str) -> None:
        if self._mutations_enabled:
            self.create_requested.emit(scope)

    def _request_rename(self) -> None:
        if self._mutations_enabled and self.current_graph_id():
            self.rename_requested.emit()

    def _request_delete(self) -> None:
        if self._mutations_enabled and self.current_graph_id():
            self.delete_requested.emit()

    def _emit_availability(self, checked: bool) -> None:
        if self._loading or not self._mutations_enabled:
            return
        if self.current_graph_id():
            self.availability_changed.emit(bool(checked))


__all__ = ["SCOPE_LABELS", "WorkflowBar", "WorkflowRow"]
