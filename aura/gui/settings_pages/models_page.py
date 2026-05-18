from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import Qt, QObject, Signal, QThread
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from aura.config import (
    AppSettings,
    fetch_provider_models,
    resolve_role_default_model,
    save_dynamic_catalog,
)
from aura.providers.base import ProviderId
from aura.providers.registry import provider_registry
from aura.gui.theme import FG_DIM
from aura.gui.aura_widget import GlassSwitch

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
    def __init__(self, settings: AppSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._settings = settings

        self._discovery_inflight: set[str] = set()
        self._discovery_thread: QThread | None = None
        self._discovery_worker: DiscoveryWorker | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        # P/W mode toggle
        self._pw_mode_chk = GlassSwitch(
            "Planner/Worker mode (planner chats; worker executes code changes)",
            self._settings.planner_worker_mode,
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
        self._planner_provider_combo.currentIndexChanged.connect(
            self._on_planner_provider_changed
        )
        form.addRow("Planner provider:", self._planner_provider_combo)

        self._planner_model_combo = QComboBox()
        form.addRow("Planner model:", self._planner_model_combo)

        self._planner_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._planner_thinking_combo.addItem(label, val)
        form.addRow("Planner thinking:", self._planner_thinking_combo)

        # Separator
        sep1 = QLabel("Worker")
        sep1.setStyleSheet(
            f"color: {FG_DIM}; font-weight: 600; font-size: 11px;"
            " text-transform: uppercase; letter-spacing: 0.04em;"
        )
        form.addRow("", sep1)

        # Worker Provider
        self._worker_provider_combo = QComboBox()
        for pid in provider_registry.ids():
            spec = provider_registry.get(pid)
            self._worker_provider_combo.addItem(spec.label, pid)
        self._worker_provider_combo.setCurrentIndex(
            provider_registry.ids().index(self._settings.worker_provider)
        )
        self._worker_provider_combo.currentIndexChanged.connect(
            self._on_worker_provider_changed
        )
        form.addRow("Worker provider:", self._worker_provider_combo)

        self._worker_model_combo = QComboBox()
        form.addRow("Worker model:", self._worker_model_combo)

        self._worker_thinking_combo = QComboBox()
        for label, val in _THINKING_ITEMS:
            self._worker_thinking_combo.addItem(label, val)
        form.addRow("Worker thinking:", self._worker_thinking_combo)

        # Populate model combos
        self._populate_all_role_models()

        self._set_combo_to_data(
            self._planner_thinking_combo, self._settings.default_planner_thinking
        )
        self._set_combo_to_data(
            self._worker_thinking_combo, self._settings.default_worker_thinking
        )

        # Temperature
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

        layout.addLayout(form)
        layout.addStretch()

        # Start background discovery
        self._start_discovery(self._settings.planner_provider)
        self._start_discovery(self._settings.worker_provider)

    # --- Thread cleanup ---

    def cleanup_threads(self) -> None:
        self._cleanup_thread("_discovery_thread", "_discovery_worker")

    def _cleanup_thread(self, thread_attr: str, worker_attr: str, wait_ms: int = 15000) -> None:
        thread = getattr(self, thread_attr, None)
        if thread is None:
            return
        try:
            if thread.isRunning():
                thread.quit()
                if not thread.wait(wait_ms):
                    logger.warning(
                        "Settings dialog thread did not stop cleanly: %s", thread_attr
                    )
                    thread.wait()
        except RuntimeError:
            pass

    # --- Model discovery ---

    def _start_discovery(self, provider_id: ProviderId) -> None:
        if provider_id in self._discovery_inflight:
            return
        self._discovery_inflight.add(provider_id)

        self._discovery_thread = QThread(self)
        self._discovery_worker = DiscoveryWorker(provider_id)
        self._discovery_worker.moveToThread(self._discovery_thread)
        self._discovery_thread.started.connect(self._discovery_worker.run)
        self._discovery_worker.finished.connect(self._on_discovery_finished)
        self._discovery_worker.finished.connect(self._discovery_thread.quit)
        self._discovery_worker.finished.connect(self._discovery_worker.deleteLater)
        self._discovery_thread.finished.connect(self._discovery_thread.deleteLater)
        self._discovery_thread.start()

    def _on_discovery_finished(
        self,
        provider_id: str,
        models: dict,
        pricing: dict,
        error: str,
    ) -> None:
        self._discovery_inflight.discard(provider_id)
        if error:
            logger.warning("Model discovery failed for %s: %s", provider_id, error)
            return

        cfg = provider_registry.get(provider_id)  # type: ignore[arg-type]
        cfg.models.update(models)
        cfg.pricing.update(pricing)
        save_dynamic_catalog(provider_id, models, pricing)  # type: ignore[arg-type]

        planner_pid: ProviderId = self._planner_provider_combo.currentData()  # type: ignore[assignment]
        worker_pid: ProviderId = self._worker_provider_combo.currentData()  # type: ignore[assignment]

        if provider_id == planner_pid:
            current = self._planner_model_combo.currentData()
            self._populate_role_models(
                self._planner_model_combo, planner_pid, current, role="planner"
            )

        if provider_id == worker_pid:
            current = self._worker_model_combo.currentData()
            self._populate_role_models(
                self._worker_model_combo, worker_pid, current, role="worker"
            )

    # --- Provider / Model helpers ---

    def _on_planner_provider_changed(self) -> None:
        provider_id: ProviderId = self._planner_provider_combo.currentData()  # type: ignore[assignment]
        self._populate_role_models(
            self._planner_model_combo,
            provider_id,
            resolve_role_default_model(provider_id, "planner"),
            role="planner",
        )
        self._start_discovery(provider_id)

    def _on_worker_provider_changed(self) -> None:
        provider_id: ProviderId = self._worker_provider_combo.currentData()  # type: ignore[assignment]
        self._populate_role_models(
            self._worker_model_combo,
            provider_id,
            resolve_role_default_model(provider_id, "worker"),
            role="worker",
        )
        self._start_discovery(provider_id)

    def _populate_all_role_models(self) -> None:
        planner_pid: ProviderId = self._planner_provider_combo.currentData()  # type: ignore[assignment]
        worker_pid: ProviderId = self._worker_provider_combo.currentData()  # type: ignore[assignment]
        self._populate_role_models(
            self._planner_model_combo,
            planner_pid,
            resolve_role_default_model(planner_pid, "planner"),
            role="planner",
        )
        self._populate_role_models(
            self._worker_model_combo,
            worker_pid,
            resolve_role_default_model(worker_pid, "worker"),
            role="worker",
        )

    def _populate_role_models(
        self,
        combo: QComboBox,
        provider_id: ProviderId,
        current_selection: str,
        role: str = "",
    ) -> None:
        cfg = provider_registry.get(provider_id)
        combo.blockSignals(True)
        combo.clear()

        seen: set[str] = set()
        items: list[tuple[str, str]] = []  # (label, id)

        def add_model(mid: str, label: str = "") -> None:
            if mid in seen:
                return
            seen.add(mid)
            items.append((label or mid, mid))

        for info in cfg.models.values():
            if info.id not in seen:
                add_model(info.id, info.label)

        add_model(cfg.default_model)

        if current_selection:
            add_model(current_selection)

        if provider_id == "deepseek" and role == "worker":
            from aura.providers.catalog import DEFAULT_WORKER_MODEL
            add_model(DEFAULT_WORKER_MODEL)

        if provider_id == "deepseek" and role == "planner":
            from aura.providers.catalog import DEFAULT_PLANNER_MODEL
            add_model(DEFAULT_PLANNER_MODEL)

        for label, mid in items:
            combo.addItem(label, mid)

        if current_selection:
            idx = combo.findData(current_selection)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                idx = combo.findData(cfg.default_model)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        else:
            idx = combo.findData(cfg.default_model)
            if idx >= 0:
                combo.setCurrentIndex(idx)

        combo.blockSignals(False)

    def _set_combo_to_data(self, combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)

    # --- P/W toggle ---

    def _on_pw_toggled(self, _checked: bool) -> None:
        self._refresh_pw_enabled()

    def _refresh_pw_enabled(self) -> None:
        enabled = self._pw_mode_chk.isChecked()
        self._planner_model_combo.setEnabled(enabled)
        self._planner_thinking_combo.setEnabled(enabled)
        self._worker_model_combo.setEnabled(enabled)
        self._worker_thinking_combo.setEnabled(enabled)
        self._worker_temperature_spin.setEnabled(enabled)

    # --- Collect ---

    def collect_settings(self, settings: AppSettings) -> None:
        settings.planner_provider = self._planner_provider_combo.currentData()
        settings.worker_provider = self._worker_provider_combo.currentData()
        settings.planner_worker_mode = self._pw_mode_chk.isChecked()
        settings.default_planner_model = self._planner_model_combo.currentData()
        settings.default_worker_model = self._worker_model_combo.currentData()
        settings.default_planner_thinking = self._planner_thinking_combo.currentData()
        settings.default_worker_thinking = self._worker_thinking_combo.currentData()
        settings.temperature = self._temperature_spin.value()
        settings.worker_temperature = self._worker_temperature_spin.value()
