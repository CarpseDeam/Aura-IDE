from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from aura.gui.theme import BG_ALT, BORDER, FG, FG_DIM, FG_MUTED, WARN

STATE_RESOLVING = "resolving"
STATE_RESOLVED = "resolved"


class MismatchResolutionCard(QFrame):
    """Compact card showing a worker handoff mismatch that needs Planner resolution."""

    def __init__(
        self,
        tool_call_id: str,
        kind: str = "",
        question: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._tool_call_id = tool_call_id
        self._kind = kind
        self._question = question
        self._state = STATE_RESOLVING

        self.setFrameStyle(QFrame.StyledPanel | QFrame.Plain)
        self.setObjectName("mismatchResolutionCard")
        self.setStyleSheet(
            f"QFrame#mismatchResolutionCard {{ background: {BG_ALT}; "
            f"border: 1px solid {BORDER}; border-radius: 8px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        # Title
        self._title_label = QLabel("Worker needs Planner resolution")
        self._title_label.setStyleSheet(
            f"font-weight: 700; font-size: 13px; color: {WARN}; background: transparent; border: none;"
        )
        layout.addWidget(self._title_label)

        # Status
        self._status_label = QLabel("Planner is resolving the handoff.")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent; border: none;")
        layout.addWidget(self._status_label)

        # Kind (hidden by default)
        self._kind_label = QLabel()
        self._kind_label.setStyleSheet(f"color: {FG_MUTED}; font-size: 12px; background: transparent; border: none;")
        self._kind_label.setWordWrap(True)
        self._kind_label.setVisible(False)
        layout.addWidget(self._kind_label)

        # Question (hidden by default)
        self._question_label = QLabel()
        self._question_label.setStyleSheet(f"color: {FG}; font-size: 12px; background: transparent; border: none;")
        self._question_label.setWordWrap(True)
        self._question_label.setVisible(False)
        layout.addWidget(self._question_label)

        # State (right-aligned)
        state_row = QHBoxLayout()
        state_row.setContentsMargins(0, 0, 0, 0)
        state_row.addStretch(1)
        self._state_label = QLabel("Resolving")
        self._state_label.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px; background: transparent; border: none;")
        state_row.addWidget(self._state_label)
        layout.addLayout(state_row)

        self.setMaximumWidth(480)
        self.setMinimumWidth(280)

        self._refresh_text()

    def _refresh_text(self) -> None:
        """Update label visibility and text based on current attributes."""
        if self._kind:
            self._kind_label.setText(f"Mismatch: {self._kind}")
            self._kind_label.setVisible(True)
        else:
            self._kind_label.setVisible(False)

        if self._question:
            self._question_label.setText(f"Question: {self._question}")
            self._question_label.setVisible(True)
        else:
            self._question_label.setVisible(False)

        if self._state == STATE_RESOLVED:
            self._state_label.setText("Resolved")
        else:
            self._state_label.setText("Resolving")

    def update_mismatch(self, kind: str, question: str) -> None:
        """Update the mismatch kind and question text, resetting state to resolving."""
        self._kind = kind
        self._question = question
        self._state = STATE_RESOLVING
        self._refresh_text()

    def mark_resolved(self) -> None:
        """Set state to resolved and update the state label."""
        self._state = STATE_RESOLVED
        self._refresh_text()
