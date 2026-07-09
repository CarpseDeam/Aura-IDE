"""Info hub pane: Worker Log tab with activity and final report."""

from __future__ import annotations

import logging

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
from aura.gui.theme import ACCENT, BG, BG_RAISED, BORDER, FG, FG_MUTED, SUCCESS
from aura.gui.widgets.worker_todo import WorkerTodoWidget
from aura.gui.worker_log_stream import WorkerLogStreamBuffer

_log = logging.getLogger(__name__)


class InfoHubPane(QWidget):
    """Bottom pane with permanent Worker Log tab.

    Public API:
        append_reasoning(text) -> None
        append_content(text) -> None
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



        layout.addWidget(self._tabs)

        # ---- Worker Log tab (permanent, index 0) ----
        self._log_tab = QWidget(self)
        self._log_tab.setMinimumSize(0, 0)
        self._log_tab.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        log_layout = QVBoxLayout(self._log_tab)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(0)

        # Worker Log header row
        self._log_header = QWidget(self._log_tab)
        header_layout = QHBoxLayout(self._log_header)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.setSpacing(8)

        # Title
        header_title = QLabel("WORKER LOG")
        header_title.setObjectName("paneTitleWorkspace")
        header_layout.addWidget(header_title)

        # Status chip
        self._status_chip = QLabel("")
        self._status_chip.setObjectName("workerLogStatusChip")
        header_layout.addWidget(self._status_chip)

        header_layout.addStretch(1)

        # Copy Receipt button — shown after a final summary exists
        self._copy_receipt_btn = QPushButton("Copy Receipt")
        self._copy_receipt_btn.setObjectName("primary")
        self._copy_receipt_btn.setMinimumSize(44, 36)
        self._copy_receipt_btn.setVisible(False)
        self._copy_receipt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._copy_receipt_btn.clicked.connect(self._on_header_copy_receipt)
        header_layout.addWidget(self._copy_receipt_btn)

        # Stop Worker button
        self._stop_worker_btn = QPushButton("Stop Worker")
        self._stop_worker_btn.setObjectName("danger")
        self._stop_worker_btn.setMinimumSize(44, 36)
        self._stop_worker_btn.setVisible(False)
        self._stop_worker_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_worker_btn.clicked.connect(self._on_stop_worker_clicked)
        header_layout.addWidget(self._stop_worker_btn)

        # Insert header at the top of the log tab layout
        log_layout.insertWidget(0, self._log_header, 0)

        self._todo_widget = WorkerTodoWidget(self._log_tab)
        log_layout.addWidget(self._todo_widget, 0)

        # Worker log text area: activity/tool calls first, final report last.
        self._log_view = QPlainTextEdit(self._log_tab)
        self._log_view.setReadOnly(True)
        self._log_view.setMinimumSize(0, 0)
        self._log_view.setFont(_mono_font(10))
        self._log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self._log_view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._log_view.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self._log_view.setStyleSheet(
            f"background: transparent; color: {FG}; border: none; padding: 8px;"
        )
        self._log_view.setPlaceholderText("Worker output will appear here.")
        log_layout.addWidget(self._log_view, 1)
        self._log_stream = WorkerLogStreamBuffer(self._append_worker_log_batch, parent=self)
        self._activity_entry_count = 0
        self._receipt_text: str = ""

        # Dynamic cards area (diff cards, error cards)
        self._cards_layout = QVBoxLayout()
        self._cards_layout.setContentsMargins(8, 0, 8, 8)
        self._cards_layout.setSpacing(6)
        log_layout.addLayout(self._cards_layout, 0)

        log_layout.setStretch(0, 0)  # header
        log_layout.setStretch(1, 0)  # todo
        log_layout.setStretch(2, 1)  # log view
        log_layout.setStretch(3, 0)  # cards

        self._tabs.addTab(self._log_tab, "Worker Log")
        self._tabs.tabBar().setVisible(False)

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

    def update_activity(self, entries: list[dict]) -> None:
        """Append new Worker Activity entries to the single Worker Log stream."""
        if not entries:
            self._activity_entry_count = 0
            return

        if len(entries) < self._activity_entry_count:
            self._activity_entry_count = 0

        new_entries = entries[self._activity_entry_count :]
        self._activity_entry_count = len(entries)
        lines = [
            line
            for line in (_activity_log_line(entry) for entry in new_entries)
            if line
        ]
        if not lines:
            return

        self._log_stream.flush()
        prefix = "\n" if self._log_view.toPlainText() else ""
        self._append_worker_log_batch(prefix + "\n".join(lines) + "\n")

    def update_worker_todo(self, items: list[dict[str, str]]) -> None:
        """Render the latest Worker TODO snapshot."""
        self._todo_widget.update_snapshot(items)

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
        self._receipt_text = receipt_text
        self._copy_receipt_btn.setVisible(True)
        self._status_chip.setText("● Receipt")
        self._status_chip.setStyleSheet(f"color: {ACCENT}; font-size: 10px; font-weight: 600;")

    def _on_header_copy_receipt(self) -> None:
        """Copy the stored receipt text to clipboard and show brief Copied! feedback."""
        if not self._receipt_text:
            return
        QGuiApplication.clipboard().setText(self._receipt_text)
        original_text = self._copy_receipt_btn.text()
        self._copy_receipt_btn.setText("Copied!")
        self._copy_receipt_btn.setEnabled(False)
        QTimer.singleShot(2000, lambda: self._reset_header_copy_btn(original_text))

    def _reset_header_copy_btn(self, original_text: str) -> None:
        self._copy_receipt_btn.setText(original_text)
        self._copy_receipt_btn.setEnabled(True)

    def clear(self) -> None:
        """Reset the Worker Log: clear text, activity, and dynamic cards."""
        _log.info("DIAGNOSTIC InfoHubPane.clear called")
        self.clear_log()
        self.update_activity([])

    def clear_log(self) -> None:
        """Clear log text and dynamic cards."""
        _log.info("DIAGNOSTIC InfoHubPane.clear_log called — clearing log text, TODO, cards")
        self._log_stream.clear()
        self._log_view.setPlainText("")
        self._activity_entry_count = 0
        self._todo_widget.clear()
        self._copy_receipt_btn.setVisible(False)
        self._receipt_text = ""
        self._status_chip.setText("Idle")
        self._status_chip.setStyleSheet(f"color: {FG_MUTED}; font-size: 10px;")

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
        self._stop_worker_btn.setVisible(running)
        if running:
            self._stop_worker_btn.setEnabled(True)
            self._stop_worker_btn.setText("Stop Worker")
            self._status_chip.setText("● Live")
            self._status_chip.setStyleSheet(f"color: {SUCCESS}; font-size: 10px; font-weight: 600;")
            self._copy_receipt_btn.setVisible(False)
        else:
            self._stop_worker_btn.setVisible(False)
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
    if normalized == WorkerOutcomeStatus.approval_rejected.value:
        return "Changes rejected."
    if normalized == WorkerOutcomeStatus.harness_error.value:
        return "Worker Error."
    if normalized in {
        WorkerOutcomeStatus.completed.value,
        WorkerOutcomeStatus.completed_with_caveats.value,
        WorkerOutcomeStatus.validation_failed.value,
        WorkerOutcomeStatus.edit_mechanics_blocked.value,
        WorkerOutcomeStatus.scope_mismatch.value,
    }:
        return "Worker Report."
    if ok and not needs_followup:
        return "Worker Report."
    return "Worker Report."


_SUPPRESSED_ACTIVITY_KINDS = frozenset(
    {
        "campaign_started",
        "step_started",
        "step_completed",
        "step_failed",
        "final_report_started",
        "final_report_completed",
        "final_report_failed",
    }
)


def _activity_log_line(entry: dict) -> str:
    """Return the user-visible Worker Log line for one activity entry."""
    if not isinstance(entry, dict):
        return ""
    kind = str(entry.get("kind") or "")
    if kind in _SUPPRESSED_ACTIVITY_KINDS:
        return ""
    message = str(entry.get("message") or "").strip()
    return message
