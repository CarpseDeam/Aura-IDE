from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    APP_NAME,
    AppSettings,
    get_api_key,
    save_settings,
    set_api_key,
)
from aura.providers.registry import provider_registry
from aura.gui.theme import FG_DIM, SUCCESS, WARN


class ApiKeysPage(QWidget):
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        title = QLabel("API Provider Keys")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        self._provider_rows: dict[str, dict[str, object]] = {}

        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)

            row = QHBoxLayout()
            row.setSpacing(6)

            key_input = QLineEdit()
            key_input.setEchoMode(QLineEdit.EchoMode.Password)
            key_input.setPlaceholderText("Paste API key here...")
            row.addWidget(key_input, 1)

            save_btn = QPushButton("Save")
            save_btn.setToolTip("Encrypt and store this key on disk")
            save_btn.clicked.connect(lambda checked=False, p=pid, inp=key_input: self._on_save_key(p, inp))
            row.addWidget(save_btn)

            clear_btn = QPushButton("Clear")
            clear_btn.setToolTip("Remove stored key for this provider")
            clear_btn.clicked.connect(lambda checked=False, p=pid: self._on_clear_key(p))
            row.addWidget(clear_btn)

            row_widget = QWidget()
            row_widget.setLayout(row)
            form.addRow(f"{spec.label}:", row_widget)

            status_label = QLabel("")
            status_label.setWordWrap(True)
            form.addRow("", status_label)

            self._provider_rows[pid] = {
                "input": key_input,
                "status": status_label,
            }

            self._refresh_key_status(pid)

        # Tavily separator
        tavily_sep = QLabel("Web Search (Tavily)")
        tavily_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", tavily_sep)

        tavily_row = QHBoxLayout()
        tavily_row.setSpacing(6)
        self._tavily_key_input = QLineEdit()
        self._tavily_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._tavily_key_input.setPlaceholderText("Paste Tavily API key here...")
        tavily_row.addWidget(self._tavily_key_input, 1)

        self._save_tavily_btn = QPushButton("Save")
        self._save_tavily_btn.clicked.connect(self._on_save_tavily_key)
        tavily_row.addWidget(self._save_tavily_btn)

        self._clear_tavily_btn = QPushButton("Clear")
        self._clear_tavily_btn.clicked.connect(self._on_clear_tavily_key)
        tavily_row.addWidget(self._clear_tavily_btn)

        tavily_widget = QWidget()
        tavily_widget.setLayout(tavily_row)
        form.addRow("Tavily Key:", tavily_widget)

        self._tavily_status = QLabel("")
        self._tavily_status.setWordWrap(True)
        form.addRow("", self._tavily_status)
        self._refresh_tavily_status()

        layout.addLayout(form)
        layout.addStretch()

    # --- Provider key helpers ---

    def _refresh_key_status(self, provider_id: str) -> None:
        row = self._provider_rows[provider_id]
        status_label: QLabel = row["status"]  # type: ignore[assignment]
        cfg = provider_registry.get(provider_id)
        if os.environ.get(cfg.env_key):
            text = f"{cfg.label} key loaded from {cfg.env_key}."
            color = SUCCESS
        elif get_api_key(provider_id):
            text = f"{cfg.label} key is stored locally."
            color = SUCCESS
        else:
            text = f"No {cfg.label} key found. Set {cfg.env_key} or save one here."
            color = WARN
        status_label.setText(text)
        status_label.setStyleSheet(f"color: {color};")

    def _on_save_key(self, provider_id: str, key_input: QLineEdit) -> None:
        key = key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste an API key before saving.")
            return
        set_api_key(provider_id, key)
        key_input.clear()
        self._refresh_key_status(provider_id)

    def _on_clear_key(self, provider_id: str) -> None:
        from aura.key_manager import get_key_manager
        get_key_manager().delete_key(provider_id)
        self._refresh_key_status(provider_id)

    # --- Tavily ---

    def _refresh_tavily_status(self) -> None:
        if os.environ.get("TAVILY_API_KEY"):
            text = "Tavily key loaded from TAVILY_API_KEY."
            color = SUCCESS
        elif self._settings.tavily_api_key:
            text = "Tavily key is saved in settings."
            color = SUCCESS
        else:
            text = "No Tavily key saved. Web search will be unavailable."
            color = WARN
        self._tavily_status.setText(text)
        self._tavily_status.setStyleSheet(f"color: {color};")

    def _on_save_tavily_key(self) -> None:
        key = self._tavily_key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste a Tavily key before saving.")
            return
        self._settings.tavily_api_key = key
        self._tavily_key_input.clear()
        save_settings(self._settings)
        self._refresh_tavily_status()

    def _on_clear_tavily_key(self) -> None:
        self._settings.tavily_api_key = ""
        save_settings(self._settings)
        self._refresh_tavily_status()

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        settings.tavily_api_key = self._settings.tavily_api_key
