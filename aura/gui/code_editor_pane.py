"""Tabbed code editor pane with syntax highlighting and worker edit animation.

Each tab represents a file being written/edited by the worker.  Content is
revealed progressively via QTimer-driven typing, and full-file edits animate
as line-aware delete/retype transitions.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
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
from aura.gui.editor.diff_overlay import DiffOverlay
from aura.gui.editor.edit_animation import EditAnimation
from aura.gui.smooth_code_streamer import SmoothCodeStreamer
from aura.gui.syntax import PygmentsHighlighter, language_from_path
from aura.gui.theme import ACCENT, BG, BORDER, FG, FG_MUTED

logger = logging.getLogger(__name__)

STREAM_LARGE_FILE_THRESHOLD = 50_000

class CodeEditorPane(QWidget):
    """Tabbed code editor with streaming typewriter animation.

    Public API:
        open_or_focus_tab(tool_id, file_path) -> None
        stream_content(tool_id, content) -> None
        finalize_tab(tool_id) -> None
        close_all_tabs() -> None
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

        # Internal tracking
        self._editors: dict[str, QPlainTextEdit] = {}
        self._typing_state: dict[str, dict] = {}
        self._tool_aliases: dict[str, str] = {}
        self._worker_tabs_by_path: dict[str, str] = {}
        # Map tab index -> tool_id so we can clean up on close
        self._tab_index_to_tool_id: dict[int, str] = {}
        self._file_tabs: dict[Path, QPlainTextEdit] = {}
        self._editor_file_paths: dict[QPlainTextEdit, Path] = {}
        self._editor_highlighters: dict[QPlainTextEdit, PygmentsHighlighter] = {}
        self._workspace_root: Path | None = None
        self._read_only_mode = False

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

    def open_file(self, file_path: Path) -> None:
        """Open a workspace file in a readonly selectable editor tab."""
        path = Path(file_path)
        if not path.exists() or path.is_dir():
            return
        resolved = path.resolve()
        if resolved in self._file_tabs:
            idx = self._tabs.indexOf(self._file_tabs[resolved])
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
            return

        try:
            text = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            QMessageBox.warning(self, "Open File", f"Could not open {path}:\n{exc}")
            logger.exception("Failed to open file for focused actions: %s", path)
            return

        editor = self._create_editor(resolved)
        editor.setPlainText(text)
        self._editor_highlighters[editor] = PygmentsHighlighter(
            editor.document(), language_from_path(str(resolved))
        )

        idx = self._tabs.addTab(editor, resolved.name)
        self._tabs.setTabToolTip(idx, self._rel_path(resolved))
        self._tabs.setCurrentIndex(idx)
        self._install_tab_close_button(idx, editor)

        self._file_tabs[resolved] = editor
        self._editor_file_paths[editor] = resolved
        logger.info("Opened file for focused actions: %s", resolved)
        self._update_empty_state()

    def open_or_focus_tab(self, tool_id: str, file_path: str) -> None:
        """Create a new tab for *file_path* or focus an existing one.

        Args:
            tool_id: Unique identifier for this tool call (worker_tool_id).
            file_path: Absolute or relative path to the file being edited.
        """
        # If a tab for this tool_id already exists, just focus it
        canonical_tool_id = self._canonical_tool_id(tool_id)
        if canonical_tool_id in self._editors:
            idx = self._tabs.indexOf(self._editors[canonical_tool_id])
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
            return

        path_key = self._worker_path_key(file_path)
        basename = Path(file_path).name
        language = language_from_path(file_path)
        existing_tool_id = self._worker_tabs_by_path.get(path_key)
        existing_editor = (
            self._editors.get(existing_tool_id) if existing_tool_id is not None else None
        )
        if existing_tool_id is not None and existing_editor is not None:
            self._tool_aliases[tool_id] = existing_tool_id
            self._editors[tool_id] = existing_editor
            state = self._typing_state.get(existing_tool_id)
            if state is not None:
                state["active_count"] = state.get("active_count", 0) + 1
                state["target"] = ""
                state["position"] = 0
                state["timer"].stop()
                state["streamer"].set_text_immediately("")
            else:
                existing_editor.clear()
            idx = self._tabs.indexOf(existing_editor)
            if idx >= 0:
                self._tabs.setTabText(idx, f"{basename} ●")
                self._tabs.setCurrentIndex(idx)
            return

        editor = self._create_editor(Path(file_path))

        # Attach syntax highlighter
        self._editor_highlighters[editor] = PygmentsHighlighter(editor.document(), language)

        idx = self._tabs.addTab(editor, f"{basename} ●")
        self._tabs.setCurrentIndex(idx)
        self._install_tab_close_button(idx, editor)

        self._editors[tool_id] = editor
        self._tool_aliases[tool_id] = tool_id
        self._worker_tabs_by_path[path_key] = tool_id
        self._tab_index_to_tool_id[idx] = tool_id

        streamer = SmoothCodeStreamer(editor, self)

        # Initialise typing state
        self._typing_state[tool_id] = {
            "timer": QTimer(self),
            "streamer": streamer,
            "target": "",
            "position": 0,
            "language": language,
            "path": file_path,
            "path_key": path_key,
            "basename": basename,
            "tool_id": tool_id,
            "active_count": 1,
            "animation_phase": "type",
            "animation_prefix": "",
            "animation_suffix": "",
            "animation_old_middle": "",
            "animation_new_middle": "",
            "animation_char_index": 0,
            "animation_hold_ticks": 0,
            "animation_old_lines": [],
            "animation_delete_line_count": 0,
            "animation_change_start": 0,
            "animation_change_line": 1,
            "animation_old_start": 0,
            "animation_old_end": 0,
            "pending_done": False,
        }
        timer: QTimer = self._typing_state[tool_id]["timer"]
        timer.timeout.connect(lambda tid=tool_id: self._on_typing_tick(tid))
        timer.setInterval(EditAnimation.ANIM_TICK_MS)
        streamer.finished.connect(
            lambda tid=tool_id: self._on_streamer_finished(tid)
        )

        self._update_empty_state()

    def set_content(self, tool_id: str, content: str) -> None:
        """Immediately show full content for an existing worker tab."""
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        editor = self._editors.get(canonical_tool_id)
        if state is None or editor is None:
            return
        state["timer"].stop()
        state["streamer"].stop()
        state["target"] = content
        state["position"] = len(content)
        state["animation_phase"] = ""
        state["animation_new_middle"] = ""
        state["streamer"].set_text_immediately(content)

    def animate_content_transition(
        self, tool_id: str, old_content: str, new_content: str
    ) -> None:
        """Animate a full-file transition for an existing worker tab."""
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        editor = self._editors.get(canonical_tool_id)
        if state is None or editor is None:
            return

        timer: QTimer = state["timer"]
        timer.stop()
        streamer: SmoothCodeStreamer = state["streamer"]
        streamer.stop()
        state["target"] = new_content

        # Large file bypass: skip animation, set editor content immediately
        if len(new_content) > STREAM_LARGE_FILE_THRESHOLD:
            self.set_content(canonical_tool_id, new_content)
            return

        if old_content == new_content:
            self.set_content(canonical_tool_id, new_content)
            return

        if not old_content:
            streamer.set_text_immediately("")
            state["position"] = 0
            state["animation_phase"] = "type"
            state["target"] = new_content
            streamer.set_target(new_content)
            return

        if not EditAnimation.should_animate(old_content, new_content):
            self.set_content(canonical_tool_id, new_content)
            return

        old_start, old_end, new_start, new_end = EditAnimation.compute_animation_region(
            old_content, new_content
        )
        old_mid = old_content[old_start:old_end]
        new_mid = new_content[new_start:new_end]

        state["animation_prefix"] = new_content[:new_start]
        state["animation_suffix"] = new_content[new_end:]
        state["animation_old_middle"] = old_mid
        state["animation_new_middle"] = new_mid
        state["animation_char_index"] = 0
        state["animation_hold_ticks"] = (
            EditAnimation.DELETE_HOLD_TICKS if old_mid else EditAnimation.TYPE_HOLD_TICKS
        )
        old_lines = old_mid.splitlines(keepends=True)
        if old_mid and not old_lines:
            old_lines = [old_mid]
        state["animation_old_lines"] = old_lines
        state["animation_delete_line_count"] = len(old_lines)
        state["animation_change_start"] = len(state["animation_prefix"])
        state["animation_change_line"] = EditAnimation.line_number_at(new_content, new_start)
        state["animation_old_start"] = old_start
        state["animation_old_end"] = old_end
        state["position"] = 0
        state["animation_phase"] = "replace_hold"

        streamer.set_text_immediately(old_content)
        EditAnimation.focus_editor_position(editor, old_start)
        DiffOverlay.mark_deleted(editor, old_start, old_end)
        self._set_tab_status(canonical_tool_id, f":{state['animation_change_line']} - editing")
        timer.start()

    def stream_content(self, tool_id: str, content: str) -> None:
        """Update the target content for the typing animation.

        If the typing timer is not yet running, it will be started.  The
        animation progressively reveals characters from the current position
        toward the new target.

        Args:
            tool_id: The worker_tool_id previously passed to open_or_focus_tab.
            content: The latest full content of the file.
        """
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        if state is None:
            return
        if len(content) > STREAM_LARGE_FILE_THRESHOLD:
            self.set_content(canonical_tool_id, content)
            return
        state["target"] = content
        state["animation_phase"] = "type"
        state["animation_new_middle"] = ""
        state["streamer"].set_target(content)

    def finalize_tab(self, tool_id: str) -> None:
        """Flush remaining characters immediately and mark the tab as done.

        Args:
            tool_id: The worker_tool_id previously passed to open_or_focus_tab.
        """
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        if state is None:
            return

        timer: QTimer = state["timer"]
        streamer: SmoothCodeStreamer = state["streamer"]

        state["active_count"] = max(0, state.get("active_count", 1) - 1)
        self._tool_aliases.pop(tool_id, None)
        if tool_id != canonical_tool_id:
            self._editors.pop(tool_id, None)

        if state["active_count"] == 0:
            if timer.isActive() or streamer.is_active():
                state["pending_done"] = True
                if streamer.is_active():
                    streamer.finish()
            else:
                self._set_tab_status(canonical_tool_id, " ✓")

    def close_all_tabs(self) -> None:
        """Remove every tab, disconnect timers, and clear internal tracking."""
        # Stop all typing timers
        for state in self._typing_state.values():
            timer: QTimer = state["timer"]
            timer.stop()
            timer.deleteLater()
            state["streamer"].stop()
            state["streamer"].deleteLater()

        self._typing_state.clear()
        self._editors.clear()
        self._tool_aliases.clear()
        self._worker_tabs_by_path.clear()
        self._tab_index_to_tool_id.clear()
        self._file_tabs.clear()
        self._editor_file_paths.clear()
        self._clear_highlighters()

        # Remove all tabs without triggering close handlers
        self._tabs.blockSignals(True)
        while self._tabs.count() > 0:
            self._tabs.removeTab(0)
        self._tabs.blockSignals(False)
        self._update_empty_state()

    def close_worker_tabs(self) -> None:
        """Remove streaming worker tabs while preserving user-opened file tabs."""
        for state in self._typing_state.values():
            timer: QTimer = state["timer"]
            timer.stop()
            timer.deleteLater()
            state["streamer"].stop()
            state["streamer"].deleteLater()
        worker_editors = list(dict.fromkeys(self._editors.values()))
        self._typing_state.clear()
        self._editors.clear()
        self._tool_aliases.clear()
        self._worker_tabs_by_path.clear()
        self._tab_index_to_tool_id.clear()

        self._tabs.blockSignals(True)
        for editor in worker_editors:
            idx = self._tabs.indexOf(editor)
            if idx >= 0:
                self._tabs.removeTab(idx)
            self._editor_file_paths.pop(editor, None)
            self._detach_highlighter(editor)
        self._tabs.blockSignals(False)
        self._update_empty_state()

    def ask_about_current_selection(self) -> None:
        editor = self._current_editor()
        if editor is None:
            return
        self._run_focused_action(editor, "ask")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_editor(self, file_path: Path) -> QPlainTextEdit:
        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setMinimumSize(0, 0)
        editor.setFont(_mono_font(10))
        editor.setCursorWidth(2)
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        editor.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        editor.customContextMenuRequested.connect(
            lambda pos, e=editor: self._on_editor_context_menu(e, pos)
        )
        editor.setStyleSheet(
            f"background: {BG}; color: {FG}; border: none; padding: 8px;"
        )
        self._editor_file_paths[editor] = Path(file_path)
        return editor

    def _install_tab_close_button(self, index: int, editor: QWidget) -> None:
        btn = QToolButton(self._tabs.tabBar())
        btn.setText("\u00d7")
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

    def _canonical_tool_id(self, tool_id: str) -> str:
        return self._tool_aliases.get(tool_id, tool_id)

    def _worker_path_key(self, file_path: str) -> str:
        return Path(file_path).as_posix()

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
        selected_text = cursor.selectedText().replace("\u2029", "\n")
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

    def _on_typing_tick(self, tool_id: str) -> None:
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        editor = self._editors.get(canonical_tool_id)
        if state is None or editor is None:
            return
        EditAnimation.tick(state, editor, set_status=self._set_tab_status)

    def _on_streamer_finished(self, tool_id: str) -> None:
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        editor = self._editors.get(canonical_tool_id)
        if state is None or editor is None:
            return
        EditAnimation.on_streamer_finished(
            state, editor, set_status=self._set_tab_status
        )

    def _set_tab_status(self, tool_id: str, suffix: str) -> None:
        canonical_tool_id = self._canonical_tool_id(tool_id)
        state = self._typing_state.get(canonical_tool_id)
        editor = self._editors.get(canonical_tool_id)
        if state is None or editor is None:
            return
        idx = self._tabs.indexOf(editor)
        if idx >= 0:
            self._tabs.setTabText(idx, f"{state['basename']}{suffix}")

    def _on_tab_close_requested(self, index: int) -> None:
        """Handle user clicking the close button on a tab."""
        closed_widget = self._tabs.widget(index)
        tool_id = self._tab_index_to_tool_id.pop(index, None)
        if tool_id is not None:
            state = self._typing_state.pop(tool_id, None)
            if state is not None:
                timer: QTimer = state["timer"]
                timer.stop()
                timer.deleteLater()
                state["streamer"].stop()
                state["streamer"].deleteLater()
                self._worker_tabs_by_path.pop(state.get("path_key", ""), None)
            aliases = [
                tid for tid, canonical in self._tool_aliases.items()
                if canonical == tool_id
            ]
            for alias in aliases:
                self._tool_aliases.pop(alias, None)
                self._editors.pop(alias, None)
            self._editors.pop(tool_id, None)

        self._tabs.removeTab(index)

        if isinstance(closed_widget, QPlainTextEdit):
            self._editor_file_paths.pop(closed_widget, None)
            self._detach_highlighter(closed_widget)

        # Rebuild the index -> tool_id mapping since indices shifted
        self._tab_index_to_tool_id.clear()
        for tid, editor in self._editors.items():
            if self._canonical_tool_id(tid) != tid:
                continue
            idx = self._tabs.indexOf(editor)
            if idx >= 0:
                self._tab_index_to_tool_id[idx] = tid

        stale_paths = [
            path for path, editor in self._file_tabs.items()
            if self._tabs.indexOf(editor) < 0
        ]
        for path in stale_paths:
            editor = self._file_tabs.pop(path)
            self._editor_file_paths.pop(editor, None)
            self._detach_highlighter(editor)

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

    @staticmethod
    def _compute_animation_region(old: str, new: str) -> tuple[int, int, int, int]:
        return EditAnimation.compute_animation_region(old, new)

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
