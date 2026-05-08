"""Read-only inline diff display, after the user has decided."""
from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QVBoxLayout

from aura.gui.cards._helpers import _HAVE_PYGMENTS, _mono_font
from aura.gui.diff_dialog import render_unified_diff
from aura.gui.syntax import DiffHighlighter, language_from_path
from aura.gui.theme import BG, BORDER, DANGER, FG, SUCCESS


class DiffCard(QFrame):
    """Read-only inline diff display, after the user has decided."""

    def __init__(self, rel_path: str, old: str, new: str, decision: str, is_new_file: bool) -> None:
        super().__init__()
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(6)

        title_color = {
            "approve": SUCCESS,
            "reject": DANGER,
            "reject_all": DANGER,
        }.get(decision, FG)
        verb = {
            "approve": "Applied",
            "reject": "Rejected",
            "reject_all": "Rejected (all writes in this turn)",
        }.get(decision, decision)
        verb_prefix = "Created" if (is_new_file and decision == "approve") else verb

        title = QLabel(f"{verb_prefix}: {rel_path}")
        title.setStyleSheet(f"color: {title_color}; font-weight: 600;")
        layout.addWidget(title)

        diff_view = QPlainTextEdit()
        diff_view.setReadOnly(True)
        diff_view.setFont(_mono_font(9))
        diff_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        diff_view.setMaximumHeight(360)
        diff_view.setStyleSheet(
            f"background: {BG}; color: {FG}; border: 1px solid {BORDER}; "
            "border-radius: 4px; padding: 6px;"
        )
        if is_new_file:
            text = "\n".join(f"+{line}" for line in new.splitlines())
        else:
            text = render_unified_diff(old, new, rel_path) or "(no textual difference)"

        # Native syntax highlighting via DiffHighlighter
        if _HAVE_PYGMENTS:
            lang = language_from_path(rel_path) or "text"
            self._diff_highlighter = DiffHighlighter(diff_view.document(), lang)

        diff_view.setPlainText(text)
        layout.addWidget(diff_view)
