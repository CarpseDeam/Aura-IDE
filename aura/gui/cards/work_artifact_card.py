"""WorkArtifactCard — shows the visible Work Artifact with item statuses.

Displays artifact goal, constraints, allowed files, work items with statuses,
current item, and receipt summaries. Provides a "Review current item" action
but does not dispatch Worker directly — opens SpecCard for user review first.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from aura.gui.cards._helpers import _MarkdownTextBlock
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import ACCENT, BG_ALT, BG_RAISED, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN
from aura.work_artifact.projection import WorkArtifactProjection

_ITEM_STATUS_COLORS = {
    "pending": FG_DIM,
    "active": ACCENT,
    "done": SUCCESS,
    "blocked": DANGER,
}


class WorkArtifactCard(QFrame):
    """Visible Work Artifact card — shows multi-item work breakdown."""

    review_requested = Signal(str)  # tool_call_id — user wants to review the current item

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

        # ── Summary row ──
        summary_text = (
            f"{projection.completed_count} done · "
            f"{projection.blocked_count} blocked · "
            f"{projection.active_count} active · "
            f"{projection.pending_count} pending"
        )
        if projection.is_complete:
            summary_text += " · ✅ Complete"
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

        # ── Review/advance button ──
        outer.addSpacing(8)
        self._review_btn = QPushButton("Review current item", parent=self)
        self._review_btn.setObjectName("primary")
        self._review_btn.setMinimumHeight(34)
        self._review_btn.clicked.connect(self._on_review_clicked)
        outer.addWidget(self._review_btn)

        self._update_button_state()

    # ── building blocks ───────────────────────────────────────────────────

    def _build_item_row(self, item_dict: dict[str, Any]) -> QFrame:
        """Build a horizontal row for one work item."""
        frame = QFrame(self)
        status = str(item_dict.get("status", "pending"))
        color = _ITEM_STATUS_COLORS.get(status, FG_DIM)
        status_icon = {
            "pending": "⏳",
            "active": "▶",
            "done": "✅",
            "blocked": "❌",
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

        status_label = QLabel(status.upper())
        status_label.setStyleSheet(
            f"color: {color}; font-size: 10px; font-weight: 700; "
            f"background: transparent; border: none;"
        )
        row.addWidget(status_label)

        # If it's the current item, add a marker
        item_id = str(item_dict.get("id", ""))
        if item_id == self._projection.current_item_id:
            current_marker = QLabel("← current")
            current_marker.setStyleSheet(
                f"color: {ACCENT}; font-size: 10px; font-weight: 600; "
                f"background: transparent; border: none;"
            )
            row.addWidget(current_marker)

        return frame

    def _on_review_clicked(self) -> None:
        """Emit review_requested signal to open SpecCard for current item."""
        self.review_requested.emit(self._tool_call_id)

    def _update_button_state(self) -> None:
        """Enable/disable review button based on artifact state."""
        if self._projection.is_complete:
            self._review_btn.setText("All items complete")
            self._review_btn.setEnabled(False)
        elif not self._projection.current_item_id:
            self._review_btn.setText("No current item")
            self._review_btn.setEnabled(False)
        else:
            self._review_btn.setText("Review current item")
            self._review_btn.setEnabled(True)

    # ── public API ────────────────────────────────────────────────────────

    def update_projection(self, projection: WorkArtifactProjection) -> None:
        """Update the card with a new projection snapshot — rebuilds item rows and summary."""
        self._projection = projection

        # Update summary counts
        summary_text = (
            f"{projection.completed_count} done · "
            f"{projection.blocked_count} blocked · "
            f"{projection.active_count} active · "
            f"{projection.pending_count} pending"
        )
        if projection.is_complete:
            summary_text += " · ✅ Complete"
        self._summary_label.setText(summary_text)

        # Tear down old item widgets
        layout = self.layout()
        for widget in self._item_widgets:
            layout.removeWidget(widget)
            widget.deleteLater()
        self._item_widgets.clear()

        # Find insertion point — just before the review button
        btn_idx = -1
        for i in range(layout.count()):
            if layout.itemAt(i) and layout.itemAt(i).widget() is self._review_btn:
                btn_idx = i
                break

        # Rebuild item rows from the new projection
        if btn_idx >= 0:
            insert_at = btn_idx
            for item_dict in projection.items:
                frame = self._build_item_row(item_dict)
                self._item_widgets.append(frame)
                layout.insertWidget(insert_at, frame)
                insert_at += 1
        else:
            # Fallback — shouldn't happen, but just append
            for item_dict in projection.items:
                frame = self._build_item_row(item_dict)
                self._item_widgets.append(frame)
                layout.addWidget(frame)

        self._update_button_state()

    def tool_call_id(self) -> str:
        return self._tool_call_id
