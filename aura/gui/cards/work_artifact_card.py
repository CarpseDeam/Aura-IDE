"""WorkArtifactCard — shows the visible Work Artifact with item statuses.

Displays artifact goal, constraints, allowed files, work items with statuses,
and receipt summaries. Provides a single run-level view without individual
dispatchable units.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from aura.gui.cards._helpers import _MarkdownTextBlock
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import ACCENT, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM, FG_MUTED, SUCCESS
from aura.work_artifact.projection import WorkArtifactProjection

_ALLOWED_DISPLAY_STATUSES = {"pending", "active", "done"}


def _normalize_status(status: str) -> str:
    """Map any status to one of the three allowed display statuses."""
    return status if status in _ALLOWED_DISPLAY_STATUSES else "pending"


_ITEM_STATUS_COLORS = {
    "pending": FG_DIM,
    "active": ACCENT,
    "done": SUCCESS,
}


class WorkArtifactCard(QFrame):
    """Visible Work Artifact card — shows multi-item work breakdown."""

    review_requested = Signal(str)  # Kept for compatibility but not emitted.

    def __init__(
        self,
        projection: WorkArtifactProjection,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("artifact_card")
        self._projection = projection
        self._tool_call_id = projection.artifact_id

        self.setStyleSheet(
            f"QFrame#artifact_card {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {ACCENT}; border-radius: 8px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(8)

        # ── Header ──
        header = QLabel("📋 Work Artifact")
        header.setStyleSheet(
            f"color: {ACCENT}; font-weight: 700; font-size: 12px; "
            f"background: transparent; border: none;"
        )
        outer.addWidget(header)

        # ── Goal ──
        goal_label = _MarkdownTextBlock(
            _render_markdown_with_code(projection.goal), parent=self
        )
        goal_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG}; font-size: 14px;"
        )
        outer.addWidget(goal_label)

        # ── Summary row with progress counts ──
        summary_text = self._build_summary_text(projection)
        self._summary_label = QLabel(summary_text)
        self._summary_label.setStyleSheet(
            f"color: {FG_DIM}; font-size: 11px; background: transparent; border: none;"
        )
        outer.addWidget(self._summary_label)

        # ── Constraints section ──
        if projection.constraints:
            outer.addSpacing(4)
            constraints_header = QLabel("CONSTRAINTS")
            constraints_header.setStyleSheet(
                f"color: {FG_MUTED}; font-weight: 700; font-size: 10px; "
                f"background: transparent; border: none;"
            )
            outer.addWidget(constraints_header)
            for constraint in projection.constraints:
                c_label = QLabel(f"• {constraint}")
                c_label.setStyleSheet(
                    f"color: {FG_DIM}; font-size: 11px; "
                    f"background: transparent; border: none;"
                )
                outer.addWidget(c_label)

        # ── Allowed files section ──
        if projection.allowed_files:
            outer.addSpacing(4)
            files_header = QLabel("ALLOWED FILES")
            files_header.setStyleSheet(
                f"color: {FG_MUTED}; font-weight: 700; font-size: 10px; "
                f"background: transparent; border: none;"
            )
            outer.addWidget(files_header)
            for f_path in projection.allowed_files:
                f_label = QLabel(f"📄 {f_path}")
                f_label.setStyleSheet(
                    f"color: {FG_DIM}; font-size: 11px; font-family: "
                    f"'Geist Mono', 'JetBrains Mono', monospace; "
                    f"background: transparent; border: none;"
                )
                outer.addWidget(f_label)

        # ── Work items list ──
        outer.addSpacing(4)
        items_header = QLabel("WORK ITEMS")
        items_header.setStyleSheet(
            f"color: {FG_MUTED}; font-weight: 700; font-size: 10px; "
            f"background: transparent; border: none;"
        )
        outer.addWidget(items_header)

        self._item_widgets: list[QFrame] = []
        for item_dict in projection.items:
            item_frame = self._build_item_row(item_dict)
            self._item_widgets.append(item_frame)
            outer.addWidget(item_frame)

    # ── building blocks ───────────────────────────────────────────────────

    @staticmethod
    def _build_summary_text(projection: WorkArtifactProjection) -> str:
        """Build a progress summary line from projection counts."""
        done = projection.completed_count
        active = projection.active_count
        pending = projection.pending_count
        total = done + active + pending

        if projection.is_complete:
            return f"✅ {done}/{total} done"
        if done > 0 or active > 0:
            parts = [f"{done}/{total} done"]
            if active:
                parts.append(f"{active} active")
            if pending:
                parts.append(f"{pending} pending")
            return "▶ " + ", ".join(parts)
        return f"⏳ {total} pending" if total > 0 else "⏳ Pending"

    def _build_item_row(self, item_dict: dict[str, Any]) -> QFrame:
        """Build a horizontal row for one work item using its own status."""
        frame = QFrame(self)
        raw_status = str(item_dict.get("status", "pending"))
        status = _normalize_status(raw_status)
        color = _ITEM_STATUS_COLORS.get(status, FG_DIM)
        status_icon = {
            "pending": "⏳",
            "active": "▶",
            "done": "✅",
        }.get(status, "⏳")

        frame.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-left: 3px solid {color}; border-radius: 4px;"
        )

        row = QHBoxLayout(frame)
        row.setContentsMargins(8, 6, 8, 6)

        icon_label = QLabel(status_icon)
        icon_label.setStyleSheet(
            f"color: {color}; font-size: 12px; background: transparent; border: none;"
        )
        row.addWidget(icon_label)

        title = str(item_dict.get("title", ""))
        title_label = QLabel(title)
        title_label.setStyleSheet(
            f"color: {FG}; font-size: 11px; font-weight: 600; "
            f"background: transparent; border: none;"
        )
        row.addWidget(title_label)

        row.addStretch(1)

        return frame

    # ── public API ────────────────────────────────────────────────────────

    def update_projection(self, projection: WorkArtifactProjection) -> None:
        """Update the card with a new projection snapshot — rebuilds item rows and summary."""
        self._projection = projection

        # Update summary with progress counts
        self._summary_label.setText(self._build_summary_text(projection))

        # Tear down old item widgets
        layout = self.layout()
        for widget in self._item_widgets:
            layout.removeWidget(widget)
            widget.deleteLater()
        self._item_widgets.clear()

        # Rebuild item rows from the new projection
        for item_dict in projection.items:
            frame = self._build_item_row(item_dict)
            self._item_widgets.append(frame)
            layout.addWidget(frame)

    def tool_call_id(self) -> str:
        return self._tool_call_id
