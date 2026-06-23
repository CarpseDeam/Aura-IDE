"""First-run project selection launchpad."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)
from aura.gui.theme import ACCENT, BG_RAISED, BORDER, FG, FG_DIM, FG_MUTED


class _ActionCard(QFrame):
    """Clickable card with icon, title, and description."""

    def __init__(
        self,
        emoji: str,
        title: str,
        description: str,
        *,
        badge_text: str | None = None,
        border_color: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setObjectName("launchpadCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        card_border = border_color or BORDER
        self.setStyleSheet(
            f"#launchpadCard {{"
            f"  background: {BG_RAISED};"
            f"  border: 1px solid {card_border};"
            f"  border-radius: 12px;"
            f"  padding: 24px;"
            f"}}"
            f"#launchpadCard:hover {{"
            f"  border-color: {ACCENT};"
            f"  background: rgba(122, 162, 247, 0.04);"
            f"}}"
        )

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        # Optional badge at top-right corner
        if badge_text:
            badge_row = QHBoxLayout()
            badge_row.setContentsMargins(0, 0, 0, 0)
            badge_row.addStretch()
            badge_lbl = QLabel(badge_text)
            badge_lbl.setStyleSheet(
                f"font-size: 11px; font-weight: 700; color: {ACCENT}; background: transparent;"
            )
            badge_row.addWidget(badge_lbl)
            layout.addLayout(badge_row)

        icon_lbl = QLabel(emoji)
        icon_lbl.setStyleSheet("font-size: 32px; background: transparent;")
        icon_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_lbl)

        title_lbl = QLabel(title)
        title_lbl.setStyleSheet(
            f"font-size: 16px; font-weight: 600; color: {FG}; background: transparent;"
        )
        title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_lbl)

        desc_lbl = QLabel(description)
        desc_lbl.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        desc_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc_lbl.setWordWrap(True)
        layout.addWidget(desc_lbl)

        self._action_btn = QPushButton(title)
        self._action_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._action_btn.setStyleSheet(
            f"QPushButton {{"
            f"  background: {ACCENT}; color: #0a0a0f; border: none;"
            f"  border-radius: 8px; padding: 10px 20px; font-size: 13px; font-weight: 600;"
            f"}}"
            f"QPushButton:hover {{ background: #8bb3f8; }}"
        )
        layout.addWidget(self._action_btn)

    @property
    def action_button(self) -> QPushButton:
        return self._action_btn


class ProjectLaunchpad(QWidget):
    """Full-page launchpad shown when no workspace is selected."""

    open_existing_requested = Signal()
    create_new_requested = Signal()
    create_demo_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Expanding)
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.setSpacing(40)

        # Title area
        title_block = QVBoxLayout()
        title_block.setSpacing(8)

        heading = QLabel("Welcome to Aura")
        heading.setStyleSheet(
            f"font-size: 28px; font-weight: 700; color: {FG}; background: transparent;"
        )
        heading.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_block.addWidget(heading)

        sub = QLabel("Choose where Aura should work")
        sub.setStyleSheet(
            f"font-size: 15px; color: {FG_DIM}; background: transparent;"
        )
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title_block.addWidget(sub)

        explain = QLabel(
            "Aura needs a project folder to build its index, read files, and run tools. "
            "You can change this later from the left sidebar."
        )
        explain.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent; max-width: 480px;"
        )
        explain.setAlignment(Qt.AlignmentFlag.AlignCenter)
        explain.setWordWrap(True)
        title_block.addWidget(explain)

        layout.addLayout(title_block)

        # First-run hints section for new users
        hints_frame = QFrame()
        hints_frame.setStyleSheet(
            f"QFrame {{"
            f"  background: {BG_RAISED};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 8px;"
            f"  padding: 12px;"
            f"}}"
        )
        hints_layout = QVBoxLayout(hints_frame)
        hints_layout.setSpacing(6)

        hint1 = QLabel("\U0001f680 New here? Try the demo project first \u2014 it\'s a safe sandbox.")
        hint1.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent;"
        )
        hint1.setWordWrap(True)
        hint1.setAlignment(Qt.AlignmentFlag.AlignLeft)
        hints_layout.addWidget(hint1)

        hint2 = QLabel("\U0001f4c2 You can also open an existing project folder.")
        hint2.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent;"
        )
        hint2.setWordWrap(True)
        hint2.setAlignment(Qt.AlignmentFlag.AlignLeft)
        hints_layout.addWidget(hint2)

        hint3 = QLabel("\u2699\ufe0f No AI provider yet? You can browse first, then set up Aura Credits in Settings.")
        hint3.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent;"
        )
        hint3.setWordWrap(True)
        hint3.setAlignment(Qt.AlignmentFlag.AlignLeft)
        hints_layout.addWidget(hint3)

        layout.addWidget(hints_frame)

        # Three action cards in a horizontal row
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(20)
        cards_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        open_card = _ActionCard(
            "\U0001f4c2", "Open Existing Project",
            "Choose an existing project folder. Aura will scan and index it."
        )
        open_card.action_button.clicked.connect(self.open_existing_requested.emit)
        open_card.action_button.setToolTip(
            "Choose a specific project folder. Avoid Desktop, Downloads, or your whole Documents folder."
        )
        cards_layout.addWidget(open_card)

        create_card = _ActionCard(
            "\U0001f4dd", "Create New Project",
            "Create an empty folder. Aura will set it up as a workspace."
        )
        create_card.action_button.clicked.connect(self.create_new_requested.emit)
        create_card.action_button.setToolTip("Start from an empty folder.")
        cards_layout.addWidget(create_card)

        demo_card = _ActionCard(
            "\U0001f680", "Try Demo Project",
            "Create a tiny safe demo to try the Planner \u2192 Worker \u2192 Diff \u2192 Validation loop.",
            badge_text="\u2605 Recommended",
            border_color="rgba(122, 162, 247, 0.5)",
        )
        demo_card.action_button.clicked.connect(self.create_demo_requested.emit)
        demo_card.action_button.setToolTip(
            "Create a tiny safe project to test Aura without touching your real code."
        )
        cards_layout.addWidget(demo_card)

        layout.addLayout(cards_layout)
