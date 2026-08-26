"""The floating Skills window: presentation, filtering, and user intent.

One reusable non-modal window owned by
:class:`aura.gui.skills_manager.controller.SkillsManagerController`. It shows
the rows it is given, groups them by scope, filters them against the search
box, tracks the current row, renders its detail, and emits what the user
asked for. It never scans a skill directory, interprets precedence, edits a
manifest, or deletes anything — the controller and SkillLibrary own all of
that.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from aura.gui.skills_manager.models import SCOPE_LABELS, SCOPE_ORDER, SkillDetail, SkillRow
from aura.gui.theme import BG, BG_ALT, BORDER, FG, FG_DIM, FG_MUTED

_ID_ROLE = Qt.ItemDataRole.UserRole

#: Shown under the actions so the capability boundary is never in doubt.
PERMISSION_REMINDER = (
    "Skills guide how Aura works. They never grant extra shell, network, "
    "file-mutation, script-execution, or external-read permissions."
)


def _matches(row: SkillRow, query: str) -> bool:
    """True when *row* survives the search box, across its useful text."""
    if not query:
        return True
    haystack = " ".join((row.name, row.description, row.scope_label, row.status_text)).lower()
    return all(term in haystack for term in query.split())


class SkillsManagerWindow(QDialog):
    """Searchable Project / Personal / Bundled inventory with a detail pane."""

    current_row_changed = Signal(str)
    use_requested = Signal(str)
    enable_toggle_requested = Signal(str, bool)
    uninstall_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Skills")
        self.setModal(False)
        self.setWindowFlag(Qt.WindowType.Tool, True)
        self.resize(880, 560)

        self._rows: tuple[SkillRow, ...] = ()
        self._items: dict[str, QTreeWidgetItem] = {}
        self._groups: dict[str, QTreeWidgetItem] = {}
        self._current_id: str = ""
        self._mutations_enabled = True

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 12, 14, 12)
        outer.setSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Search skills by name, description, scope, or state")
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(lambda _text: self._rebuild())
        outer.addWidget(self._search)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.addWidget(self._build_tree())
        splitter.addWidget(self._build_details())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 4)
        splitter.setSizes([360, 480])
        outer.addWidget(splitter, 1)

        outer.addLayout(self._build_actions())

        reminder = QLabel(PERMISSION_REMINDER)
        reminder.setWordWrap(True)
        reminder.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px; background: transparent;")
        outer.addWidget(reminder)

        self._update_actions()

    # ---- construction ------------------------------------------------------

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
        self._tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        return self._tree

    def _build_details(self) -> QWidget:
        panel = QWidget()
        panel.setStyleSheet(f"background: {BG}; color: {FG};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        self._detail_title = QLabel()
        self._detail_title.setWordWrap(True)
        self._detail_title.setStyleSheet(
            f"color: {FG}; font-size: 15px; font-weight: 600; background: transparent;"
        )
        layout.addWidget(self._detail_title)

        self._detail_meta = QLabel()
        self._detail_meta.setWordWrap(True)
        self._detail_meta.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        layout.addWidget(self._detail_meta)

        self._detail_body = QLabel()
        self._detail_body.setWordWrap(True)
        self._detail_body.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._detail_body.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._detail_body.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        layout.addWidget(self._detail_body, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {BORDER}; border-radius: 6px; }}")
        return scroll

    def _build_actions(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(8)

        self._use_btn = QPushButton("Use in next message")
        self._use_btn.setObjectName("primary")
        self._use_btn.clicked.connect(self._on_use_clicked)
        actions.addWidget(self._use_btn)

        self._enable_btn = QPushButton("Disable")
        self._enable_btn.clicked.connect(self._on_enable_clicked)
        actions.addWidget(self._enable_btn)

        self._uninstall_btn = QPushButton("Uninstall")
        self._uninstall_btn.setObjectName("danger")
        self._uninstall_btn.clicked.connect(self._on_uninstall_clicked)
        actions.addWidget(self._uninstall_btn)

        actions.addStretch(1)

        close = QPushButton("Close")
        close.clicked.connect(self.hide)
        actions.addWidget(close)
        return actions

    # ---- controller-facing API ---------------------------------------------

    def set_rows(self, rows: tuple[SkillRow, ...]) -> None:
        """Replace the whole inventory, keeping the current row when it survives."""
        self._rows = tuple(rows)
        self._rebuild()

    def set_details(self, detail: SkillDetail | None) -> None:
        """Render the detail pane for the current row."""
        if detail is None:
            self._detail_title.setText("No skill selected")
            self._detail_meta.setText("")
            self._detail_body.setText(
                "Select a skill to see what it covers and how Aura would use it."
            )
            return
        self._detail_title.setText(detail.name)
        self._detail_meta.setText(f"{detail.scope_label}  ·  {detail.status_text}")
        self._detail_body.setText(_detail_body_text(detail))

    def set_mutations_enabled(self, enabled: bool) -> None:
        """Allow or forbid enable/disable/uninstall without hiding the inventory."""
        self._mutations_enabled = bool(enabled)
        self._update_actions()

    # ---- current row -------------------------------------------------------

    def current_install_id(self) -> str:
        return self._current_id

    def select_row(self, install_id: str) -> bool:
        """Make *install_id* the current row, if it is currently visible."""
        item = self._items.get(install_id)
        if item is None:
            return False
        self._tree.setCurrentItem(item)
        return True

    def set_search_text(self, text: str) -> None:
        self._search.setText(text)

    def visible_row_ids(self) -> dict[str, tuple[str, ...]]:
        """Visible install ids per scope, in rendered order."""
        visible: dict[str, tuple[str, ...]] = {}
        for scope in SCOPE_ORDER:
            group = self._groups.get(scope)
            if group is None or group.isHidden():
                visible[scope] = ()
                continue
            visible[scope] = tuple(
                str(group.child(index).data(0, _ID_ROLE)) for index in range(group.childCount())
            )
        return visible

    def details_text(self) -> str:
        """Everything the detail pane currently renders, as plain text."""
        parts = (
            self._detail_title.text(),
            self._detail_meta.text(),
            self._detail_body.text(),
        )
        return "\n".join(part for part in parts if part)

    # ---- internals ---------------------------------------------------------

    def _row(self, install_id: str) -> SkillRow | None:
        return next((row for row in self._rows if row.install_id == install_id), None)

    def _rebuild(self) -> None:
        previous = self._current_id
        query = self._search.text().strip().lower()

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
                if row.scope != scope or not _matches(row, query):
                    continue
                item = QTreeWidgetItem(group)
                item.setData(0, _ID_ROLE, row.install_id)
                item.setText(0, _row_text(row))
                item.setToolTip(0, row.description or row.status_text)
                self._items[row.install_id] = item
                count += 1
            group.setText(0, f"{SCOPE_LABELS[scope]}  ({count})")
            group.setExpanded(True)
            group.setHidden(count == 0)

        self._tree.setCurrentItem(self._items.get(previous) or self._first_visible_item())
        self._tree.blockSignals(False)
        self._sync_current()

    def _first_visible_item(self) -> QTreeWidgetItem | None:
        for scope in SCOPE_ORDER:
            group = self._groups.get(scope)
            if group is not None and group.childCount():
                return group.child(0)
        return None

    def _sync_current(self) -> None:
        item = self._tree.currentItem()
        raw = item.data(0, _ID_ROLE) if item is not None else None
        self._current_id = str(raw) if raw else ""
        self._update_actions()
        self.current_row_changed.emit(self._current_id)

    def _update_actions(self) -> None:
        row = self._row(self._current_id)
        if row is None:
            self._use_btn.setText("Use in next message")
            self._use_btn.setEnabled(False)
            self._enable_btn.setEnabled(False)
            self._uninstall_btn.setVisible(False)
            return

        if row.already_selected:
            self._use_btn.setText("Added to next message")
            self._use_btn.setEnabled(False)
        else:
            self._use_btn.setText("Use in next message")
            self._use_btn.setEnabled(row.selectable)
        self._use_btn.setToolTip(_use_tooltip(row))

        self._enable_btn.setText("Disable" if row.enabled else "Enable")
        self._enable_btn.setEnabled(self._mutations_enabled)
        # Bundled skills are immutable, so the action never appears for them.
        self._uninstall_btn.setVisible(row.can_uninstall)
        self._uninstall_btn.setEnabled(row.can_uninstall and self._mutations_enabled)

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        raw = item.data(0, _ID_ROLE)
        row = self._row(str(raw)) if raw else None
        if row is not None and row.selectable:
            self.use_requested.emit(row.install_id)

    def _on_use_clicked(self) -> None:
        row = self._row(self._current_id)
        if row is not None and row.selectable:
            self.use_requested.emit(row.install_id)

    def _on_enable_clicked(self) -> None:
        row = self._row(self._current_id)
        if row is not None and self._mutations_enabled:
            self.enable_toggle_requested.emit(row.install_id, not row.enabled)

    def _on_uninstall_clicked(self) -> None:
        row = self._row(self._current_id)
        if row is not None and row.can_uninstall and self._mutations_enabled:
            self.uninstall_requested.emit(row.install_id)


def _row_text(row: SkillRow) -> str:
    head = f"{row.name}   ·   {row.status_text}"
    if row.already_selected:
        head = f"{head}   ·   added"
    return f"{head}\n{row.description}" if row.description else head


def _use_tooltip(row: SkillRow) -> str:
    if row.already_selected:
        return "Already added to your next message."
    if row.selectable:
        return "Add this skill to the message you are about to send."
    if not row.valid:
        return "This skill could not be loaded, so it cannot be used."
    if not row.enabled:
        return "Enable this skill before adding it to a message."
    return "Another installed skill takes precedence, or this skill does not apply to this workspace."


def _detail_body_text(detail: SkillDetail) -> str:
    blocks: list[str] = []
    if detail.description:
        blocks.append(detail.description)
    if detail.fields:
        blocks.append("\n".join(f"{label}: {value}" for label, value in detail.fields))
    if detail.diagnostics:
        blocks.append("Diagnostics:\n" + "\n".join(f"• {line}" for line in detail.diagnostics))
    return "\n\n".join(blocks)


__all__ = ["PERMISSION_REMINDER", "SkillsManagerWindow"]
