"""Agent definition/form widgets, separate from roster and page ownership."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

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
from aura.agents.models import THINKING_ORDER, AgentThinking
from aura.agents.validation import MAX_AGENT_DESCRIPTION_CHARS
from aura.gui.theme import BG, DANGER, FG, FG_DIM

INHERIT_TARGET_LABEL = "Inherit Aura's provider and model"
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
    name: str
    description: str
    instructions: str
    provider: str = ""
    model: str = ""
    thinking: AgentThinking = AgentThinking.INHERIT


@dataclass(frozen=True)
class ProviderChoices:
    providers: tuple[tuple[str, str], ...] = ()
    models: dict[str, tuple[str, ...]] = field(default_factory=dict)


def catalog_choices() -> ProviderChoices:
    try:
        from aura.providers.registry import ProviderRegistry

        specs = ProviderRegistry().all()
    except Exception:
        return ProviderChoices()
    return ProviderChoices(
        providers=tuple(
            (pid, getattr(spec, "label", pid)) for pid, spec in sorted(specs.items())
        ),
        models={
            pid: tuple(sorted(getattr(spec, "models", {}) or {}))
            for pid, spec in specs.items()
        },
    )


class AgentEditor(QWidget):
    """Owns the definition form and emits form-level user intent."""

    save_requested = Signal(object)
    delete_requested = Signal(str)
    permission_changed = Signal(str, str)

    def __init__(self, choices: ProviderChoices, parent: QWidget | None = None) -> None:
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

        self.provider = QComboBox()
        self.provider.addItem(INHERIT_TARGET_LABEL, "")
        for provider_id, label in choices.providers:
            self.provider.addItem(label, provider_id)
        self.provider.currentIndexChanged.connect(lambda _index: self._sync_models())

        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        target_row = QHBoxLayout()
        target_row.setSpacing(8)
        target_row.addLayout(_labelled("Provider", self.provider), 1)
        target_row.addLayout(_labelled("Model", self.model), 1)
        layout.addLayout(target_row)

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
        provider = str(self.provider.currentData() or "")
        return AgentDraft(
            agent_id=self._detail.agent_id,
            name=self.name.text().strip(),
            description=self.description.text().strip(),
            instructions=self.instructions.toPlainText().strip(),
            provider=provider,
            model=self.model.currentText().strip() if provider else "",
            thinking=(
                AgentThinking.parse(self.thinking.currentData())
                or AgentThinking.INHERIT
            ),
        )

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
                self.provider.setCurrentIndex(0)
                self.model.setCurrentText("")
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
                self._select_data(self.provider, detail.provider)
                self._sync_models()
                self.model.setCurrentText(detail.model)
                self._select_data(self.thinking, detail.thinking.value)
                self._select_data(self.permission, detail.permission.value)
        finally:
            self._loading = False
        self._update_actions()

    def _sync_models(self) -> None:
        provider = str(self.provider.currentData() or "")
        current = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        if provider:
            self.model.addItems(list(self._choices.models.get(provider, ())))
            self.model.setCurrentText(current)
        else:
            self.model.setCurrentText("")
        self.model.blockSignals(False)
        self.model.setEnabled(bool(provider) and self._editable())

    def _editable(self) -> bool:
        return self._mutations_enabled and self._detail is not None

    def _update_actions(self) -> None:
        editable = self._editable()
        for widget in (self.name, self.description):
            widget.setReadOnly(not editable)
            widget.setEnabled(self._detail is not None)
        self.instructions.setReadOnly(not editable)
        self.instructions.setEnabled(self._detail is not None)
        self.provider.setEnabled(editable)
        self.model.setEnabled(editable and bool(self.provider.currentData()))
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
            self.delete_requested.emit(self._detail.agent_id)

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
    "INHERIT_TARGET_LABEL",
    "AgentDetail",
    "AgentDraft",
    "AgentEditor",
    "ProviderChoices",
    "catalog_choices",
]
