from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.config import AppSettings
from aura.gui.credits_panel import AuraCreditsPanel


class AuraCreditsPopoutDialog(QDialog):
    credits_claimed = Signal()
    credits_changed = Signal()

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Aura Credits")
        self.setModal(True)
        self.setMinimumSize(500, 620)
        self.resize(560, 700)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        self._panel = AuraCreditsPanel(settings, self)
        self._panel.credits_claimed.connect(self.credits_claimed)
        self._panel.credits_changed.connect(self.credits_changed)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(self._panel)
        layout.addWidget(scroll, 1)

        button_row = QHBoxLayout()
        button_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        button_row.addWidget(close_btn)
        layout.addLayout(button_row)

    def _cleanup_threads(self) -> None:
        self._panel.cleanup_threads()

    def done(self, result: int) -> None:  # type: ignore[override]
        self._cleanup_threads()
        super().done(result)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._cleanup_threads()
        super().closeEvent(event)
