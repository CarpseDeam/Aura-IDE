"""First-run setup dialog for when no providers are configured."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from aura.config import APP_NAME


class SetupDialog(QDialog):
    """First-run setup dialog shown when no provider API keys are available.

    Offers three choices: open API settings, continue in read-only mode, or exit.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._continue_readonly = False

        self.setWindowTitle(f"Set up {APP_NAME}")
        self.setModal(True)
        self.resize(480, 200)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(16)

        label = QLabel(
            f"{APP_NAME} needs at least one AI provider configured before "
            "Planner/Worker can run. You can configure DeepSeek, OpenAI, "
            "OpenRouter, Anthropic, Gemini, Claude Code, or Codex."
        )
        label.setWordWrap(True)
        layout.addWidget(label)

        layout.addStretch(1)

        button_box = QDialogButtonBox(self)

        open_settings_btn = QPushButton("Open Provider Settings")
        button_box.addButton(open_settings_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        open_settings_btn.clicked.connect(self.accept)

        continue_btn = QPushButton("Continue Read-only")
        button_box.addButton(continue_btn, QDialogButtonBox.ButtonRole.HelpRole)
        continue_btn.clicked.connect(self._on_continue_readonly)

        exit_btn = QPushButton("Exit")
        button_box.addButton(exit_btn, QDialogButtonBox.ButtonRole.RejectRole)
        exit_btn.clicked.connect(self.reject)

        layout.addWidget(button_box)

    def _on_continue_readonly(self) -> None:
        self._continue_readonly = True
        self.accept()
