"""Tabbed code editor pane with syntax highlighting and character-by-character
typing animation for streaming file content from the worker.

Each tab represents a file being written/edited by the worker.  Content is
revealed progressively via a QTimer-driven typing effect, and tabs are
automatically closed when the worker finishes.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _mono_font
from aura.gui.syntax import PygmentsHighlighter, language_from_path
from aura.gui.theme import ACCENT, BG, BORDER, FG


class CodeEditorPane(QWidget):
    """Tabbed code editor with streaming typewriter animation.

    Public API:
        open_or_focus_tab(tool_id, file_path) -> None
        stream_content(tool_id, content) -> None
        finalize_tab(tool_id) -> None
        close_all_tabs() -> None
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget(self)
        self._tabs.setTabsClosable(True)
        self._tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self._tabs.setStyleSheet(self._tab_widget_style())
        layout.addWidget(self._tabs)

        # Internal tracking
        self._editors: dict[str, QPlainTextEdit] = {}
        self._typing_state: dict[str, dict] = {}
        # Map tab index -> tool_id so we can clean up on close
        self._tab_index_to_tool_id: dict[int, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_or_focus_tab(self, tool_id: str, file_path: str) -> None:
        """Create a new tab for *file_path* or focus an existing one.

        Args:
            tool_id: Unique identifier for this tool call (worker_tool_id).
            file_path: Absolute or relative path to the file being edited.
        """
        # If a tab for this tool_id already exists, just focus it
        if tool_id in self._editors:
            idx = self._tabs.indexOf(self._editors[tool_id])
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
            return

        basename = Path(file_path).name
        language = language_from_path(file_path)

        editor = QPlainTextEdit(self)
        editor.setReadOnly(True)
        editor.setFont(_mono_font(10))
        editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.setStyleSheet(
            f"background: {BG}; color: {FG}; border: none; padding: 8px;"
        )

        # Attach syntax highlighter
        PygmentsHighlighter(editor.document(), language)

        idx = self._tabs.addTab(editor, f"{basename} ●")
        self._tabs.setCurrentIndex(idx)

        self._editors[tool_id] = editor
        self._tab_index_to_tool_id[idx] = tool_id

        # Initialise typing state
        self._typing_state[tool_id] = {
            "timer": QTimer(self),
            "target": "",
            "position": 0,
            "language": language,
            "path": file_path,
            "basename": basename,
        }
        timer: QTimer = self._typing_state[tool_id]["timer"]
        timer.timeout.connect(lambda tid=tool_id: self._on_typing_tick(tid))
        timer.setInterval(33)  # ~30 fps

    def stream_content(self, tool_id: str, content: str) -> None:
        """Update the target content for the typing animation.

        If the typing timer is not yet running, it will be started.  The
        animation progressively reveals characters from the current position
        toward the new target.

        Args:
            tool_id: The worker_tool_id previously passed to open_or_focus_tab.
            content: The latest full content of the file.
        """
        state = self._typing_state.get(tool_id)
        if state is None:
            return
        state["target"] = content
        timer: QTimer = state["timer"]
        if not timer.isActive():
            timer.start()

    def finalize_tab(self, tool_id: str) -> None:
        """Flush remaining characters immediately and mark the tab as done.

        Args:
            tool_id: The worker_tool_id previously passed to open_or_focus_tab.
        """
        state = self._typing_state.get(tool_id)
        if state is None:
            return

        timer: QTimer = state["timer"]
        timer.stop()

        editor = self._editors.get(tool_id)
        if editor is not None:
            # Flush all remaining content
            target = state["target"]
            editor.setPlainText(target)
            # Auto-scroll to bottom
            sb = editor.verticalScrollBar()
            sb.setValue(sb.maximum())

        # Update tab label
        idx = self._tabs.indexOf(editor) if editor is not None else -1
        if idx >= 0:
            basename = state["basename"]
            self._tabs.setTabText(idx, f"{basename} ✓")

    def close_all_tabs(self) -> None:
        """Remove every tab, disconnect timers, and clear internal tracking."""
        # Stop all typing timers
        for state in self._typing_state.values():
            timer: QTimer = state["timer"]
            timer.stop()
            timer.deleteLater()

        self._typing_state.clear()
        self._editors.clear()
        self._tab_index_to_tool_id.clear()

        # Remove all tabs without triggering close handlers
        self._tabs.blockSignals(True)
        while self._tabs.count() > 0:
            self._tabs.removeTab(0)
        self._tabs.blockSignals(False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_typing_tick(self, tool_id: str) -> None:
        """Reveal ~5 more characters of the target content."""
        state = self._typing_state.get(tool_id)
        if state is None:
            return

        editor = self._editors.get(tool_id)
        if editor is None:
            return

        target = state["target"]
        pos = state["position"]

        if pos >= len(target):
            state["timer"].stop()
            return

        pos += 5
        state["position"] = pos
        editor.setPlainText(target[:pos])

        # Auto-scroll to bottom
        sb = editor.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_tab_close_requested(self, index: int) -> None:
        """Handle user clicking the close button on a tab."""
        tool_id = self._tab_index_to_tool_id.pop(index, None)
        if tool_id is not None:
            state = self._typing_state.pop(tool_id, None)
            if state is not None:
                timer: QTimer = state["timer"]
                timer.stop()
                timer.deleteLater()
            self._editors.pop(tool_id, None)

        self._tabs.removeTab(index)

        # Rebuild the index -> tool_id mapping since indices shifted
        self._tab_index_to_tool_id.clear()
        for tid, editor in self._editors.items():
            idx = self._tabs.indexOf(editor)
            if idx >= 0:
                self._tab_index_to_tool_id[idx] = tid

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
            QTabBar::close-button {{
                image: none;
                background: transparent;
                border: none;
                padding: 0;
                margin: 0 0 0 6px;
            }}
            QTabBar::close-button:hover {{
                background: rgba(247, 118, 142, 0.20);
                border-radius: 3px;
            }}
        """
