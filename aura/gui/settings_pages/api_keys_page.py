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
    get_provider_kind,
    is_external_cli_available,
    set_api_key,
)
from aura.providers.registry import provider_registry
from aura.gui.theme import FG_DIM, FG_MUTED, SUCCESS, WARN


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

        title = QLabel("Provider Setup")
        title.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", title)

        sub_label = QLabel(
            "API key providers are configured inside Aura. "
            "External CLI providers are configured outside Aura."
        )
        sub_label.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        sub_label.setWordWrap(True)
        form.addRow("", sub_label)

        self._provider_rows: dict[str, dict[str, object]] = {}

        for pid in provider_registry.ids():
            if pid == "aura":
                continue
            spec = provider_registry.get(pid)
            kind = get_provider_kind(pid)

            if kind == "api_key":
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

            elif kind == "external_cli":
                status_row = QHBoxLayout()
                status_row.setSpacing(6)

                status_label = QLabel("")
                status_label.setWordWrap(True)
                status_row.addWidget(status_label, 1)

                refresh_btn = QPushButton("Refresh")
                refresh_btn.setToolTip("Re-check CLI availability")
                refresh_btn.clicked.connect(lambda checked=False, p=pid: self._refresh_key_status(p))
                status_row.addWidget(refresh_btn)

                row_widget = QWidget()
                row_widget.setLayout(status_row)
                form.addRow(f"{spec.label}:", row_widget)

                self._provider_rows[pid] = {
                    "status": status_label,
                }

                self._refresh_key_status(pid)

            elif kind == "local":
                status_label = QLabel("Coming soon")
                status_label.setStyleSheet(f"color: {FG_MUTED}; font-style: italic;")
                status_label.setWordWrap(True)
                form.addRow(f"{spec.label}:", status_label)

                self._provider_rows[pid] = {
                    "status": status_label,
                }

        layout.addLayout(form)
        layout.addStretch()

    # --- Provider key helpers ---

    def _refresh_key_status(self, provider_id: str) -> None:
        row = self._provider_rows[provider_id]
        status_label: QLabel = row["status"]  # type: ignore[assignment]
        cfg = provider_registry.get(provider_id)
        kind = get_provider_kind(provider_id)

        if kind == "api_key":
            if os.environ.get(cfg.env_key):
                text = f"{cfg.label} key loaded from {cfg.env_key}."
                color = SUCCESS
            elif get_api_key(provider_id):
                text = f"{cfg.label} key is stored locally."
                color = SUCCESS
            else:
                text = f"No {cfg.label} key found. Set {cfg.env_key} or save one here."
                color = WARN
        elif kind == "external_cli":
            if is_external_cli_available(provider_id):
                text = f"{cfg.label} — ✓ Available"
                color = SUCCESS
            else:
                text = f"{cfg.label} — Install/sign in to the CLI, then refresh."
                color = WARN
        else:
            text = "Coming soon"
            color = FG_MUTED

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

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        return
