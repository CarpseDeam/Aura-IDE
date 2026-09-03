"""The Agents page — the management surface opened from the rail and /agents.

One modeless window, in four parts. Above, the workflow being authored and
what may be done to it. On the left, the Agent Library: the project and
personal definitions, created, edited, deleted, and dragged onto the canvas.
In the middle, the canvas itself. On the right, an inspector that keeps the
two kinds of editing visibly apart — what an agent is asked to do *here*, and
the reusable definition that changes it everywhere.

The split of responsibility is unchanged from the day this page only listed
agents. It renders the rows it is given, collects what the user typed or
picked, and emits it. It never reads a definitions directory, writes a file,
resolves an id, or decides a permission —
:class:`aura.gui.main_window_agents.MainWindowAgentsController` owns agent
storage and this user's grants, and
:class:`aura.gui.main_window_agents_graphs.AgentsGraphController` owns
workflows and their validation.

Two kinds of change leave here by different routes, because they belong to
different owners. Editing a definition is a document edit and lands on Save.
Availability and permission are private local decisions and apply the moment
they are made — they are never part of a definition and never written into
the project.

Whether Aura may reach for a workflow on its own is neither of those, and it
is not asked here at all: it is one switch, in the main toolbar, over the
workflow this window has open. Run, above the canvas, is a different question
— it runs what is in front of you, once, now — and it works whatever that
switch says.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aura.agents.local_state import AgentPermission
from aura.gui.agents_editor import (
    AgentDetail,
    AgentDraft,
    AgentEditor,
    ModelChoices,
    ModelTargetChoice,
    catalog_choices,
)
from aura.gui.agents_library import (
    SCOPE_LABELS,
    SCOPE_ORDER,
    AgentLibrary,
    AgentRow,
)
from aura.gui.agents_workflow_bar import WorkflowBar, WorkflowRow
from aura.gui.agents_workflow_canvas import WorkflowScene, WorkflowView
from aura.gui.agents_workflow_inspector import (
    ConnectionInfo,
    OccurrenceInfo,
    WorkflowInfo,
    WorkflowInspector,
)
from aura.gui.theme import BG, BORDER, FG, FG_DIM, FG_MUTED

_BUSY_NOTE = (
    "Aura is running a turn. You can read your agents; changes are available "
    "again when the turn finishes."
)


class AgentsPage(QDialog):
    """Modeless Agents window: library, canvas, inspector, and the local grants."""

    visibility_changed = Signal(bool)
    current_row_changed = Signal(str)
    create_requested = Signal(str)  # scope key
    save_requested = Signal(object)  # AgentDraft
    delete_requested = Signal(str, str)  # scope key, agent id
    availability_changed = Signal(str, bool)
    permission_changed = Signal(str, str)  # agent id, AgentPermission value

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        choices: ModelChoices | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("agentsPage")
        self.setWindowTitle("Agents")
        self.setModal(False)
        self.setMinimumSize(940, 560)
        self.resize(1360, 780)
        self.setStyleSheet(
            f"QDialog#agentsPage {{ background: {BG}; border: 1px solid {BORDER}; }}"
        )

        self._choices = choices or catalog_choices()
        self._detail: AgentDetail | None = None
        self._mutations_enabled = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        layout.addLayout(self._build_header())

        self.workflow_bar = WorkflowBar()
        layout.addWidget(self.workflow_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.addWidget(self._build_library())
        splitter.addWidget(self._build_canvas())
        splitter.addWidget(self._build_inspector())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 5)
        splitter.setStretchFactor(2, 3)
        splitter.setSizes([300, 660, 400])
        layout.addWidget(splitter, 1)
        layout.addLayout(self._build_footer())

    # ---- construction ------------------------------------------------------

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)

        title = QLabel("Agents")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(15)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {FG}; background: transparent;")
        row.addWidget(title)

        subtitle = QLabel(
            "Named helpers Aura can hand a scoped piece of work to, and the "
            "workflows that put them in order."
        )
        subtitle.setStyleSheet(
            f"color: {FG_DIM}; font-size: 12px; background: transparent;"
        )
        row.addWidget(subtitle)
        row.addStretch(1)
        return row

    def _build_library(self) -> QWidget:
        self._library = AgentLibrary()
        self._library.create_requested.connect(self._request_create)
        self._library.current_row_changed.connect(self.current_row_changed)
        self._library.availability_changed.connect(self.availability_changed)
        return self._library

    def _build_canvas(self) -> QWidget:
        self.scene = WorkflowScene(self)
        self.view = WorkflowView(self.scene)
        return self.view

    def _build_inspector(self) -> QWidget:
        self._editor = AgentEditor(self._choices)
        self._editor.save_requested.connect(self.save_requested)
        self._editor.delete_requested.connect(self.delete_requested)
        self._editor.permission_changed.connect(self.permission_changed)
        self.inspector = WorkflowInspector(self._editor)

        # Compatibility aliases for the page's established test/controller
        # surface. Ownership remains inside AgentEditor.
        self._name = self._editor.name
        self._description = self._editor.description
        self._instructions = self._editor.instructions
        self._model = self._editor.model
        self._thinking = self._editor.thinking
        self._permission = self._editor.permission
        self._save_btn = self._editor.save_button
        self._delete_btn = self._editor.delete_button

        scroller = QScrollArea()
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QScrollArea.Shape.NoFrame)
        scroller.setWidget(self.inspector)
        return scroller

    def _build_footer(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        row.addWidget(self._status, 1)

        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        row.addWidget(close)
        return row

    # ---- the library's established surface ---------------------------------

    @property
    def _items(self) -> dict[str, QTreeWidgetItem]:
        return self._library.items

    @property
    def _tree(self):
        return self._library.tree

    @property
    def _new_project_btn(self) -> QPushButton:
        return self._library.new_project_button

    @property
    def _new_personal_btn(self) -> QPushButton:
        return self._library.new_personal_button

    # ---- controller-facing API ---------------------------------------------

    def set_rows(self, rows: tuple[AgentRow, ...]) -> None:
        """Replace the whole roster, keeping the current row when it survives."""
        self._library.set_rows(rows)

    def set_detail(self, detail: AgentDetail | None) -> None:
        """Load the editor with the current agent, or clear it."""
        self._detail = detail
        self._editor.set_detail(detail)

    def apply_local_state(
        self, agent_id: str, *, available: bool, permission: AgentPermission
    ) -> None:
        """Re-render one row after a local decision, without rebuilding the list."""
        if not self._library.apply_local_state(
            agent_id, available=available, permission=permission
        ):
            return
        if self._detail is not None and self._detail.agent_id == agent_id:
            self._detail = replace(
                self._detail, available=available, permission=permission
            )
            self._editor.apply_local_state(available=available, permission=permission)

    def set_mutations_enabled(self, enabled: bool) -> None:
        """Allow or forbid every change without hiding anything.

        Browsing stays live during a turn: definitions, the roster, permissions,
        and every workflow are all frozen, because a running turn may already
        be acting on the answers they gave.
        """
        self._mutations_enabled = bool(enabled)
        self._status.setText("" if self._mutations_enabled else _BUSY_NOTE)
        self._library.set_mutations_enabled(self._mutations_enabled)
        self.inspector.set_mutations_enabled(self._mutations_enabled)
        self.workflow_bar.set_mutations_enabled(self._mutations_enabled)
        self.scene.set_editable(self._mutations_enabled)

    def mutations_enabled(self) -> bool:
        return self._mutations_enabled

    def is_open(self) -> bool:
        return self.isVisible()

    def current_agent_id(self) -> str:
        return self._library.current_agent_id()

    def current_source_key(self) -> str:
        return self._library.current_source_key()

    def select_agent(self, agent_id: str, scope: str = "") -> bool:
        return self._library.select_agent(agent_id, scope)

    def visible_agent_ids(self) -> dict[str, tuple[str, ...]]:
        """Visible agent ids per scope, in rendered order."""
        return self._library.visible_agent_ids()

    def draft(self) -> AgentDraft | None:
        """What Save would send right now, or None with nothing loaded."""
        return self._editor.draft()

    # ---- the workflow surface ----------------------------------------------

    def set_workflow_rows(self, rows: tuple[WorkflowRow, ...], current_id: str) -> None:
        self.workflow_bar.set_rows(rows, current_id)

    def set_workflow_runnable(self, runnable: bool) -> None:
        self.workflow_bar.set_runnable(runnable)

    def set_workflow_running(self, running: bool) -> None:
        self.workflow_bar.set_running(running)

    def set_run_states(self, nodes: dict, connections: dict | None = None) -> None:
        """Show which steps are running, finished, or never got to."""
        self.scene.set_run_states(nodes, connections)

    def set_model_choices(self, choices: ModelChoices) -> None:
        """Re-list the editor's qualified targets after catalogs change."""
        self._choices = choices
        self._editor.set_choices(choices)

    def set_workflow_info(self, info: WorkflowInfo | None) -> None:
        self.inspector.set_workflow(info)

    def set_occurrence(self, occurrence: OccurrenceInfo | None) -> None:
        self.inspector.set_occurrence(occurrence)

    def set_connection(self, connection: ConnectionInfo | None) -> None:
        self.inspector.set_connection(connection)

    def current_workflow_id(self) -> str:
        return self.workflow_bar.current_graph_id()

    # ---- user intent -------------------------------------------------------

    def _request_create(self, scope: str) -> None:
        if self._mutations_enabled:
            self.create_requested.emit(scope)

    # ---- Qt lifecycle ------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        super().closeEvent(event)
        self.visibility_changed.emit(False)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self.visibility_changed.emit(False)


__all__ = [
    "SCOPE_LABELS",
    "SCOPE_ORDER",
    "AgentDetail",
    "AgentDraft",
    "AgentRow",
    "AgentsPage",
    "ConnectionInfo",
    "ModelChoices",
    "ModelTargetChoice",
    "OccurrenceInfo",
    "WorkflowInfo",
    "WorkflowRow",
    "catalog_choices",
]
