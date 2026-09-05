"""Saved Workflow controls and details around a native graph preview."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QToolButton, QVBoxLayout

from aura.agents.workflow_document import WorkflowSaved
from aura.gui.cards.workflow_preview import WorkflowPreview
from aura.gui.theme import BG_TOOL_CARD, BORDER, DANGER, FG, FG_DIM, LABEL_AGENTS, SUCCESS


class WorkflowCard(QFrame):
    run_requested = Signal()
    open_requested = Signal()
    undo_requested = Signal()
    layout_changed = Signal()

    def __init__(self, saved: WorkflowSaved, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("savedWorkflowCard")
        self.setStyleSheet(
            f"QFrame#savedWorkflowCard {{ background: {BG_TOOL_CARD}; border: 1px solid {BORDER}; "
            f"border-left: 3px solid {LABEL_AGENTS}; border-radius: 10px; }}"
        )
        self._busy = False
        self._can_mutate = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 10, 12, 10)
        header = QHBoxLayout()
        self.title = QLabel()
        self.title.setTextFormat(Qt.TextFormat.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet(f"color: {FG}; font-weight: 600;")
        header.addWidget(self.title, 1)
        self.status = QLabel()
        self.status.setStyleSheet(f"color: {SUCCESS};")
        header.addWidget(self.status)
        outer.addLayout(header)
        self.preview = WorkflowPreview(saved.document, self)
        outer.addWidget(self.preview)
        self.details_button = QToolButton()
        self.details_button.setText("Details")
        self.details_button.setCheckable(True)
        self.details_button.toggled.connect(self._toggle_details)
        self.details = QLabel()
        self.details.setTextFormat(Qt.TextFormat.PlainText)
        self.details.setWordWrap(True)
        self.details.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self.details.hide()
        outer.addWidget(self.details)
        actions = QHBoxLayout()
        self.run_button = QPushButton("Run")
        self.run_button.clicked.connect(self.run_requested)
        self.open_button = QPushButton("Open Workflow")
        self.open_button.clicked.connect(self.open_requested)
        self.undo_button = QPushButton("Undo")
        self.undo_button.clicked.connect(self.undo_requested)
        for button in (self.run_button, self.open_button, self.undo_button):
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            actions.addWidget(button)
        actions.addStretch(1)
        actions.addWidget(self.details_button)
        outer.addLayout(actions)
        self.error = QLabel()
        self.error.setTextFormat(Qt.TextFormat.PlainText)
        self.error.setWordWrap(True)
        self.error.setStyleSheet(f"color: {DANGER};")
        self.error.hide()
        outer.addWidget(self.error)
        self.set_saved(saved)

    def set_saved(self, saved: WorkflowSaved) -> None:
        self.saved = saved
        self.title.setText(saved.document.graph.name)
        self.status.setText(saved.status)
        self.preview.set_document(saved.document)
        entries = {entry.agent_id: entry for entry in saved.document.agents}
        lines = [saved.document.graph.description] if saved.document.graph.description else []
        for node in saved.document.graph.nodes:
            entry = entries.get(node.agent_id)
            if entry is not None:
                definition = entry.definition
                target = f"{definition.provider} / {definition.model}" if definition.model else "Aura's current model"
                lines.append(f"{entry.name} · {target} · {entry.permission.label}\n{node.assignment}")
        self.details.setText("\n\n".join(lines))
        self.error.hide()
        self.set_busy(self._busy, can_mutate=self._can_mutate)
        self.layout_changed.emit()

    def set_busy(self, busy: bool, *, can_mutate: bool = True) -> None:
        self._busy, self._can_mutate = busy, can_mutate
        self.run_button.setEnabled(not busy)
        self.undo_button.setEnabled(not busy and can_mutate)
        self.undo_button.setVisible(self.saved.can_undo)

    def show_error(self, message: str) -> None:
        self.error.setText(message)
        self.error.show()
        self.layout_changed.emit()

    def _toggle_details(self, checked: bool) -> None:
        self.details.setVisible(checked)
        self.layout_changed.emit()
