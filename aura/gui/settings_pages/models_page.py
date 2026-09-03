from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    AppSettings,
    fetch_provider_models,
    get_provider_kind,
    resolve_production_default_model,
    save_dynamic_catalog,
)
from aura.gui.theme import DANGER, FG_DIM, FG_MUTED, SUCCESS, WARN
from aura.gui.widgets.no_wheel_combo import NoWheelComboBox
from aura.gui.widgets.searchable_model_combo import SearchableModelCombo
from aura.providers.base import ProviderId
from aura.providers.local_openai import (
    DEFAULT_LOCAL_OPENAI_BASE_URL,
    is_valid_local_openai_base_url,
    normalize_local_openai_base_url,
)
from aura.providers.model_presentation import build_model_picker_items
from aura.providers.registry import provider_registry

logger = logging.getLogger(__name__)

_THINKING_ITEMS: list[tuple[str, str]] = [
    ("Off", "off"),
    ("High", "high"),
    ("Max", "max"),
]

_LOCAL_PROVIDER_ID = "local_openai"


class DiscoveryWorker(QObject):
    finished = Signal(str, dict, dict, str)  # provider_id, models, pricing, error_msg

    def __init__(self, provider_id: ProviderId, *, base_url: str | None = None):
        super().__init__()
        self.provider_id = provider_id
        self.base_url = base_url

    def run(self):
        try:
            models, pricing, error = fetch_provider_models(
                self.provider_id,
                base_url=self.base_url,
            )
            self.finished.emit(self.provider_id, models, pricing, error or "")
        except Exception as exc:
            self.finished.emit(self.provider_id, {}, {}, str(exc))


