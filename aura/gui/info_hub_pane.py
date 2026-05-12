"""Info hub pane — permanent Worker Log tab plus dynamic terminal tabs.

The Worker Log tab contains a TODO list, a typewriter-style reasoning/content
stream, and a dynamic area for diff/error cards.  Terminal tabs are created
on demand for ``run_terminal_command`` tool calls and auto-closed when the
worker finishes.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.gui.aura_widget import TodoListWidget
from aura.gui.cards._helpers import _mono_font
from aura.gui.cards.diff_card import DiffCard
from aura.gui.cards.error_card import ErrorCard
from aura.gui.cards.terminal_card import TerminalCard
from aura.gui.theme import ACCENT, BG, BORDER, FG


class InfoHubPane(QWidget):
    """Bottom pane with permanent Worker Log and dynamic terminal tabs.

    Public API:
        append_reasoning(text) -> None
        append_content(text) -> None
        update_todo_list(tasks) -> None
        open_terminal_tab(tool_id, command) -> None
        append_terminal_output(tool_id, text) -> None
        finalize_terminal(tool_id, exit_code) -> None
        close_all_terminal_tabs() -> None
        add_diff_card(rel_path, old, new, decision, is_new_file) -> None
        add_error(message) -> None
        show_final_summary(ok, summary) -> None
        clear() -> None
    """

    WORKER_LOG_INDEX = 0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget(self)
        self._tabs.setStyleSheet(self._tab_widget_style())
        layout.addWidget(self._tabs)

        # Corner widget: Close All Terminals button
        self._close_all_btn = QToolButton(self)
        self._close_all_btn.setText("Close Terminals")
        self._close_all_btn.setObjectName("closeTerminalsBtn")
        self._close_all_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_all_btn.clicked.connect(self.close_all_terminal_tabs)
        self._tabs.setCornerWidget(self._close_all_btn, Qt.TopRightCorner)
        self._close_all_btn.setVisible(False)

        # ---- Worker Log tab (permanent, index 0) ----
        self._log_tab = QWidget(self)
        log_layout = QVBoxLayout(self._log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        # TODO list widget
        self._todo_widget = TodoListWidget(self._log_tab)
        log_layout.addWidget(self._todo_widget)

        # Typewriter log text area
        self._log_view = QPlainTextEdit(self._log_tab)
        self._log_view.setReadOnly(True)
        self._log_view.setFont(_mono_font(10))
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._log_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._log_view.setStyleSheet(
            f"background: transparent; color: {FG}; border: none; padding: 8px;"
        )
        log_layout.addWidget(self._log_view, 1)

        # Dynamic cards area (diff cards, error cards)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setContentsMargins(8, 0, 8, 8)
        self._cards_layout.setSpacing(6)
        log_layout.addLayout(self._cards_layout)

        self._tabs.addTab(self._log_tab, "Worker Log")

        # ---- Internal state ----
        self._terminal_tabs: dict[str, TerminalCard] = {}
        self._tab_index_to_tool_id: dict[int, str] = {}

        # Typewriter state for the log
        self._log_buffer = ""
        self._log_visible = ""
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._on_log_tick)
        self._log_timer.setInterval(20)  # 2 chars per tick

    # ------------------------------------------------------------------
    # Public API — Worker Log
    # ------------------------------------------------------------------

    def append_reasoning(self, text: str) -> None:
        """Append text to the Worker Log buffer with typewriter effect."""
        self._log_buffer += text
        if not self._log_timer.isActive():
            self._log_timer.start()

    def append_content(self, text: str) -> None:
        """Append text to the Worker Log buffer with typewriter effect."""
        self._log_buffer += text
        if not self._log_timer.isActive():
            self._log_timer.start()

    def update_todo_list(self, tasks: list[dict]) -> None:
        """Delegate to the embedded TodoListWidget."""
        self._todo_widget.update_tasks(tasks)

    def add_diff_card(
        self,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        """Create a DiffCard and add it to the Worker Log's dynamic cards area."""
        card = DiffCard(rel_path, old, new, decision, is_new_file, parent=self._log_tab)
        self._cards_layout.addWidget(card)

    def add_error(self, message: str) -> None:
        """Create an ErrorCard and add it to the Worker Log's dynamic cards area."""
        card = ErrorCard("Worker Error", message, parent=self._log_tab)
        self._cards_layout.addWidget(card)

    def show_final_summary(self, ok: bool, summary: str) -> None:
        """Append a formatted summary block to the Worker Log text.

        Flushes the typewriter immediately so the summary is visible at once.
        """
        # Flush any pending typewriter content
        self._flush_log()

        prefix = "✅ Worker completed successfully." if ok else "⚠️ Worker failed."
        block = f"\n\n{'─' * 40}\n{prefix}\n{summary}\n{'─' * 40}\n"
        self._log_view.insertPlainText(block)

        # Auto-scroll to bottom
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear(self) -> None:
        """Reset the Worker Log: clear text, todo, and dynamic cards."""
        self._log_timer.stop()
        self._log_buffer = ""
        self._log_visible = ""
        self._log_view.setPlainText("")

        self._todo_widget.update_tasks([])

        # Remove all dynamic cards
        while self._cards_layout.count() > 0:
            item = self._cards_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    # ------------------------------------------------------------------
    # Public API — Terminal tabs
    # ------------------------------------------------------------------

    def open_terminal_tab(self, tool_id: str, command: str) -> None:
        """Create a new terminal tab with a TerminalCard and focus it.

        Args:
            tool_id: Unique worker_tool_id for this terminal session.
            command: The shell command being executed.
        """
        if tool_id in self._terminal_tabs:
            # Already exists — focus it
            card = self._terminal_tabs[tool_id]
            idx = self._tabs.indexOf(card)
            if idx >= 0:
                self._tabs.setCurrentIndex(idx)
            return

        label = command if command and command != "..." else "Terminal"
        card = TerminalCard(command=command, parent=self)
        idx = self._tabs.addTab(card, label)
        self._tabs.setCurrentIndex(idx)

        self._terminal_tabs[tool_id] = card
        self._tab_index_to_tool_id[idx] = tool_id
        self._close_all_btn.setVisible(True)

    def append_terminal_output(self, tool_id: str, text: str) -> None:
        """Route output text to the terminal tab for *tool_id*.

        If no tab exists yet, one is created with a placeholder command.
        """
        if tool_id not in self._terminal_tabs:
            self.open_terminal_tab(tool_id, "...")
        self._terminal_tabs[tool_id].append_output(text)

    def finalize_terminal(self, tool_id: str, exit_code: int) -> None:
        """Mark the terminal tab as done/failed based on exit_code."""
        card = self._terminal_tabs.get(tool_id)
        if card is not None:
            card.set_result(exit_code)

    def close_all_terminal_tabs(self) -> None:
        """Remove all terminal tabs, keeping only the Worker Log tab."""
        # Collect indices to remove (all except index 0)
        indices_to_remove = []
        for idx in range(self._tabs.count()):
            if idx != self.WORKER_LOG_INDEX:
                indices_to_remove.append(idx)

        # Remove in reverse order to keep indices stable
        for idx in reversed(indices_to_remove):
            tool_id = self._tab_index_to_tool_id.pop(idx, None)
            if tool_id is not None:
                self._terminal_tabs.pop(tool_id, None)
            self._tabs.removeTab(idx)

        # Rebuild index mapping
        self._tab_index_to_tool_id.clear()
        for tid, card in self._terminal_tabs.items():
            idx = self._tabs.indexOf(card)
            if idx >= 0:
                self._tab_index_to_tool_id[idx] = tid
        
        self._close_all_btn.setVisible(False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _on_log_tick(self) -> None:
        """Reveal 2 more characters of the log buffer."""
        if len(self._log_visible) >= len(self._log_buffer):
            self._log_timer.stop()
            return
        self._log_visible = self._log_buffer[:len(self._log_visible) + 2]
        self._log_view.setPlainText(self._log_visible)

        # Auto-scroll to bottom
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _flush_log(self) -> None:
        """Immediately reveal all buffered log text."""
        self._log_timer.stop()
        self._log_visible = self._log_buffer
        self._log_view.setPlainText(self._log_visible)
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

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
