from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import ThinkingMode
from aura.drones.build_spec import DroneBuildSpec
from aura.drones.workshop_runner import DroneWorkshopResponse, DroneWorkshopRunner
from aura.gui.theme import ACCENT, BG, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM, FG_MUTED


class DroneWorkshopDialog(QDialog):
    """Modal dialog for building a Drone via conversation with Aura."""

    buildSpecApproved = Signal(object)  # emits DroneBuildSpec

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
        self._current_spec: DroneBuildSpec | None = None
        self._runner_thread: QThread | None = None
        self._runner: DroneWorkshopRunner | None = None
        self._assistant_buffer: str = ""
        self._assistant_message_started: bool = False

        self.setWindowTitle("Drone Workshop")
        self.setMinimumWidth(620)
        self.setModal(True)
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

        # -- Transcript area (read-only) --
        self._transcript = QPlainTextEdit()
        self._transcript.setReadOnly(True)
        self._transcript.setPlainText(
            "Aura: Hi. What kind of Drone do we want to build today?"
        )
        self._transcript.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 8px; color: {FG}; }}"
        )
        layout.addWidget(self._transcript, 1)

        # -- Input row --
        input_row = QHBoxLayout()
        input_row.setSpacing(8)

        self._input_edit = QLineEdit()
        self._input_edit.setPlaceholderText("Describe the Drone you need...")
        self._input_edit.setStyleSheet(
            f"QLineEdit {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 5px; padding: 6px 8px; color: {FG}; }}"
        )
        self._input_edit.returnPressed.connect(self._on_send)
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

        layout.addLayout(input_row)

        # -- Preview: Drone Build Spec card --
        preview_title = QLabel("DRONE BUILD SPEC")
        preview_title.setStyleSheet(
            f"font-size: 11px; font-weight: 700; color: {FG_DIM}; "
            f"letter-spacing: 0.08em; padding: 8px 0 4px 0; background: transparent;"
        )
        layout.addWidget(preview_title)

        self._preview_card = QFrame()
        self._preview_card.setStyleSheet(
            f"QFrame {{ background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 8px; padding: 16px; }}"
        )
        preview_layout = QVBoxLayout(self._preview_card)
        preview_layout.setContentsMargins(12, 12, 12, 12)

        self._preview_text = QPlainTextEdit()
        self._preview_text.setReadOnly(True)
        self._preview_text.setPlaceholderText(
            "The Workshop will show the proposed Drone spec here "
            "once it has enough details."
        )
        self._preview_text.setStyleSheet(
            f"QPlainTextEdit {{ background: transparent; border: none; "
            f"color: {FG_MUTED}; font-size: 13px; }}"
        )
        preview_layout.addWidget(self._preview_text)

        layout.addWidget(self._preview_card)

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
        text = self._input_edit.text().strip()
        if not text:
            return
        if self._runner_thread is not None and self._runner_thread.isRunning():
            return  # runner already active

        # Add user message to transcript
        self._transcript.appendPlainText(f"\nYou: {text}")
        self._conversation.append({"role": "user", "content": text})
        self._input_edit.clear()

        # Disable input while running
        self._input_edit.setEnabled(False)
        self._send_btn.setEnabled(False)

        # Start assistant message
        self._assistant_buffer = ""
        self._assistant_message_started = False

        self._runner = DroneWorkshopRunner(parent=None)
        self._runner_thread = QThread(self)
        self._runner.moveToThread(self._runner_thread)

        # Connect signals
        self._runner.contentDelta.connect(self._on_content_delta)
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

    def _on_content_delta(self, text: str) -> None:
        self._assistant_buffer += text
        if not self._assistant_message_started:
            self._transcript.appendPlainText("\nAura: ")
            self._assistant_message_started = True
        # Stream inline: move cursor to end, insert text
        cursor = self._transcript.textCursor()
        cursor.movePosition(cursor.End)
        cursor.insertText(text)
        self._transcript.setTextCursor(cursor)
        self._transcript.ensureCursorVisible()

    def _on_response_ready(self, response: DroneWorkshopResponse) -> None:
        # If we had no streamed content, show the message now
        if not self._assistant_buffer.strip() and response.message:
            self._transcript.appendPlainText(f"\nAura: {response.message}")

        if response.kind == "question":
            # Append assistant message to conversation
            display = response.message or "Got it. Tell me more."
            self._conversation.append({"role": "assistant", "content": display})
            # Preview unchanged; build button stays disabled

        elif response.kind == "spec":
            self._current_spec = response.spec
            display = response.message or "Here's the proposed Drone spec."
            self._conversation.append({"role": "assistant", "content": display})
            # Update preview
            if response.spec is not None:
                md = response.spec.preview_markdown()
                if response.validation_errors:
                    errs = "\n".join(f"• {e}" for e in response.validation_errors)
                    md += f"\n\n⚠️ **Validation Issues:**\n{errs}"
                self._preview_text.setPlainText(md)
                # Enable build button only if no validation errors
                self._build_btn.setEnabled(len(response.validation_errors) == 0)
            else:
                self._preview_text.setPlainText(
                    "(The model returned a spec response but the spec was missing.)"
                )
                self._build_btn.setEnabled(False)

        elif response.kind == "error":
            self._transcript.appendPlainText(f"\n⚠️ {response.message}")
            # Keep build button disabled

        self._assistant_buffer = ""
        self._assistant_message_started = False

    def _on_api_error(self, status_code: int, message: str) -> None:
        self._transcript.appendPlainText(f"\n⚠️ API Error: {message}")
        self._build_btn.setEnabled(False)

    def _on_runner_finished(self) -> None:
        # Re-enable input
        self._input_edit.setEnabled(True)
        self._send_btn.setEnabled(True)
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
        """User clicked Build this Drone — emit spec and accept."""
        if self._current_spec is not None:
            self.buildSpecApproved.emit(self._current_spec)
        self.accept()

    def reject(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
        super().reject()
