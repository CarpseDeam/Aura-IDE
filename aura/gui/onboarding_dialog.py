"""Onboarding flow for new users — Quick Start tips and setup guide."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QWidget,
    QFrame
)

from aura.config import APP_NAME, icon_path, media_path
from aura.gui.theme import ACCENT, BG_RAISED, BORDER, FG, FG_DIM, SUCCESS


class OnboardingDialog(QDialog):
    """A professional multi-step welcome guide for first-time users."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Welcome to {APP_NAME}")
        self.setWindowIcon(QIcon(str(icon_path())))
        self.setFixedSize(600, 450)
        
        # Glass-morphism style
        self.setStyleSheet(f"""
            QDialog {{ background: #0f111a; }}
            QLabel {{ color: {FG}; }}
            #paneTitle {{ font-size: 22px; font-weight: 700; color: {ACCENT}; }}
            #stepText {{ font-size: 14px; line-height: 1.5; color: {FG}; }}
            #tipBox {{ background: {BG_RAISED}; border: 1px solid {BORDER}; border-radius: 8px; padding: 12px; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 30, 30, 20)
        layout.setSpacing(20)

        # Step stack
        self._stack = QStackedWidget()
        self._setup_steps()
        layout.addWidget(self._stack, 1)

        # Bottom navigation
        nav = QHBoxLayout()
        
        self._back_btn = QPushButton("Back")
        self._back_btn.setFixedWidth(80)
        self._back_btn.clicked.connect(self._on_back)
        self._back_btn.setVisible(False)
        nav.addWidget(self._back_btn)
        
        nav.addStretch(1)

        self._dots = QLabel("")
        self._update_dots()
        nav.addWidget(self._dots)

        nav.addStretch(1)

        self._next_btn = QPushButton("Next")
        self._next_btn.setFixedWidth(80)
        self._next_btn.setStyleSheet(f"background: {ACCENT}; color: white; font-weight: 600; border-radius: 4px; padding: 6px;")
        self._next_btn.clicked.connect(self._on_next)
        nav.addWidget(self._next_btn)

        layout.addLayout(nav)

    def _setup_steps(self) -> None:
        # Step 1: Welcome
        s1 = QWidget()
        l1 = QVBoxLayout(s1)
        l1.setSpacing(16)
        
        title = QLabel(f"Welcome to {APP_NAME}")
        title.setObjectName("paneTitle")
        l1.addWidget(title, 0, Qt.AlignmentFlag.AlignCenter)
        
        desc = QLabel(
            "Aura is a desktop AI Orchestration IDE designed to help you "
            "build, debug, and understand complex codebases with ease."
        )
        desc.setObjectName("stepText")
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l1.addWidget(desc)

        # Large icon/logo placeholder
        logo = QLabel()
        px = QPixmap(str(icon_path())).scaled(128, 128, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        logo.setPixmap(px)
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        l1.addWidget(logo, 1)

        self._stack.addWidget(s1)

        # Step 2: API Keys
        s2 = QWidget()
        l2 = QVBoxLayout(s2)
        l2.setSpacing(12)
        
        t2 = QLabel("1. Setup API Keys")
        t2.setObjectName("paneTitle")
        l2.addWidget(t2)
        
        d2 = QLabel(
            "To get started, you'll need an API key from one of our supported providers. "
            "We recommend <b>DeepSeek</b> for the best cost-to-performance ratio."
        )
        d2.setObjectName("stepText")
        d2.setWordWrap(True)
        l2.addWidget(d2)

        tip = QFrame()
        tip.setObjectName("tipBox")
        tl = QVBoxLayout(tip)
        tl.addWidget(QLabel("<b>How to add keys:</b>"))
        tl.addWidget(QLabel("• Click the gear icon (Settings) in the top-right."))
        tl.addWidget(QLabel("• Select your provider (DeepSeek, OpenAI, etc.)."))
        tl.addWidget(QLabel("• Paste your key and click <b>Save</b>."))
        tl.addWidget(QLabel(f"<i style='color: {SUCCESS};'>Keys are stored locally with hardware encryption.</i>"))
        l2.addWidget(tip)

        self._stack.addWidget(s2)

        # Step 3: Workspace
        s3 = QWidget()
        l3 = QVBoxLayout(s3)
        l3.setSpacing(12)
        
        t3 = QLabel("2. Select Workspace")
        t3.setObjectName("paneTitle")
        l3.addWidget(t3)
        
        d3 = QLabel(
            "Aura works best when it can 'see' your whole project. Use the <b>Change Root</b> "
            "button in the left sidebar to select your project's folder."
        )
        d3.setObjectName("stepText")
        d3.setWordWrap(True)
        l3.addWidget(d3)

        tip3 = QFrame()
        tip3.setObjectName("tipBox")
        tl3 = QVBoxLayout(tip3)
        tl3.addWidget(QLabel("<b>Why this matters:</b>"))
        tl3.addWidget(QLabel("• It builds a local index for fast semantic search."))
        tl3.addWidget(QLabel("• It allows the AI to read and edit your files directly."))
        tl3.addWidget(QLabel("• It automatically ignores junk folders like .git and .venv."))
        l3.addWidget(tip3)

        self._stack.addWidget(s3)

        # Step 4: Planner/Worker
        s4 = QWidget()
        l4 = QVBoxLayout(s4)
        l4.setSpacing(12)
        
        t4 = QLabel("3. Planner & Worker")
        t4.setObjectName("paneTitle")
        l4.addWidget(t4)
        
        d4 = QLabel(
            "Aura uses a dual-model system: The <b>Planner</b> chats with you to design a solution, "
            "and the <b>Worker</b> executes the actual code changes."
        )
        d4.setObjectName("stepText")
        d4.setWordWrap(True)
        l4.addWidget(d4)

        tip4 = QFrame()
        tip4.setObjectName("tipBox")
        tl4 = QVBoxLayout(tip4)
        tl4.addWidget(QLabel("<b>Pro Tip: Automation</b>"))
        tl4.addWidget(QLabel("Look for the <b>Dispatch</b> and <b>Approve</b> toggles in the top bar:"))
        tl4.addWidget(QLabel("• <b>Dispatch:</b> Auto-start the worker after a plan is made."))
        tl4.addWidget(QLabel("• <b>Approve:</b> Auto-apply file edits without manual diff review."))
        l4.addWidget(tip4)

        self._stack.addWidget(s4)

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
        self._dots.setText(" ".join(dots))
