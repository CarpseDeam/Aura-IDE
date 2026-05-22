from __future__ import annotations

from typing import Any
from PySide6.QtCore import QEasingCurve, QVariantAnimation
from PySide6.QtWidgets import QFrame, QGraphicsOpacityEffect, QLabel, QVBoxLayout, QWidget

from aura.gui.theme import BG, BORDER, FG_DIM, SUCCESS, WARN


def normalize_todo_tasks(tasks: list[Any]) -> list[dict[str, Any]]:
    """Normalize user-provided task lists into a standard format.

    Supports description/content/text/task fields and maps various status
    aliases to pending, active, or done. Clamps descriptions to 220 chars.
    Does not mutate the input list.
    """
    normalized: list[dict[str, Any]] = []
    if not isinstance(tasks, list):
        return normalized

    for t in tasks:
        if isinstance(t, dict):
            desc: str = ""
            for key in ("description", "content", "text", "task"):
                if key in t:
                    desc = str(t[key])
                    break

            if len(desc) > 220:
                desc = desc[:217] + "..."

            raw_status: str = ""
            for key in ("status", "state"):
                if key in t:
                    raw_status = str(t[key]).lower().strip()
                    break

            status: str = "pending"
            if raw_status in ("done", "completed", "complete"):
                status = "done"
            elif raw_status in ("active", "in_progress", "doing", "current"):
                status = "active"
            else:
                status = "pending"

            normalized.append({"description": desc, "status": status})
        elif isinstance(t, str):
            desc_str: str = t
            if len(desc_str) > 220:
                desc_str = desc_str[:217] + "..."
            normalized.append({"description": desc_str, "status": "pending"})

    return normalized


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
        self._task_widgets: list[QLabel] = []
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
        self._task_statuses.clear()
        self._last_sig = ()

    def update_tasks(self, tasks: list[Any]) -> None:
        """Update the displayed TODO tasks, reusing existing rows and managing animations."""
        normalized = normalize_todo_tasks(tasks)

        # 1. Signature caching to avoid redundant widget updates
        sig = tuple((t["description"], t["status"]) for t in normalized)
        if self._last_sig == sig:
            return
        self._last_sig = sig

        if not normalized:
            self.setVisible(False)
            self._cleanup_all()
            return

        self.setVisible(True)

        # 2. Grow widget list to fit task count
        while len(self._task_widgets) < len(normalized):
            label = QLabel(self)
            label.setWordWrap(True)
            font = label.font()
            font.setFamily("Geist Mono, JetBrains Mono, Consolas, monospace")
            font.setPointSize(11)
            label.setFont(font)
            self._tasks_layout.addWidget(label)
            self._task_widgets.append(label)
            self._task_statuses.append("")
            self._task_anims.append(None)

        # 3. Shrink widget list and clean up unused rows
        while len(self._task_widgets) > len(normalized):
            label = self._task_widgets.pop()
            self._tasks_layout.removeWidget(label)

            if label.graphicsEffect() is not None:
                label.setGraphicsEffect(None)

            label.deleteLater()
            self._task_statuses.pop()
            anim = self._task_anims.pop()
            if anim is not None:
                anim.stop()
                anim.deleteLater()

        # 4. Synchronize widget states and manage active animations
        for i, task in enumerate(normalized):
            description = task["description"]
            status = task["status"]

            if status == "done":
                prefix, color = "\u2713", SUCCESS
            elif status == "active":
                prefix, color = "\u25ba", WARN
            else:
                prefix, color = "\u25cb", FG_DIM

            label = self._task_widgets[i]
            label.setText(f"{prefix} {description}")
            label.setStyleSheet(f"color: {color}; padding: 1px 0;")

            font = label.font()
            if status == "done":
                font.setStrikeOut(True)
                font.setBold(False)
            elif status == "active":
                font.setStrikeOut(False)
                font.setBold(True)
            else:
                font.setStrikeOut(False)
                font.setBold(False)
            label.setFont(font)

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

                    if label.graphicsEffect() is not None:
                        label.setGraphicsEffect(None)

                    effect = QGraphicsOpacityEffect(label)
                    label.setGraphicsEffect(effect)

                    pulse = QVariantAnimation(label)
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

                    if label.graphicsEffect() is not None:
                        label.setGraphicsEffect(None)

            self._task_statuses[i] = status

        # Keep self._pulse_anims updated for test compatibility
        self._pulse_anims = [anim for anim in self._task_anims if anim is not None]
