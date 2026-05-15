"""Onboarding flow for new users — a polished 5-step wizard."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QWidget,
    QFrame,
)

from aura.config import APP_NAME, has_api_key, icon_path
from aura.gui.theme import ACCENT, BG_RAISED, BORDER, FG, FG_DIM, SUCCESS, WARN


MISSION_CARDS = [
    (
        "🔍",
        "Scan this workspace and explain its structure. Do not edit files.",
    ),
    (
        "💡",
        "Review this project and suggest safe improvement opportunities. Do not edit files.",
    ),
    (
        "📝",
        "Create or update a minimal Getting Started section in the README. Show every diff before applying.",
    ),
]

_MISSION_TEXTS = [m[1] for m in MISSION_CARDS]


class _MissionCard(QFrame):
    """A clickable card that represents a single mission option."""

    def __init__(self, emoji: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.mission_text = text
        self._selected = False

        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("missionCard")
        self.setStyleSheet(
            f"QFrame#missionCard {{"
            f"  background: {BG_RAISED};"
            f"  border: 2px solid {BORDER};"
            f"  border-radius: 8px;"
            f"  padding: 12px;"
            f"}}"
            f"QFrame#missionCard:hover {{"
            f"  border-color: {ACCENT};"
            f"}}"
        )

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        emoji_label = QLabel(emoji)
        emoji_label.setStyleSheet("font-size: 24px; background: transparent;")
        emoji_label.setFixedWidth(36)
        emoji_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(emoji_label)

        text_label = QLabel(text)
        text_label.setWordWrap(True)
        text_label.setStyleSheet(f"color: {FG}; font-size: 13px; background: transparent;")
        layout.addWidget(text_label, 1)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        border_color = ACCENT if selected else BORDER
        self.setStyleSheet(
            f"QFrame#missionCard {{"
            f"  background: {BG_RAISED};"
            f"  border: 2px solid {border_color};"
            f"  border-radius: 8px;"
            f"  padding: 12px;"
            f"}}"
            f"QFrame#missionCard:hover {{"
            f"  border-color: {ACCENT};"
            f"}}"
        )

    def mousePressEvent(self, event) -> None:
        super().mousePressEvent(event)
        # Notify parent dialog via the parent widget
        p = self.parent()
        while p is not None:
            if isinstance(p, OnboardingDialog):
                p._select_mission(self.mission_text)
                return
            p = p.parent()


class OnboardingDialog(QDialog):
    """A polished 5-step wizard for first-time Aura users.

    After acceptance (Finish), ``selected_mission_text`` holds the
    user-chosen first mission prompt string.
    """

    def __init__(
        self,
        parent: QWidget | None = None,
        workspace_path: str = "",
        on_change_workspace: Callable[[], str | None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.selected_mission_text: str = _MISSION_TEXTS[0]
        self.open_settings_requested = False
        self._workspace_path = workspace_path
        self._on_change_workspace = on_change_workspace

        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setWindowIcon(QIcon(str(icon_path())))
        self.setFixedSize(680, 540)

        # Dark background
        self.setStyleSheet("QDialog { background: #0f111a; }")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(40, 30, 40, 20)
        layout.setSpacing(16)

        # Step stack
        self._stack = QStackedWidget()
        self._setup_steps()
        layout.addWidget(self._stack, 1)

        # Dot indicators
        self._dots_label = QLabel("")
        self._dots_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._dots_label)

        # Bottom navigation
        nav = QHBoxLayout()
        nav.setSpacing(12)

        self._back_btn = QPushButton("Back")
        self._back_btn.setFixedWidth(90)
        self._back_btn.clicked.connect(self._on_back)
        self._back_btn.setVisible(False)
        nav.addWidget(self._back_btn)

        nav.addStretch(1)

        self._next_btn = QPushButton("Next")
        self._next_btn.setObjectName("primary")
        self._next_btn.setFixedWidth(100)
        self._next_btn.clicked.connect(self._on_next)
        nav.addWidget(self._next_btn)

        layout.addLayout(nav)

        self._update_dots()

    # ---- step construction ------------------------------------------------

    def _setup_steps(self) -> None:
        self._stack.addWidget(self._step_welcome())
        self._stack.addWidget(self._step_workspace())
        self._stack.addWidget(self._step_safety())
        self._stack.addWidget(self._step_provider())
        self._stack.addWidget(self._step_mission())

    def _make_title(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: 22px; font-weight: 700; color: {ACCENT}; background: transparent;")
        lbl.setWordWrap(True)
        return lbl

    def _make_body(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"font-size: 13px; color: {FG}; background: transparent;")
        lbl.setWordWrap(True)
        return lbl

    def _make_card(self, text: str, color: str, bold_prefix: str = "") -> QFrame:
        card = QFrame()
        card.setObjectName("infoCard")
        card.setStyleSheet(
            f"QFrame#infoCard {{"
            f"  background: {BG_RAISED};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 8px;"
            f"  padding: 12px;"
            f"}}"
        )
        cl = QHBoxLayout(card)
        cl.setContentsMargins(12, 10, 12, 10)
        indicator = QLabel("●")
        indicator.setStyleSheet(f"color: {color}; font-size: 14px; background: transparent;")
        indicator.setFixedWidth(20)
        cl.addWidget(indicator)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {FG}; font-size: 12px; background: transparent;")
        if bold_prefix:
            label.setText(f"<b>{bold_prefix}</b> {text}")
        cl.addWidget(label, 1)
        return card

    # Step 1: Welcome
    def _step_welcome(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(16)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # Logo
        logo = QLabel()
        px = QPixmap(str(icon_path())).scaled(
            96, 96, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
        )
        logo.setPixmap(px)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setStyleSheet("background: transparent;")
        layout.addWidget(logo)

        layout.addSpacing(8)

        title = self._make_title(f"Welcome to {APP_NAME}")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        body = self._make_body(
            f"{APP_NAME} is an AI coding assistant that reads your project, "
            "plans changes with a <b>Planner</b> model, and executes them with "
            "a <b>Worker</b> model. Every file edit is reviewed by you before "
            "it's applied."
        )
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(body)

        layout.addStretch(1)
        return w

    # Step 2: Workspace
    def _step_workspace(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 10, 0, 0)

        title = self._make_title("Your Workspace")
        layout.addWidget(title)

        body = self._make_body(
            f"{APP_NAME} works inside a project folder, builds a search index, "
            "and reads/writes files there. You can change the workspace at any time."
        )
        layout.addWidget(body)

        layout.addSpacing(8)

        # Workspace path display
        path_frame = QFrame()
        path_frame.setObjectName("pathFrame")
        path_frame.setStyleSheet(
            f"QFrame#pathFrame {{"
            f"  background: {BG_RAISED};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 8px;"
            f"  padding: 12px;"
            f"}}"
        )
        path_layout = QHBoxLayout(path_frame)
        path_layout.setContentsMargins(12, 10, 12, 10)

        self._path_label = QLabel(self._workspace_path or "(none)")
        self._path_label.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        self._path_label.setWordWrap(True)
        path_layout.addWidget(self._path_label, 1)

        change_btn = QPushButton("Change...")
        change_btn.setFixedWidth(80)
        change_btn.clicked.connect(self._on_change_workspace)
        path_layout.addWidget(change_btn)

        layout.addWidget(path_frame)

        layout.addStretch(1)
        return w

    def _on_change_workspace(self) -> None:
        if self._on_change_workspace is not None:
            new_path = self._on_change_workspace()
            if new_path is not None:
                self._workspace_path = new_path
                self._path_label.setText(new_path)

    # Step 3: Safety
    def _step_safety(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 10, 0, 0)

        title = self._make_title("Safety & Control")
        layout.addWidget(title)

        body = self._make_body(
            f"{APP_NAME} is designed with safety at its core. Here's how your code is protected:"
        )
        layout.addWidget(body)

        layout.addSpacing(4)

        # Card 1: Diff Approval (safe, default ON)
        card1 = self._make_card(
            "Before any file is edited, Aura shows you the exact diff. "
            "You approve or reject every change.",
            SUCCESS,
            bold_prefix="Diff Approval  —  ",
        )
        layout.addWidget(card1)

        # Card 2: Auto-Approve (advanced, default OFF)
        card2 = self._make_card(
            "Skips diff review. Faster, but you won't see changes before "
            "they're applied. Recommended for experienced users only.",
            WARN,
            bold_prefix="Auto-Approve  —  ",
        )
        layout.addWidget(card2)

        # Card 3: Auto-Dispatch (advanced, default OFF)
        card3 = self._make_card(
            "Sends specs directly to the Worker without your confirmation. "
            "Faster but less guided.",
            WARN,
            bold_prefix="Auto-Dispatch  —  ",
        )
        layout.addWidget(card3)

        # Card 4: Git Checkpoints
        card4 = self._make_card(
            "Aura auto-commits before every change so you can always undo.",
            SUCCESS,
            bold_prefix="Git Checkpoints  —  ",
        )
        layout.addWidget(card4)

        note = QLabel("You can change these anytime in Settings.")
        note.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; background: transparent;")
        note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(note)

        layout.addStretch(1)
        return w

    # Step 4: AI Provider
    def _step_provider(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 10, 0, 0)

        title = self._make_title("AI Provider Setup")
        layout.addWidget(title)

        body = self._make_body(
            f"{APP_NAME} supports multiple AI providers. By default it uses "
            "<b>DeepSeek</b> for the best cost-to-performance ratio."
        )
        layout.addWidget(body)

        layout.addSpacing(8)

        # API key status card
        status_frame = QFrame()
        status_frame.setObjectName("statusFrame")
        status_frame.setStyleSheet(
            f"QFrame#statusFrame {{"
            f"  background: {BG_RAISED};"
            f"  border: 1px solid {BORDER};"
            f"  border-radius: 8px;"
            f"  padding: 16px;"
            f"}}"
        )
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(16, 14, 16, 14)

        has_key = has_api_key()
        if has_key:
            indicator_color = SUCCESS
            status_text = "✓  API key configured"
        else:
            indicator_color = WARN
            status_text = "No API key found — you can add one in Settings"

        indicator = QLabel("●")
        indicator.setStyleSheet(f"color: {indicator_color}; font-size: 18px; background: transparent;")
        indicator.setFixedWidth(24)
        status_layout.addWidget(indicator)

        status_label = QLabel(status_text)
        status_label.setStyleSheet(f"color: {FG}; font-size: 13px; background: transparent;")
        status_layout.addWidget(status_label, 1)

        layout.addWidget(status_frame)

        layout.addSpacing(8)

        open_settings_btn = QPushButton("Open Settings")
        open_settings_btn.clicked.connect(self._on_open_settings)
        layout.addWidget(open_settings_btn, 0, Qt.AlignmentFlag.AlignCenter)

        layout.addStretch(1)
        return w

    # Step 5: First Mission
    def _step_mission(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setSpacing(12)
        layout.setContentsMargins(0, 10, 0, 0)

        title = self._make_title("Your First Mission")
        layout.addWidget(title)

        subtitle = self._make_body(
            "Choose a safe first prompt to get started. Aura will place it in "
            "the chat — you decide when to send it."
        )
        layout.addWidget(subtitle)

        layout.addSpacing(8)

        self._mission_cards: list[_MissionCard] = []
        for i, (emoji, text) in enumerate(MISSION_CARDS):
            card = _MissionCard(emoji, text, parent=w)
            if i == 0:
                card.set_selected(True)
            self._mission_cards.append(card)
            layout.addWidget(card)

        layout.addStretch(1)
        return w

    def _select_mission(self, mission_text: str) -> None:
        self.selected_mission_text = mission_text
        for card in self._mission_cards:
            card.set_selected(card.mission_text == mission_text)

    def _on_open_settings(self) -> None:
        self.open_settings_requested = True
        self.reject()

    # ---- navigation -------------------------------------------------------

    def _on_next(self) -> None:
        idx = self._stack.currentIndex()
        if idx < self._stack.count() - 1:
            self._stack.setCurrentIndex(idx + 1)
            self._back_btn.setVisible(True)
            self._update_dots()
            if idx + 1 == self._stack.count() - 1:
                self._next_btn.setText("Finish")
        else:
            self.accept()

    def _on_back(self) -> None:
        idx = self._stack.currentIndex()
        if idx > 0:
            self._stack.setCurrentIndex(idx - 1)
            self._next_btn.setText("Next")
            if idx - 1 == 0:
                self._back_btn.setVisible(False)
            self._update_dots()

    def _update_dots(self) -> None:
        count = self._stack.count()
        idx = self._stack.currentIndex()
        dots = []
        for i in range(count):
            if i == idx:
                dots.append(f"<span style='color: {ACCENT}; font-size: 20px;'>●</span>")
            else:
                dots.append(f"<span style='color: {FG_DIM}; font-size: 20px;'>●</span>")
        self._dots_label.setText(" ".join(dots))
