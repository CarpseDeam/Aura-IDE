from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aura.config import ThinkingMode
from aura.drones.build_spec import DroneBuildBrief
from aura.drones.workshop_runner import DroneWorkshopResponse, DroneWorkshopRunner
from aura.gui.cards.assistant_card import AssistantCard
from aura.gui.cards.user_card import UserCard
from aura.gui.theme import ACCENT, BG, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM, FG_MUTED


class _WorkshopTextEdit(QTextEdit):
    """Multiline auto-growing text edit for the Drone Workshop."""

    submitted = Signal()

    MAX_LINES = 8

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText(
            "Describe the Drone you need\u2026\nCtrl+Enter to send, Enter for newline"
        )
        self.setAcceptRichText(False)
        self.setWordWrapMode(QTextOption.WrapMode.WordWrap)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.document().contentsChanged.connect(self._adjust_height)
        self._adjust_height()

    def _adjust_height(self) -> None:
        line_h = self.fontMetrics().lineSpacing()
        doc_h = int(self.document().size().height())
        target = min(line_h * self.MAX_LINES, max(line_h, doc_h)) + 14
        self.setFixedHeight(target)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier
                | Qt.KeyboardModifier.MetaModifier
            ):
                self.submitted.emit()
                return
        super().keyPressEvent(event)


