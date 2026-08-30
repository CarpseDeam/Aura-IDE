"""The Agents page — the management surface opened from the rail and /agents.

One modeless window that shows the project and personal agent definitions,
edits them, and records the two decisions that belong to this user alone:
which agents are available to Aura, and what each of them is allowed to do.

The split of responsibility mirrors the Skills manager. This page renders
the rows it is given, collects what the user typed or picked, and emits it.
It never reads a definitions directory, writes a file, resolves an id, or
decides a permission — :class:`aura.gui.main_window_agents.MainWindowAgentsController`
owns all of that through :class:`aura.agents.store.AgentStore` and
:class:`aura.agents.local_state.AgentLocalState`.

Two kinds of change leave here by different routes, because they belong to
different owners. Editing a definition is a document edit and lands on Save.
Availability and permission are private local decisions and apply the moment
they are made — they are never part of a definition and never written into
the project.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aura.agents.local_state import TERMINAL_WARNING, AgentPermission
from aura.gui.agents_editor import (
    INHERIT_TARGET_LABEL,
    AgentDetail,
    AgentDraft,
    AgentEditor,
    ProviderChoices,
    catalog_choices,
)
from aura.gui.theme import BG, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED, WARN

_ID_ROLE = Qt.ItemDataRole.UserRole

#: Group keys, in the order the page lists them.
SCOPE_ORDER: tuple[str, ...] = ("project", "personal")

SCOPE_LABELS: dict[str, str] = {"project": "Project", "personal": "Personal"}

_BUSY_NOTE = (
    "Aura is running a turn. You can read your agents; changes are available "
    "again when the turn finishes."
)


@dataclass(frozen=True)
class AgentRow:
    """One agent as the list shows it.

    ``available`` and ``permission`` come from this user's private local
    state, never from the definition — a project definition has no say in
    either.
    """

    agent_id: str
    scope: str
    name: str
    description: str
    target_label: str
    thinking_label: str
    available: bool
    permission: AgentPermission
    valid: bool = True
    errors: tuple[str, ...] = ()

    @property
    def scope_label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope.title())


class AgentsPage(QDialog):
    """Modeless Agents window: two lists, one editor, and the local grants."""

    visibility_changed = Signal(bool)
    current_row_changed = Signal(str)
    create_requested = Signal(str)  # scope key
    save_requested = Signal(object)  # AgentDraft
    delete_requested = Signal(str)
    availability_changed = Signal(str, bool)
    permission_changed = Signal(str, str)  # agent id, AgentPermission value

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        choices: ProviderChoices | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("agentsPage")
        self.setWindowTitle("Agents")
        self.setModal(False)
        self.setMinimumSize(720, 480)
        self.resize(940, 620)
        self.setStyleSheet(
            f"QDialog#agentsPage {{ background: {BG}; border: 1px solid {BORDER}; }}"
        )

        self._choices = choices or catalog_choices()
        self._rows: tuple[AgentRow, ...] = ()
        self._items: dict[str, QTreeWidgetItem] = {}
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._current_id: str = ""
        self._detail: AgentDetail | None = None
        self._mutations_enabled = True
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        layout.addLayout(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.addWidget(self._build_tree())
        splitter.addWidget(self._build_editor())
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([320, 560])
        layout.addWidget(splitter, 1)

        self._warning = QLabel(f"⚠  {TERMINAL_WARNING}")
        self._warning.setObjectName("agentsTerminalWarning")
        self._warning.setWordWrap(True)
        self._warning.setStyleSheet(
            f"color: {WARN}; font-size: 11px; background: transparent;"
        )
        layout.addWidget(self._warning)

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

        subtitle = QLabel("Named helpers Aura can hand a scoped piece of work to.")
        subtitle.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        row.addWidget(subtitle)
        row.addStretch(1)

        self._new_project_btn = QPushButton("New project agent")
        self._new_project_btn.setToolTip(
            "Create an agent that lives in this project and travels with it."
        )
        self._new_project_btn.clicked.connect(lambda: self._request_create("project"))
        row.addWidget(self._new_project_btn)

        self._new_personal_btn = QPushButton("New personal agent")
        self._new_personal_btn.setToolTip(
            "Create an agent that stays on this computer, in every project you open."
        )
        self._new_personal_btn.clicked.connect(lambda: self._request_create("personal"))
        row.addWidget(self._new_personal_btn)
        return row

    def _build_tree(self) -> QWidget:
        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setRootIsDecorated(False)
        self._tree.setIndentation(14)
        self._tree.setUniformRowHeights(False)
        self._tree.setStyleSheet(
            f"QTreeWidget {{ background: {BG_ALT}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 6px; padding: 4px; }"
        )
        self._tree.currentItemChanged.connect(lambda _cur, _prev: self._sync_current())
        self._tree.itemChanged.connect(self._on_item_changed)
        return self._tree

    def _build_editor(self) -> QWidget:
        self._editor = AgentEditor(self._choices)
        self._editor.save_requested.connect(self.save_requested)
        self._editor.delete_requested.connect(self.delete_requested)
        self._editor.permission_changed.connect(self.permission_changed)
        # Compatibility aliases for the page's established test/controller
        # surface. Ownership remains inside AgentEditor.
        self._name = self._editor.name
        self._description = self._editor.description
        self._instructions = self._editor.instructions
        self._provider = self._editor.provider
        self._model = self._editor.model
        self._thinking = self._editor.thinking
        self._permission = self._editor.permission
        self._save_btn = self._editor.save_button
        self._delete_btn = self._editor.delete_button
        return self._editor

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

    # ---- controller-facing API ---------------------------------------------

    def set_rows(self, rows: tuple[AgentRow, ...]) -> None:
        """Replace the whole roster, keeping the current row when it survives."""
        self._rows = tuple(rows)
        self._rebuild()

    def set_detail(self, detail: AgentDetail | None) -> None:
        """Load the editor with the current agent, or clear it."""
        self._detail = detail
        self._editor.set_detail(detail)

    def apply_local_state(
        self, agent_id: str, *, available: bool, permission: AgentPermission
    ) -> None:
        """Re-render one row after a local decision, without rebuilding the list.

        Availability arrives from the row's own check box and permission from
        the editor's combo, so both land while Qt is still inside that
        widget's signal. Rebuilding here would destroy the very item or
        repopulate the very combo that is mid-emit, so this updates in place
        instead.
        """
        row = self._row(agent_id)
        if row is None:
            return
        updated = replace(row, available=available, permission=permission)
        self._rows = tuple(
            updated if candidate.agent_id == agent_id else candidate for candidate in self._rows
        )

        self._loading = True
        try:
            item = self._items.get(agent_id)
            if item is not None:
                item.setText(0, _row_text(updated))
                item.setToolTip(0, _row_tooltip(updated))
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked if available else Qt.CheckState.Unchecked,
                )
            if self._detail is not None and self._detail.agent_id == agent_id:
                self._detail = replace(
                    self._detail, available=available, permission=permission
                )
                self._editor.apply_local_state(
                    available=available, permission=permission
                )
        finally:
            self._loading = False

    def set_mutations_enabled(self, enabled: bool) -> None:
        """Allow or forbid every change without hiding anything.

        Browsing stays live during a turn: definitions, the roster, and
        permissions are all frozen, because a running turn may already be
        acting on the answers they gave.
        """
        self._mutations_enabled = bool(enabled)
        self._status.setText("" if self._mutations_enabled else _BUSY_NOTE)
        self._editor.set_mutations_enabled(self._mutations_enabled)
        self._rebuild()

    def mutations_enabled(self) -> bool:
        return self._mutations_enabled

    def is_open(self) -> bool:
        return self.isVisible()

    def current_agent_id(self) -> str:
        return self._current_id

    def select_agent(self, agent_id: str) -> bool:
        item = self._items.get(agent_id)
        if item is None:
            return False
        self._tree.setCurrentItem(item)
        return True

    def visible_agent_ids(self) -> dict[str, tuple[str, ...]]:
        """Visible agent ids per scope, in rendered order."""
        visible: dict[str, tuple[str, ...]] = {}
        for scope in SCOPE_ORDER:
            group = self._groups.get(scope)
            if group is None:
                visible[scope] = ()
                continue
            visible[scope] = tuple(
                str(group.child(index).data(0, _ID_ROLE)) for index in range(group.childCount())
            )
        return visible

    def draft(self) -> AgentDraft | None:
        """What Save would send right now, or None with nothing loaded."""
        return self._editor.draft()

    # ---- rendering ---------------------------------------------------------

    def _row(self, agent_id: str) -> AgentRow | None:
        return next((row for row in self._rows if row.agent_id == agent_id), None)

    def _rebuild(self) -> None:
        previous = self._current_id
        self._loading = True
        self._tree.blockSignals(True)
        self._tree.clear()
        self._items = {}
        self._groups = {}

        for scope in SCOPE_ORDER:
            group = QTreeWidgetItem(self._tree)
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._groups[scope] = group
            count = 0
            for row in self._rows:
                if row.scope != scope:
                    continue
                item = QTreeWidgetItem(group)
                item.setData(0, _ID_ROLE, row.agent_id)
                item.setText(0, _row_text(row))
                item.setToolTip(0, _row_tooltip(row))
                item.setFlags(_item_flags(row, self._mutations_enabled))
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked if row.available else Qt.CheckState.Unchecked,
                )
                self._items[row.agent_id] = item
                count += 1
            group.setText(0, f"{SCOPE_LABELS[scope]}  ({count})")
            group.setExpanded(True)

        self._tree.setCurrentItem(self._items.get(previous) or self._first_item())
        self._tree.blockSignals(False)
        self._loading = False
        self._sync_current()

    def _first_item(self) -> QTreeWidgetItem | None:
        for scope in SCOPE_ORDER:
            group = self._groups.get(scope)
            if group is not None and group.childCount():
                return group.child(0)
        return None

    def _sync_current(self) -> None:
        item = self._tree.currentItem()
        raw = item.data(0, _ID_ROLE) if item is not None else None
        current = str(raw) if raw else ""
        changed = current != self._current_id
        self._current_id = current
        self._update_actions()
        if changed or not current:
            self.current_row_changed.emit(current)

    def _update_actions(self) -> None:
        self._new_project_btn.setEnabled(self._mutations_enabled)
        self._new_personal_btn.setEnabled(self._mutations_enabled)
        self._editor.set_mutations_enabled(self._mutations_enabled)

    # ---- user intent -------------------------------------------------------

    def _request_create(self, scope: str) -> None:
        if self._mutations_enabled:
            self.create_requested.emit(scope)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._loading or not self._mutations_enabled:
            return
        raw = item.data(0, _ID_ROLE)
        if not raw:
            return
        agent_id = str(raw)
        available = item.checkState(0) == Qt.CheckState.Checked
        row = self._row(agent_id)
        if row is not None and row.available == available:
            return
        self.availability_changed.emit(agent_id, available)

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


def _item_flags(row: AgentRow, mutations_enabled: bool) -> Qt.ItemFlag:
    """A broken definition cannot be made available, and a running turn freezes all."""
    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    if row.valid and mutations_enabled:
        flags |= Qt.ItemFlag.ItemIsUserCheckable
    return flags


def _row_text(row: AgentRow) -> str:
    if not row.valid:
        return f"{row.name}   ·   could not be loaded"
    head = f"{row.name}   ·   {row.permission.label}"
    if row.description:
        return f"{head}\n{row.description}"
    return head


def _row_tooltip(row: AgentRow) -> str:
    if not row.valid:
        return "\n".join(row.errors) or "This definition could not be loaded."
    parts = [row.description, row.target_label, f"Thinking: {row.thinking_label}"]
    return "\n".join(part for part in parts if part)


__all__ = [
    "INHERIT_TARGET_LABEL",
    "SCOPE_LABELS",
    "SCOPE_ORDER",
    "AgentDetail",
    "AgentDraft",
    "AgentRow",
    "AgentsPage",
    "ProviderChoices",
    "catalog_choices",
]
