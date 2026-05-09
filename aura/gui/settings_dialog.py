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
    QLineEdit,
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
    fetch_provider_models,
    get_provider,
    save_dynamic_catalog,
    save_settings,
    set_api_key,
)
from aura.gui.theme import FG_DIM, SUCCESS, WARN, ACCENT, BG_RAISED, BORDER, FG
from aura.gui.aura_widget import GlassSwitch

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
            worker_temperature=settings.worker_temperature,
            system_prompt=settings.system_prompt,
            planner_system_prompt=settings.planner_system_prompt,
            worker_system_prompt=settings.worker_system_prompt,
            auto_commit_enabled=settings.auto_commit_enabled,
            auto_dispatch=settings.auto_dispatch,
            auto_approve=settings.auto_approve,
            sandbox_mode=settings.sandbox_mode,
            tavily_api_key=settings.tavily_api_key,
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
        for pid in PROVIDERS:
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

        # ---- API Key input ----
        api_key_row = QHBoxLayout()
        api_key_row.setSpacing(6)
        
        self._api_key_input = QLineEdit()
        self._api_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_key_input.setPlaceholderText("Paste API key here...")
        api_key_row.addWidget(self._api_key_input, 1)
        
        self._save_key_btn = QPushButton("Save")
        self._save_key_btn.setToolTip("Encrypt and store this key on disk")
        self._save_key_btn.clicked.connect(self._on_save_api_key)
        api_key_row.addWidget(self._save_key_btn)
        
        self._clear_key_btn = QPushButton("Clear")
        self._clear_key_btn.setToolTip("Remove stored key for this provider")
        self._clear_key_btn.clicked.connect(self._on_clear_api_key)
        api_key_row.addWidget(self._clear_key_btn)
        
        api_key_widget = QWidget()
        api_key_widget.setLayout(api_key_row)
        form.addRow("API Key:", api_key_widget)
        
        # Status label below the input
        self._api_key_status = QLabel("")
        self._api_key_status.setWordWrap(True)
        form.addRow("", self._api_key_status)
        
        # ---- Tavily API Key ----
        tavily_sep = QLabel("Web Search (Tavily)")
        tavily_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", tavily_sep)

        tavily_key_row = QHBoxLayout()
        tavily_key_row.setSpacing(6)
        self._tavily_key_input = QLineEdit()
        self._tavily_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._tavily_key_input.setPlaceholderText("Paste Tavily API key here...")
        tavily_key_row.addWidget(self._tavily_key_input, 1)

        self._save_tavily_btn = QPushButton("Save")
        self._save_tavily_btn.clicked.connect(self._on_save_tavily_key)
        tavily_key_row.addWidget(self._save_tavily_btn)

        self._clear_tavily_btn = QPushButton("Clear")
        self._clear_tavily_btn.clicked.connect(self._on_clear_tavily_key)
        tavily_key_row.addWidget(self._clear_tavily_btn)

        tavily_widget = QWidget()
        tavily_widget.setLayout(tavily_key_row)
        form.addRow("Tavily Key:", tavily_widget)

        self._tavily_status = QLabel("")
        self._tavily_status.setWordWrap(True)
        form.addRow("", self._tavily_status)
        self._refresh_tavily_status()

        current_provider = self._settings.provider
        self._refresh_api_key_status(current_provider)

        # ---- Model combos ----
        self._model_combo = QComboBox()
        form.addRow("Default model:", self._model_combo)

        self._thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._thinking_combo.addItem(label, val)
        form.addRow("Default thinking:", self._thinking_combo)

        self._restore_chk = GlassSwitch("Restore most-recent conversation on launch", self._settings.restore_last_conversation)
        form.addRow("", self._restore_chk)

        self._pw_mode_chk = GlassSwitch(
            "Planner/Worker mode (planner chats; worker executes code changes)",
            self._settings.planner_worker_mode
        )
        self._pw_mode_chk.toggled.connect(self._on_pw_toggled)
        form.addRow("", self._pw_mode_chk)

        self._planner_model_combo = QComboBox()
        form.addRow("Planner model:", self._planner_model_combo)

        self._planner_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._thinking_combo.addItem(label, val)
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

        self._vision_enabled_chk = GlassSwitch(
            "Enable local vision model for image descriptions",
            self._settings.vision_enabled
        )
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

        # --- Automation ---
        auto_sep = QLabel("Automation")
        auto_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", auto_sep)

        self._auto_commit_chk = GlassSwitch(
            "Auto-commit changes after worker completes",
            self._settings.auto_commit_enabled
        )
        form.addRow("", self._auto_commit_chk)

        self._auto_dispatch_chk = GlassSwitch(
            "Auto-dispatch: Send specs to worker without approval",
            self._settings.auto_dispatch
        )
        form.addRow("", self._auto_dispatch_chk)

        self._auto_approve_chk = GlassSwitch(
            "Auto-approve: Apply file edits without diff approval",
            self._settings.auto_approve
        )
        form.addRow("", self._auto_approve_chk)

        # --- Sandbox ---
        sandbox_sep = QLabel("Execution Sandbox")
        sandbox_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", sandbox_sep)

        sandbox_note = QLabel(
            "Docker mode runs terminal commands and dynamic tools in an isolated container. "
            "Host mode runs them directly on your machine (fast but no isolation). "
            "Docker must be installed for Docker mode to work."
        )
        sandbox_note.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        sandbox_note.setWordWrap(True)
        form.addRow("", sandbox_note)

        self._sandbox_combo = QComboBox()
        # Items: (display label, internal value)
        self._sandbox_combo.addItem("Docker (recommended)", "docker")
        self._sandbox_combo.addItem("Host (no isolation)", "host")
        self._sandbox_combo.addItem("WASM (coming soon)", "wasm")
        form.addRow("Sandbox mode:", self._sandbox_combo)

        # Set current sandbox mode from saved settings
        sandbox_idx = self._sandbox_combo.findData(self._settings.sandbox_mode)
        if sandbox_idx >= 0:
            self._sandbox_combo.setCurrentIndex(sandbox_idx)

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
        from aura.prompts import PLANNER_SYSTEM_PROMPT as _PLANNER_PROMPT, WORKER_SYSTEM_PROMPT as _WORKER_PROMPT, SINGLE_SYSTEM_PROMPT as _SINGLE_PROMPT

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

        # ---- Search (Tavily) API Key ----
        search_sep = QLabel("Web Search (Tavily)")
        search_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", search_sep)

        tavily_key_row = QHBoxLayout()
        tavily_key_row.setSpacing(6)
        self._tavily_key_input = QLineEdit()
        self._tavily_key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._tavily_key_input.setPlaceholderText("Paste Tavily API key here...")
        tavily_key_row.addWidget(self._tavily_key_input, 1)

        self._save_tavily_btn = QPushButton("Save")
        self._save_tavily_btn.clicked.connect(self._on_save_tavily_key)
        tavily_key_row.addWidget(self._save_tavily_btn)

        self._clear_tavily_btn = QPushButton("Clear")
        self._clear_tavily_btn.clicked.connect(self._on_clear_tavily_key)
        tavily_key_row.addWidget(self._clear_tavily_btn)

        tavily_widget = QWidget()
        tavily_widget.setLayout(tavily_key_row)
        form.addRow("Tavily Key:", tavily_widget)

        self._tavily_status = QLabel("")
        self._tavily_status.setWordWrap(True)
        form.addRow("", self._tavily_status)
        self._refresh_tavily_status()

        outer.addLayout(form)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

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
            auto_commit_enabled=self._auto_commit_chk.isChecked(),
            auto_dispatch=self._auto_dispatch_chk.isChecked(),
            auto_approve=self._auto_approve_chk.isChecked(),
            sandbox_mode=self._sandbox_combo.currentData(),
            tavily_api_key=self._settings.tavily_api_key, # preserve if set via other means
        )

    def accept(self) -> None:  # type: ignore[override]
        # Persist on OK.
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()
