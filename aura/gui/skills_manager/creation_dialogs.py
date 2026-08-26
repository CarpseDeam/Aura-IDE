"""The one plain-language question used to start skill creation."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from aura.gui.skills_manager.models import scope_label
from aura.skills.identity import InstallScope


@dataclass(frozen=True)
class SkillCreationRequest:
    """The nontechnical choices needed to author one skill."""

    description: str
    scope: InstallScope = InstallScope.PROJECT
    preferred_name: str = ""


class SkillCreationIntakeDialog(QDialog):
    """Description, destination, and an optional preferred name."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create with Aura")
        self.setModal(True)
        self.resize(520, 360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        layout.addWidget(QLabel("What should Aura become better at?"))
        self._description = QTextEdit()
        self._description.setPlaceholderText(
            "Describe the task, project knowledge, or working style Aura should learn."
        )
        layout.addWidget(self._description, 1)

        layout.addWidget(QLabel("Destination"))
        self._destination = QComboBox()
        for scope in (InstallScope.PROJECT, InstallScope.PERSONAL):
            self._destination.addItem(scope_label(scope.value), scope)
        layout.addWidget(self._destination)

        layout.addWidget(QLabel("Preferred name (optional)"))
        self._preferred_name = QLineEdit()
        self._preferred_name.setPlaceholderText("Aura will choose a valid name if left blank")
        layout.addWidget(self._preferred_name)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._create_button = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        self._create_button.setText("Create")
        self._create_button.setEnabled(False)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        self._description.textChanged.connect(self._sync_create_enabled)
        layout.addWidget(self._buttons)

    def _sync_create_enabled(self) -> None:
        self._create_button.setEnabled(bool(self._description.toPlainText().strip()))

    def request(self) -> SkillCreationRequest:
        try:
            scope = InstallScope(self._destination.currentData())
        except (TypeError, ValueError):
            scope = InstallScope.PROJECT
        return SkillCreationRequest(
            description=self._description.toPlainText().strip(),
            scope=scope,
            preferred_name=self._preferred_name.text().strip(),
        )

    def offered_scopes(self) -> tuple[InstallScope, ...]:
        return tuple(self._destination.itemData(index) for index in range(self._destination.count()))


class SkillCreationPrompts:
    """Replaceable seam for the creation intake and local failures."""

    def ask(self, parent: QWidget | None) -> SkillCreationRequest | None:
        dialog = SkillCreationIntakeDialog(parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.request()

    def show_error(self, parent: QWidget | None, message: str) -> None:
        QMessageBox.warning(parent, "Create skill", message)


__all__ = ["SkillCreationIntakeDialog", "SkillCreationPrompts", "SkillCreationRequest"]
