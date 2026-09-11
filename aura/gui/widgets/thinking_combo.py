"""Keep thinking controls consistent with the client's capability mapping."""

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import QComboBox

from aura.client.reasoning import openrouter_thinking_options, resolve_reasoning_request
from aura.providers.base import THINKING_MODES, normalize_thinking_mode
from aura.providers.registry import provider_registry


def sync_thinking_combo(
    combo: QComboBox, provider: str, model: str, thinking: str | None = None,
    *, inherit_label: str | None = None,
) -> None:
    before = combo.currentData()
    selected = thinking or before
    requested = normalize_thinking_mode(selected) or "high"
    cfg = provider_registry.get(provider) if provider_registry.has(provider) else None
    options = [(mode, mode.title()) for mode in THINKING_MODES]
    effective = requested
    tooltip = ""
    if cfg and cfg.kind == "local":
        effective = "off"
    elif provider == "openrouter":
        info = cfg.models.get(model) if cfg else None
        options = [(o.mode, o.label) for o in openrouter_thinking_options(info)]
        effective = resolve_reasoning_request(provider, requested, model_info=info).thinking
        tooltip = "Reasoning options advertised by the selected model."
    if inherit_label is not None:
        options.insert(0, ("inherit", inherit_label))
        if selected == "inherit":
            effective = "inherit"
    with QSignalBlocker(combo):
        combo.clear()
        for mode, label in options:
            combo.addItem(label, mode)
        combo.setCurrentIndex(combo.findData(effective))
        combo.setEnabled(len(options) > 1 and not (cfg and cfg.kind == "local"))
        combo.setToolTip(tooltip)
    if before != combo.currentData():
        combo.currentIndexChanged.emit(combo.currentIndex())
