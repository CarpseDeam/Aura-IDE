"""Summary card shown after a worker dispatch completes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BG_ALT, DANGER, FG, FG_DIM, SUCCESS


class WorkerSummaryCard(QFrame):
    """A card displayed in the chat after a worker finishes execution.

    Shows a status header (success/failure icon), the original goal,
    and a rendered summary of what the worker accomplished.
    """

    def __init__(
        self, tool_call_id: str, goal: str, ok: bool, summary: str,
        needs_followup: bool = False, parent=None
    ) -> None:
        super().__init__(parent)
        self.tool_call_id = tool_call_id

        self.setObjectName("workerSummaryCard")
        self.setStyleSheet(
            f"QFrame#workerSummaryCard {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {SUCCESS if ok else DANGER}; "
            f"border-radius: 8px; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Header — pick label based on status
        if ok:
            header_text = "✅ Worker completed"
        elif needs_followup:
            header_text = "⚠️ Worker needs follow-up"
        else:
            header_text = "❌ Worker failed"
        header = QLabel(header_text)
        header.setStyleSheet(
            f"color: {SUCCESS if ok else DANGER}; "
            f"font-weight: 700; font-size: 12px;"
        )
        layout.addWidget(header)

        # Goal (dim, italic)
        if goal:
            goal_label = QLabel(goal)
            goal_label.setWordWrap(True)
            goal_label.setStyleSheet(f"color: {FG_DIM}; font-style: italic;")
            layout.addWidget(goal_label)

        # Summary (rich markdown)
        if summary:
            body = QLabel()
            body.setWordWrap(True)
            body.setTextFormat(Qt.TextFormat.RichText)
            body.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
            )
            body.setText(_render_markdown_with_code(summary, color=FG))
            layout.addWidget(body)
