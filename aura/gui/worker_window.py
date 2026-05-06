"""Embeddable panel for worker dispatch output.

Shows worker streaming (reasoning, tool calls, diffs, code) with auto-scroll
for every dispatch in a single, persistent panel embedded in the main window.
"""

from __future__ import annotations

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.chat_view import (
    AssistantCard,
    CodeWriterCard,
    DiffCard,
    ErrorCard,
    ToolCallCard,
    _fade_in_widget,
)
from aura.gui.aura_widget import AuraWidget
from aura.gui.theme import BG, BORDER, DANGER, FG_DIM, SUCCESS, WARN


CODE_WRITER_NAMES = frozenset({"write_file", "edit_file"})


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan with live status updates."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("todoListWidget")
        self.setStyleSheet(
            f"QFrame#todoListWidget {{"
            f"  background: {BG};"
            f"  border-bottom: 1px solid {BORDER};"
            f"  padding: 0;"
            f"}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(4)

        # Header
        header = QLabel("TODO LIST")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        # Container for task labels
        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self.setVisible(False)  # Hidden until tasks arrive

    def update_tasks(self, tasks: list[dict]) -> None:
        """Clear and redraw the task list from the worker's update_todo_list tool."""
        # Remove old task labels
        while self._tasks_layout.count() > 0:
            item = self._tasks_layout.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()

        if not tasks:
            self.setVisible(False)
            return

        self.setVisible(True)

        for task in tasks:
            description = task.get("description", "")
            status = task.get("status", "pending")

            # Choose prefix and color
            if status == "done":
                prefix = "✓"
                color = SUCCESS
            elif status == "active":
                prefix = "►"
                color = WARN
            else:  # pending
                prefix = "○"
                color = FG_DIM

            label_text = f"{prefix} {description}"
            label = QLabel(label_text)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

            # Monospace font
            font = label.font()
            font.setFamily("Cascadia Mono, Consolas, monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPointSize(11)
            label.setFont(font)

            # Bold for active tasks
            if status == "active":
                font.setBold(True)
                label.setFont(font)

            label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            self._tasks_layout.addWidget(label)


class WorkerWindow(QWidget):
    """Embeddable panel showing live worker activity for all dispatches."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Worker Activity")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 8px 12px;")
        layout.addWidget(header)

        # Pinned TODO list
        self._todo_widget = TodoListWidget()
        layout.addWidget(self._todo_widget)

        # Central scroll area
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._layout = QVBoxLayout(container)
        self._layout.setContentsMargins(12, 12, 12, 12)
        self._layout.setSpacing(12)
        self._layout.addStretch(1)
        scroll.setWidget(container)
        layout.addWidget(scroll, 1)
        self._scroll = scroll
        self._scroll_anim: QPropertyAnimation | None = None

        # Status label at the bottom
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(
            f"color: {FG_DIM}; padding: 6px 12px; border-top: 1px solid {BORDER};"
        )
        layout.addWidget(self._status_label)

        # Internal state
        self._current_assistant: AssistantCard | None = None
        self._tool_cards: dict[str, ToolCallCard | CodeWriterCard] = {}
        self._tool_owner: dict[str, AssistantCard] = {}

    # ---- helpers -----------------------------------------------------------

    def _scroll_to_bottom(self) -> None:
        bar = self._scroll.verticalScrollBar()
        # Stop any in-flight smooth scroll
        if self._scroll_anim is not None:
            self._scroll_anim.stop()
        self._scroll_anim = QPropertyAnimation(bar, b"value")
        self._scroll_anim.setDuration(150)
        self._scroll_anim.setStartValue(bar.value())
        self._scroll_anim.setEndValue(bar.maximum())
        self._scroll_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_anim.start()

    def _add_card(self, w: QWidget) -> None:
        self._layout.insertWidget(self._layout.count() - 1, w)
        _fade_in_widget(w)
        self._scroll_to_bottom()

    def _current_or_new_assistant(self) -> AssistantCard:
        if self._current_assistant is None:
            return self.begin_assistant()
        return self._current_assistant

    # ---- public streaming API ----------------------------------------------

    def begin_assistant(self) -> AssistantCard:
        card = AssistantCard()
        self._current_assistant = card
        wrapper = AuraWidget(card, glow_color=SUCCESS, glow_spread=16)
        self._add_card(wrapper)
        wrapper.start_aura()
        return card

    def append_reasoning(self, text: str) -> None:
        self._current_or_new_assistant().append_reasoning(text)
        self._scroll_to_bottom()

    def append_content(self, text: str) -> None:
        ac = self._current_or_new_assistant()
        ac.reasoning_done()
        ac.append_content(text)
        self._scroll_to_bottom()

    def add_tool_call(self, worker_tool_id: str, name: str) -> None:
        if name == "update_todo_list":
            return  # Pinned todo widget handles this, don't render a tool card.
        ac = self._current_or_new_assistant()
        if name in CODE_WRITER_NAMES:
            card: ToolCallCard | CodeWriterCard = CodeWriterCard(name)
        else:
            card = ToolCallCard(name)
        # Add directly to the assistant's tool cluster (bypass add_tool_card
        # which always creates a plain ToolCallCard).
        ac._tool_cards[worker_tool_id] = card
        if not ac._tool_cluster.isVisible():
            ac._tool_cluster.setVisible(True)
        ac._tool_cluster_layout.addWidget(card)
        _fade_in_widget(card)
        self._tool_cards[worker_tool_id] = card
        self._tool_owner[worker_tool_id] = ac
        self._scroll_to_bottom()

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        card = self._tool_cards.get(worker_tool_id)
        if card is not None:
            card.append_args(fragment)
        self._scroll_to_bottom()

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str) -> None:
        card = self._tool_cards.get(worker_tool_id)
        if card is not None:
            card.set_result(ok, result)
        self._scroll_to_bottom()

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        ac = self._tool_owner.get(worker_tool_id) or self._current_or_new_assistant()
        card = DiffCard(rel_path, old, new, decision, is_new_file)
        ac.add_footer_widget(card)
        self._scroll_to_bottom()

    def add_error(self, message: str) -> None:
        err = ErrorCard("Worker error", message)
        self._add_card(err)

    def worker_finished(self, ok: bool, summary: str) -> None:
        # Stop the spinning aura on the current card
        ac = self._current_assistant
        if ac is not None:
            wrapper = ac.parentWidget()
            if isinstance(wrapper, AuraWidget):
                wrapper.stop_aura()
            ac.finalize_content()
            self._current_assistant = None

        if ok:
            self._status_label.setText("Completed")
            self._status_label.setStyleSheet(
                f"color: {SUCCESS}; font-weight: 600; padding: 6px 12px; border-top: 1px solid {BORDER};"
            )
        else:
            self._status_label.setText("Completed with errors")
            self._status_label.setStyleSheet(
                f"color: {DANGER}; font-weight: 600; padding: 6px 12px; border-top: 1px solid {BORDER};"
            )

    def worker_cancelled(self) -> None:
        ac = self._current_assistant
        if ac is not None:
            wrapper = ac.parentWidget()
            if isinstance(wrapper, AuraWidget):
                wrapper.stop_aura()
        self._status_label.setText("Cancelled")
        self._status_label.setStyleSheet(
            f"color: {DANGER}; padding: 6px 12px; border-top: 1px solid {BORDER};"
        )

    def update_todo_list(self, tasks: list) -> None:
        """Forward the worker's TODO list update to the pinned widget."""
        self._todo_widget.update_tasks(tasks)

    def clear(self) -> None:
        """Remove all cards and reset state (called on New Conversation)."""
        # Remove every widget from the layout except the trailing stretch.
        while self._layout.count() > 1:
            item = self._layout.takeAt(0)
            if item is not None:
                w = item.widget()
                if w is not None:
                    w.deleteLater()
        self._current_assistant = None
        self._tool_cards.clear()
        self._tool_owner.clear()
        self._todo_widget.update_tasks([])
