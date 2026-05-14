"""Compact status card for a worker plan being written."""
from __future__ import annotations

import json

from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
)

from aura.gui.cards._helpers import _fade_in_widget
from aura.gui.theme import BG_TOOL_CARD, BORDER, DANGER, SUCCESS, WARN


class PlanWriterCard(QFrame):
    """Small status indicator for temporary planner output."""

    STATE_RUNNING = "running"
    STATE_DONE = "done"
    STATE_FAILED = "failed"
    STATE_PHASE = "phase"

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("planWriterCard")
        self.setMinimumWidth(0)
        self.setMinimumHeight(26)
        self.setMaximumHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._goal: str = ""
        self._latest_spec: str = ""
        self._state = self.STATE_RUNNING

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(0)

        self._status = QLabel(self)
        self._status.setObjectName("planWriterStatus")
        self._status.setMinimumWidth(0)
        self._status.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        self._status.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed
        )
        layout.addWidget(self._status)

        self._refresh()
        _fade_in_widget(self)

    def _refresh(self) -> None:
        state_color = {
            self.STATE_RUNNING: WARN,
            self.STATE_DONE: SUCCESS,
            self.STATE_FAILED: DANGER,
            self.STATE_PHASE: WARN,
        }[self._state]

        text = {
            self.STATE_RUNNING: (
                f"⚡ Writing plan: {self._goal}" if self._goal else "⚡ Writing plan..."
            ),
            self.STATE_DONE: "⚡ Plan ready ✓",
            self.STATE_FAILED: "⚡ Plan failed ✗",
            self.STATE_PHASE: "⚡ Phase complete — preparing follow-up",
        }[self._state]

        metrics = QFontMetrics(self._status.font())
        available = max(40, self._status.width() or self.width() - 20)
        self._status.setText(
            metrics.elidedText(text, Qt.TextElideMode.ElideRight, available)
        )

        tooltip_parts = []
        if self._goal:
            tooltip_parts.append(f"Goal: {self._goal}")
        if self._latest_spec:
            tooltip_parts.append(f"Latest spec:\n{self._latest_spec[:2000]}")
        tooltip = "\n\n".join(tooltip_parts) or "Writing plan"
        self.setToolTip(tooltip)
        self._status.setToolTip(tooltip)

        self.setStyleSheet(
            f"""
            QFrame#planWriterCard {{
                background: {BG_TOOL_CARD};
                border: 1px solid {BORDER};
                border-left: 2px solid {state_color};
                border-radius: 8px;
            }}
            QLabel#planWriterStatus {{
                color: {state_color};
                font-size: 12px;
                font-weight: 600;
            }}
            """
        )

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._refresh()

    def set_goal(self, goal: str) -> None:
        self._goal = goal
        self._refresh()

    def update_spec(self, spec: str) -> None:
        """Store the latest streamed spec without displaying it inline."""
        self._latest_spec = spec
        self._refresh()

    def set_result(self, ok: bool, result_text: str | None = None) -> None:
        if result_text:
            try:
                parsed = json.loads(result_text)
            except (json.JSONDecodeError, TypeError):
                parsed = {}
            if isinstance(parsed, dict) and parsed.get("phase_boundary"):
                self._state = self.STATE_PHASE
                self._refresh()
                return
        self._state = self.STATE_DONE if ok else self.STATE_FAILED
        self._refresh()
