"""Plan Review card — the "Plan Ready" inline chat presentation.

Visual reference: the historical Aura SpecCard
(``git show 5ae8533c867dc5c0f14cdf97b5030c6a5493b165:aura/gui/cards/spec_card.py``).
This is a focused rewrite for the model-facing ``review_implementation_plan``
tool: no Worker/dispatch terminology, no dispatch state, and no Fast/Careful
Plan or risk heuristics — Plan Review has exactly one policy, the user's Plan
toggle, so there is nothing left for a classifier to guess.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.cards._helpers import _MarkdownTextBlock
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import (
    ACCENT,
    ACCENT_HOVER,
    BG_ALT,
    BG_RAISED,
    BORDER,
    BORDER_STRONG,
    DANGER,
    FG,
    FG_DIM,
    FG_MUTED,
    SUCCESS,
)

_IMPLEMENT_BUTTON_STYLE = f"""
QPushButton#planImplementPrimary {{
    background: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT_HOVER};
    border-radius: 5px;
    padding: 6px 14px;
    font-weight: 700;
}}
QPushButton#planImplementPrimary:hover {{
    background: {ACCENT_HOVER};
    color: #ffffff;
    border-color: #c2d4ff;
}}
QPushButton#planImplementPrimary:pressed {{
    background: #5f89dc;
    color: #ffffff;
    border-color: {ACCENT};
}}
QPushButton#planImplementPrimary:disabled {{
    background: {BG_RAISED};
    color: {FG_MUTED};
    border-color: {BORDER_STRONG};
}}
"""


class PlanReviewCard(QFrame):
    """Inline "Plan Ready" card: Implement / Edit Plan / Cancel.

    Emits the review id (not a tool_call_id — Plan Review is a human
    interaction, not a dispatch) on each button so the controller can resolve
    the matching pending review through the proxy.
    """

    implement_clicked = Signal(str)  # review_id (current values)
    edit_clicked = Signal(str)       # review_id
    cancel_clicked = Signal(str)     # review_id

    def __init__(
        self,
        review_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._review_id = review_id
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._summary = summary
        self._resolved = False

        self.setStyleSheet(
            f"QFrame#card {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {ACCENT}; border-radius: 8px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(8)

        header = QLabel("⚡ Plan Ready", parent=self)
        header.setStyleSheet(f"color: {ACCENT}; font-weight: 700; font-size: 12px;")
        outer.addWidget(header)

        self._goal_label = _MarkdownTextBlock(_render_markdown_with_code(self._goal), parent=self)
        self._goal_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG}; font-size: 14px;"
        )
        outer.addWidget(self._goal_label)

        outer.addSpacing(6)
        outer.addWidget(self._make_section_header("STRATEGY"))
        self._strategy_label = _MarkdownTextBlock(
            _render_markdown_with_code(self._strategy_text()), parent=self
        )
        self._strategy_label.setStyleSheet(f"background: transparent; border: none; color: {FG};")
        outer.addWidget(self._strategy_label)

        outer.addSpacing(6)
        outer.addWidget(self._make_section_header("SCOPE"))
        self._files_container = QWidget(self)
        files_layout = QVBoxLayout(self._files_container)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.setSpacing(4)
        self._refresh_files_list(files_layout)
        outer.addWidget(self._files_container)

        outer.addSpacing(6)
        outer.addWidget(self._make_section_header("VALIDATION"))
        self._acceptance_label = _MarkdownTextBlock(
            _render_markdown_with_code(self._acceptance), parent=self
        )
        self._acceptance_label.setStyleSheet(f"background: transparent; border: none; color: {FG_DIM};")
        outer.addWidget(self._acceptance_label)

        outer.addSpacing(6)
        self._spec_body_label = _MarkdownTextBlock(_render_markdown_with_code(self._spec), parent=self)
        self._spec_body_label.setStyleSheet(f"background: transparent; border: none; color: {FG};")
        self._full_plan_section = _CollapsibleSection(
            "Show Full Plan", self._spec_body_label, start_open=False, prominent=False,
        )
        self._full_plan_section._toggle.clicked.connect(self._on_full_plan_toggled)
        outer.addWidget(self._full_plan_section)

        (
            self._buttons_row,
            self._implement_btn,
            self._edit_btn,
            self._cancel_btn,
        ) = self._build_button_row()
        outer.addWidget(self._buttons_row)

        self._status_label = QLabel("", parent=self)
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(False)
        outer.addWidget(self._status_label)

        self._status_label.setText("Plan ready — waiting for review.")
        self._status_label.setVisible(True)

    # ---- layout helpers ---------------------------------------------------

    def _make_section_header(self, text: str) -> QLabel:
        header = QLabel(text)
        header.setStyleSheet(f"color: {FG_MUTED}; font-weight: 700; font-size: 10px;")
        return header

    def _on_full_plan_toggled(self) -> None:
        self._full_plan_section.set_title(
            "Hide Full Plan" if self._full_plan_section._open else "Show Full Plan"
        )

    def _strategy_text(self) -> str:
        if self._summary:
            return self._summary
        for line in self._spec.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                return line[:300] + "…" if len(line) > 300 else line
        return "No summary available."

    def _refresh_files_list(self, layout: QVBoxLayout) -> None:
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if not self._files:
            lbl = QLabel("Files will be discovered as needed", parent=self._files_container)
            lbl.setStyleSheet(f"color: {FG_MUTED}; font-style: italic; font-size: 11px;")
            layout.addWidget(lbl)
            return

        for path in self._files:
            lbl = QLabel(f"• {path}", parent=self._files_container)
            lbl.setStyleSheet(
                f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
                f"border-radius: 4px; padding: 2px 8px; "
                f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
                f"font-size: 11px;"
            )
            lbl.setToolTip(path)
            layout.addWidget(lbl)

    def _build_button_row(self) -> tuple[QWidget, QPushButton, QPushButton, QPushButton]:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        implement_btn = QPushButton("Implement", parent=row)
        implement_btn.setObjectName("planImplementPrimary")
        implement_btn.setStyleSheet(_IMPLEMENT_BUTTON_STYLE)
        implement_btn.setMinimumHeight(34)
        implement_btn.setMinimumWidth(128)
        implement_btn.clicked.connect(self._on_implement)
        layout.addWidget(implement_btn)

        edit_btn = QPushButton("Edit Plan", parent=row)
        edit_btn.setMinimumHeight(32)
        edit_btn.setMinimumWidth(96)
        edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._review_id))
        layout.addWidget(edit_btn)

        layout.addStretch(1)

        cancel_btn = QPushButton("Cancel", parent=row)
        cancel_btn.setObjectName("danger")
        cancel_btn.setMinimumHeight(32)
        cancel_btn.setMinimumWidth(88)
        cancel_btn.clicked.connect(self._on_cancel)
        layout.addWidget(cancel_btn)

        return row, implement_btn, edit_btn, cancel_btn

    # ---- public API ---------------------------------------------------

    def review_id(self) -> str:
        return self._review_id

    def current_plan(self) -> tuple[str, list[str], str, str, str]:
        """Return (goal, files, spec, acceptance, summary)."""
        return (self._goal, list(self._files), self._spec, self._acceptance, self._summary)

    def update_plan(
        self, goal: str, files: list[str], spec: str, acceptance: str, summary: str
    ) -> None:
        """Apply user edits and refresh every derived display."""
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._summary = summary
        self._goal_label.setHtml(_render_markdown_with_code(self._goal))
        self._strategy_label.setHtml(_render_markdown_with_code(self._strategy_text()))
        self._acceptance_label.setHtml(_render_markdown_with_code(self._acceptance))
        self._spec_body_label.setHtml(_render_markdown_with_code(self._spec))
        self._refresh_files_list(self._files_container.layout())

    def is_resolved(self) -> bool:
        return self._resolved

    # ---- button handlers ------------------------------------------------

    def _on_implement(self) -> None:
        self._resolved = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Plan approved — implementing…")
        self._status_label.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.implement_clicked.emit(self._review_id)

    def _on_cancel(self) -> None:
        self._resolved = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Cancelled")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.cancel_clicked.emit(self._review_id)

    def show_resolved(self, *, approved: bool) -> None:
        """Render a durable resolved state without emitting a signal.

        Used both right after a live decision settles and for non-interactive
        conversation replay — replay never resumes execution, it only ever
        shows a status a human already decided.
        """
        self._resolved = True
        self._buttons_row.setVisible(False)
        if approved:
            self._status_label.setText("Plan approved")
            self._status_label.setStyleSheet(f"color: {SUCCESS}; font-size: 11px;")
        else:
            self._status_label.setText("Cancelled")
            self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)


__all__ = ["PlanReviewCard"]
