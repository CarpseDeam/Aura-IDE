"""Embeddable panel for worker dispatch output.

Shows a pinned TODO list and a single scrolling card for worker streaming output.
"""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsOpacityEffect,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import BG, BORDER, FG_DIM, SUCCESS, WARN


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

        self._pulse_anims: list[QVariantAnimation] = []

        self.setVisible(False)  # Hidden until tasks arrive

    def update_tasks(self, tasks: list[dict]) -> None:
        """Clear and redraw the task list from the worker's update_todo_list tool."""
        # Stop any running pulse animations
        for anim in self._pulse_anims:
            anim.stop()
            anim.deleteLater()
        self._pulse_anims.clear()

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
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setStyleHint(QFont.StyleHint.Monospace)
            font.setPointSize(11)
            label.setFont(font)

            # Bold for active tasks
            if status == "active":
                font.setBold(True)
                label.setFont(font)

                # Add a breathing pulse animation to the label
                effect = QGraphicsOpacityEffect(label)
                effect.setOpacity(1.0)
                label.setGraphicsEffect(effect)

                pulse = QVariantAnimation(label)
                pulse.setStartValue(0.55)
                pulse.setEndValue(1.0)
                pulse.setDuration(900)
                pulse.setLoopCount(-1)
                pulse.setEasingCurve(QEasingCurve.Type.InOutSine)

                def _make_opacity_setter(eff):
                    return lambda v: eff.setOpacity(v)

                pulse.valueChanged.connect(_make_opacity_setter(effect))
                pulse.start()
                self._pulse_anims.append(pulse)

            label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            self._tasks_layout.addWidget(label)


class WorkerWindow(QWidget):
    """Embeddable panel showing live worker activity for all dispatches."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Header
        header = QLabel("Worker")
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 8px 12px;")
        layout.addWidget(header)

        # Pinned TODO list
        self._todo_widget = TodoListWidget()
        layout.addWidget(self._todo_widget)

        # Single scrollable card for worker output
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")

        self._card = QLabel()
        self._card.setWordWrap(True)
        self._card.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._card.setStyleSheet(
            f"color: #eaecef;"
            f"background: {BG};"
            f"padding: 12px;"
            f"border-top: 1px solid {BORDER};"
        )
        font = self._card.font()
        font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(11)
        self._card.setFont(font)
        self._card.setVisible(False)

        scroll.setWidget(self._card)
        layout.addWidget(scroll, 1)

        # Internal state
        self._active = False

    # ---- public streaming API ----------------------------------------------

    def begin_assistant(self) -> None:
        self._card.setText("")
        self._card.setVisible(True)
        self._active = True

    def append_reasoning(self, text: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + f"[thinking] {text}")

    def append_content(self, text: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + text)

    def add_tool_call(self, worker_tool_id: str, name: str) -> None:
        if not self._active:
            return
        if name == "update_todo_list":
            return  # Pinned todo widget handles this, don't render a tool card.
        current = self._card.text()
        self._card.setText(current + f"\n🛠 {name}")

    def append_tool_args(self, worker_tool_id: str, fragment: str) -> None:
        # no-op: simplified card doesn't show tool args
        pass

    def set_tool_result(self, worker_tool_id: str, ok: bool, result: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        if ok:
            self._card.setText(current + " ✓")
        else:
            self._card.setText(current + " ✗")

    def append_terminal_output(self, worker_tool_id: str, text: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + text)

    def add_diff_card(
        self,
        worker_tool_id: str,
        rel_path: str,
        old: str,
        new: str,
        decision: str,
        is_new_file: bool,
    ) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + f"\n📄 {decision}: {rel_path}")

    def add_error(self, message: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + f"\n❌ {message}")

    def worker_finished(self, ok: bool, summary: str) -> None:
        if not self._active:
            return
        current = self._card.text()
        if ok:
            self._card.setText(current + "\n---\n✅ Completed")
        else:
            self._card.setText(current + "\n---\n❌ Completed with errors")
        self._active = False

    def worker_cancelled(self) -> None:
        if not self._active:
            return
        current = self._card.text()
        self._card.setText(current + "\n---\n⏹ Cancelled")
        self._active = False

    def update_todo_list(self, tasks: list) -> None:
        """Forward the worker's TODO list update to the pinned widget."""
        self._todo_widget.update_tasks(tasks)

    def clear(self) -> None:
        """Remove all card content and reset state (called on New Conversation)."""
        self._card.setText("")
        self._card.setVisible(False)
        self._todo_widget.update_tasks([])
        self._active = False
