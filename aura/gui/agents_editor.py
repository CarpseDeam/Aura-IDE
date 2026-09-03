"""Agent definition/form widgets, separate from roster and page ownership.

Provider and model are presented as one searchable target. The definition
stores only their identifiers; endpoint and credential configuration remains
local to the machine running Aura. An inherited target stores neither.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from aura.agents.local_state import PERMISSION_ORDER, AgentPermission
from aura.agents.models import CURRENT_MODEL_LABEL, THINKING_ORDER, AgentThinking
from aura.agents.validation import MAX_AGENT_DESCRIPTION_CHARS, MAX_AGENT_NAME_CHARS
from aura.gui.theme import BG, DANGER, FG, FG_DIM
from aura.gui.widgets.searchable_model_combo import SearchableModelCombo
from aura.providers.model_presentation import build_model_picker_items

SCOPE_LABELS: dict[str, str] = {"project": "Project", "personal": "Personal"}


@dataclass(frozen=True)
class AgentDetail:
    agent_id: str
    scope: str
    name: str
    description: str
    instructions: str
    provider: str
    model: str
    thinking: AgentThinking
    permission: AgentPermission
    available: bool
    valid: bool = True
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentDraft:
    """Definition fields only; local grants are emitted separately."""

    agent_id: str
    scope: str
    name: str
    description: str
    instructions: str
    provider: str = ""
    model: str = ""
    thinking: AgentThinking = AgentThinking.INHERIT


@dataclass(frozen=True)
class ModelTargetChoice:
    """One provider-qualified model row in the combined target picker."""

    provider: str
    model: str
    label: str


@dataclass(frozen=True)
class ModelChoices:
    """Provider-qualified targets plus the exact target Aura currently uses."""

    targets: tuple[ModelTargetChoice, ...] = ()
    current_provider: str = ""
    current_model: str = ""


def catalog_choices(provider: str = "", current_model: str = "") -> ModelChoices:
    """All registered executable model targets, or an empty safe result."""
    try:
        from aura.providers.registry import ProviderRegistry

        registry = ProviderRegistry()
    except Exception:
        return ModelChoices(
            current_provider=str(provider or ""),
            current_model=str(current_model or ""),
        )

    targets: list[ModelTargetChoice] = []
    for provider_id in registry.ids():
        spec = registry.get(provider_id)
        if spec.kind not in {"api_key", "local"}:
            continue
        items = build_model_picker_items(
            provider_id,
            spec.models,
            default_model=spec.default_model,
            current_selection=(
                str(current_model or "") if provider_id == provider else ""
            ),
        )
        targets.extend(
            ModelTargetChoice(
                provider=provider_id,
                model=item.model_id,
                label=f"{spec.label} — {item.label}",
            )
            for item in items
        )
    return ModelChoices(
        targets=tuple(targets),
        current_provider=str(provider or ""),
        current_model=str(current_model or ""),
    )


class AgentEditor(QWidget):
    """Owns the definition form and emits form-level user intent."""

    save_requested = Signal(object)
    delete_requested = Signal(str, str)
    permission_changed = Signal(str, str)

    def __init__(self, choices: ModelChoices, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choices = choices
        self._detail: AgentDetail | None = None
        self._mutations_enabled = True
        self._loading = False
        self.setStyleSheet(f"background: {BG}; color: {FG};")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(8)

        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setStyleSheet(
            f"color: {FG}; font-size: 14px; font-weight: 600; background: transparent;"
        )
        layout.addWidget(self.heading)

        self.errors_label = QLabel()
        self.errors_label.setWordWrap(True)
        self.errors_label.setStyleSheet(
            f"color: {DANGER}; font-size: 11px; background: transparent;"
        )
        layout.addWidget(self.errors_label)

        self.name = QLineEdit()
        self.name.setMaxLength(MAX_AGENT_NAME_CHARS)
        self.name.setPlaceholderText("Display name, e.g. Reviewer")
        layout.addLayout(_labelled("Name", self.name))

        self.description = QLineEdit()
        self.description.setMaxLength(MAX_AGENT_DESCRIPTION_CHARS)
        self.description.setPlaceholderText(
            "One line: what this agent is for. Aura reads it when choosing whom to ask."
        )
        layout.addLayout(_labelled("Delegation description", self.description))

        self.instructions = QPlainTextEdit()
        self.instructions.setPlaceholderText("The full brief this agent works from. Markdown.")
        self.instructions.setMinimumHeight(140)
        layout.addLayout(_labelled("Instructions", self.instructions), 1)

        self.model = SearchableModelCombo()
        self.model.setToolTip(
            "The provider and model this agent runs. Inherit Aura follows the "
            "submitted root turn; provider configuration stays on this machine."
        )
        self._load_targets()
        layout.addLayout(_labelled("Model target", self.model))

        self.thinking = QComboBox()
        for mode in THINKING_ORDER:
            self.thinking.addItem(mode.label, mode.value)
        self.model.currentIndexChanged.connect(self._on_model_target_changed)

        self.permission = QComboBox()
        for permission in PERMISSION_ORDER:
            self.permission.addItem(permission.label, permission.value)
        self.permission.setToolTip(
            "What this agent may do in this project, on this computer. Your "
            "choice only — it is never written into the project."
        )
        self.permission.currentIndexChanged.connect(
            lambda _index: self._emit_permission_change()
        )
        controls = QHBoxLayout()
        controls.setSpacing(8)
        controls.addLayout(_labelled("Thinking", self.thinking), 1)
        controls.addLayout(_labelled("Permission", self.permission), 2)
        layout.addLayout(controls)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.save_button = QPushButton("Save")
        self.save_button.setObjectName("primary")
        self.save_button.clicked.connect(self._save)
        actions.addWidget(self.save_button)
        self.delete_button = QPushButton("Delete")
        self.delete_button.setObjectName("danger")
        self.delete_button.clicked.connect(self._delete)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.render()

    @property
    def detail(self) -> AgentDetail | None:
        return self._detail

    def set_detail(self, detail: AgentDetail | None) -> None:
        self._detail = detail
        self.render()

    def apply_local_state(self, *, available: bool, permission: AgentPermission) -> None:
        if self._detail is None:
            return
        self._detail = replace(
            self._detail, available=available, permission=permission
        )
        self._loading = True
        try:
            self._select_data(self.permission, permission.value)
        finally:
            self._loading = False

    def set_mutations_enabled(self, enabled: bool) -> None:
        self._mutations_enabled = bool(enabled)
        self._update_actions()

    def draft(self) -> AgentDraft | None:
        if self._detail is None:
            return None
        target = self.model.currentData()
        provider = ""
        model = ""
        if isinstance(target, (tuple, list)) and len(target) == 2:
            provider = str(target[0] or "").strip()
            model = str(target[1] or "").strip()
        return AgentDraft(
            agent_id=self._detail.agent_id,
            scope=self._detail.scope,
            name=self.name.text().strip(),
            description=self.description.text().strip(),
            instructions=self.instructions.toPlainText().strip(),
            provider=provider,
            model=model,
            thinking=(
                AgentThinking.parse(self.thinking.currentData())
                or AgentThinking.INHERIT
            ),
        )

    def set_choices(self, choices: ModelChoices) -> None:
        """Re-list the models after Aura's provider or model changed."""
        live_target = self.model.currentData() if self._detail is not None else None
        if isinstance(live_target, (tuple, list)) and len(live_target) == 2:
            selected_provider = str(live_target[0] or "").strip()
            selected_model = str(live_target[1] or "").strip()
        else:
            selected_provider = self._detail.provider if self._detail is not None else ""
            selected_model = self._detail.model if self._detail is not None else ""
        self._choices = choices
        self._loading = True
        try:
            self._load_targets()
            self._select_target(selected_provider, selected_model)
            self._sync_thinking_for_target()
        finally:
            self._loading = False

    def render(self) -> None:
        detail = self._detail
        self._loading = True
        try:
            if detail is None:
                self.heading.setText("No agent selected")
                self.errors_label.clear()
                self.errors_label.setVisible(False)
                self.name.clear()
                self.description.clear()
                self.instructions.clear()
                self._select_target("", "")
                self.thinking.setCurrentIndex(0)
                self.permission.setCurrentIndex(0)
            else:
                scope = SCOPE_LABELS.get(detail.scope, detail.scope.title())
                self.heading.setText(f"{detail.name or 'Untitled agent'}  ·  {scope}")
                self.errors_label.setText(
                    "\n".join(f"• {line}" for line in detail.errors)
                    if detail.errors else ""
                )
                self.errors_label.setVisible(bool(detail.errors))
                self.name.setText(detail.name)
                self.description.setText(detail.description)
                self.instructions.setPlainText(detail.instructions)
                self._select_target(detail.provider, detail.model)
                self._select_data(self.thinking, detail.thinking.value)
                self._select_data(self.permission, detail.permission.value)
            self._sync_thinking_for_target()
        finally:
            self._loading = False
        self._update_actions()

    def _load_targets(self) -> None:
        """Fill the one picker with every provider-qualified model target."""
        self.model.blockSignals(True)
        self.model.clear()
        inherit_label = f"Use {CURRENT_MODEL_LABEL}"
        current = self._choice_label(
            self._choices.current_provider,
            self._choices.current_model,
        )
        if current:
            inherit_label += f" ({current})"
        self.model.addItem(inherit_label, ("", ""))
        for target in self._choices.targets:
            self.model.addItem(target.label, (target.provider, target.model))
        self.model.blockSignals(False)

    def _select_target(self, provider: str, model: str) -> None:
        """Select a catalog target, preserving any stored compatibility pair."""
        value = (str(provider or "").strip(), str(model or "").strip())
        # PySide round-trips a Python tuple through QVariant correctly, but
        # QComboBox.findData() does not reliably compare that tuple on every
        # supported Qt build. Compare the returned Python values ourselves.
        index = next(
            (
                row
                for row in range(self.model.count())
                if self.model.itemData(row) == value
            ),
            -1,
        )
        if index < 0 and any(value):
            self.model.addItem(self._compatibility_label(*value), value)
            index = self.model.count() - 1
        self.model.setCurrentIndex(index if index >= 0 else 0)

    def _choice_label(self, provider: str, model: str) -> str:
        value = (str(provider or "").strip(), str(model or "").strip())
        if not any(value):
            return ""
        for target in self._choices.targets:
            if (target.provider, target.model) == value:
                return target.label
        return self._compatibility_label(*value)

    @staticmethod
    def _compatibility_label(provider: str, model: str) -> str:
        if provider and model:
            return f"{provider} — {model}"
        if provider:
            return f"{provider} — default model"
        return f"Aura's provider — {model}"

    def _editable(self) -> bool:
        return self._mutations_enabled and self._detail is not None

    def _selected_provider(self) -> str:
        target = self.model.currentData()
        if isinstance(target, (tuple, list)) and len(target) == 2:
            return str(target[0] or "").strip()
        return ""

    def _target_is_local(self) -> bool:
        provider = self._selected_provider()
        if not provider:
            provider = self._choices.current_provider.strip()
        return provider == "local_openai"

    def _sync_thinking_for_target(self) -> None:
        # An explicitly pinned local target stores Off because that is its
        # portable runtime contract. An inherited target keeps ``inherit`` in
        # the definition even while Aura itself happens to be local; the
        # disabled control still makes clear that the effective run is Off.
        if self._selected_provider() == "local_openai":
            self._select_data(self.thinking, AgentThinking.OFF.value)
        self._update_actions()

    def _on_model_target_changed(self, _index: int) -> None:
        self._sync_thinking_for_target()

    def _update_actions(self) -> None:
        editable = self._editable()
        for widget in (self.name, self.description):
            widget.setReadOnly(not editable)
            widget.setEnabled(self._detail is not None)
        self.instructions.setReadOnly(not editable)
        self.instructions.setEnabled(self._detail is not None)
        self.model.setEnabled(editable)
        self.thinking.setEnabled(editable and not self._target_is_local())
        self.permission.setEnabled(editable)
        self.save_button.setEnabled(editable)
        self.delete_button.setEnabled(editable)

    def _save(self) -> None:
        draft = self.draft()
        if draft is not None and self._mutations_enabled:
            self.save_requested.emit(draft)

    def _delete(self) -> None:
        if self._detail is not None and self._mutations_enabled:
            self.delete_requested.emit(self._detail.scope, self._detail.agent_id)

    def _emit_permission_change(self) -> None:
        if self._loading or not self._mutations_enabled or self._detail is None:
            return
        value = str(self.permission.currentData() or AgentPermission.READ_ONLY.value)
        if value != self._detail.permission.value:
            self.permission_changed.emit(self._detail.agent_id, value)

    @staticmethod
    def _select_data(combo: QComboBox, value: str) -> None:
        index = combo.findData(value)
        combo.setCurrentIndex(index if index >= 0 else 0)


def _labelled(text: str, widget: QWidget) -> QVBoxLayout:
    layout = QVBoxLayout()
    layout.setSpacing(3)
    label = QLabel(text)
    label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px; background: transparent;")
    layout.addWidget(label)
    layout.addWidget(widget)
    return layout


__all__ = [
    "AgentDetail",
    "AgentDraft",
    "AgentEditor",
    "ModelChoices",
    "ModelTargetChoice",
    "catalog_choices",
]
