"""Modal Settings dialog — exposes the persisted AppSettings + a few
read-only environment / workspace facts.
"""
from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import (
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
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from aura.backends import (
    ClaudeCodeBackend,
    CodexBackend,
    GeminiCLIBackend,
)
from aura.config import (
    APP_NAME,
    AppSettings,
    ProviderId,
    fetch_provider_models,
    get_api_key,
    save_dynamic_catalog,
    save_settings,
    set_api_key,
)
from aura.providers.registry import provider_registry
from aura.gui.theme import FG_DIM, SUCCESS, WARN
from aura.gui.aura_widget import GlassSwitch

logger = logging.getLogger(__name__)

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


class AuthWorker(QObject):
    """Background worker for CLI authentication."""

    finished = Signal(str, bool, str, str)  # backend_name, success, message, error

    def __init__(self, backend_name: str) -> None:
        super().__init__()
        self._backend_name = backend_name

    def run(self) -> None:
        """Run the CLI auth flow and emit finished with the result."""
        ok = False
        message = ""
        error = ""
        try:
            if self._backend_name == "gemini_cli":
                backend = GeminiCLIBackend()
                ok = backend.run_cli_auth()
            elif self._backend_name == "claude_code":
                backend = ClaudeCodeBackend()
                ok = backend.run_cli_auth()
            elif self._backend_name == "codex":
                backend = CodexBackend()
                ok = backend.run_cli_auth()
            elif self._backend_name == "codex_device":
                backend = CodexBackend()
                ok = backend.run_device_auth()
            if ok:
                message = f"{self._backend_name} authenticated successfully."
            else:
                message = f"{self._backend_name} authentication did not complete."
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("AuthWorker failed for %s: %s", self._backend_name, exc)
            ok = False
        self.finished.emit(self._backend_name, ok, message, error)


class AuthStatusWorker(QObject):
    """Background worker for checking CLI auth status without blocking."""

    finished = Signal(dict)

    def run(self) -> None:
        """Check all CLI backends and emit results."""
        results = {}
        try:
            results["gemini_cli"] = GeminiCLIBackend().check_auth()
        except Exception:
            logger.exception("check_auth failed for gemini_cli")
            results["gemini_cli"] = False
        try:
            results["claude_code"] = ClaudeCodeBackend().check_auth()
        except Exception:
            logger.exception("check_auth failed for claude_code")
            results["claude_code"] = False
        try:
            results["codex"] = CodexBackend().check_auth()
        except Exception:
            logger.exception("check_auth failed for codex")
            results["codex"] = False
        self.finished.emit(results)


class AuthPollingWorker(QObject):
    """Poll check_auth() for a single backend until auth succeeds or timeout."""

    finished = Signal(str, bool)  # backend_name, authed

    def __init__(self, backend_name: str, max_seconds: int = 120) -> None:
        super().__init__()
        self._backend_name = backend_name
        self._max_seconds = max_seconds

    def run(self) -> None:
        """Poll check_auth() every 2 seconds until authed or timeout."""
        import time

        deadline = time.monotonic() + self._max_seconds
        authed = False
        while time.monotonic() < deadline:
            try:
                if self._backend_name == "gemini_cli":
                    authed = GeminiCLIBackend().check_auth()
                elif self._backend_name == "claude_code":
                    authed = ClaudeCodeBackend().check_auth()
                elif self._backend_name == "codex":
                    authed = CodexBackend().check_auth()
                else:
                    break
                if authed:
                    break
            except Exception:
                logger.exception("AuthPollingWorker check_auth failed for %s", self._backend_name)
                break
            time.sleep(2)
        self.finished.emit(self._backend_name, authed)


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

        self._settings = copy.deepcopy(settings)
        self._on_change_root = on_change_root

        # Thread tracking for safe cleanup
        self._auth_thread: QThread | None = None
        self._auth_worker: AuthWorker | None = None
        self._auth_polling_thread: QThread | None = None
        self._auth_polling_worker: AuthPollingWorker | None = None
        self._auth_status_thread: QThread | None = None
        self._auth_status_worker: AuthStatusWorker | None = None
        self._discovery_thread: QThread | None = None
        self._discovery_worker: DiscoveryWorker | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 14)
        outer.setSpacing(14)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumHeight(420)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 10, 0)
        content_layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # ---- Provider selection ----
        provider_row = QHBoxLayout()
        self._provider_combo = QComboBox()
        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            self._provider_combo.addItem(spec.label, pid)
        self._provider_combo.setCurrentIndex(
            provider_registry.ids().index(self._settings.provider)
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

        # Planner Provider
        self._planner_provider_combo = QComboBox()
        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            self._planner_provider_combo.addItem(spec.label, pid)
        self._planner_provider_combo.setCurrentIndex(
            provider_registry.ids().index(self._settings.planner_provider)
        )
        self._planner_provider_combo.currentIndexChanged.connect(self._on_planner_provider_changed)
        form.addRow("Planner provider:", self._planner_provider_combo)

        self._planner_model_combo = QComboBox()
        form.addRow("Planner model:", self._planner_model_combo)

        self._planner_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._planner_thinking_combo.addItem(label, val)
        form.addRow("Planner thinking:", self._planner_thinking_combo)

        # Worker Provider
        self._worker_provider_combo = QComboBox()
        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            self._worker_provider_combo.addItem(spec.label, pid)
        self._worker_provider_combo.setCurrentIndex(
            provider_registry.ids().index(self._settings.worker_provider)
        )
        self._worker_provider_combo.currentIndexChanged.connect(self._on_worker_provider_changed)
        form.addRow("Worker provider:", self._worker_provider_combo)

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

        self._max_rounds_spin = QSpinBox()
        self._max_rounds_spin.setRange(1, 500)
        self._max_rounds_spin.setToolTip(
            "Maximum number of tool-call rounds allowed in a single user turn."
        )
        self._max_rounds_spin.setValue(self._settings.max_tool_rounds)
        form.addRow("Max tool rounds:", self._max_rounds_spin)

        # --- Agent Backends ---
        backend_sep = QLabel("Agent Backends")
        backend_sep.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", backend_sep)

        backend_note = QLabel(
            "CLI-based agents require additional setup. "
            "Click 'Configure' next to each agent for instructions."
        )
        backend_note.setStyleSheet(f"color: {FG_DIM}; font-size: 10px;")
        backend_note.setWordWrap(True)
        form.addRow("", backend_note)

        # ---- Gemini CLI (npm) ----
        gemini_cli_container = QWidget()
        gemini_cli_layout = QVBoxLayout(gemini_cli_container)
        gemini_cli_layout.setContentsMargins(0, 0, 0, 0)
        gemini_cli_layout.setSpacing(2)

        gemini_cli_row = QHBoxLayout()
        gemini_cli_row.setSpacing(6)
        gemini_cli_label = QLabel("Gemini CLI")
        self._gemini_cli_auth_status = QLabel("Checking...")
        self._gemini_cli_auth_btn = QPushButton("Login")
        self._gemini_cli_auth_btn.clicked.connect(self._on_gemini_cli_auth)
        self._gemini_cli_recheck_btn = QPushButton("Recheck Status")
        self._gemini_cli_recheck_btn.clicked.connect(
            lambda: self._on_recheck_status("gemini_cli")
        )
        self._gemini_cli_recheck_btn.hide()
        gemini_cli_row.addWidget(gemini_cli_label, 1)
        gemini_cli_row.addWidget(self._gemini_cli_auth_status)
        gemini_cli_row.addWidget(self._gemini_cli_auth_btn)
        gemini_cli_row.addWidget(self._gemini_cli_recheck_btn)
        gemini_cli_layout.addLayout(gemini_cli_row)

        self._gemini_cli_auth_msg = QLabel("")
        self._gemini_cli_auth_msg.setWordWrap(True)
        self._gemini_cli_auth_msg.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        self._gemini_cli_auth_msg.hide()
        gemini_cli_layout.addWidget(self._gemini_cli_auth_msg)

        form.addRow("", gemini_cli_container)

        # ---- Claude Code ----
        claude_container = QWidget()
        claude_layout = QVBoxLayout(claude_container)
        claude_layout.setContentsMargins(0, 0, 0, 0)
        claude_layout.setSpacing(2)

        claude_row = QHBoxLayout()
        claude_row.setSpacing(6)
        claude_label = QLabel("Claude Code")
        self._claude_auth_status = QLabel("Checking...")
        self._claude_auth_btn = QPushButton("Login")
        self._claude_auth_btn.clicked.connect(self._on_claude_auth)
        self._claude_recheck_btn = QPushButton("Recheck Status")
        self._claude_recheck_btn.clicked.connect(
            lambda: self._on_recheck_status("claude_code")
        )
        self._claude_recheck_btn.hide()
        claude_row.addWidget(claude_label, 1)
        claude_row.addWidget(self._claude_auth_status)
        claude_row.addWidget(self._claude_auth_btn)
        claude_row.addWidget(self._claude_recheck_btn)
        claude_layout.addLayout(claude_row)

        self._claude_auth_msg = QLabel("")
        self._claude_auth_msg.setWordWrap(True)
        self._claude_auth_msg.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        self._claude_auth_msg.hide()
        claude_layout.addWidget(self._claude_auth_msg)

        form.addRow("", claude_container)

        # ---- Codex ----
        codex_container = QWidget()
        codex_layout = QVBoxLayout(codex_container)
        codex_layout.setContentsMargins(0, 0, 0, 0)
        codex_layout.setSpacing(2)

        codex_row = QHBoxLayout()
        codex_row.setSpacing(6)
        codex_label = QLabel("Codex CLI")
        self._codex_auth_status = QLabel("Checking...")
        self._codex_auth_btn = QPushButton("Login")
        self._codex_auth_btn.clicked.connect(self._on_codex_auth)
        self._codex_recheck_btn = QPushButton("Recheck Status")
        self._codex_recheck_btn.clicked.connect(
            lambda: self._on_recheck_status("codex")
        )
        self._codex_recheck_btn.hide()
        self._codex_device_auth_btn = QPushButton("Use Device Auth")
        self._codex_device_auth_btn.clicked.connect(self._on_codex_device_auth)
        self._codex_device_auth_btn.hide()
        codex_row.addWidget(codex_label, 1)
        codex_row.addWidget(self._codex_auth_status)
        codex_row.addWidget(self._codex_auth_btn)
        codex_row.addWidget(self._codex_recheck_btn)
        codex_row.addWidget(self._codex_device_auth_btn)
        codex_layout.addLayout(codex_row)

        self._codex_auth_msg = QLabel("")
        self._codex_auth_msg.setWordWrap(True)
        self._codex_auth_msg.setStyleSheet(f"color: {WARN}; font-size: 10px;")
        self._codex_auth_msg.hide()
        codex_layout.addWidget(self._codex_auth_msg)

        form.addRow("", codex_container)

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

        content_layout.addLayout(form)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        self._start_auth_status_check()

    def _start_auth_status_check(self) -> None:
        """Check initial CLI auth status asynchronously."""
        self._auth_status_thread = QThread(self)
        self._auth_status_worker = AuthStatusWorker()
        self._auth_status_worker.moveToThread(self._auth_status_thread)
        self._auth_status_thread.started.connect(self._auth_status_worker.run)
        self._auth_status_worker.finished.connect(self._on_auth_status_finished)
        self._auth_status_worker.finished.connect(self._auth_status_thread.quit)
        self._auth_status_worker.finished.connect(self._auth_status_worker.deleteLater)
        self._auth_status_thread.finished.connect(self._auth_status_thread.deleteLater)
        self._auth_status_thread.start()

    def _cleanup_thread(self, thread_attr: str, worker_attr: str, wait_ms: int = 15000) -> None:
        """Stop a dialog-owned QThread before Qt can destroy it."""
        thread = getattr(self, thread_attr, None)
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(wait_ms):
                    logger.warning("Settings dialog thread did not stop cleanly: %s", thread_attr)
                    thread.wait()
        except RuntimeError:
            pass

    def _cleanup_threads(self) -> None:
        for thread_attr, worker_attr in (
            ("_auth_thread", "_auth_worker"),
            ("_auth_polling_thread", "_auth_polling_worker"),
            ("_auth_status_thread", "_auth_status_worker"),
            ("_discovery_thread", "_discovery_worker"),
        ):
            self._cleanup_thread(thread_attr, worker_attr)

    def done(self, result: int) -> None:  # type: ignore[override]
        self._cleanup_threads()
        super().done(result)

    def closeEvent(self, event):  # type: ignore[override]
        """Clean up any running auth/polling threads when the dialog is closed."""
        self._cleanup_threads()
        super().closeEvent(event)

    # --- Provider / Model helpers ---

    def _on_provider_changed(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()
        self._populate_model_combos(provider_id)
        self._refresh_api_key_status(provider_id)
        self._api_key_input.setPlaceholderText("Paste API key here...")

    def _on_planner_provider_changed(self) -> None:
        provider_id: ProviderId = self._planner_provider_combo.currentData()
        self._populate_role_models(self._planner_model_combo, provider_id, self._settings.default_planner_model)

    def _on_worker_provider_changed(self) -> None:
        provider_id: ProviderId = self._worker_provider_combo.currentData()
        self._populate_role_models(self._worker_model_combo, provider_id, self._settings.default_worker_model)

    def _populate_model_combos(self, provider_id: ProviderId) -> None:
        self._populate_role_models(self._model_combo, self._provider_combo.currentData(), self._settings.default_model)
        self._populate_role_models(self._planner_model_combo, self._planner_provider_combo.currentData(), self._settings.default_planner_model)
        self._populate_role_models(self._worker_model_combo, self._worker_provider_combo.currentData(), self._settings.default_worker_model)

        self._set_combo_to_data(self._thinking_combo, self._settings.default_thinking)
        self._set_combo_to_data(
            self._planner_thinking_combo, self._settings.default_planner_thinking
        )
        self._set_combo_to_data(
            self._worker_thinking_combo, self._settings.default_worker_thinking
        )

    def _populate_role_models(self, combo: QComboBox, provider_id: ProviderId, current_selection: str) -> None:
        cfg = provider_registry.get(provider_id)
        combo.blockSignals(True)
        combo.clear()
        for info in cfg.models.values():
            combo.addItem(info.label, info.id)
        if current_selection and combo.findData(current_selection) < 0:
            combo.addItem(current_selection, current_selection)
        idx = combo.findData(current_selection)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        else:
            default_idx = combo.findData(cfg.default_model)
            if default_idx >= 0:
                combo.setCurrentIndex(default_idx)
        combo.blockSignals(False)

    def _set_combo_to_data(self, combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    def _refresh_api_key_status(self, provider_id: ProviderId) -> None:
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
        self._api_key_status.setText(text)
        self._api_key_status.setStyleSheet(f"color: {color};")

    def _on_save_api_key(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()
        key = self._api_key_input.text().strip()
        if not key:
            QMessageBox.information(self, APP_NAME, "Paste an API key before saving.")
            return
        set_api_key(provider_id, key)
        self._api_key_input.clear()
        self._refresh_api_key_status(provider_id)

    def _on_clear_api_key(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()
        from aura.key_manager import get_key_manager

        get_key_manager().delete_key(provider_id)
        self._refresh_api_key_status(provider_id)

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
        save_settings(self.result_settings())
        self._refresh_tavily_status()

    def _on_clear_tavily_key(self) -> None:
        self._settings.tavily_api_key = ""
        save_settings(self.result_settings())
        self._refresh_tavily_status()

    def _on_refresh_models(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()
        self._refresh_btn.setEnabled(False)
        self._refresh_btn.setText("Refreshing...")

        self._discovery_thread = QThread(self)
        self._discovery_worker = DiscoveryWorker(provider_id)
        self._discovery_worker.moveToThread(self._discovery_thread)
        self._discovery_thread.started.connect(self._discovery_worker.run)
        self._discovery_worker.finished.connect(self._on_models_refreshed)
        self._discovery_worker.finished.connect(self._discovery_thread.quit)
        self._discovery_worker.finished.connect(self._discovery_worker.deleteLater)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        self._discovery_thread.start()

    def _on_models_refreshed(
        self,
        provider_id: str,
        models: dict,
        pricing: dict,
        error: str,
    ) -> None:
        self._refresh_btn.setEnabled(True)
        self._refresh_btn.setText("Refresh")
        if error:
            QMessageBox.warning(self, APP_NAME, f"Could not refresh models:\n{error}")
            return

        cfg = provider_registry.get(provider_id)  # type: ignore[arg-type]
        cfg.models.update(models)
        cfg.pricing.update(pricing)
        save_dynamic_catalog(provider_id, models, pricing)  # type: ignore[arg-type]
        self._populate_model_combos(provider_id)  # type: ignore[arg-type]
        QMessageBox.information(self, APP_NAME, "Model list refreshed.")

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

    # --- Auth UI helpers ---

    def _on_auth_status_finished(self, results: dict[str, bool]) -> None:
        """Update UI labels based on background auth checks."""
        self._update_auth_ui(
            "gemini_cli",
            self._gemini_cli_auth_status,
            self._gemini_cli_auth_btn,
            results.get("gemini_cli", False),
            recheck_btn=self._gemini_cli_recheck_btn,
            msg_label=self._gemini_cli_auth_msg,
        )
        self._update_auth_ui(
            "claude_code",
            self._claude_auth_status,
            self._claude_auth_btn,
            results.get("claude_code", False),
            recheck_btn=self._claude_recheck_btn,
            msg_label=self._claude_auth_msg,
        )
        self._update_auth_ui(
            "codex",
            self._codex_auth_status,
            self._codex_auth_btn,
            results.get("codex", False),
            recheck_btn=self._codex_recheck_btn,
            msg_label=self._codex_auth_msg,
            device_auth_btn=self._codex_device_auth_btn,
        )

        pass

    def _update_auth_ui(
        self,
        name: str,
        label: QLabel,
        btn: QPushButton,
        authed: bool,
        message: str = "",
        recheck_btn: QPushButton | None = None,
        msg_label: QLabel | None = None,
        device_auth_btn: QPushButton | None = None,
    ) -> None:
        if authed:
            label.setText("\u2713 Authenticated")
            label.setStyleSheet(f"color: {SUCCESS};")
            btn.hide()
            if recheck_btn:
                recheck_btn.hide()
            if device_auth_btn:
                device_auth_btn.hide()
            if msg_label:
                msg_label.hide()
        else:
            label.setText("\u2717 Login Required")
            label.setStyleSheet(f"color: {WARN};")
            btn.show()
            btn.setText("Login")
            btn.setEnabled(True)
            if recheck_btn:
                recheck_btn.show()
            if device_auth_btn:
                device_auth_btn.show()
            if msg_label:
                if message:
                    msg_label.setText(message)
                    msg_label.show()
                else:
                    msg_label.hide()

    def _get_msg_label(self, backend_name: str) -> QLabel | None:
        mapping = {
            "gemini_cli": self._gemini_cli_auth_msg,
            "claude_code": self._claude_auth_msg,
            "codex": self._codex_auth_msg,
        }
        return mapping.get(backend_name)

    def _get_recheck_btn(self, backend_name: str) -> QPushButton | None:
        mapping = {
            "gemini_cli": self._gemini_cli_recheck_btn,
            "claude_code": self._claude_recheck_btn,
            "codex": self._codex_recheck_btn,
        }
        return mapping.get(backend_name)

    def _get_device_auth_btn(self, backend_name: str) -> QPushButton | None:
        if backend_name == "codex":
            return self._codex_device_auth_btn
        return None

    def _get_status_label(self, backend_name: str) -> QLabel | None:
        mapping = {
            "gemini_cli": self._gemini_cli_auth_status,
            "claude_code": self._claude_auth_status,
            "codex": self._codex_auth_status,
        }
        return mapping.get(backend_name)

    def _get_auth_btn(self, backend_name: str) -> QPushButton | None:
        mapping = {
            "gemini_cli": self._gemini_cli_auth_btn,
            "claude_code": self._claude_auth_btn,
            "codex": self._codex_auth_btn,
        }
        return mapping.get(backend_name)

    def _check_gemini_cli_auth(self) -> None:
        """Query gemini CLI credential state and update the UI accordingly."""
        try:
            authed = GeminiCLIBackend().check_auth()
        except Exception:
            authed = False
        self._update_auth_ui(
            "gemini_cli", self._gemini_cli_auth_status, self._gemini_cli_auth_btn, authed,
            recheck_btn=self._gemini_cli_recheck_btn,
            msg_label=self._gemini_cli_auth_msg,
        )

    def _on_gemini_cli_auth(self) -> None:
        """Launch the CLI auth flow in a background thread."""
        self._launch_cli_auth(
            "gemini_cli",
            self._gemini_cli_auth_btn,
            self._gemini_cli_auth_status,
            self._gemini_cli_recheck_btn,
            self._gemini_cli_auth_msg,
            device_auth_btn=None,
        )

    def _check_claude_auth(self) -> None:
        try:
            authed = ClaudeCodeBackend().check_auth()
        except Exception:
            authed = False
        self._update_auth_ui(
            "claude_code", self._claude_auth_status, self._claude_auth_btn, authed,
            recheck_btn=self._claude_recheck_btn,
            msg_label=self._claude_auth_msg,
        )

    def _on_claude_auth(self) -> None:
        self._launch_cli_auth(
            "claude_code",
            self._claude_auth_btn,
            self._claude_auth_status,
            self._claude_recheck_btn,
            self._claude_auth_msg,
            device_auth_btn=None,
        )

    def _check_codex_auth(self) -> None:
        try:
            authed = CodexBackend().check_auth()
        except Exception:
            authed = False
        self._update_auth_ui(
            "codex", self._codex_auth_status, self._codex_auth_btn, authed,
            recheck_btn=self._codex_recheck_btn,
            msg_label=self._codex_auth_msg,
            device_auth_btn=self._codex_device_auth_btn,
        )

    def _on_codex_auth(self) -> None:
        self._launch_cli_auth(
            "codex",
            self._codex_auth_btn,
            self._codex_auth_status,
            self._codex_recheck_btn,
            self._codex_auth_msg,
            device_auth_btn=self._codex_device_auth_btn,
        )

    def _launch_cli_auth(
        self,
        backend_name: str,
        btn: QPushButton,
        status_label: QLabel,
        recheck_btn: QPushButton,
        msg_label: QLabel,
        device_auth_btn: QPushButton | None = None,
    ) -> None:
        """Launch CLI auth in a background thread with new signal signature."""
        btn.setEnabled(False)
        btn.setText("Launching terminal...")
        status_label.setText("Authenticating...")
        status_label.setStyleSheet(f"color: {FG_DIM};")

        # Show recheck button; hide device auth initially
        recheck_btn.show()
        if device_auth_btn:
            device_auth_btn.show()
        msg_label.hide()

        self._auth_thread = QThread(self)
        self._auth_worker = AuthWorker(backend_name)
        self._auth_worker.moveToThread(self._auth_thread)
        self._auth_thread.started.connect(self._auth_worker.run)

        self._auth_worker.finished.connect(
            lambda name, ok, message, error: self._on_auth_finished(
                name, ok, message, error,
                btn, status_label, recheck_btn, msg_label, device_auth_btn,
            )
        )

        self._auth_worker.finished.connect(self._auth_thread.quit)
        self._auth_worker.finished.connect(self._auth_worker.deleteLater)
        self._auth_thread.finished.connect(self._auth_thread.deleteLater)
        self._auth_thread.start()

    def _on_auth_finished(
        self,
        backend_name: str,
        ok: bool,
        message: str,
        error: str,
        btn: QPushButton | None = None,
        status_label: QLabel | None = None,
        recheck_btn: QPushButton | None = None,
        msg_label: QLabel | None = None,
        device_auth_btn: QPushButton | None = None,
    ) -> None:
        """Callback when the CLI auth thread completes."""
        if btn is None:
            btn = self._get_auth_btn(backend_name)
        if status_label is None:
            status_label = self._get_status_label(backend_name)
        if recheck_btn is None:
            recheck_btn = self._get_recheck_btn(backend_name)
        if msg_label is None:
            msg_label = self._get_msg_label(backend_name)
        if device_auth_btn is None:
            device_auth_btn = self._get_device_auth_btn(backend_name)

        if ok:
            # Re-check auth status to confirm
            if backend_name == "gemini_cli":
                self._check_gemini_cli_auth()
            elif backend_name == "claude_code":
                self._check_claude_auth()
            elif backend_name == "codex":
                self._check_codex_auth()
        else:
            # Show error/message in the message label
            display_msg = ""
            if error:
                display_msg = f"Error: {error}"
            elif message:
                display_msg = message
            else:
                display_msg = "Authentication did not complete."

            # For codex, append manual instructions
            if backend_name == "codex":
                display_msg += "\n\n" + CodexBackend.get_manual_auth_instructions()

            if msg_label:
                msg_label.setText(display_msg)
                msg_label.show()

            # Restore Login button so user can retry
            if btn:
                btn.setEnabled(True)
                btn.setText("Login")

            if error:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    f"Authentication failed for {backend_name}:\n{error}",
                )

        pass

    def _on_recheck_status(self, backend_name: str) -> None:
        """Re-check auth status using a polling worker with a short timeout."""
        status_label = self._get_status_label(backend_name)
        if status_label:
            status_label.setText("Checking...")
            status_label.setStyleSheet(f"color: {FG_DIM};")

        recheck_btn = self._get_recheck_btn(backend_name)
        if recheck_btn:
            recheck_btn.setEnabled(False)

        self._auth_polling_thread = QThread(self)
        self._auth_polling_worker = AuthPollingWorker(backend_name, max_seconds=15)
        self._auth_polling_worker.moveToThread(self._auth_polling_thread)
        self._auth_polling_thread.started.connect(self._auth_polling_worker.run)

        self._auth_polling_worker.finished.connect(
            lambda name, authed: self._on_polling_finished(name, authed)
        )
        self._auth_polling_worker.finished.connect(self._auth_polling_thread.quit)
        self._auth_polling_worker.finished.connect(self._auth_polling_worker.deleteLater)
        self._auth_polling_thread.finished.connect(self._auth_polling_thread.deleteLater)
        self._auth_polling_thread.start()

    def _on_polling_finished(self, backend_name: str, authed: bool) -> None:
        """Callback when the polling worker completes."""
        status_label = self._get_status_label(backend_name)
        auth_btn = self._get_auth_btn(backend_name)
        recheck_btn = self._get_recheck_btn(backend_name)
        msg_label = self._get_msg_label(backend_name)
        device_auth_btn = self._get_device_auth_btn(backend_name)

        self._update_auth_ui(
            backend_name,
            status_label,
            auth_btn,
            authed,
            recheck_btn=recheck_btn,
            msg_label=msg_label,
            device_auth_btn=device_auth_btn,
        )

        if recheck_btn:
            recheck_btn.setEnabled(True)

    def _on_codex_device_auth(self) -> None:
        """Launch codex device-auth (--device-auth) flow."""
        backend_name = "codex_device"
        btn = self._codex_auth_btn
        status_label = self._codex_auth_status
        recheck_btn = self._codex_recheck_btn
        msg_label = self._codex_auth_msg

        btn.setEnabled(False)
        btn.setText("Launching device auth...")
        status_label.setText("Authenticating...")
        status_label.setStyleSheet(f"color: {FG_DIM};")
        recheck_btn.show()
        msg_label.hide()

        self._auth_thread = QThread(self)
        self._auth_worker = AuthWorker(backend_name)
        self._auth_worker.moveToThread(self._auth_thread)
        self._auth_thread.started.connect(self._auth_worker.run)

        self._auth_worker.finished.connect(
            lambda name, ok, message, error: self._on_auth_finished(
                "codex", ok, message, error,
                btn, status_label, recheck_btn, msg_label, self._codex_device_auth_btn,
            )
        )

        self._auth_worker.finished.connect(self._auth_thread.quit)
        self._auth_worker.finished.connect(self._auth_worker.deleteLater)
        self._auth_thread.finished.connect(self._auth_thread.deleteLater)
        self._auth_thread.start()

    def _cancel_auth_waiting(self, backend_name: str) -> None:
        """Cancel any waiting auth state and restore the UI for a given backend."""
        logger.info("User cancelled auth waiting for %s", backend_name)

        btn = self._get_auth_btn(backend_name)
        if btn:
            btn.setEnabled(True)
            btn.setText("Login")

        status_label = self._get_status_label(backend_name)
        if status_label:
            status_label.setText("\u2717 Login Required")
            status_label.setStyleSheet(f"color: {WARN};")

        recheck_btn = self._get_recheck_btn(backend_name)
        if recheck_btn:
            recheck_btn.hide()

        device_auth_btn = self._get_device_auth_btn(backend_name)
        if device_auth_btn:
            device_auth_btn.hide()

        msg_label = self._get_msg_label(backend_name)
        if msg_label:
            msg_label.hide()

    # --- Result ---

    def result_settings(self) -> AppSettings:

        """Read the current widget values and return a fresh AppSettings."""
        provider_id: ProviderId = self._provider_combo.currentData()

        result = AppSettings(
            provider=provider_id,
            planner_provider=self._planner_provider_combo.currentData(),
            worker_provider=self._worker_provider_combo.currentData(),
            default_model=self._model_combo.currentData(),
            default_thinking=self._thinking_combo.currentData(),
            restore_last_conversation=self._restore_chk.isChecked(),
            planner_worker_mode=self._pw_mode_chk.isChecked(),
            show_planner_reasoning=self._settings.show_planner_reasoning,
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
            max_tool_rounds=self._max_rounds_spin.value(),
            sandbox_mode=self._sandbox_combo.currentData(),
            tavily_api_key=self._settings.tavily_api_key, # preserve if set via other means
            terminal_window_geometry=self._settings.terminal_window_geometry,
            first_launch_done=self._settings.first_launch_done,
            onboarding_checklist=dict(self._settings.onboarding_checklist),
            onboarding_version=self._settings.onboarding_version,
            humanizer_enabled=self._settings.humanizer_enabled,
            humanizer_gate_enabled=self._settings.humanizer_gate_enabled,
            humanizer_gate_min_severity=self._settings.humanizer_gate_min_severity,
            humanizer_feature_log=self._settings.humanizer_feature_log,
            humanizer_observe=self._settings.humanizer_observe,
        )
        if not result.planner_worker_mode:
            result.planner_provider = result.provider
            result.default_planner_model = result.default_model
            result.default_planner_thinking = result.default_thinking
        return result

    def accept(self) -> None:  # type: ignore[override]
        # Persist on OK.
        new_settings = self.result_settings()
        save_settings(new_settings)
        self._settings = new_settings
        super().accept()
