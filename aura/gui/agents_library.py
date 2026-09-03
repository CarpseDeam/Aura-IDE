"""The Agent Library — the left column of the Agents page.

Two grouped lists, project then personal, each row carrying the one decision
that is checkable in place: whether Aura may use that agent at all. Creating,
editing, and deleting a definition all still go out as intent; nothing here
reads or writes a file.

The library is also the source of every agent that reaches a workflow canvas.
A row is draggable, and the drag carries the agent's scope and its immutable
id — never its name, and never a copy of what it is told to do — so dropping
one on a canvas places an *occurrence* that keeps pointing at the definition
it came from.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aura.agents.local_state import AgentPermission
from aura.gui.theme import BG_ALT, BORDER, FG

#: What a dragged library row carries: ``<scope>:<agent id>``. Names are
#: labels and change; the id is the agent.
AGENT_MIME = "application/x-aura-agent"

_ID_ROLE = Qt.ItemDataRole.UserRole
_AGENT_ID_ROLE = Qt.ItemDataRole.UserRole + 1

#: Group keys, in the order the library lists them.
SCOPE_ORDER: tuple[str, ...] = ("project", "personal")

SCOPE_LABELS: dict[str, str] = {"project": "Project", "personal": "Personal"}


@dataclass(frozen=True)
class AgentRow:
    """One agent as the list shows it.

    ``available`` and ``permission`` come from this user's private local
    state, never from the definition — a project definition has no say in
    either. ``model_label`` is already provider-qualified when the definition
    selects a provider, so the tooltip never hides a mixed-model target.
    """

    agent_id: str
    scope: str
    name: str
    description: str
    model_label: str
    thinking_label: str
    available: bool
    permission: AgentPermission
    valid: bool = True
    errors: tuple[str, ...] = ()

    @property
    def scope_label(self) -> str:
        return SCOPE_LABELS.get(self.scope, self.scope.title())

    @property
    def source_key(self) -> str:
        return source_key(self.scope, self.agent_id)


class AgentLibraryTree(QTreeWidget):
    """The rows themselves, and the one thing they know how to be: a drag."""

    def mimeData(  # noqa: N802 - Qt naming
        self, items: Sequence[QTreeWidgetItem]
    ) -> QMimeData:
        payload = QMimeData()
        for item in items:
            raw = item.data(0, _ID_ROLE)
            if raw:
                payload.setData(AGENT_MIME, str(raw).encode("utf-8"))
                payload.setText(item.text(0).splitlines()[0])
                break
        return payload


class AgentLibrary(QWidget):
    """The library column: create buttons and the two grouped lists."""

    create_requested = Signal(str)  # scope key
    current_row_changed = Signal(str)  # source key
    availability_changed = Signal(str, bool)  # agent id, available

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[AgentRow, ...] = ()
        self._items: dict[str, QTreeWidgetItem] = {}
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._current_source_key: str = ""
        self._current_id: str = ""
        self._mutations_enabled = True
        self._loading = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self.new_project_button = _small_button(
            "New project agent",
            "Create an agent that lives in this project and travels with it.",
        )
        self.new_project_button.clicked.connect(lambda: self._request_create("project"))
        buttons.addWidget(self.new_project_button)
        self.new_personal_button = _small_button(
            "New personal agent",
            "Create an agent that stays on this computer, in every project you open.",
        )
        self.new_personal_button.clicked.connect(
            lambda: self._request_create("personal")
        )
        buttons.addWidget(self.new_personal_button)
        layout.addLayout(buttons)

        self.tree = AgentLibraryTree()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(14)
        self.tree.setUniformRowHeights(False)
        self.tree.setDragEnabled(True)
        self.tree.setDragDropMode(QTreeWidget.DragDropMode.DragOnly)
        self.tree.setToolTip("Drag an agent onto the canvas to place it in a workflow.")
        self.tree.setStyleSheet(
            f"QTreeWidget {{ background: {BG_ALT}; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 4px; }}"
        )
        self.tree.currentItemChanged.connect(lambda _cur, _prev: self.sync_current())
        self.tree.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.tree, 1)

    # ---- what the page asks for --------------------------------------------

    @property
    def items(self) -> dict[str, QTreeWidgetItem]:
        return self._items

    @property
    def rows(self) -> tuple[AgentRow, ...]:
        return self._rows

    def current_source_key(self) -> str:
        return self._current_source_key

    def current_agent_id(self) -> str:
        return self._current_id

    def set_rows(self, rows: tuple[AgentRow, ...]) -> None:
        """Replace the whole roster, keeping the current row when it survives."""
        self._rows = tuple(rows)
        self.rebuild()

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self.new_project_button.setEnabled(self._mutations_enabled)
        self.new_personal_button.setEnabled(self._mutations_enabled)
        self.rebuild()

    def row(self, key: str) -> AgentRow | None:
        return next((row for row in self._rows if row.source_key == key), None)

    def select_agent(self, agent_id: str, scope: str = "") -> bool:
        matches = [
            row
            for row in self._rows
            if row.agent_id == agent_id and (not scope or row.scope == scope)
        ]
        if len(matches) != 1:
            return False
        item = self._items.get(matches[0].source_key)
        if item is None:
            return False
        self.tree.setCurrentItem(item)
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
                str(group.child(index).data(0, _AGENT_ID_ROLE))
                for index in range(group.childCount())
            )
        return visible

    def apply_local_state(
        self, agent_id: str, *, available: bool, permission: AgentPermission
    ) -> tuple[AgentRow, ...]:
        """Re-render one agent's rows in place, without rebuilding the list.

        Availability arrives from the row's own check box, so Qt is still
        inside that item's signal. Rebuilding here would destroy the very
        item mid-emit, so this updates the text and check state instead.
        """
        matching = [row for row in self._rows if row.agent_id == agent_id]
        if not matching:
            return ()
        updated_by_key = {
            row.source_key: replace(row, available=available, permission=permission)
            for row in matching
        }
        self._rows = tuple(
            updated_by_key.get(candidate.source_key, candidate)
            for candidate in self._rows
        )
        self._loading = True
        try:
            for key, updated in updated_by_key.items():
                item = self._items.get(key)
                if item is None:
                    continue
                item.setText(0, _row_text(updated))
                item.setToolTip(0, _row_tooltip(updated))
                item.setCheckState(
                    0, Qt.CheckState.Checked if available else Qt.CheckState.Unchecked
                )
        finally:
            self._loading = False
        return tuple(updated_by_key.values())

    # ---- rendering ---------------------------------------------------------

    def rebuild(self) -> None:
        previous = self._current_source_key
        self._loading = True
        self.tree.blockSignals(True)
        self.tree.clear()
        self._items = {}
        self._groups = {}

        for scope in SCOPE_ORDER:
            group = QTreeWidgetItem(self.tree)
            group.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self._groups[scope] = group
            count = 0
            for row in self._rows:
                if row.scope != scope:
                    continue
                item = QTreeWidgetItem(group)
                item.setData(0, _ID_ROLE, row.source_key)
                item.setData(0, _AGENT_ID_ROLE, row.agent_id)
                item.setText(0, _row_text(row))
                item.setToolTip(0, _row_tooltip(row))
                item.setFlags(_item_flags(row, self._mutations_enabled))
                item.setCheckState(
                    0,
                    Qt.CheckState.Checked if row.available else Qt.CheckState.Unchecked,
                )
                self._items[row.source_key] = item
                count += 1
            group.setText(0, f"{SCOPE_LABELS[scope]}  ({count})")
            group.setExpanded(True)

        self.tree.setCurrentItem(self._items.get(previous) or self._first_item())
        self.tree.blockSignals(False)
        self._loading = False
        self.sync_current()

    def _first_item(self) -> QTreeWidgetItem | None:
        for scope in SCOPE_ORDER:
            group = self._groups.get(scope)
            if group is not None and group.childCount():
                return group.child(0)
        return None

    def sync_current(self) -> None:
        item = self.tree.currentItem()
        raw = item.data(0, _ID_ROLE) if item is not None else None
        current = str(raw) if raw else ""
        row = self.row(current)
        changed = current != self._current_source_key
        self._current_source_key = current
        self._current_id = row.agent_id if row is not None else ""
        if changed or not current:
            self.current_row_changed.emit(current)

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
        row = self.row(str(raw))
        if row is None:
            return
        available = item.checkState(0) == Qt.CheckState.Checked
        if row.available == available:
            return
        self.availability_changed.emit(row.agent_id, available)


def _item_flags(row: AgentRow, mutations_enabled: bool) -> Qt.ItemFlag:
    """A broken definition cannot be made available, and a running turn freezes all."""
    flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
    if row.valid:
        flags |= Qt.ItemFlag.ItemIsDragEnabled
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
    parts = [row.description, row.model_label, f"Thinking: {row.thinking_label}"]
    return "\n".join(part for part in parts if part)


def source_key(scope: str, agent_id: str) -> str:
    return f"{scope}:{agent_id}"


def _small_button(text: str, tooltip: str) -> QPushButton:
    button = QPushButton(text)
    button.setToolTip(tooltip)
    return button


__all__ = [
    "AGENT_MIME",
    "SCOPE_LABELS",
    "SCOPE_ORDER",
    "AgentLibrary",
    "AgentLibraryTree",
    "AgentRow",
    "source_key",
]
