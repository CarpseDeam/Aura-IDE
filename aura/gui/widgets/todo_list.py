from __future__ import annotations

import logging
from typing import Any
from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from aura.config import media_path
from aura.gui.theme import BG, BORDER, FG_DIM, SUCCESS, WARN
from aura.todo_state import normalize_todo_tasks, todo_signature

_log = logging.getLogger(__name__)


class TodoListWidget(QFrame):
    """Pinned TODO list showing the worker's execution plan."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("todoListWidget")
        self.setStyleSheet(
            f"QFrame#todoListWidget {{  background: {BG};  border-bottom: 1px solid {BORDER};  padding: 0;}}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(4)

        header = QLabel("TODO LIST", self)
        header.setObjectName("paneTitleTodo")
        header.setStyleSheet("padding: 0 0 4px 0;")
        outer.addWidget(header)

        self._tasks_layout = QVBoxLayout()
        self._tasks_layout.setContentsMargins(0, 0, 0, 0)
        self._tasks_layout.setSpacing(2)
        outer.addLayout(self._tasks_layout)

        self._pulse_anims: list[QVariantAnimation] = []
        self._task_widgets: list[QFrame] = []
        self._task_icon_labels: list[QLabel] = []
        self._task_desc_labels: list[QLabel] = []
        self._task_statuses: list[str] = []
        self._task_anims: list[QVariantAnimation | None] = []
        self._last_sig: tuple[tuple[str, str], ...] = ()
        self.setVisible(False)

    def _cleanup_all(self) -> None:
        """Safely clean up all row animations, effects, and labels."""
        for anim in self._task_anims:
            if anim is not None:
                anim.stop()
                anim.deleteLater()
        self._task_anims.clear()
        self._pulse_anims.clear()

        for label in self._task_widgets:
            if label.graphicsEffect() is not None:
                label.setGraphicsEffect(None)
            self._tasks_layout.removeWidget(label)
            label.deleteLater()
        self._task_widgets.clear()
        self._task_icon_labels.clear()
        self._task_desc_labels.clear()
        self._task_statuses.clear()
        self._last_sig = ()

    def update_tasks(self, tasks: list[Any]) -> None:
        """Update the displayed TODO tasks, reusing existing rows and managing animations."""
        normalized = normalize_todo_tasks(tasks)

        # 1. Signature caching to avoid redundant widget updates
        sig = todo_signature(normalized)
        if self._last_sig == sig:
            _log.debug(
                "TodoListWidget.update_tasks sig unchanged — skipped (task_count=%d)",
                len(normalized),
            )
            return
        _log.debug(
            "TodoListWidget.update_tasks task_count=%d sig=%s -> %s",
            len(normalized),
            [(d, s) for d, s in self._last_sig],
            [(d, s) for d, s in sig],
        )
        self._last_sig = sig

        if not normalized:
            self.setVisible(False)
            self._cleanup_all()
            return

        self.setVisible(True)

        # 2. Grow widget list to fit task count
        while len(self._task_widgets) < len(normalized):
            row = QFrame(self)
            row.setStyleSheet("border: none; background: transparent;")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(4)

            icon_label = QLabel(row)
            icon_label.setFixedSize(16, 16)
            row_layout.addWidget(icon_label)

            desc_label = QLabel(row)
            desc_label.setWordWrap(True)
            font = desc_label.font()
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setPointSize(11)
            desc_label.setFont(font)
            row_layout.addWidget(desc_label, stretch=1)

            self._tasks_layout.addWidget(row)
            self._task_widgets.append(row)
            self._task_icon_labels.append(icon_label)
            self._task_desc_labels.append(desc_label)
            self._task_statuses.append("")
            self._task_anims.append(None)

        # 3. Shrink widget list and clean up unused rows
        while len(self._task_widgets) > len(normalized):
            row = self._task_widgets.pop()
            self._tasks_layout.removeWidget(row)

            if row.graphicsEffect() is not None:
                row.setGraphicsEffect(None)

            row.deleteLater()
            self._task_icon_labels.pop()
            self._task_desc_labels.pop()
            self._task_statuses.pop()
            anim = self._task_anims.pop()
            if anim is not None:
                anim.stop()
                anim.deleteLater()

        # 4. Synchronize widget states and manage active animations
        for i, task in enumerate(normalized):
            description = task["description"]
            status = task["status"]

            icon_label = self._task_icon_labels[i]
            desc_label = self._task_desc_labels[i]
            row = self._task_widgets[i]

            if status == "done":
                color = SUCCESS
                svg_path = media_path("check_box.svg")
                svg_content = svg_path.read_text(encoding="utf-8")
                svg_content = svg_content.replace('fill="#e3e3e3"', f'fill="{SUCCESS}"')
                pixmap = QPixmap()
                pixmap.loadFromData(svg_content.encode("utf-8"))
                scaled_pixmap = pixmap.scaled(
                    16, 16, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
                icon_label.setPixmap(scaled_pixmap)
                icon_label.setFixedSize(16, 16)
                icon_label.setStyleSheet("background: transparent;")
                desc_label.setText(description)
                desc_label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            elif status == "active":
                color = WARN
                icon_label.setText("\u25ba")
                icon_label.setFixedSize(16, 16)
                icon_label.setStyleSheet(f"color: {color}; background: transparent;")
                desc_label.setText(description)
                desc_label.setStyleSheet(f"color: {color}; padding: 1px 0;")
            else:
                color = FG_DIM
                icon_label.setText("\u25cb")
                icon_label.setFixedSize(16, 16)
                icon_label.setStyleSheet(f"color: {color}; background: transparent;")
                desc_label.setText(description)
                desc_label.setStyleSheet(f"color: {color}; padding: 1px 0;")

            font = desc_label.font()
            if status == "done":
                font.setStrikeOut(True)
                font.setBold(False)
            elif status == "active":
                font.setStrikeOut(False)
                font.setBold(True)
            else:
                font.setStrikeOut(False)
                font.setBold(False)
            desc_label.setFont(font)

            old_status = self._task_statuses[i]
            if status == "active":
                if old_status != "active":
                    # Transitioning into active: build animation
                    if self._task_anims[i] is not None:
                        old_anim = self._task_anims[i]
                        if old_anim is not None:
                            old_anim.stop()
                            old_anim.deleteLater()
                        self._task_anims[i] = None

                    if row.graphicsEffect() is not None:
                        row.setGraphicsEffect(None)

                    effect = QGraphicsOpacityEffect(row)
                    row.setGraphicsEffect(effect)

                    pulse = QVariantAnimation(row)
                    pulse.setStartValue(0.55)
                    pulse.setEndValue(1.0)
                    pulse.setDuration(900)
                    pulse.setLoopCount(-1)
                    pulse.setEasingCurve(QEasingCurve.Type.InOutSine)
                    pulse.valueChanged.connect(lambda v, eff=effect: eff.setOpacity(v))
                    pulse.start()
                    self._task_anims[i] = pulse
            else:
                if old_status == "active":
                    # Transitioning away from active: stop animation and clear effect
                    anim = self._task_anims[i]
                    if anim is not None:
                        anim.stop()
                        anim.deleteLater()
                        self._task_anims[i] = None

                    if row.graphicsEffect() is not None:
                        row.setGraphicsEffect(None)

            self._task_statuses[i] = status

        # Keep self._pulse_anims updated for test compatibility
        self._pulse_anims = [anim for anim in self._task_anims if anim is not None]