class DroneWorkshopDialog(QDialog):
    """Dialog for building a Drone via conversation with Aura."""

    buildSpecApproved = Signal(object)  # emits DroneBuildBrief (legacy)
    drone_build_requested = Signal(object)  # emits DroneBuildBrief

    def __init__(
        self,
        workspace_root: Path | None = None,
        provider_id: str = "deepseek",
        model: str = "",
        thinking: ThinkingMode = "disabled",
        temperature: float = 0.4,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._provider_id = provider_id
        self._model = model
        self._thinking = thinking
        self._temperature = temperature

        self._conversation: list[dict[str, str]] = []
        self._last_valid_brief: DroneBuildBrief | None = None
        self._runner_thread: QThread | None = None
        self._runner: DroneWorkshopRunner | None = None
        self._thinking_card: AssistantCard | None = None

        self.setWindowTitle("Drone Workshop")
        self.resize(900, 720)
        self.setMinimumSize(680, 480)
        self.setStyleSheet(f"QDialog {{ background: {BG_ALT}; }}")

        self._build_ui()

    # -- UI construction --

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # -- Header --
        title = QLabel("Drone Workshop")
        title.setStyleSheet(
            "font-size: 21px; font-weight: 700;"
            f" color: {FG}; background: transparent;"
        )
        layout.addWidget(title)

        # -- Splitter: conversation (top) | brief panel (bottom) --
        self._splitter = QSplitter(Qt.Orientation.Vertical)
        self._splitter.setHandleWidth(3)

        # -- Conversation area --
        conversation_widget = QWidget()
        conv_layout = QVBoxLayout(conversation_widget)
        conv_layout.setContentsMargins(0, 0, 0, 0)
        conv_layout.setSpacing(8)

        # Message column (scrollable)
        self._msg_scroll = QScrollArea()
        self._msg_scroll.setWidgetResizable(True)
        self._msg_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self._msg_scroll.setStyleSheet(
            f"QScrollArea {{ border: none; background: {BG}; }}"
        )

        self._msg_column = QWidget()
        self._msg_column.setStyleSheet(f"background: {BG};")
        self._msg_layout = QVBoxLayout(self._msg_column)
        self._msg_layout.setContentsMargins(8, 8, 8, 8)
        self._msg_layout.setSpacing(10)
        self._msg_layout.addStretch()

        self._msg_scroll.setWidget(self._msg_column)

        conv_layout.addWidget(self._msg_scroll, 1)

        # -- Input row --
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self._input_edit = _WorkshopTextEdit()
        self._input_edit.setStyleSheet(
            f"QTextEdit {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 4px 6px; color: {FG}; }}"
        )
        self._input_edit.submitted.connect(self._on_send)
        input_row.addWidget(self._input_edit, 1)

        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("primary")
        self._send_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._send_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 18px; font-weight: 600; font-size: 13px; }}"
        )
        self._send_btn.clicked.connect(self._on_send)
        input_row.addWidget(self._send_btn)

        conv_layout.addLayout(input_row)

        self._splitter.addWidget(conversation_widget)

        # -- Brief panel --
        brief_panel = QWidget()
        brief_layout = QVBoxLayout(brief_panel)
        brief_layout.setContentsMargins(0, 0, 0, 0)
        brief_layout.setSpacing(8)

        brief_title = QLabel("DRONE BUILD BRIEF")
        brief_title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {FG_DIM}; "
            f"letter-spacing: 0.08em; background: transparent;"
        )
        brief_layout.addWidget(brief_title)

        # Compact build brief card
        self._brief_card = QFrame()
        self._brief_card.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self._brief_card.setMaximumHeight(140)
        self._brief_card.setStyleSheet(
            f"QFrame {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 8px; padding: 12px; }}"
        )
        card_layout = QVBoxLayout(self._brief_card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        card_layout.setSpacing(4)

        # Empty state
        self._brief_empty = QLabel(
            "No build brief yet — describe your Drone in the conversation above."
        )
        self._brief_empty.setWordWrap(True)
        self._brief_empty.setStyleSheet(
            f"color: {FG_MUTED}; font-size: 13px; background: transparent;"
        )
        card_layout.addWidget(self._brief_empty)

        # Valid brief content (hidden initially)
        self._brief_valid_widget = QWidget()
        valid_layout = QVBoxLayout(self._brief_valid_widget)
        valid_layout.setContentsMargins(0, 0, 0, 0)
        valid_layout.setSpacing(2)

        self._brief_name = QLabel()
        self._brief_name.setStyleSheet(
            f"font-size: 15px; font-weight: 700; color: {FG}; background: transparent;"
        )
        valid_layout.addWidget(self._brief_name)

        self._brief_description = QLabel()
        self._brief_description.setWordWrap(True)
        self._brief_description.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        valid_layout.addWidget(self._brief_description)

        self._brief_tools = QLabel()
        self._brief_tools.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        valid_layout.addWidget(self._brief_tools)

        self._brief_permissions = QLabel()
        self._brief_permissions.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        valid_layout.addWidget(self._brief_permissions)

        self._brief_output = QLabel()
        self._brief_output.setStyleSheet(
            f"font-size: 11px; color: {FG_MUTED}; background: transparent;"
        )
        valid_layout.addWidget(self._brief_output)

        self._brief_valid_widget.setVisible(False)
        card_layout.addWidget(self._brief_valid_widget)

        brief_layout.addWidget(self._brief_card)

        self._splitter.addWidget(brief_panel)

        # Stretch: 3 parts conversation, 1 part brief
        self._splitter.setStretchFactor(0, 3)
        self._splitter.setStretchFactor(1, 1)

        layout.addWidget(self._splitter, 1)

        # -- Bottom buttons --
        button_row = QHBoxLayout()
        button_row.setSpacing(8)
        button_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; "
            f"padding: 6px 20px; font-weight: 600; }}"
        )
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        self._build_btn = QPushButton("Build this Drone")
        self._build_btn.setObjectName("primary")
        self._build_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._build_btn.setEnabled(False)
        self._build_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 20px; font-weight: 600; }}"
            f"QPushButton#primary:disabled {{ background: #2a2a30; color: #555566; "
            f"border: 1px solid #333340; }}"
        )
        self._build_btn.clicked.connect(self._on_approve_build)
        button_row.addWidget(self._build_btn)

        layout.addLayout(button_row)

    # -- Send behavior --

    def _on_send(self) -> None:
        text = self._input_edit.toPlainText().strip()
        if not text:
            return
        if self._runner_thread is not None and self._runner_thread.isRunning():
            return  # runner already active

        # Add user message card to column
        user_card = UserCard(text, self._msg_column)
        self._add_message_card(user_card)
        self._conversation.append({"role": "user", "content": text})
        self._input_edit.clear()

        # Disable input and build button while running; show thinking state
        self._input_edit.setEnabled(False)
        self._send_btn.setEnabled(False)
        self._send_btn.setText("Thinking…")
        self._build_btn.setEnabled(False)
        self._send_btn.setStyleSheet(
            "QPushButton { background: #2a2a30; color: #555566; "
            "border: 1px solid #333340; border-radius: 6px; "
            "padding: 6px 18px; font-weight: 600; font-size: 13px; }"
        )

        # Start assistant message — show thinking card
        self._thinking_card = AssistantCard(self._msg_column)
        self._thinking_card.show_thinking_message("Aura is thinking…")
        self._add_message_card(self._thinking_card)

        self._runner = DroneWorkshopRunner(parent=None)
        self._runner_thread = QThread(self)
        self._runner.moveToThread(self._runner_thread)

        # Connect signals
        self._runner.responseReady.connect(self._on_response_ready)
        self._runner.apiError.connect(self._on_api_error)
        self._runner.finished.connect(self._on_runner_finished)

        self._runner_thread.started.connect(
            lambda: self._runner.run(
                conversation=self._conversation,
                provider_id=self._provider_id,
                model=self._model,
                thinking=self._thinking,
                temperature=self._temperature,
            )
        )
        self._runner_thread.start()

    def _on_response_ready(self, response: DroneWorkshopResponse) -> None:
        if response.error:
            if self._thinking_card:
                self._thinking_card.set_error(f"Error: {response.error}")
            self._conversation.append({"role": "assistant", "content": response.error})
            self._build_btn.setEnabled(False)
            self._build_btn.setText("Build this Drone")
            return

        text = response.message or ""
        if text:
            if self._thinking_card:
                self._thinking_card.set_content(text)

        if response.kind == "question":
            display = response.message or "Got it. Tell me more."
            self._conversation.append({"role": "assistant", "content": display})
            # Brief card unchanged; build button stays disabled

        elif response.kind == "brief":
            display = response.message or "Here's the build brief."
            self._conversation.append({"role": "assistant", "content": display})
            if response.brief is not None:
                self._last_valid_brief = response.brief
                self._update_brief_card(response.brief)
                if response.brief.is_ready_to_build():
                    self._build_btn.setEnabled(True)
                    self._build_btn.setText("✓ Build This Drone")
                else:
                    self._build_btn.setEnabled(False)
                    self._build_btn.setText("Build this Drone")
            else:
                self._build_btn.setEnabled(False)
                self._build_btn.setText("Build this Drone")

        elif response.kind == "error":
            if self._thinking_card:
                self._thinking_card.set_error(f"Error: {response.message}")
            self._build_btn.setEnabled(False)
            self._build_btn.setText("Build this Drone")

    def _on_api_error(self, status_code: int, message: str) -> None:
        if self._thinking_card:
            self._thinking_card.set_error(f"API Error ({status_code}): {message}")
        self._build_btn.setEnabled(False)
        self._build_btn.setText("Build this Drone")

    def _on_runner_finished(self) -> None:
        # Re-enable input; restore Send button
        self._input_edit.setEnabled(True)
        self._send_btn.setEnabled(True)
        self._send_btn.setText("Send")
        self._send_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 18px; font-weight: 600; font-size: 13px; }}"
        )
        # Re-enable build button if a valid brief is available
        if self._last_valid_brief is not None and self._last_valid_brief.is_ready_to_build():
            self._build_btn.setEnabled(True)
            self._build_btn.setText("✓ Build This Drone")
        # Clean up thread
        if self._runner_thread is not None:
            self._runner_thread.quit()
            self._runner_thread.wait(2000)
            self._runner_thread.deleteLater()
            self._runner_thread = None
        if self._runner is not None:
            self._runner.deleteLater()
            self._runner = None

    def _on_approve_build(self) -> None:
        """User clicked Build this Drone — emit signal and accept."""
        if self._last_valid_brief is not None:
            self.buildSpecApproved.emit(self._last_valid_brief)
            self.drone_build_requested.emit(self._last_valid_brief)
        self.accept()

    def reject(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
        self._last_valid_brief = None
        super().reject()

    # -- Public API --

    def result_brief(self) -> DroneBuildBrief | None:
        """Return the last valid build brief (backward compat)."""
        return self._last_valid_brief

    # -- Private helpers --

    def _add_message_card(self, card: QWidget) -> None:
        """Add a card to the message column and auto-scroll."""
        # Remove the bottom stretch spacer
        if self._msg_layout.count():
            last = self._msg_layout.itemAt(self._msg_layout.count() - 1)
            if last and last.spacerItem():
                self._msg_layout.removeItem(last)
        self._msg_layout.addWidget(card)
        # Add stretch back to keep messages at top
        self._msg_layout.addStretch()
        self._scroll_to_bottom()

    def _scroll_to_bottom(self) -> None:
        """Scroll to the bottom after layout settles."""
        QTimer.singleShot(0, lambda: self._msg_scroll.verticalScrollBar().setValue(
            self._msg_scroll.verticalScrollBar().maximum()
        ))

    def _update_brief_card(self, brief: DroneBuildBrief) -> None:
        """Update the compact brief card with build brief information."""
        self._brief_empty.setVisible(False)
        self._brief_valid_widget.setVisible(True)

        text = brief.build_brief.strip()
        lines = text.split("\n")

        # First line as title
        title = lines[0].strip() if lines else "Build Brief"
        if len(title) > 60:
            title = title[:57] + "..."
        self._brief_name.setText(title)

        # Remaining lines as description
        desc = "\n".join(lines[1:]).strip() if len(lines) > 1 else ""
        if not desc:
            desc = text[:200] if text else ""
        if len(desc) > 200:
            desc = desc[:197] + "..."
        self._brief_description.setText(desc)
        self._brief_description.setVisible(bool(desc))

        # Tools — not available in current data model, hide
        self._brief_tools.setVisible(False)

        # Permissions / readiness badge
        if brief.is_ready_to_build():
            self._brief_permissions.setText("\u2713 Ready to build")
            self._brief_permissions.setStyleSheet(
                "font-size: 11px; color: #4caf50; background: transparent; font-weight: 600;"
            )
        else:
            self._brief_permissions.setText("In progress — more details needed")
            self._brief_permissions.setStyleSheet(
                "font-size: 11px; color: #ff9800; background: transparent; font-weight: 600;"
            )
        self._brief_permissions.setVisible(True)

        # Output format — not available in current data model, hide
        self._brief_output.setVisible(False)
