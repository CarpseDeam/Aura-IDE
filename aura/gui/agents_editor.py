"""Agent definition/form widgets, separate from roster and page ownership.

There is no provider control here, and its absence is the design rather than
a hidden default. An agent runs on whichever provider Aura itself is set to
for the turn that invoked it, so a reusable agent — a project one especially
— cannot pin somebody else's machine to a service they have no key for. What
is selectable is the model, listed for Aura's current provider and resolved
under it. An agent that names none runs whatever model Aura is running.
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

SCOPE_LABELS: dict[str, str] = {"project": "Project", "personal": "Personal"}


@dataclass(frozen=True)
class AgentDetail:
    agent_id: str
    scope: str
    name: str
    description: str
    instructions: str
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
    model: str = ""
    thinking: AgentThinking = AgentThinking.INHERIT


@dataclass(frozen=True)
class ModelChoices:
    """The models an agent may be pointed at, and the one Aura is on.

    Both come from Aura's *current* provider, because that is the provider
    every agent will run under. ``current_model`` is what an agent that names
    no model of its own actually runs, so it is what the control shows for
    one — never a blank box the user has to interpret.
    """

    models: tuple[str, ...] = ()
    current_model: str = ""


def catalog_choices(provider: str = "", current_model: str = "") -> ModelChoices:
    """The model list for *provider*, or an empty one it is safe to render."""
    try:
        from aura.providers.registry import ProviderRegistry

        registry = ProviderRegistry()
        spec = registry.get(provider) if registry.has(provider) else None
    except Exception:
        return ModelChoices(current_model=str(current_model or ""))
    models = tuple(sorted(getattr(spec, "models", {}) or {})) if spec else ()
    return ModelChoices(models=models, current_model=str(current_model or ""))


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

        # Editable so a model Aura's cached catalog has not caught up with can
        # still be typed; the list is what the current provider advertises.
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.model.setToolTip(
            "The model this agent runs, under whichever provider Aura is set "
            "to. An agent never chooses a provider."
        )
        self._load_models()
        layout.addLayout(_labelled("Model", self.model))

        self.thinking = QComboBox()
        for mode in THINKING_ORDER:
            self.thinking.addItem(mode.label, mode.value)

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
        model_text = self.model.currentText().strip()
        model_index = self.model.currentIndex()
        model_data = self.model.currentData()
        model = (
            str(model_data or "")
            if model_index >= 0
            and model_data is not None
            and model_text == self.model.itemText(model_index)
            else model_text
        )
        return AgentDraft(
            agent_id=self._detail.agent_id,
            scope=self._detail.scope,
            name=self.name.text().strip(),
            description=self.description.text().strip(),
            instructions=self.instructions.toPlainText().strip(),
            model=model,
            thinking=(
                AgentThinking.parse(self.thinking.currentData())
                or AgentThinking.INHERIT
            ),
        )

    def set_choices(self, choices: ModelChoices) -> None:
        """Re-list the models after Aura's provider or model changed."""
        self._choices = choices
        self._loading = True
        try:
            self._load_models()
            self._select_model(self._detail.model if self._detail is not None else "")
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
                self._select_model("")
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
                self._select_model(detail.model)
                self._select_data(self.thinking, detail.thinking.value)
                self._select_data(self.permission, detail.permission.value)
        finally:
            self._loading = False
        self._update_actions()

    def _load_models(self) -> None:
        """Fill the list with what Aura's current provider offers."""
        self.model.blockSignals(True)
        self.model.clear()
        inherit_label = f"Use {CURRENT_MODEL_LABEL}"
        if self._choices.current_model:
            inherit_label += f" ({self._choices.current_model})"
        self.model.addItem(inherit_label, "")
        for model in self._choices.models:
            self.model.addItem(model, model)
        self.model.blockSignals(False)

    def _select_model(self, model: str) -> None:
        """Select inherit, a catalog model, or an explicitly typed model id."""
        value = str(model or "").strip()
        index = self.model.findData(value)
        if index >= 0:
            self.model.setCurrentIndex(index)
        else:
            self.model.setCurrentText(value)

    def _editable(self) -> bool:
        return self._mutations_enabled and self._detail is not None

    def _update_actions(self) -> None:
        editable = self._editable()
        for widget in (self.name, self.description):
            widget.setReadOnly(not editable)
            widget.setEnabled(self._detail is not None)
        self.instructions.setReadOnly(not editable)
        self.instructions.setEnabled(self._detail is not None)
        self.model.setEnabled(editable)
        self.thinking.setEnabled(editable)
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
    "catalog_choices",
]
