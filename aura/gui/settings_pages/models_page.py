from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    AppSettings,
    fetch_provider_models,
    get_provider_kind,
    resolve_role_default_model,
    save_dynamic_catalog,
)
from aura.gui.theme import FG_DIM
from aura.gui.widgets.no_wheel_combo import NoWheelComboBox
from aura.providers.base import ProviderId
from aura.providers.registry import provider_registry

logger = logging.getLogger(__name__)

_THINKING_ITEMS: list[tuple[str, str]] = [
    ("Off", "off"),
    ("High", "high"),
    ("Max", "max"),
]


class DiscoveryWorker(QObject):
    finished = Signal(str, dict, dict, str)  # provider_id, models, pricing, error_msg

    def __init__(self, provider_id: ProviderId):
        super().__init__()
        self.provider_id = provider_id

    def run(self):
        try:
            models, pricing, error = fetch_provider_models(self.provider_id)
            self.finished.emit(self.provider_id, models, pricing, error or "")
        except Exception as exc:
            self.finished.emit(self.provider_id, {}, {}, str(exc))


class ModelsPage(QWidget):
    """One production configuration: provider, model, thinking, temperature.

    Normal Aura coding runs one continuous production model. The historical
    Planner/Worker mode toggle and its separate role controls are not part of
    the normal product and are no longer exposed here. The legacy fields on
    ``AppSettings`` are preserved untouched for backward compatibility.
    """

    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._discovery_inflight: set[str] = set()
        self._discovery_threads: dict[str, QThread] = {}
        self._discovery_workers: dict[str, DiscoveryWorker] = {}
        self._closing: bool = False

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

        self._model_combo = NoWheelComboBox()

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
        self._start_discovery(current_provider)

        self._set_combo_to_data(self._thinking_combo, self._settings.default_thinking)
        self._temperature_spin.setValue(self._settings.temperature)

        # --- 4. Connect Signals ---
        # Connect AFTER initial population to avoid spurious signal firing.
        self._provider_combo.currentIndexChanged.connect(self._on_provider_changed)
        self._refresh_btn.clicked.connect(
            lambda: self._start_discovery(self._provider_combo.currentData())
        )

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

    def _start_discovery(self, provider_id: ProviderId) -> None:
        if not provider_id or provider_id in self._discovery_inflight:
            return
        self._discovery_inflight.add(provider_id)

        thread = QThread(self)
        worker = DiscoveryWorker(provider_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_discovery_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_discovery_thread_finished)
        thread.start()

        self._discovery_threads[provider_id] = thread
        self._discovery_workers[provider_id] = worker

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

    def _forget_discovery_thread(self, provider_id: str) -> None:
        thread = self._discovery_threads.pop(provider_id, None)
        self._discovery_workers.pop(provider_id, None)
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
        self._discovery_inflight.discard(provider_id)
        if error:
            logger.warning("Model discovery failed for %s: %s", provider_id, error)
            return

        cfg = provider_registry.get(provider_id)  # type: ignore[arg-type]
        cfg.models.update(models)
        cfg.pricing.update(pricing)
        save_dynamic_catalog(provider_id, models, pricing)  # type: ignore[arg-type]

        active: ProviderId = self._provider_combo.currentData()  # type: ignore[assignment]
        if provider_id == active:
            self._populate_models(active, self._model_combo.currentData())
            self._refresh_btn.setVisible(provider_id == "openrouter")

    # --- Provider / Model helpers ---

    def _on_provider_changed(self) -> None:
        provider_id: ProviderId = self._provider_combo.currentData()  # type: ignore[assignment]
        self._populate_models(
            provider_id,
            resolve_role_default_model(provider_id, "production"),
        )
        self._start_discovery(provider_id)
        self._refresh_btn.setVisible(provider_id == "openrouter")

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
        combo.blockSignals(True)
        combo.clear()

        seen: set[str] = set()
        items: list[tuple[str, str]] = []  # (label, id)

        def add_model(mid: str, label: str = "") -> None:
            if not mid or mid in seen:
                return
            seen.add(mid)
            items.append((label or mid, mid))

        for info in cfg.models.values():
            add_model(info.id, info.label)

        add_model(cfg.default_model)
        if current_selection:
            add_model(current_selection)

        for label, mid in items:
            combo.addItem(label, mid)

        idx = combo.findData(current_selection) if current_selection else -1
        if idx < 0:
            idx = combo.findData(cfg.default_model)
        if idx >= 0:
            combo.setCurrentIndex(idx)

        combo.blockSignals(False)

    def _set_combo_to_data(self, combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        """Write the one production configuration back onto *settings*.

        Legacy Planner/Worker fields are intentionally left untouched so old
        persisted configurations keep round-tripping.
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
        settings.temperature = self._temperature_spin.value()
