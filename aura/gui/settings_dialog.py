"""Modal Settings dialog — exposes the persisted AppSettings + a few
read-only environment / workspace facts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
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
    fetch_provider_models,
    get_api_key,
    get_provider,
    icon_path,
    save_dynamic_catalog,
    save_settings,
)
from aura.gui.theme import DANGER, FG_DIM, SUCCESS, WARN

_THINKING_ITEMS: list[tuple[str, str]] = [
    ("Off", "off"),
    ("High", "high"),
    ("Max", "max"),
]


class DiscoveryWorker(QObject):
    """Background worker for model discovery."""
    finished = Signal(str, dict, dict, str) # provider_id, models, pricing, error_msg

    def __init__(self, provider_id: ProviderId):
        super().__init__()
        self.provider_id = provider_id

    def run(self):
        try:
            models, pricing, error = fetch_provider_models(self.provider_id)
            self.finished.emit(self.provider_id, models, pricing, error or "")
        except Exception as exc:
            self.finished.emit(self.provider_id, {}, {}, str(exc))


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
            temperature=settings.temperature,
            system_prompt=settings.system_prompt,
            planner_system_prompt=settings.planner_system_prompt,
            worker_system_prompt=settings.worker_system_prompt,
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
        provider_row = QHBoxLayout()
        self._provider_combo = QComboBox()
        for pid in ("deepseek", "openai", "google", "openrouter"):
            cfg = PROVIDERS[pid]  # type: ignore[literal-required]
            self._provider_combo.addItem(cfg.label, pid)
        self._provider_combo.setCurrentIndex(
            list(PROVIDERS.keys()).index(self._settings.provider)
        )
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        provider_row.addWidget(self._provider_combo, 1)
        
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.setToolTip("Fetch latest models and pricing from provider API")
        self._refresh_btn.clicked.connect(self._on_refresh_models)
        provider_row.addWidget(self._refresh_btn)
        
        form.addRow("Provider:", provider_row)

        # ---- API Key status ----
        self._api_key_status = QLabel("")
        form.addRow("API Key:", self._api_key_status)

        current_provider = self._settings.provider
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

        # --- Temperature ---
        temp_sep = QLabel("Temperature")
        temp_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", temp_sep)

        self._temperature_spin = QDoubleSpinBox()
        self._temperature_spin.setRange(0.0, 2.0)
        self._temperature_spin.setSingleStep(0.1)
        self._temperature_spin.setDecimals(1)
        self._temperature_spin.setToolTip(
            "Controls response randomness. 0 = deterministic, 2 = maximum creativity. "
            "Only applied when thinking is Off."
        )
        self._temperature_spin.setValue(self._settings.temperature)
        form.addRow("Temperature:", self._temperature_spin)

        self._worker_temperature_spin = QDoubleSpinBox()
        self._worker_temperature_spin.setRange(0.0, 2.0)
        self._worker_temperature_spin.setSingleStep(0.1)
        self._worker_temperature_spin.setDecimals(1)
        self._worker_temperature_spin.setToolTip(
            "Controls response randomness for the worker model. Lower = more deterministic."
        )
        self._worker_temperature_spin.setValue(self._settings.worker_temperature)
        form.addRow("Worker Temperature:", self._worker_temperature_spin)

        self._refresh_pw_enabled()

        # --- System Prompts ---
        prompts_sep = QLabel("System Prompts")
        prompts_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", prompts_sep)

        prompts_note = QLabel(
            "Leave blank to use the built-in default. "
            "Custom prompts take effect on the next conversation turn."
        )
        prompts_note.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        prompts_note.setWordWrap(True)
        form.addRow("", prompts_note)

        # Lazy import to avoid circular dependency at module level.
        from aura.bridge.qt_bridge import PLANNER_SYSTEM_PROMPT as _PLANNER_PROMPT, WORKER_SYSTEM_PROMPT as _WORKER_PROMPT
        from aura.gui.main_window import SYSTEM_PROMPT as _SINGLE_PROMPT

        # Single-mode prompt
        self._single_prompt_edit = QPlainTextEdit()
        self._single_prompt_edit.setFixedHeight(80)
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
        self._planner_prompt_edit.setFixedHeight(80)
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
        self._worker_prompt_edit.setFixedHeight(80)
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
        self._populate_model_combos(provider_id)
        self._refresh_api_key_status(provider_id)

    def _on_refresh_models(self) -> None:
        """Trigger an async fetch of models for the current provider."""
        provider_id: ProviderId = self._provider_combo.currentData()
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Fetching...")

        # Use QThread and DiscoveryWorker for robust background execution
        self._discovery_thread = QThread()
        self._discovery_worker = DiscoveryWorker(provider_id)
        self._discovery_worker.moveToThread(self._discovery_thread)
        
        self._discovery_thread.started.connect(self._discovery_worker.run)
        self._discovery_worker.finished.connect(self._on_refresh_done)
        self._discovery_worker.finished.connect(self._discovery_thread.quit)
        self._discovery_worker.finished.connect(self._discovery_worker.deleteLater)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        
        self._discovery_thread.start()

    def _on_refresh_done(self, provider_id: ProviderId, models: dict, pricing: dict, error: str) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")

        if error:
            QMessageBox.warning(
                self, 
                "Refresh Failed", 
                f"Failed to fetch models for {get_provider(provider_id).label}:\n\n{error}"
            )
            return

        if not models:
            QMessageBox.warning(
                self, 
                "Refresh Failed", 
                f"No models were returned by {get_provider(provider_id).label}."
            )
            return

        # Update the global PROVIDERS registry for this session
        cfg = PROVIDERS[provider_id]
        cfg.models.update(models)
        cfg.pricing.update(pricing)
        
        # Persist to models_cache.json
        save_dynamic_catalog(provider_id, models, pricing)
        
        # Re-populate the combos
        self._populate_model_combos(provider_id)

    def _refresh_api_key_status(self, provider_id: ProviderId) -> None:
        cfg = PROVIDERS[provider_id]
        env_val = os.environ.get(cfg.env_key)
        if env_val:
            self._api_key_status.setText(f"{cfg.env_key}: ✓ set")
            self._api_key_status.setStyleSheet(f"color: {SUCCESS};")
        else:
            self._api_key_status.setText(f"{cfg.env_key}: missing — set in your shell and restart")
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
        self._worker_temperature_spin.setEnabled(enabled)

    def result_settings(self) -> AppSettings:
        """Read the current widget values and return a fresh AppSettings."""
        provider_id: ProviderId = self._provider_combo.currentData()

        return AppSettings(
            provider=provider_id,
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
            temperature=self._temperature_spin.value(),
            worker_temperature=self._worker_temperature_spin.value(),
            system_prompt=self._single_prompt_edit.toPlainText().strip(),
            planner_system_prompt=self._planner_prompt_edit.toPlainText().strip(),
            worker_system_prompt=self._worker_prompt_edit.toPlainText().strip(),
        )

    def accept(self) -> None:  # type: ignore[override]
        # Persist on OK.
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()
