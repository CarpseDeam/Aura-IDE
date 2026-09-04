"""The strip above the canvas: which workflow, and what may be done to it.

Choosing, creating, renaming, and deleting a workflow live here, and so does
running one by hand. Run and Stop are deliberately not connected to the
Agents switch in the main toolbar: that switch decides whether *Aura* may
use Agents, including any saved Workflow that is runnable when a turn is
submitted. The picker changes only what is being edited or run manually; it
never hides a conversation target. Someone authoring a workflow needs to try
it long before they enable Agents, so this bar can always run the workflow it
has open and says nothing about availability — there is exactly one such
control, and it is not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QToolButton,
    QWidget,
)

from aura.gui.theme import FG_DIM, SUCCESS, WARN

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
    """Workflow selection, lifecycle, and running the open one by hand."""

    workflow_selected = Signal(str)  # graph id
    create_requested = Signal(str)  # scope key
    rename_requested = Signal()
    delete_requested = Signal()
    run_requested = Signal()
    stop_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[WorkflowRow, ...] = ()
        self._mutations_enabled = True
        self._runnable = False
        self._running = False
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

        self.run_button = QPushButton("Run")
        self.run_button.setObjectName("primary")
        self.run_button.setToolTip(
            "Run this workflow once, now. Independent of the Agents switch in "
            "the toolbar."
        )
        self.run_button.clicked.connect(self._request_run)
        row.addWidget(self.run_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Request cancellation of this workflow run.")
        self.stop_button.clicked.connect(self.stop_requested)
        row.addWidget(self.stop_button)

        self._update_run_actions()

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

    def set_runnable(self, runnable: bool) -> None:
        """Whether the open workflow is complete enough to be run at all."""
        self._runnable = bool(runnable)
        self._update_run_actions()

    def set_running(self, running: bool) -> None:
        """Swap Run for Stop while a run is in flight.

        Selection and editing stay live during a run: the run is working from
        a plan frozen when it started, so nothing the user does to the canvas
        now can reach it.
        """
        self._running = bool(running)
        self._update_run_actions()

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
        self._update_run_actions()

    def _update_run_actions(self) -> None:
        has_workflow = bool(self.current_graph_id())
        self.run_button.setEnabled(
            has_workflow and self._runnable and not self._running
        )
        self.stop_button.setEnabled(self._running)

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

    def _request_run(self) -> None:
        if self._runnable and not self._running and self.current_graph_id():
            self.run_requested.emit()


__all__ = ["SCOPE_LABELS", "WorkflowBar", "WorkflowRow"]
