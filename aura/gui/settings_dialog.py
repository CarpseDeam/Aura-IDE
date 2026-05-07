"""Modal Settings dialog — exposes the persisted AppSettings + a few
read-only environment / workspace facts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    APP_NAME,
    PROVIDERS,
    AppSettings,
    ProviderId,
    ThinkingMode,
    get_api_key,
    get_provider,
    icon_path,
    save_settings,
)
from aura.gui.theme import DANGER, FG_DIM, SUCCESS, WARN

_THINKING_ITEMS: list[tuple[str, str]] = [
    ("Off", "off"),
    ("High", "high"),
    ("Max", "max"),
]


class SettingsDialog(QDialog):
    """Modal settings.

    `on_change_root` is fired when the user clicks "Change..." next to the
    workspace label so the host (MainWindow) can run the same picker it uses
    elsewhere; we don't fork that logic here.
    """

    def __init__(
        self,
        settings: AppSettings,
        workspace_root: Path | None,
        on_change_root: Callable[[], None],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — Settings")
        self.setModal(True)
        self.resize(580, 0)

        self._settings = AppSettings(
            provider=settings.provider,
            api_keys=dict(settings.api_keys),
            default_model=settings.default_model,
            default_thinking=settings.default_thinking,
            restore_last_conversation=settings.restore_last_conversation,
            planner_worker_mode=settings.planner_worker_mode,
            default_planner_model=settings.default_planner_model,
            default_worker_model=settings.default_worker_model,
            default_planner_thinking=settings.default_planner_thinking,
            default_worker_thinking=settings.default_worker_thinking,
            vision_enabled=settings.vision_enabled,
            vision_model=settings.vision_model,
            vision_endpoint=settings.vision_endpoint,
        )
        self._on_change_root = on_change_root

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(14)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # ---- Provider selection ----
        self._provider_combo = QComboBox()
        for pid in ("deepseek", "openai", "google"):
            cfg = PROVIDERS[pid]  # type: ignore[literal-required]
            self._provider_combo.addItem(cfg.label, pid)
        self._provider_combo.setCurrentIndex(
            list(PROVIDERS.keys()).index(self._settings.provider)
        )
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        form.addRow("Provider:", self._provider_combo)

        # ---- API Key input ----
        self._api_key_input = QLineEdit()
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("sk-...  Paste your API key here")
        api_key_row = QHBoxLayout()
        api_key_row.setSpacing(8)
        api_key_row.addWidget(self._api_key_input, 1)
        self._api_key_status = QLabel("")
        api_key_row.addWidget(self._api_key_status)
        form.addRow("API Key:", api_key_row)

        # Populate API key input
        current_provider = self._settings.provider
        stored_key = self._settings.api_keys.get(current_provider, "")
        env_key = os_environ_get(PROVIDERS[current_provider].env_key)
        if stored_key:
            self._api_key_input.setText(stored_key)
        self._refresh_api_key_status(current_provider)

        # ---- Model combos ----
        self._model_combo = QComboBox()
        form.addRow("Default model:", self._model_combo)

        self._thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._thinking_combo.addItem(label, val)
        form.addRow("Default thinking:", self._thinking_combo)

        self._restore_chk = QCheckBox("Restore most-recent conversation on launch")
        self._restore_chk.setChecked(self._settings.restore_last_conversation)
        form.addRow("", self._restore_chk)

        self._pw_mode_chk = QCheckBox(
            "Planner/Worker mode (planner chats; worker executes code changes)"
        )
        self._pw_mode_chk.setChecked(self._settings.planner_worker_mode)
        self._pw_mode_chk.toggled.connect(self._on_pw_toggled)
        form.addRow("", self._pw_mode_chk)

        self._planner_model_combo = QComboBox()
        form.addRow("Planner model:", self._planner_model_combo)

        self._planner_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._planner_thinking_combo.addItem(label, val)
        form.addRow("Planner thinking:", self._planner_thinking_combo)

        self._worker_model_combo = QComboBox()
        form.addRow("Worker model:", self._worker_model_combo)

        self._worker_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._worker_thinking_combo.addItem(label, val)
        form.addRow("Worker thinking:", self._worker_thinking_combo)

        self._refresh_pw_enabled()

        # Populate model combos for the current provider
        self._populate_model_combos(current_provider)

        # --- Vision settings ---
        vision_sep = QLabel("Vision (Local Model)")
        vision_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", vision_sep)

        self._vision_enabled_chk = QCheckBox(
            "Enable local vision model for image descriptions"
        )
        self._vision_enabled_chk.setChecked(self._settings.vision_enabled)
        form.addRow("", self._vision_enabled_chk)

        self._vision_model_combo = QComboBox()
        self._vision_model_combo.setEditable(True)
        self._vision_model_combo.addItems(
            ["llama3.2-vision", "llava:13b", "minicpm-v", "bakllava"]
        )
        self._vision_model_combo.setCurrentText(self._settings.vision_model)
        form.addRow("Vision model:", self._vision_model_combo)

        self._vision_endpoint_combo = QComboBox()
        self._vision_endpoint_combo.setEditable(True)
        self._vision_endpoint_combo.addItems(["http://localhost:11434/v1"])
        self._vision_endpoint_combo.setCurrentText(self._settings.vision_endpoint)
        form.addRow("Vision endpoint:", self._vision_endpoint_combo)

        # Workspace
        ws_row = QHBoxLayout()
        ws_row.setSpacing(8)
        self._ws_label = QLabel(str(workspace_root) if workspace_root else "(none)")
        self._ws_label.setStyleSheet(f"color: {FG_DIM};")
        self._ws_label.setWordWrap(True)
        ws_row.addWidget(self._ws_label, 1)
        change_btn = QPushButton("Change...")
        change_btn.clicked.connect(self._on_change_root_clicked)
        ws_row.addWidget(change_btn)
        ws_widget = QWidget()
        ws_widget.setLayout(ws_row)
        form.addRow("Workspace root:", ws_widget)

        # Backups info
        if workspace_root is not None:
            backup_path = workspace_root / ".aura" / "backups"
            backup_text = (
                f"Stored in {backup_path}, never auto-deleted. Manage manually."
            )
        else:
            backup_text = "Stored under <workspace>/.aura/backups/, never auto-deleted."
        backup_label = QLabel(backup_text)
        backup_label.setStyleSheet(f"color: {FG_DIM};")
        backup_label.setWordWrap(True)
        form.addRow("Backups:", backup_label)

        outer.addLayout(form)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    # ---- Provider change handler ------------------------------------------

    def _on_provider_changed(self, _index: int) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()
        cfg = PROVIDERS[provider_id]
        self._populate_model_combos(provider_id)
        self._refresh_api_key_status(provider_id)

        # Pre-fill API key from stored settings or env var.
        stored = self._settings.api_keys.get(provider_id, "")
        env_val = os_environ_get(cfg.env_key)
        if stored:
            self._api_key_input.setText(stored)
        elif env_val:
            self._api_key_input.setText(env_val)
        else:
            self._api_key_input.clear()

    def _refresh_api_key_status(self, provider_id: ProviderId) -> None:
        cfg = PROVIDERS[provider_id]
        env_val = os_environ_get(cfg.env_key)
        stored = self._settings.api_keys.get(provider_id, "")
        if env_val:
            self._api_key_status.setText("✓ from env")
            self._api_key_status.setStyleSheet(f"color: {SUCCESS};")
        elif stored:
            self._api_key_status.setText("✓ stored")
            self._api_key_status.setStyleSheet(f"color: {SUCCESS};")
        else:
            self._api_key_status.setText("missing")
            self._api_key_status.setStyleSheet(f"color: {WARN};")

    # ---- Model combo helpers ----------------------------------------------

    def _populate_model_combos(self, provider_id: ProviderId) -> None:
        cfg = PROVIDERS[provider_id]
        models = cfg.models

        def _fill(combo: QComboBox, current_val: str | None = None) -> None:
            combo.blockSignals(True)
            combo.clear()
            for mid, info in models.items():
                combo.addItem(info.label, mid)
            if current_val is not None:
                idx = combo.findData(current_val)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
                else:
                    # Fall back to provider default
                    default_idx = combo.findData(cfg.default_model)
                    if default_idx >= 0:
                        combo.setCurrentIndex(default_idx)
            combo.blockSignals(False)

        _fill(self._model_combo, self._settings.default_model)
        _fill(self._planner_model_combo, self._settings.default_planner_model)
        _fill(self._worker_model_combo, self._settings.default_worker_model)

        # Reset thinking combos to user's saved preference.
        self._thinking_combo.blockSignals(True)
        self._thinking_combo.setCurrentIndex(
            [v for _, v in _THINKING_ITEMS].index(self._settings.default_thinking)
        )
        self._thinking_combo.blockSignals(False)

        self._planner_thinking_combo.blockSignals(True)
        self._planner_thinking_combo.setCurrentIndex(
            [v for _, v in _THINKING_ITEMS].index(self._settings.default_planner_thinking)
        )
        self._planner_thinking_combo.blockSignals(False)

        self._worker_thinking_combo.blockSignals(True)
        self._worker_thinking_combo.setCurrentIndex(
            [v for _, v in _THINKING_ITEMS].index(self._settings.default_worker_thinking)
        )
        self._worker_thinking_combo.blockSignals(False)

    # ---- Change root ------------------------------------------------------

    def _on_change_root_clicked(self) -> None:
        self._on_change_root()
        # Host updated the workspace root — refresh display from the host:
        host = self.parent()
        if host is not None and hasattr(host, "_workspace_root"):
            new_root = getattr(host, "_workspace_root")
            self._ws_label.setText(str(new_root) if new_root else "(none)")

    def _on_pw_toggled(self, _checked: bool) -> None:
        self._refresh_pw_enabled()

    def _refresh_pw_enabled(self) -> None:
        enabled = self._pw_mode_chk.isChecked()
        self._planner_model_combo.setEnabled(enabled)
        self._planner_thinking_combo.setEnabled(enabled)
        self._worker_model_combo.setEnabled(enabled)
        self._worker_thinking_combo.setEnabled(enabled)

    def result_settings(self) -> AppSettings:
        """Read the current widget values and return a fresh AppSettings."""
        provider_id: ProviderId = self._provider_combo.currentData()

        # Build api_keys dict: preserve keys for other providers, update current
        api_keys = dict(self._settings.api_keys)
        new_key = self._api_key_input.text().strip()
        if new_key:
            api_keys[provider_id] = new_key
        else:
            api_keys.pop(provider_id, None)

        return AppSettings(
            provider=provider_id,
            api_keys=api_keys,
            default_model=self._model_combo.currentData(),
            default_thinking=self._thinking_combo.currentData(),
            restore_last_conversation=self._restore_chk.isChecked(),
            planner_worker_mode=self._pw_mode_chk.isChecked(),
            default_planner_model=self._planner_model_combo.currentData(),
            default_worker_model=self._worker_model_combo.currentData(),
            default_planner_thinking=self._planner_thinking_combo.currentData(),
            default_worker_thinking=self._worker_thinking_combo.currentData(),
            vision_enabled=self._vision_enabled_chk.isChecked(),
            vision_model=self._vision_model_combo.currentText(),
            vision_endpoint=self._vision_endpoint_combo.currentText(),
        )

    def accept(self) -> None:  # type: ignore[override]
        # Persist on OK.
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()


def os_environ_get(key: str) -> str:
    """Read an environment variable, returning empty string if not set."""
    import os
    return os.environ.get(key, "")
