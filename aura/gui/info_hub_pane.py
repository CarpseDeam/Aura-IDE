"""Info hub pane: Worker Log tab with TODO list, reasoning, and diff/error cards."""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import media_path
from aura.gui.cards._helpers import _mono_font
from aura.gui.cards.diff_card import DiffCard
from aura.gui.cards.error_card import ErrorCard
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, FG
from aura.gui.widgets.todo_list import TodoListWidget
from aura.gui.worker_log_stream import WorkerLogStreamBuffer


class InfoHubPane(QWidget):
    """Bottom pane with permanent Worker Log tab.

    Public API:
        append_reasoning(text) -> None
        append_content(text) -> None
        update_todo_list(tasks) -> None
        add_diff_card(rel_path, old, new, decision, is_new_file) -> None
        add_error(message) -> None
        flush_worker_log() -> None
        mark_worker_log_boundary() -> None
        show_final_summary(ok, summary) -> None
        clear() -> None
    """

    stop_worker_requested = Signal()

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

        # Stop Worker button in worker log footer
        self._stop_worker_btn = QPushButton("Stop Worker")
        self._stop_worker_btn.setObjectName("danger")
        self._stop_worker_btn.setMinimumSize(44, 36)
        self._stop_worker_btn.setVisible(False)
        self._stop_worker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_worker_btn.clicked.connect(self._on_stop_worker_clicked)

        layout.addWidget(self._tabs)

        # ---- Worker Log tab (permanent, index 0) ----
        self._log_tab = QWidget(self)
        log_layout = QVBoxLayout(self._log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        # TODO list widget
        self._todo_widget = TodoListWidget(self._log_tab)
        log_layout.addWidget(self._todo_widget)

        # Worker Activity feed (append-only heartbeat, under TODO)
        self._activity_container = QWidget(self._log_tab)
        self._activity_container.setObjectName("workerActivityFeed")
        self._activity_container.setStyleSheet(
            f"#workerActivityFeed {{ background: transparent; border-bottom: 1px solid {BORDER}; padding: 0; }}"
        )
        self._activity_layout = QVBoxLayout(self._activity_container)
        self._activity_layout.setContentsMargins(12, 2, 12, 4)
        self._activity_layout.setSpacing(0)
        self._activity_container.setVisible(False)
        log_layout.addWidget(self._activity_container)

        # Worker prose log text area
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
        self._log_stream = WorkerLogStreamBuffer(self._append_worker_log_batch, parent=self)

        # Dynamic cards area (diff cards, error cards)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setContentsMargins(8, 0, 8, 8)
        self._cards_layout.setSpacing(6)
        log_layout.addLayout(self._cards_layout)

        # Worker footer bar with Stop Worker button
        self._worker_footer = QWidget(self._log_tab)
        footer_layout = QHBoxLayout(self._worker_footer)
        footer_layout.setContentsMargins(8, 4, 8, 4)
        footer_layout.setSpacing(0)
        footer_layout.addWidget(self._stop_worker_btn)
        footer_layout.addStretch(1)
        self._worker_footer.setVisible(False)
        log_layout.addWidget(self._worker_footer)

        self._tabs.addTab(self._log_tab, "Worker Log")

    # Public API — Worker Log

    def append_reasoning(self, text: str) -> None:
        """Append reasoning prose to the Worker Log through the stream buffer."""
        self._log_stream.append("reasoning", text)

    def append_content(self, text: str) -> None:
        """Append content prose to the Worker Log through the stream buffer."""
        self._log_stream.append("content", text)

    def flush_worker_log(self) -> None:
        """Flush any pending Worker Log prose immediately."""
        self._log_stream.flush()

    def mark_worker_log_boundary(self) -> None:
        """Make the next Worker prose append start after a paragraph boundary."""
        self._log_stream.mark_boundary()

    def _append_worker_log_batch(self, text: str) -> None:
        """Insert one buffered Worker Log prose batch and scroll once."""
        cursor = self._log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._log_view.setTextCursor(cursor)
        self._log_view.insertPlainText(text)
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def update_todo_list(self, tasks: list[dict]) -> None:
        """Delegate to the embedded TodoListWidget."""
        self._todo_widget.update_tasks(tasks)

    def update_activity(self, entries: list[dict]) -> None:
        """Render the Worker Activity feed (append-only heartbeat)."""
        # Clear old activity rows
        while self._activity_layout.count() > 0:
            item = self._activity_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if not entries:
            self._activity_container.setVisible(False)
            return

        # Show only the last 15 most recent entries
        visible = entries[-15:]
        for entry in visible:
            label = QLabel(entry.get("message", ""), self._activity_container)
            label.setFont(_mono_font(9))
            label.setStyleSheet(f"color: {FG}; padding: 0; border: none; background: transparent;")
            self._activity_layout.addWidget(label)

        self._activity_container.setVisible(True)

    def add_diff_card(
        self,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        """Create a DiffCard and add it to the Worker Log's dynamic cards area."""
        self.flush_worker_log()
        card = DiffCard(rel_path, old, new, decision, is_new_file, parent=self._log_tab)
        self._cards_layout.addWidget(card)
        self.mark_worker_log_boundary()

    def add_error(self, message: str) -> None:
        """Create an ErrorCard and add it to the Worker Log's dynamic cards area."""
        self.flush_worker_log()
        card = ErrorCard("Worker Error", message, parent=self._log_tab)
        self._cards_layout.addWidget(card)
        self.mark_worker_log_boundary()

    def show_final_summary(self, ok: bool, summary: str, needs_followup: bool = False, status: str | None = None) -> None:
        """Append a formatted summary block to the Worker Log text.

        Flushes buffered prose immediately so the summary is ordered correctly.
        """
        self._log_stream.flush()
        prefix = _final_summary_label(ok, needs_followup=needs_followup, status=status)
        block = f"\n\n{'─' * 40}\n{prefix}\n{summary}\n{'─' * 40}\n"
        self._log_view.insertPlainText(block)

        # Auto-scroll to bottom
        sb = self._log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

        receipt_text = f"{'═' * 46}\n{prefix}\n{summary}\n{'═' * 46}"

        # Compact copy-icon button in a right-aligned row
        row = QWidget(self._log_tab)
        row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 8, 0)
        row_layout.setSpacing(0)

        row_layout.addStretch(1)

        copy_btn = QToolButton(row)
        copy_btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        copy_btn.setIconSize(QSize(16, 16))
        copy_btn.setToolTip("Copy summary")
        copy_btn.setStyleSheet(
            f"QToolButton {{ border: none; border-radius: 3px; padding: 2px; }} "
            f"QToolButton:hover {{ background: {BG_RAISED}; }}"
        )
        copy_btn.clicked.connect(lambda checked, b=copy_btn, r=receipt_text: self._on_copy_summary(b, r))
        row_layout.addWidget(copy_btn)

        self._cards_layout.addWidget(row)

    def _on_copy_summary(self, btn: QToolButton, receipt_text: str) -> None:
        QGuiApplication.clipboard().setText(receipt_text)
        btn.setIcon(QIcon(str(media_path("check.svg"))))
        btn.setText("")
        btn.setToolTip("Copied!")
        QTimer.singleShot(2000, lambda: self._reset_copy_summary_btn(btn))

    def _reset_copy_summary_btn(self, btn: QToolButton) -> None:
        btn.setIcon(QIcon(str(media_path("copy-classic.svg"))))
        btn.setText("")
        btn.setToolTip("Copy summary")

    def clear(self) -> None:
        """Reset the Worker Log: clear text, todo, activity, and dynamic cards."""
        self.clear_log()
        self._todo_widget.update_tasks([])
        self.update_activity([])

    def clear_log(self) -> None:
        """Clear log text and dynamic cards without touching the TODO rail."""
        self._log_stream.clear()
        self._log_view.setPlainText("")

        # Remove all dynamic cards
        while self._cards_layout.count() > 0:
            item = self._cards_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

    def _on_stop_worker_clicked(self) -> None:
        """Click handler: disable button, show stopping text, emit signal."""
        self._stop_worker_btn.setEnabled(False)
        self._stop_worker_btn.setText("Stopping Worker...")
        self.stop_worker_requested.emit()

    def set_worker_running(self, running: bool) -> None:
        """Show/hide the Stop Worker button based on worker running state."""
        self._worker_footer.setVisible(running)
        self._stop_worker_btn.setVisible(running)
        if running:
            self._stop_worker_btn.setEnabled(True)
            self._stop_worker_btn.setText("Stop Worker")
        else:
            self._stop_worker_btn.setEnabled(True)
            self._stop_worker_btn.setText("Stop Worker")

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


def _final_summary_label(
    ok: bool,
    *,
    needs_followup: bool = False,
    status: str | None = None,
) -> str:
    from aura.conversation.worker_outcome import WorkerOutcomeStatus, normalize_outcome_status

    normalized = normalize_outcome_status(status)
    if normalized == WorkerOutcomeStatus.cancelled.value:
        return "Cancelled."
    if ok and not needs_followup:
        return "Completed."
    if normalized in {
        WorkerOutcomeStatus.completed.value,
        WorkerOutcomeStatus.completed_with_caveats.value,
    }:
        return "Completed."
    if needs_followup or normalized in {
        WorkerOutcomeStatus.validation_failed.value,
        WorkerOutcomeStatus.edit_mechanics_blocked.value,
        WorkerOutcomeStatus.scope_mismatch.value,
    }:
        return "Needs attention."
    return "Failed."
