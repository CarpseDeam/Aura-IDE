"""Info hub pane: Worker Log tab with TODO list, reasoning, and diff/error cards."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from aura.gui.cards._helpers import _mono_font
from aura.gui.cards.diff_card import DiffCard
from aura.gui.cards.error_card import ErrorCard
from aura.gui.theme import ACCENT, BG, BORDER, FG, FG_MUTED
from aura.gui.widgets.todo_list import TodoListWidget


class InfoHubPane(QWidget):
    """Bottom pane with permanent Worker Log tab.

    Public API:
        append_reasoning(text) -> None
        append_content(text) -> None
        update_todo_list(tasks) -> None
        add_diff_card(rel_path, old, new, decision, is_new_file) -> None
        add_error(message) -> None
        show_final_summary(ok, summary) -> None
        clear() -> None
    """

    _LOG_REVEAL_CHARS_PER_TICK = 16

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget(self)
        self._tabs.setMinimumSize(0, 0)
        self._tabs.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._tabs.setStyleSheet(self._tab_widget_style())
        # No corner widget; terminal output lives in the floating TerminalWindow.
        layout.addWidget(self._tabs)

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
        self._log_view.setMinimumSize(0, 0)
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

        # Typewriter state for the log
        self._log_buffer = ""
        self._log_visible = ""
        self._log_timer = QTimer(self)
        self._log_timer.timeout.connect(self._on_log_tick)
        self._log_timer.setInterval(20)  # reveal N chars per tick (see _LOG_REVEAL_CHARS_PER_TICK)

    # Public API — Worker Log

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

    def show_final_summary(self, ok: bool, summary: str, needs_followup: bool = False, status: str | None = None) -> None:
        """Append a formatted summary block to the Worker Log text.

        Flushes the typewriter immediately so the summary is visible at once.
        """
        # Flush any pending typewriter content
        self._flush_log()

        if status is not None:
            from aura.conversation.dispatch import WorkerOutcomeStatus
            status_labels = {
                WorkerOutcomeStatus.completed.value: "✅ Worker completed successfully.",
                WorkerOutcomeStatus.completed_with_caveats.value: "✅ Worker completed with caveats.",
                WorkerOutcomeStatus.needs_followup.value: "⚠️ Worker needs follow-up.",
                WorkerOutcomeStatus.validation_failed.value: "❌ Worker validation failed.",
                WorkerOutcomeStatus.edit_mechanics_blocked.value: "⚠️ Worker edit mechanics blocked.",
                WorkerOutcomeStatus.craft_blocked.value: "❌ Worker craft blocked.",
                WorkerOutcomeStatus.craft_rejected.value: "❌ Worker craft rejected.",
                WorkerOutcomeStatus.scope_mismatch.value: "⚠️ Worker scope mismatch.",
                WorkerOutcomeStatus.approval_rejected.value: "❌ Worker approval rejected.",
                WorkerOutcomeStatus.cancelled.value: "🔶 Worker cancelled.",
                WorkerOutcomeStatus.harness_error.value: "❌ Worker harness error.",
            }
            prefix = status_labels.get(status, "❓ Unknown status.")
        elif ok:
            prefix = "✅ Worker completed successfully."
        elif needs_followup:
            prefix = "⚠️ Worker needs follow-up."
        else:
            prefix = "Harness error."
        block = f"\n\n{'─' * 40}\n{prefix}\n{summary}\n{'─' * 40}\n"
        self._log_view.insertPlainText(block)

        # Auto-scroll to bottom
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

        # Add Copy Summary button
        btn = QPushButton("📋 Copy Summary", self._log_tab)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFlat(True)
        btn.setStyleSheet(f"""
            QPushButton {{
                color: {FG_MUTED};
                font-size: 11px;
                border: none;
                padding: 2px 0;
                text-decoration: none;
            }}
            QPushButton:hover {{
                color: {ACCENT};
            }}
        """)
        receipt_text = f"{'═' * 46}\n{prefix}\n{summary}\n{'═' * 46}"
        btn.clicked.connect(lambda checked, b=btn, r=receipt_text: self._on_copy_summary(b, r))
        self._cards_layout.addWidget(btn)


    def _on_copy_summary(self, btn: QPushButton, receipt_text: str) -> None:
        """Copy summary to clipboard and briefly show 'Copied!'."""
        QGuiApplication.clipboard().setText(receipt_text)
        btn.setText("Copied!")
        QTimer.singleShot(1500, lambda: btn.setText("📋 Copy Summary"))

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


    def _on_log_tick(self) -> None:
        """Reveal more characters of the log buffer."""
        if len(self._log_visible) >= len(self._log_buffer):
            self._log_timer.stop()
            return
            
        chunk_size = self._LOG_REVEAL_CHARS_PER_TICK
        next_chunk = self._log_buffer[len(self._log_visible):len(self._log_visible) + chunk_size]
        self._log_visible += next_chunk
        
        cursor = self._log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._log_view.setTextCursor(cursor)
        self._log_view.insertPlainText(next_chunk)

        # Auto-scroll to bottom
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _flush_log(self) -> None:
        """Immediately reveal all buffered log text."""
        self._log_timer.stop()
        if len(self._log_visible) < len(self._log_buffer):
            remaining = self._log_buffer[len(self._log_visible):]
            self._log_visible = self._log_buffer
            
            cursor = self._log_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self._log_view.setTextCursor(cursor)
            self._log_view.insertPlainText(remaining)
            
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    # Styling

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
