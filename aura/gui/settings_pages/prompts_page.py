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
from aura.gui.theme import FG_DIM


class PromptsPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        from aura.prompts import (
            PLANNER_SYSTEM_PROMPT as _PLANNER_PROMPT,
            WORKER_SYSTEM_PROMPT as _WORKER_PROMPT,
            SINGLE_SYSTEM_PROMPT as _SINGLE_PROMPT,
        )

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

        # Single-mode prompt
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
        form.addRow("Single mode:", single_widget)

        # Planner prompt
        self._planner_prompt_edit = QPlainTextEdit()
        self._planner_prompt_edit.setFixedHeight(120)
        self._planner_prompt_edit.setPlaceholderText(_PLANNER_PROMPT[:80] + "...")
        self._planner_prompt_edit.setPlainText(self._settings.planner_system_prompt)
        planner_reset_btn = QPushButton("Reset")
        planner_reset_btn.clicked.connect(lambda: self._planner_prompt_edit.clear())
        planner_row = QHBoxLayout()
        planner_row.setSpacing(6)
        planner_row.addWidget(self._planner_prompt_edit, 1)
        planner_row.addWidget(planner_reset_btn)
        planner_widget = QWidget()
        planner_widget.setLayout(planner_row)
        form.addRow("Planner:", planner_widget)

        # Worker prompt
        self._worker_prompt_edit = QPlainTextEdit()
        self._worker_prompt_edit.setFixedHeight(120)
        self._worker_prompt_edit.setPlaceholderText(_WORKER_PROMPT[:80] + "...")
        self._worker_prompt_edit.setPlainText(self._settings.worker_system_prompt)
        worker_reset_btn = QPushButton("Reset")
        worker_reset_btn.clicked.connect(lambda: self._worker_prompt_edit.clear())
        worker_row = QHBoxLayout()
        worker_row.setSpacing(6)
        worker_row.addWidget(self._worker_prompt_edit, 1)
        worker_row.addWidget(worker_reset_btn)
        worker_widget = QWidget()
        worker_widget.setLayout(worker_row)
        form.addRow("Worker:", worker_widget)

        layout.addLayout(form)
        layout.addStretch()

    def collect_settings(self, settings: AppSettings) -> None:
        settings.system_prompt = self._single_prompt_edit.toPlainText().strip()
        settings.planner_system_prompt = self._planner_prompt_edit.toPlainText().strip()
        settings.worker_system_prompt = self._worker_prompt_edit.toPlainText().strip()
