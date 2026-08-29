"""Agents page — the feature shell opened from the rail's Agents entry.

This is deliberately a placeholder: it states what the surface is for and
nothing else. No roster, no editor, no execution. Real Agents behaviour
lands in a later phase and replaces the body of this page.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from aura.gui.theme import BG, BORDER, FG, FG_DIM, FG_MUTED


class AgentsPage(QDialog):
    """Modeless Agents window."""

    visibility_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("agentsPage")
        self.setWindowTitle("Agents")
        self.setModal(False)
        self.setMinimumSize(360, 220)
        self.resize(420, 260)
        self.setStyleSheet(
            f"QDialog#agentsPage {{ background: {BG}; border: 1px solid {BORDER}; }}"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        title = QLabel("Agents")
        title_font = QFont()
        title_font.setBold(True)
        title_font.setPixelSize(15)
        title.setFont(title_font)
        title.setStyleSheet(f"color: {FG}; background: transparent;")
        layout.addWidget(title)

        subtitle = QLabel(
            "Named helpers Aura can hand a scoped piece of work to."
        )
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(
            f"color: {FG_DIM}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(subtitle)

        layout.addStretch(1)

        note = QLabel("Nothing to configure yet.")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        note.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 12px; background: transparent;"
        )
        layout.addWidget(note)

        layout.addStretch(1)

    def is_open(self) -> bool:
        return self.isVisible()

    def showEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().showEvent(event)
        self.visibility_changed.emit(True)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt naming
        super().closeEvent(event)
        self.visibility_changed.emit(False)

    def hideEvent(self, event) -> None:  # noqa: N802 - Qt naming
        super().hideEvent(event)
        self.visibility_changed.emit(False)