class ModelsPage(QWidget):
    """One production configuration: provider, model, thinking, temperature.

    Normal Aura coding runs one continuous production model.
    """

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._discovery_inflight: set[str] = set()
        self._discovery_threads: dict[str, QThread] = {}
        self._discovery_workers: dict[str, DiscoveryWorker] = {}
        self._discovery_base_urls: dict[str, str | None] = {}
        self._closing: bool = False

        local_cfg = provider_registry.get(_LOCAL_PROVIDER_ID)
        self._local_baseline_models = dict(local_cfg.models)
        self._local_baseline_pricing = dict(local_cfg.pricing)
        self._local_catalog_dirty = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # --- 1. Create Widgets ---

        heading = QLabel("Production model")
        heading.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", heading)

        self._provider_combo = NoWheelComboBox()
        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            kind = get_provider_kind(pid)
            kind_label = {
                "api_key": "API Key",
                "external_cli": "External CLI",
                "local": "Local",
            }.get(kind, kind)
            self._provider_combo.addItem(f"{spec.label} ({kind_label})", pid)

        self._model_combo = SearchableModelCombo()

        self._local_endpoint_edit = QLineEdit(
            getattr(
                self._settings,
                "local_openai_base_url",
                DEFAULT_LOCAL_OPENAI_BASE_URL,
            )
        )
        self._local_endpoint_edit.setPlaceholderText(DEFAULT_LOCAL_OPENAI_BASE_URL)
        self._local_endpoint_edit.setToolTip(
            "OpenAI-compatible /v1 endpoint. Examples: "
            "Ollama http://127.0.0.1:11434/v1; "
            "LM Studio http://127.0.0.1:1234/v1; "
            "llama.cpp http://127.0.0.1:8080/v1."
        )
        self._local_discover_btn = QPushButton("Test / Discover")
        self._local_discover_btn.setToolTip(
            "Connect to the endpoint and refresh its available models"
        )

        self._local_endpoint_row = QWidget(self)
        local_endpoint_layout = QHBoxLayout(self._local_endpoint_row)
        local_endpoint_layout.setContentsMargins(0, 0, 0, 0)
        local_endpoint_layout.setSpacing(6)
        local_endpoint_layout.addWidget(self._local_endpoint_edit, 1)
        local_endpoint_layout.addWidget(self._local_discover_btn)
        self._local_endpoint_label = QLabel("Endpoint:")

        self._local_status_label = QLabel("")
        self._local_status_label.setWordWrap(True)

        self._refresh_btn = QPushButton("↻ Refresh")
        self._refresh_btn.setFixedHeight(20)
        self._refresh_btn.setStyleSheet(
            "QPushButton { background: transparent; border: none; color: #888; font-size: 10px; padding: 0 4px; }"
            "QPushButton:hover { color: #ccc; }"
        )
        self._refresh_btn.setToolTip("Fetch latest models and pricing from provider")

        self._thinking_combo = NoWheelComboBox()
        for label, val in _THINKING_ITEMS:
            self._thinking_combo.addItem(label, val)

        self._temperature_spin = QDoubleSpinBox()
        self._temperature_spin.setRange(0.0, 2.0)
        self._temperature_spin.setSingleStep(0.1)
        self._temperature_spin.setDecimals(1)
        self._temperature_spin.setToolTip(
            "Controls response randomness. 0 = deterministic, 2 = maximum creativity. "
            "Only applied when thinking is Off."
        )

        # --- 2. Setup Layout ---

        form.addRow("Provider:", self._provider_combo)

        form.addRow(self._local_endpoint_label, self._local_endpoint_row)
        form.addRow("", self._local_status_label)

        model_row = QHBoxLayout()
        model_row.setSpacing(4)
        model_row.addWidget(self._model_combo, 1)
        model_row.addWidget(self._refresh_btn)
        form.addRow("Model:", model_row)

        form.addRow("Thinking:", self._thinking_combo)
        form.addRow("Temperature:", self._temperature_spin)

        layout.addLayout(form)
        layout.addStretch()

        # --- 3. Initial Values ---

        provider_ids = provider_registry.ids()
        current_provider = (
            self._settings.provider
            if self._settings.provider in provider_ids
            else (provider_ids[0] if provider_ids else self._settings.provider)
        )
        if current_provider in provider_ids:
            self._provider_combo.setCurrentIndex(provider_ids.index(current_provider))

        self._populate_models(current_provider, self._settings.default_model)
        self._refresh_btn.setVisible(self._provider_combo.currentData() == "openrouter")
        self._set_combo_to_data(self._thinking_combo, self._settings.default_thinking)
        self._sync_thinking_for_provider(current_provider, use_provider_default=False)
        self._local_verified_endpoint: str | None = None
        local_endpoint = normalize_local_openai_base_url(
            self._local_endpoint_edit.text()
        )
        self._local_baseline_endpoint = local_endpoint
        if (
            local_endpoint
            and provider_registry.has(_LOCAL_PROVIDER_ID)
            and provider_registry.get(_LOCAL_PROVIDER_ID).models
        ):
            # A persisted endpoint plus its cached catalog is a usable prior
            # configuration. A changed endpoint must be tested before it can
            # replace that pairing.
            self._local_verified_endpoint = local_endpoint
        self._sync_local_controls(current_provider)
        if current_provider != _LOCAL_PROVIDER_ID:
            self._start_discovery(current_provider)

        self._temperature_spin.setValue(self._settings.temperature)

        # --- 4. Connect Signals ---
        # Connect AFTER initial population to avoid spurious signal firing.
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self._refresh_btn.clicked.connect(
            lambda: self._start_discovery(self._provider_combo.currentData())
        )
        self._local_discover_btn.clicked.connect(self._on_local_discover)
        self._local_endpoint_edit.textChanged.connect(self._on_local_endpoint_changed)

    # --- Thread cleanup ---

    def cleanup_threads(self) -> None:
        self._closing = True
        for provider_id, thread in list(self._discovery_threads.items()):
            try:
                if thread.isRunning():
                    thread.quit()
                    if not thread.wait(15000):
                        logger.warning(
                            "Settings dialog discovery thread did not stop cleanly: %s",
                            provider_id,
                        )
                        thread.wait()
            except RuntimeError:
                pass
            self._forget_discovery_thread(provider_id)

    # --- Model discovery ---

    def _start_discovery(
        self,
        provider_id: ProviderId,
        *,
        base_url: str | None = None,
    ) -> bool:
        if not provider_id or provider_id in self._discovery_inflight:
            return False
        self._discovery_inflight.add(provider_id)

        thread = QThread(self)
        worker = DiscoveryWorker(provider_id, base_url=base_url)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_discovery_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_discovery_thread_finished)
        thread.start()

        self._discovery_threads[provider_id] = thread
        self._discovery_workers[provider_id] = worker
        self._discovery_base_urls[provider_id] = base_url
        return True

    @Slot()
    def _on_discovery_thread_finished(self) -> None:
        thread = self.sender()
        provider_id = None
        for pid, candidate in list(self._discovery_threads.items()):
            if candidate is thread:
                provider_id = pid
                break
        if provider_id is None:
            return
        self._forget_discovery_thread(provider_id)
        if provider_id == _LOCAL_PROVIDER_ID:
            self._set_local_discovery_busy(False)

    def _forget_discovery_thread(self, provider_id: str) -> None:
        thread = self._discovery_threads.pop(provider_id, None)
        self._discovery_workers.pop(provider_id, None)
        self._discovery_base_urls.pop(provider_id, None)
        self._discovery_inflight.discard(provider_id)
        if thread is not None:
            try:
                thread.deleteLater()
            except RuntimeError:
                pass

    def _on_discovery_finished(
        self,
        provider_id: str,
        models: dict,
        pricing: dict,
        error: str,
    ) -> None:
        if self._closing:
            return
        if error:
            logger.warning("Model discovery failed for %s: %s", provider_id, error)
            if provider_id == _LOCAL_PROVIDER_ID:
                count = len(provider_registry.get(provider_id).models)
                message = f"Could not connect: {error.rstrip('.')}."
                if count:
                    message += (
                        f" Keeping {count} cached model"
                        f"{'s' if count != 1 else ''}."
                    )
                self._set_local_status(message, DANGER)
            return
        if not models:
            logger.warning(
                "Model discovery for %s returned no models; keeping existing catalog.",
                provider_id,
            )
            if provider_id == _LOCAL_PROVIDER_ID:
                self._set_local_status(
                    "Connected, but the endpoint returned no models.",
                    WARN,
                )
            return

        cfg = provider_registry.get(provider_id)  # type: ignore[arg-type]
        if provider_id in ("openrouter", _LOCAL_PROVIDER_ID):
            # OpenRouter and a local /v1/models response are current snapshots,
            # not incremental patches. Replace the shared dicts in place so a
            # model removed upstream does not linger after discovery.
            cfg.models.clear()
            cfg.models.update(models)
            cfg.pricing.clear()
            cfg.pricing.update(pricing)
        else:
            cfg.models.update(models)
            cfg.pricing.update(pricing)
        if provider_id == _LOCAL_PROVIDER_ID:
            # Keep discovery provisional until Settings Apply/OK. The provider
            # registry is shared with the live UI, so Cancel must be able to
            # restore the exact catalog it opened with.
            self._local_catalog_dirty = True
        else:
            save_dynamic_catalog(provider_id, models, pricing)  # type: ignore[arg-type]

        active: ProviderId = self._provider_combo.currentData()  # type: ignore[assignment]
        if provider_id == active:
            prior_model = self._model_combo.currentData()
            selected_model = prior_model if prior_model in models else ""
            self._populate_models(active, selected_model)
            self._refresh_btn.setVisible(provider_id == "openrouter")

        if provider_id == _LOCAL_PROVIDER_ID:
            tested_endpoint = self._discovery_base_urls.get(provider_id)
            if tested_endpoint:
                self._local_verified_endpoint = tested_endpoint
            self._set_local_status(
                f"Connected — {len(models)} model{'s' if len(models) != 1 else ''} discovered.",
                SUCCESS,
            )

    # --- Provider / Model helpers ---

    def _on_provider_changed(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()  # type: ignore[assignment]
        self._populate_models(
            provider_id,
            resolve_production_default_model(provider_id),
        )
        self._sync_local_controls(provider_id)
        self._sync_thinking_for_provider(provider_id, use_provider_default=True)
        if provider_id != _LOCAL_PROVIDER_ID:
            self._start_discovery(provider_id)
        self._refresh_btn.setVisible(provider_id == "openrouter")

    def _sync_thinking_for_provider(
        self,
        provider_id: ProviderId | None,
        *,
        use_provider_default: bool,
    ) -> None:
        if provider_id == _LOCAL_PROVIDER_ID:
            self._set_combo_to_data(self._thinking_combo, "off")
            self._thinking_combo.setEnabled(False)
            return

        self._thinking_combo.setEnabled(True)
        if (
            use_provider_default
            and provider_id
            and provider_registry.has(provider_id)
        ):
            self._set_combo_to_data(
                self._thinking_combo,
                provider_registry.get(provider_id).default_thinking,
            )

    def _sync_local_controls(self, provider_id: ProviderId | None) -> None:
        visible = provider_id == _LOCAL_PROVIDER_ID
        self._local_endpoint_label.setVisible(visible)
        self._local_endpoint_row.setVisible(visible)
        self._local_status_label.setVisible(visible)
        if visible:
            self._paint_local_status()

    def _on_local_discover(self) -> None:
        endpoint = normalize_local_openai_base_url(self._local_endpoint_edit.text())
        if not is_valid_local_openai_base_url(endpoint):
            self._set_local_status(
                "Enter a valid http:// or https:// endpoint before testing.",
                WARN,
            )
            return
        self._local_endpoint_edit.setText(endpoint)
        if self._start_discovery(_LOCAL_PROVIDER_ID, base_url=endpoint):
            self._set_local_discovery_busy(True)
            self._set_local_status("Connecting…", WARN)

    def _on_local_endpoint_changed(self, _text: str = "") -> None:
        if _LOCAL_PROVIDER_ID not in self._discovery_inflight:
            self._paint_local_status()

    def _paint_local_status(self) -> None:
        endpoint = normalize_local_openai_base_url(self._local_endpoint_edit.text())
        count = (
            len(provider_registry.get(_LOCAL_PROVIDER_ID).models)
            if provider_registry.has(_LOCAL_PROVIDER_ID)
            else 0
        )
        if not endpoint:
            self._set_local_status(
                "Enter an OpenAI-compatible endpoint, then Test / Discover.",
                FG_MUTED,
            )
        elif not is_valid_local_openai_base_url(endpoint):
            self._set_local_status(
                "Enter a valid http:// or https:// endpoint.",
                WARN,
            )
        elif not count:
            self._set_local_status("Test / Discover to load models.", FG_MUTED)
        elif self._local_verified_endpoint != endpoint:
            self._set_local_status(
                "Endpoint changed — Test / Discover before applying.",
                WARN,
            )
        else:
            self._set_local_status(
                f"Ready — {count} cached model{'s' if count != 1 else ''}.",
                FG_DIM,
            )

    def _set_local_status(self, text: str, color: str) -> None:
        self._local_status_label.setText(text)
        self._local_status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    def _set_local_discovery_busy(self, busy: bool) -> None:
        self._local_endpoint_edit.setEnabled(not busy)
        self._local_discover_btn.setEnabled(not busy)

    def _populate_models(
        self,
        provider_id: ProviderId | None,
        current_selection: str,
    ) -> None:
        combo = self._model_combo
        if not provider_id or not provider_registry.has(provider_id):
            combo.blockSignals(True)
            combo.clear()
            combo.blockSignals(False)
            return

        cfg = provider_registry.get(provider_id)
        default_model = cfg.default_model
        if provider_id == _LOCAL_PROVIDER_ID and default_model not in cfg.models:
            default_model = ""
        if provider_id == _LOCAL_PROVIDER_ID and current_selection not in cfg.models:
            current_selection = ""
        items = build_model_picker_items(
            provider_id,
            cfg.models,
            default_model=default_model,
            current_selection=current_selection,
        )
        combo.set_items(items, current_selection or default_model)

    def _set_combo_to_data(self, combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # --- Collect ---

    def validation_error(self) -> str | None:
        if _LOCAL_PROVIDER_ID in self._discovery_inflight:
            return "Wait for Test / Discover to finish before applying."

        provider = self._provider_combo.currentData()
        endpoint = normalize_local_openai_base_url(self._local_endpoint_edit.text())
        endpoint_changed = endpoint != self._local_baseline_endpoint
        local_configuration_changed = endpoint_changed or self._local_catalog_dirty
        if provider != _LOCAL_PROVIDER_ID and not local_configuration_changed:
            return None

        if not is_valid_local_openai_base_url(endpoint):
            return "Enter a valid local model endpoint, then click Test / Discover."
        if endpoint != self._local_verified_endpoint:
            return "Test / Discover the current local model endpoint before applying."

        if provider != _LOCAL_PROVIDER_ID:
            return None

        model = self._model_combo.currentData()
        cfg = provider_registry.get(_LOCAL_PROVIDER_ID)
        if not model or model not in cfg.models:
            return "Test / Discover the local endpoint and select an available model."
        return None

    def commit_changes(self) -> None:
        """Persist a successfully applied local discovery snapshot."""
        cfg = provider_registry.get(_LOCAL_PROVIDER_ID)
        if self._local_catalog_dirty:
            save_dynamic_catalog(
                _LOCAL_PROVIDER_ID,
                dict(cfg.models),
                dict(cfg.pricing),
            )
        self._local_baseline_models = dict(cfg.models)
        self._local_baseline_pricing = dict(cfg.pricing)
        self._local_baseline_endpoint = normalize_local_openai_base_url(
            self._local_endpoint_edit.text()
        )
        self._local_catalog_dirty = False

    def discard_changes(self) -> None:
        """Restore provisional local discovery when Settings is cancelled."""
        if not self._local_catalog_dirty:
            return
        cfg = provider_registry.get(_LOCAL_PROVIDER_ID)
        cfg.models.clear()
        cfg.models.update(self._local_baseline_models)
        cfg.pricing.clear()
        cfg.pricing.update(self._local_baseline_pricing)
        self._local_catalog_dirty = False

    def collect_settings(self, settings: AppSettings) -> None:
        """Write the one production configuration back onto *settings*.

        """
        provider = self._provider_combo.currentData()
        model = self._model_combo.currentData()
        thinking = self._thinking_combo.currentData()
        if provider:
            settings.provider = provider
        if model:
            settings.default_model = model
        if thinking:
            settings.default_thinking = thinking
        settings.local_openai_base_url = normalize_local_openai_base_url(
            self._local_endpoint_edit.text()
        )
        settings.temperature = self._temperature_spin.value()
