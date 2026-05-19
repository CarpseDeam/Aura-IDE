from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QVariantAnimation
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QLabel, QVBoxLayout

from aura.gui.theme import BG, BORDER, FG_DIM, SUCCESS, WARN


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan."""

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

        header = QLabel("TODO LIST", self)
        header.setObjectName("paneTitle")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self._pulse_anims: list = []
        self._task_widgets: list[QLabel] = []
        self.setVisible(False)

    def update_tasks(self, tasks: list[dict]) -> None:
        for anim in self._pulse_anims:
            anim.stop()
            anim.deleteLater()
        self._pulse_anims.clear()

        if not tasks:
            self.setVisible(False)
            return

        self.setVisible(True)

        for i, task in enumerate(tasks):
            description = task.get("description", "")
            status = task.get("status", "pending")

            if status == "done":
                prefix, color = "\u2713", SUCCESS
            elif status == "active":
                prefix, color = "\u25ba", WARN
            else:
                prefix, color = "\u25cb", FG_DIM

            if i < len(self._task_widgets):
                label = self._task_widgets[i]
            else:
                label = QLabel()
                label.setWordWrap(True)
                font = label.font()
                font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
                font.setPointSize(11)
                label.setFont(font)
                self._tasks_layout.addWidget(label)
                self._task_widgets.append(label)

            label.setText(f"{prefix} {description}")
            label.setStyleSheet(f"color: {color}; padding: 1px 0;")

            font = label.font()
            if status == "active":
                font.setBold(True)
                label.setFont(font)
                old_effect = label.graphicsEffect()
                if old_effect:
                    old_effect.deleteLater()
                effect = QGraphicsOpacityEffect(label)
                label.setGraphicsEffect(effect)
                pulse = QVariantAnimation(label)
                pulse.setStartValue(0.55)
                pulse.setEndValue(1.0)
                pulse.setDuration(900)
                pulse.setLoopCount(-1)
                pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
                pulse.valueChanged.connect(lambda v, e=effect: e.setOpacity(v))
                pulse.start()
                self._pulse_anims.append(pulse)
            else:
                font.setBold(False)
                label.setFont(font)
                old_effect = label.graphicsEffect()
                if old_effect:
                    label.setGraphicsEffect(None)
                    old_effect.deleteLater()

        while len(self._task_widgets) > len(tasks):
            widget = self._task_widgets.pop()
            self._tasks_layout.removeWidget(widget)
            widget.deleteLater()
