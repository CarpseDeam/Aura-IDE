"""Tabbed code editor pane — projects committed workspace file state.

Tabs are keyed by resolved workspace path, the single document identity
shared by both user-initiated browsing (``open_file``) and the authoritative
file-edit lifecycle projection (``aura.gui.editor.file_edit_projection``).
Content only ever changes through the explicit path-keyed API below; nothing
here streams, animates, or replays a write in progress -- a committed change
is set once, immediately, and a short highlight decorates it afterward.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import (
    QAction,
    QKeySequence,
    QShortcut,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSizePolicy,
    QStackedWidget,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.focused_actions import ACTION_LABELS, build_prompt_for_action, is_edit_action
from aura.gui.cards._helpers import _mono_font
from aura.gui.editor.applied_edit_effect import AppliedEditEffect
from aura.gui.scrollbar_style import aura_scrollbar_qss
from aura.gui.syntax import PygmentsHighlighter, language_from_path
from aura.gui.theme import ACCENT, BG, BORDER, FG, FG_MUTED

logger = logging.getLogger(__name__)


class CodeEditorPane(QWidget):
    """Tabbed, read-only preview of actual workspace file content.

    Public API:
        open_file(path) -> None
        resolve_workspace_path(path) -> Path
        read_disk_text(path) -> str | None
        path_editor(path) -> QPlainTextEdit | None
        set_path_content(path, content, *, mark_execution_origin=False) -> QPlainTextEdit
        pulse_applied(editor, old_text, new_text) -> None
        close_path(path) -> None
        close_all_tabs() -> None
        close_execution_tabs() -> None
    """

    focused_action_requested = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        # Empty state page (index 0)
        self._empty_state = QWidget(self._stack)
        empty_layout = QVBoxLayout(self._empty_state)
        empty_layout.setAlignment(Qt.AlignCenter)
        empty_title = QLabel("No file open")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_title.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        empty_subtitle = QLabel(
            "Open a file from the workspace tree, or let Aura write one."
        )
        empty_subtitle.setAlignment(Qt.AlignCenter)
        empty_subtitle.setWordWrap(True)
        empty_subtitle.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 11px; background: transparent;"
        )
        empty_layout.addWidget(empty_title)
        empty_layout.addWidget(empty_subtitle)
        self._stack.addWidget(self._empty_state)  # index 0

        # Tab widget (index 1)
        self._tabs = QTabWidget(self._stack)
        self._tabs.setMinimumSize(0, 0)
        self._tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._tabs.setTabsClosable(False)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.setStyleSheet(self._tab_widget_style())
        self._stack.addWidget(self._tabs)  # index 1

        # Path identity is the single source of truth for tab identity.
        self._tabs_by_path: dict[Path, QPlainTextEdit] = {}
        self._editor_file_paths: dict[QPlainTextEdit, Path] = {}
        self._editor_highlighters: dict[QPlainTextEdit, PygmentsHighlighter] = {}
        # Paths whose tab was opened by an applied write rather than explicit
        # user browsing, so a new turn can close only those (see
        # close_execution_tabs) while leaving user-opened tabs alone.
        self._execution_origin_paths: set[Path] = set()
        self._workspace_root: Path | None = None
        self._read_only_mode = False
        self._effect = AppliedEditEffect()

        self._ask_shortcut = QShortcut(QKeySequence("Ctrl+Shift+A"), self)
        self._ask_shortcut.activated.connect(self.ask_about_current_selection)

        # Show empty state initially since there are no tabs
        self._update_empty_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_workspace_root(self, root: Path | None) -> None:
        self._workspace_root = root

    def set_read_only_mode(self, enabled: bool) -> None:
        self._read_only_mode = enabled

    def resolve_workspace_path(self, path: str | Path) -> Path:
        """Resolve *path* (absolute or workspace-relative) to an absolute Path."""
        candidate = Path(path)
        if not candidate.is_absolute() and self._workspace_root is not None:
            candidate = self._workspace_root / candidate
        return self._normalize(candidate)

    def read_disk_text(self, path: Path) -> str | None:
        """Read *path* from disk, or None if it is missing/unreadable."""
        try:
            if path.exists() and path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            logger.exception("Failed to read workspace file: %s", path)
        return None

    def path_editor(self, path: Path) -> QPlainTextEdit | None:
        """Return the open editor for *path*, or None if it has no tab."""
        return self._tabs_by_path.get(self._normalize(path))

    def open_file(self, file_path: Path) -> None:
        """Open a workspace file in a read-only preview tab."""
        path = Path(file_path)
        if not path.exists() or path.is_dir():
            return
        resolved = self._normalize(path)
        text = self.read_disk_text(resolved)
        if text is None:
            QMessageBox.warning(self, "Open File", f"Could not open {path}")
            return
        self._execution_origin_paths.discard(resolved)
        editor = self._open_or_create_tab(resolved)
        editor.setPlainText(text)
        idx = self._tabs.indexOf(editor)
        if idx >= 0:
            self._tabs.setCurrentIndex(idx)
        logger.info("Opened file for focused actions: %s", resolved)

    def set_path_content(
        self, path: Path, content: str, *, mark_execution_origin: bool = False
    ) -> QPlainTextEdit:
        """Create-or-reuse the tab for *path* and set its committed content.

        This is the one authoritative way workspace content changes outside
        of explicit user browsing. Callers must already know *content* is
        proven on disk -- this method never fetches, waits, or animates.
        """
        resolved = self._normalize(path)
        is_new_tab = resolved not in self._tabs_by_path
        editor = self._open_or_create_tab(resolved)
        editor.setPlainText(content)
        if mark_execution_origin and is_new_tab:
            self._execution_origin_paths.add(resolved)
        idx = self._tabs.indexOf(editor)
        if idx >= 0:
            self._tabs.setCurrentIndex(idx)
        return editor

    def pulse_applied(self, editor: QPlainTextEdit, old_text: str, new_text: str) -> None:
        """Trigger the short, cancellable highlight over already-correct text."""
        self._effect.pulse(editor, old_text, new_text)

    def close_path(self, path: Path) -> None:
        """Close the tab for *path*, if one is open."""
        resolved = self._normalize(path)
        editor = self._tabs_by_path.get(resolved)
        if editor is None:
            return
        idx = self._tabs.indexOf(editor)
        if idx >= 0:
            self._on_tab_close_requested(idx)

    def close_all_tabs(self) -> None:
        """Remove every tab, disconnect effects, and clear internal tracking."""
        self._effect.cancel_all()
        self._tabs_by_path.clear()
        self._execution_origin_paths.clear()
        self._editor_file_paths.clear()
        self._clear_highlighters()

        # Remove all tabs without triggering close handlers
        self._tabs.blockSignals(True)
        while self._tabs.count() > 0:
            self._tabs.removeTab(0)
        self._tabs.blockSignals(False)
        self._update_empty_state()

    def close_execution_tabs(self) -> None:
        """Remove tabs opened by applied writes; keep explicitly opened tabs."""
        for path in list(self._execution_origin_paths):
            self.close_path(path)

    def ask_about_current_selection(self) -> None:
        editor = self._current_editor()
        if editor is None:
            return
        self._run_focused_action(editor, "ask")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _normalize(self, path: Path) -> Path:
        try:
            return Path(path).resolve()
        except OSError:
            return Path(path).absolute()

    def _open_or_create_tab(self, resolved: Path) -> QPlainTextEdit:
        existing = self._tabs_by_path.get(resolved)
        if existing is not None:
            return existing
        editor = self._create_editor(resolved)
        self._editor_highlighters[editor] = PygmentsHighlighter(
            editor.document(), language_from_path(str(resolved))
        )
        idx = self._tabs.addTab(editor, resolved.name)
        self._tabs.setTabToolTip(idx, self._rel_path(resolved))
        self._install_tab_close_button(idx, editor)
        self._tabs_by_path[resolved] = editor
        self._editor_file_paths[editor] = resolved
        self._update_empty_state()
        return editor

    def _create_editor(self, file_path: Path) -> QPlainTextEdit:
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setMinimumSize(0, 0)
        editor.setFont(_mono_font(10))
        editor.setCursorWidth(2)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        editor.customContextMenuRequested.connect(
            lambda pos, e=editor: self._on_editor_context_menu(e, pos)
        )
        editor.setStyleSheet(
            f"""
            QPlainTextEdit {{
                background: {BG};
                color: {FG};
                border: none;
                padding: 8px;
            }}
            {aura_scrollbar_qss("QPlainTextEdit")}
            """
        )
        return editor

    def _install_tab_close_button(self, index: int, editor: QWidget) -> None:
        btn = QToolButton(self._tabs.tabBar())
        btn.setText("×")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setToolTip("Close tab")
        btn.setFixedSize(16, 16)
        btn.setStyleSheet(f"""
            QToolButton {{
                background: transparent;
                color: {FG};
                border: none;
                font-size: 14px;
                font-weight: 600;
                padding: 0;
                margin: 0;
            }}
            QToolButton:hover {{
                color: #f7768e;
                background: rgba(247, 118, 142, 0.15);
                border-radius: 3px;
            }}
        """)
        btn.clicked.connect(
            lambda checked, e=editor: self._on_close_button_clicked(e)
        )
        self._tabs.tabBar().setTabButton(
            index, QTabBar.ButtonPosition.RightSide, btn
        )

    def _on_close_button_clicked(self, editor: QWidget) -> None:
        idx = self._tabs.indexOf(editor)
        if idx >= 0:
            self._on_tab_close_requested(idx)

    def _current_editor(self) -> QPlainTextEdit | None:
        current = self._tabs.currentWidget()
        return current if isinstance(current, QPlainTextEdit) else None

    def _on_editor_context_menu(self, editor: QPlainTextEdit, pos) -> None:
        menu = QMenu(editor)
        has_selection = editor.textCursor().hasSelection()
        if not has_selection:
            whole_file = QAction("No selection: use whole file", menu)
            whole_file.setEnabled(False)
            menu.addAction(whole_file)
            menu.addSeparator()

        for action_key in (
            "ask",
            "explain",
            "fix",
            "refactor",
            "simplify",
            "add_logging",
            "add_type_hints",
            "write_tests",
        ):
            label = ACTION_LABELS[action_key]
            if self._read_only_mode and is_edit_action(action_key):
                label = f"{label} (suggest only)"
            action = QAction(label, menu)
            action.triggered.connect(
                lambda _checked=False, key=action_key: self._run_focused_action(editor, key)
            )
            menu.addAction(action)

        menu.addSeparator()
        default_menu = editor.createStandardContextMenu()
        for action in default_menu.actions():
            menu.addAction(action)
        menu.exec(editor.viewport().mapToGlobal(pos))

    def _run_focused_action(self, editor: QPlainTextEdit, action_key: str) -> None:
        path = self._editor_file_paths.get(editor)
        if path is None:
            QMessageBox.information(
                self,
                "Focused Action",
                "Open a workspace file before using focused actions.",
            )
            return

        cursor = editor.textCursor()
        selected_text = cursor.selectedText().replace(" ", "\n")
        start_offset: int | None = cursor.selectionStart()
        end_offset: int | None = cursor.selectionEnd()
        if not cursor.hasSelection():
            selected_text = editor.toPlainText()
            start_offset = 0
            end_offset = len(selected_text)
            QMessageBox.information(
                self,
                "Focused Action",
                "No text is selected, so Aura will use the whole current file.",
            )

        custom_question = ""
        if action_key == "ask":
            custom_question, ok = QInputDialog.getText(
                self,
                "Ask Aura About Selection",
                "What do you want Aura to know or do?",
            )
            if not ok or not custom_question.strip():
                return
            custom_question = custom_question.strip()

        try:
            prompt = build_prompt_for_action(
                action_key=action_key,
                relative_path=self._rel_path(path),
                full_file_text=editor.toPlainText(),
                selected_text=selected_text,
                selection_start_offset=start_offset,
                selection_end_offset=end_offset,
                custom_question=custom_question,
                read_only_mode=self._read_only_mode,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Focused Action", str(exc))
            logger.exception("Failed to build focused action prompt for %s", path)
            return

        logger.info(
            "Created focused action prompt action=%s path=%s selected_chars=%s",
            action_key,
            path,
            len(selected_text),
        )
        self.focused_action_requested.emit(prompt)

        # Restore the same cursor so the highlighted region remains visible.
        if start_offset is not None and end_offset is not None:
            keep = QTextCursor(editor.document())
            keep.setPosition(start_offset)
            keep.setPosition(end_offset, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(keep)

    def _on_tab_close_requested(self, index: int) -> None:
        """Handle a tab being closed, by the user or by the projection."""
        closed_widget = self._tabs.widget(index)
        self._tabs.removeTab(index)

        if isinstance(closed_widget, QPlainTextEdit):
            self._effect.cancel(closed_widget)
            path = self._editor_file_paths.pop(closed_widget, None)
            if path is not None:
                self._tabs_by_path.pop(path, None)
                self._execution_origin_paths.discard(path)
            self._detach_highlighter(closed_widget)

        self._update_empty_state()

    def _detach_highlighter(self, editor: QPlainTextEdit) -> None:
        highlighter = self._editor_highlighters.pop(editor, None)
        if highlighter is not None:
            highlighter.setDocument(None)
            highlighter.deleteLater()

    def _clear_highlighters(self) -> None:
        for highlighter in self._editor_highlighters.values():
            highlighter.setDocument(None)
            highlighter.deleteLater()
        self._editor_highlighters.clear()

    def _rel_path(self, path: Path) -> str:
        if self._workspace_root is None:
            return str(path)
        try:
            return path.resolve().relative_to(self._workspace_root.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _update_empty_state(self) -> None:
        """Show empty-state page when no tabs exist, tabs page otherwise."""
        has_tabs = self._tabs.count() > 0
        self._stack.setCurrentIndex(1 if has_tabs else 0)

    # ------------------------------------------------------------------
    # Styling
    # ------------------------------------------------------------------

    @staticmethod
    def _tab_widget_style() -> str:
        """Return a dark, minimal QTabWidget stylesheet consistent with Aura."""
        return f"""
            QTabWidget::pane {{
                background: {BG};
                border: none;
                border-top: 1px solid {BORDER};
            }}
            QTabBar::tab {{
                background: {BG};
                color: {FG};
                border: 1px solid transparent;
                border-bottom: 1px solid {BORDER};
                padding: 6px 14px;
                margin-right: 2px;
                font-size: 12px;
            }}
            QTabBar::tab:hover {{
                background: #1e1e26;
                border-color: {BORDER};
            }}
            QTabBar::tab:selected {{
                background: #1c1c24;
                border: 1px solid {BORDER};
                border-bottom: 2px solid {ACCENT};
                color: {FG};
                font-weight: 600;
            }}
        """
