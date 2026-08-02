from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
)
from aura.gui.theme import FG_DIM


class PromptsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        from aura.prompts import SINGLE_SYSTEM_PROMPT as _SINGLE_PROMPT

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        title = QLabel("System Prompts")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        note = QLabel(
            "Leave blank to use the built-in default. "
            "Custom prompts take effect on the next conversation turn."
        )
        note.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        note.setWordWrap(True)
        form.addRow("", note)

        # The normal product has one production agent. Legacy Planner/Worker
        # prompt fields remain persisted but are intentionally not exposed.
        self._single_prompt_edit = QPlainTextEdit()
        self._single_prompt_edit.setFixedHeight(120)
        self._single_prompt_edit.setPlaceholderText(_SINGLE_PROMPT[:80] + "...")
        self._single_prompt_edit.setPlainText(self._settings.system_prompt)
        single_reset_btn = QPushButton("Reset")
        single_reset_btn.clicked.connect(lambda: self._single_prompt_edit.clear())
        single_row = QHBoxLayout()
        single_row.setSpacing(6)
        single_row.addWidget(self._single_prompt_edit, 1)
        single_row.addWidget(single_reset_btn)
        single_widget = QWidget()
        single_widget.setLayout(single_row)
        form.addRow("Production:", single_widget)

        # A custom prompt is appended to the canonical one, so overlap is easy
        # to create and impossible to see in the editor alone. This says what
        # actually survives de-duplication and what it appears to restate.
        self._diagnostics_label = QLabel()
        self._diagnostics_label.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        self._diagnostics_label.setWordWrap(True)
        form.addRow("", self._diagnostics_label)
        self._single_prompt_edit.textChanged.connect(self._refresh_diagnostics)
        self._refresh_diagnostics()

        layout.addLayout(form)
        layout.addStretch()

    def _refresh_diagnostics(self) -> None:
        diagnostics = diagnose_custom_prompt(
            RuntimeRole.SINGLE, self._single_prompt_edit.toPlainText()
        )
        self._diagnostics_label.setText(format_custom_prompt_diagnostics(diagnostics))

    def collect_settings(self, settings: AppSettings) -> None:
        settings.system_prompt = self._single_prompt_edit.toPlainText().strip()
