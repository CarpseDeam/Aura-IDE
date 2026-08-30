"""The right column: what is selected, and the two different things it edits.

The distinction this panel exists to make visible is the one the whole
feature rests on. An **assignment** is what an agent is asked to do *at this
point in this workflow* — it belongs to the occurrence, so the same agent
placed twice answers two different briefs. **Reusable Agent settings** are the
definition itself — a name, a brief, a model, a thinking mode — and editing
them changes that agent everywhere it is used, in every workflow, forever.

So the two are never mixed into one form. The assignment sits under its own
question, above its own note. The definition sits below a rule, under a
heading that says out loud how far the change reaches. Everything else here —
the workflow's own details and a selected connection — is read or nudged, and
leaves as intent for the controller to carry out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aura.gui.agents_editor import AgentEditor
from aura.gui.theme import BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

ASSIGNMENT_QUESTION = "What should this Agent do here?"
ASSIGNMENT_NOTE = (
    "This belongs to this workflow only. The same agent placed somewhere else "
    "keeps its own assignment."
)
REUSABLE_HEADING = "Reusable Agent settings"
REUSABLE_NOTE = "Changes this Agent everywhere it is used, in every workflow."


@dataclass(frozen=True)
class WorkflowInfo:
    """The selected workflow, as the inspector shows it."""

    graph_id: str
    scope_label: str
    name: str
    description: str
    runnable: bool
    issues: tuple[str, ...] = field(default_factory=tuple)
    summary: str = ""


@dataclass(frozen=True)
class OccurrenceInfo:
    """One agent occurrence on the canvas."""

    node_id: str
    agent_name: str
    assignment: str
    missing: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ConnectionInfo:
    """One selected line, and the two nodes it joins."""

    connection_id: str
    kind_label: str
    source_label: str
    target_label: str
    order: int
    routed_by_hand: bool = False
    issues: tuple[str, ...] = field(default_factory=tuple)


class WorkflowInspector(QWidget):
    """Workflow, occurrence, connection, and the reusable definition beneath."""

    description_changed = Signal(str)
    assignment_changed = Signal(str, str)  # node id, assignment
    connection_order_changed = Signal(str, int)
    connection_straighten_requested = Signal(str)
    connection_delete_requested = Signal(str)

    def __init__(self, editor: AgentEditor, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editor = editor
        self._workflow: WorkflowInfo | None = None
        self._occurrence: OccurrenceInfo | None = None
        self._connection: ConnectionInfo | None = None
        self._mutations_enabled = True
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(10)
        layout.addWidget(self._build_workflow_group())
        layout.addWidget(self._build_occurrence_group())
        layout.addWidget(self._build_connection_group())
        layout.addWidget(_rule())
        layout.addWidget(self._build_reusable_heading())
        layout.addWidget(editor, 1)
        self.render()

    # ---- construction ------------------------------------------------------

    def _build_workflow_group(self) -> QWidget:
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.workflow_heading = QLabel()
        self.workflow_heading.setWordWrap(True)
        self.workflow_heading.setStyleSheet(
            f"color: {FG}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        column.addWidget(self.workflow_heading)

        self.description = QLineEdit()
        self.description.setPlaceholderText("Optional: what this workflow is for.")
        self.description.editingFinished.connect(self._emit_description)
        column.addWidget(self.description)

        self.validation = QLabel()
        self.validation.setWordWrap(True)
        self.validation.setStyleSheet("font-size: 11px; background: transparent;")
        column.addWidget(self.validation)
        self._workflow_group = group
        return group

    def _build_occurrence_group(self) -> QWidget:
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.occurrence_heading = QLabel()
        self.occurrence_heading.setWordWrap(True)
        self.occurrence_heading.setStyleSheet(
            f"color: {FG}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        column.addWidget(self.occurrence_heading)

        question = QLabel(ASSIGNMENT_QUESTION)
        question.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; background: transparent;"
        )
        column.addWidget(question)

        self.assignment = QPlainTextEdit()
        self.assignment.setPlaceholderText(
            "The one thing this agent is asked to do at this point in the workflow."
        )
        self.assignment.setMinimumHeight(74)
        column.addWidget(self.assignment)

        note = QLabel(ASSIGNMENT_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 10px; background: transparent;"
        )
        column.addWidget(note)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.apply_assignment_button = QPushButton("Apply to this step")
        self.apply_assignment_button.clicked.connect(self._emit_assignment)
        actions.addWidget(self.apply_assignment_button)
        actions.addStretch(1)
        column.addLayout(actions)

        self.occurrence_issues = QLabel()
        self.occurrence_issues.setWordWrap(True)
        self.occurrence_issues.setStyleSheet(
            f"color: {DANGER}; font-size: 11px; background: transparent;"
        )
        column.addWidget(self.occurrence_issues)
        self._occurrence_group = group
        return group

    def _build_connection_group(self) -> QWidget:
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(4)

        self.connection_heading = QLabel()
        self.connection_heading.setWordWrap(True)
        self.connection_heading.setStyleSheet(
            f"color: {FG}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        column.addWidget(self.connection_heading)

        self.connection_detail = QLabel()
        self.connection_detail.setWordWrap(True)
        self.connection_detail.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; background: transparent;"
        )
        column.addWidget(self.connection_detail)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        order_label = QLabel("Order")
        order_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; background: transparent;"
        )
        controls.addWidget(order_label)
        self.connection_order = QSpinBox()
        self.connection_order.setRange(0, 999)
        self.connection_order.setToolTip(
            "Where this connection sits among the others drawn from the same node."
        )
        self.connection_order.valueChanged.connect(self._emit_order)
        controls.addWidget(self.connection_order)
        self.straighten_button = QPushButton("Straighten")
        self.straighten_button.setToolTip("Forget the bend saved for this connection.")
        self.straighten_button.clicked.connect(self._emit_straighten)
        controls.addWidget(self.straighten_button)
        self.remove_connection_button = QPushButton("Remove")
        self.remove_connection_button.setObjectName("danger")
        self.remove_connection_button.clicked.connect(self._emit_remove)
        controls.addWidget(self.remove_connection_button)
        controls.addStretch(1)
        column.addLayout(controls)

        self.connection_issues = QLabel()
        self.connection_issues.setWordWrap(True)
        self.connection_issues.setStyleSheet(
            f"color: {DANGER}; font-size: 11px; background: transparent;"
        )
        column.addWidget(self.connection_issues)
        self._connection_group = group
        return group

    def _build_reusable_heading(self) -> QWidget:
        group = QWidget()
        column = QVBoxLayout(group)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(2)
        heading = QLabel(REUSABLE_HEADING)
        heading.setStyleSheet(
            f"color: {FG}; font-size: 13px; font-weight: 600; background: transparent;"
        )
        column.addWidget(heading)
        note = QLabel(REUSABLE_NOTE)
        note.setWordWrap(True)
        note.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 10px; background: transparent;"
        )
        column.addWidget(note)
        return group

    # ---- what the page sets ------------------------------------------------

    def set_workflow(self, workflow: WorkflowInfo | None) -> None:
        self._workflow = workflow
        self.render()

    def set_occurrence(self, occurrence: OccurrenceInfo | None) -> None:
        self._occurrence = occurrence
        self.render()

    def set_connection(self, connection: ConnectionInfo | None) -> None:
        self._connection = connection
        self.render()

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self.editor.set_mutations_enabled(self._mutations_enabled)
        self._update_actions()

    @property
    def occurrence(self) -> OccurrenceInfo | None:
        return self._occurrence

    @property
    def connection(self) -> ConnectionInfo | None:
        return self._connection

    # ---- rendering ---------------------------------------------------------

    def render(self) -> None:
        self._loading = True
        try:
            self._render_workflow()
            self._render_occurrence()
            self._render_connection()
        finally:
            self._loading = False
        self._update_actions()

    def _render_workflow(self) -> None:
        workflow = self._workflow
        self._workflow_group.setVisible(workflow is not None)
        if workflow is None:
            return
        self.workflow_heading.setText(f"{workflow.name}  ·  {workflow.scope_label}")
        if not self.description.hasFocus():
            self.description.setText(workflow.description)
        color = SUCCESS if workflow.runnable else WARN
        lines = [workflow.summary or ""]
        lines.extend(f"• {issue}" for issue in workflow.issues)
        self.validation.setStyleSheet(
            f"color: {color}; font-size: 11px; background: transparent;"
        )
        self.validation.setText("\n".join(line for line in lines if line))

    def _render_occurrence(self) -> None:
        occurrence = self._occurrence
        self._occurrence_group.setVisible(occurrence is not None)
        if occurrence is None:
            return
        self.occurrence_heading.setText(f"This step  ·  {occurrence.agent_name}")
        if not self.assignment.hasFocus():
            self.assignment.setPlainText(occurrence.assignment)
        self.occurrence_issues.setText(
            "\n".join(f"• {issue}" for issue in occurrence.issues)
        )
        self.occurrence_issues.setVisible(bool(occurrence.issues))

    def _render_connection(self) -> None:
        connection = self._connection
        self._connection_group.setVisible(connection is not None)
        if connection is None:
            return
        self.connection_heading.setText(f"Connection  ·  {connection.kind_label}")
        routing = (
            "Routed by hand — drag its middle handle to reshape it."
            if connection.routed_by_hand
            else "Routed automatically. Drag its middle handle to shape it."
        )
        self.connection_detail.setText(
            f"{connection.source_label}  →  {connection.target_label}\n{routing}"
        )
        self.connection_order.setValue(int(connection.order))
        self.connection_issues.setText(
            "\n".join(f"• {issue}" for issue in connection.issues)
        )
        self.connection_issues.setVisible(bool(connection.issues))

    def _update_actions(self) -> None:
        editable = self._mutations_enabled
        self.description.setReadOnly(not editable or self._workflow is None)
        self.assignment.setReadOnly(not editable or self._occurrence is None)
        self.apply_assignment_button.setEnabled(
            editable and self._occurrence is not None
        )
        for widget in (
            self.connection_order,
            self.straighten_button,
            self.remove_connection_button,
        ):
            widget.setEnabled(editable and self._connection is not None)

    # ---- user intent -------------------------------------------------------

    def _emit_description(self) -> None:
        if self._loading or not self._mutations_enabled or self._workflow is None:
            return
        text = self.description.text().strip()
        if text != self._workflow.description:
            self.description_changed.emit(text)

    def _emit_assignment(self) -> None:
        if self._loading or not self._mutations_enabled or self._occurrence is None:
            return
        self.assignment_changed.emit(
            self._occurrence.node_id, self.assignment.toPlainText().strip()
        )

    def _emit_order(self, value: int) -> None:
        if self._loading or not self._mutations_enabled or self._connection is None:
            return
        if int(value) != self._connection.order:
            self.connection_order_changed.emit(self._connection.connection_id, int(value))

    def _emit_straighten(self) -> None:
        if self._mutations_enabled and self._connection is not None:
            self.connection_straighten_requested.emit(self._connection.connection_id)

    def _emit_remove(self) -> None:
        if self._mutations_enabled and self._connection is not None:
            self.connection_delete_requested.emit(self._connection.connection_id)


def _rule() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFixedHeight(1)
    line.setStyleSheet(f"background: {BORDER}; border: none;")
    return line


__all__ = [
    "ASSIGNMENT_NOTE",
    "ASSIGNMENT_QUESTION",
    "REUSABLE_HEADING",
    "REUSABLE_NOTE",
    "ConnectionInfo",
    "OccurrenceInfo",
    "WorkflowInfo",
    "WorkflowInspector",
]
