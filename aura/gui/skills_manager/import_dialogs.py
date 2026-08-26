"""What the user sees while importing a skill: source, destination, review.

Three questions and one review, in that order. Which local source (a folder
or a ZIP), where it should be installed (Project or Personal — never
Bundled), and, for a GitHub import, which public repository URL. Then the
staged result is shown in full before anything is installed.

:class:`ImportPrompts` is the seam. The import controller only ever asks
these questions through one, so a test can answer them without a native
picker, and every real dialog stays here rather than inside the flow.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.skills_manager.import_models import (
    IMPORT_PERMISSION_REMINDER,
    IMPORTABLE_SCOPES,
    SCOPE_HINTS,
    SOURCE_FOLDER,
    SOURCE_ZIP,
    ImportDecision,
    ImportPreviewView,
)
from aura.gui.skills_manager.models import scope_label
from aura.gui.theme import BG, DANGER, FG, FG_DIM, FG_MUTED, WARN
from aura.skills.identity import InstallScope

GITHUB_URL_HINT = (
    "Paste a public GitHub repository URL, or a tree/<ref>/<path> URL pointing "
    "at one skill folder. Private repositories are not supported."
)


class InstallScopeDialog(QDialog):
    """Project or Personal, defaulting to Project. Bundled is never offered."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Where should this skill be installed?")
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self._buttons: dict[InstallScope, QRadioButton] = {}
        for scope in IMPORTABLE_SCOPES:
            hint = SCOPE_HINTS.get(scope, "")
            button = QRadioButton(f"{scope_label(scope.value)} — {hint}")
            layout.addWidget(button)
            self._buttons[scope] = button
        self._buttons[IMPORTABLE_SCOPES[0]].setChecked(True)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

    def selected_scope(self) -> InstallScope:
        for scope, button in self._buttons.items():
            if button.isChecked():
                return scope
        return IMPORTABLE_SCOPES[0]

    def offered_scopes(self) -> tuple[InstallScope, ...]:
        """Exactly the destinations this dialog puts in front of the user."""
        return tuple(self._buttons)


class ImportReviewDialog(QDialog):
    """The last stop before installation: everything staged, nothing hidden.

    Renders one :class:`ImportPreviewView` and offers at most one install
    action. A conflicting preview offers replacement and nothing else, an
    invalid preview offers no install at all, and Cancel is always the
    default button so a stray Return key never replaces a skill.
    """

    def __init__(self, view: ImportPreviewView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._view = view
        self._decision = ImportDecision.CANCEL
        self.setWindowTitle("Review skill before installing")
        self.setModal(True)
        self.resize(560, 520)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 14)
        outer.setSpacing(10)

        heading = QLabel(f"Import “{view.source_label}”")
        heading.setWordWrap(True)
        heading.setStyleSheet(
            f"color: {FG}; font-size: 15px; font-weight: 600; background: transparent;"
        )
        outer.addWidget(heading)

        body = QWidget()
        body.setStyleSheet(f"background: {BG};")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(12, 10, 12, 10)
        body_layout.setSpacing(8)

        self._summary = QLabel(_summary_text(view))
        self._summary.setWordWrap(True)
        self._summary.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._summary.setStyleSheet(f"color: {FG_DIM}; font-size: 12px; background: transparent;")
        body_layout.addWidget(self._summary)

        self._metadata = QLabel(f"Relevant metadata:\n{view.metadata_text}")
        self._metadata.setWordWrap(True)
        self._metadata.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        self._metadata.setStyleSheet(
            f"color: {FG_DIM}; font-size: 12px; background: transparent;"
        )
        body_layout.addWidget(self._metadata)

        self._scripts = QLabel(f"Scripts or executable files: {view.scripts_text}")
        self._scripts.setWordWrap(True)
        colour = WARN if view.has_scripts else FG_DIM
        weight = "600" if view.has_scripts else "400"
        self._scripts.setStyleSheet(
            f"color: {colour}; font-size: 12px; font-weight: {weight}; background: transparent;"
        )
        body_layout.addWidget(self._scripts)

        self._diagnostics = QLabel(_diagnostics_text(view))
        self._diagnostics.setWordWrap(True)
        diag_colour = FG_DIM if view.installable else DANGER
        self._diagnostics.setStyleSheet(
            f"color: {diag_colour}; font-size: 12px; background: transparent;"
        )
        body_layout.addWidget(self._diagnostics)

        self._skill_markdown: QLabel | None = None
        if view.skill_markdown:
            self._skill_markdown = QLabel(
                "Complete generated SKILL.md:\n\n" + view.skill_markdown
            )
            self._skill_markdown.setWordWrap(True)
            self._skill_markdown.setTextFormat(Qt.TextFormat.PlainText)
            self._skill_markdown.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse
                | Qt.TextInteractionFlag.TextSelectableByKeyboard
            )
            self._skill_markdown.setStyleSheet(
                f"color: {FG}; font-size: 12px; background: transparent;"
            )
            body_layout.addWidget(self._skill_markdown)
        body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        reminder = QLabel(IMPORT_PERMISSION_REMINDER)
        reminder.setWordWrap(True)
        reminder.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px; background: transparent;")
        outer.addWidget(reminder)

        outer.addLayout(self._build_actions())

    def _build_actions(self) -> QHBoxLayout:
        actions = QHBoxLayout()
        actions.setSpacing(8)
        actions.addStretch(1)

        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        actions.addWidget(cancel)

        self._install_btn: QPushButton | None = None
        offered = self._view.decision
        if offered is ImportDecision.INSTALL:
            self._install_btn = QPushButton("Install")
            self._install_btn.setObjectName("primary")
        elif offered is ImportDecision.REPLACE:
            self._install_btn = QPushButton("Replace existing skill")
            self._install_btn.setObjectName("danger")
        if self._install_btn is not None:
            self._install_btn.clicked.connect(self._on_install_clicked)
            self._install_btn.setAutoDefault(False)
            self._install_btn.setDefault(False)
            actions.addWidget(self._install_btn)

        # Cancel is the safe answer, so it keeps the default key even when a
        # replacement is on offer beside it.
        cancel.setAutoDefault(True)
        cancel.setDefault(True)
        cancel.setFocus()
        return actions

    def _on_install_clicked(self) -> None:
        self._decision = self._view.decision
        self.accept()

    def decision(self) -> ImportDecision:
        """What the user chose. CANCEL unless an install action was clicked."""
        return self._decision

    def install_action_text(self) -> str:
        """The offered install action's label, or "" when none is offered."""
        return self._install_btn.text() if self._install_btn is not None else ""

    def rendered_text(self) -> str:
        """Everything this dialog shows, as plain text."""
        return "\n".join(
            (
                self.windowTitle(),
                self._summary.text(),
                self._metadata.text(),
                self._scripts.text(),
                self._diagnostics.text(),
                self._skill_markdown.text() if self._skill_markdown is not None else "",
                IMPORT_PERMISSION_REMINDER,
            )
        )


def _summary_text(view: ImportPreviewView) -> str:
    conflict = (
        "Yes — a skill with this name is already installed here."
        if view.conflict
        else "No"
    )
    return "\n".join(
        (
            f"Name: {view.name}",
            f"Description: {view.description}",
            f"Destination: {view.destination_label} — {view.destination_hint}",
            f"Already installed with this name: {conflict}",
            f"Files: {view.file_count}",
            f"Resource folders: {view.resource_dirs_text}",
        )
    )


def _diagnostics_text(view: ImportPreviewView) -> str:
    if not view.diagnostics:
        return "Validation: no problems found."
    head = (
        "Validation:"
        if view.installable
        else "Validation failed — this skill cannot be installed:"
    )
    return head + "\n" + "\n".join(f"• {line}" for line in view.diagnostics)


class ImportPrompts:
    """Every question the import flow asks, in one replaceable place."""

    def __init__(self) -> None:
        self._active_review: ImportReviewDialog | None = None

    def ask_local_source_kind(self, parent: QWidget | None) -> str:
        box = QMessageBox(parent)
        box.setWindowTitle("Import skill")
        box.setText("Import a skill from a folder or from a ZIP archive?")
        folder = box.addButton("Folder…", QMessageBox.ButtonRole.AcceptRole)
        archive = box.addButton("ZIP archive…", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked is folder:
            return SOURCE_FOLDER
        if clicked is archive:
            return SOURCE_ZIP
        return ""

    def ask_scope(self, parent: QWidget | None) -> InstallScope | None:
        dialog = InstallScopeDialog(parent)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.selected_scope()

    def ask_folder(self, parent: QWidget | None) -> str:
        return QFileDialog.getExistingDirectory(parent, "Choose a skill folder")

    def ask_zip(self, parent: QWidget | None) -> str:
        path, _filter = QFileDialog.getOpenFileName(
            parent, "Choose a skill ZIP archive", "", "ZIP archives (*.zip)"
        )
        return path

    def ask_github_url(self, parent: QWidget | None) -> str:
        text, accepted = QInputDialog.getText(parent, "Install from GitHub", GITHUB_URL_HINT)
        return text.strip() if accepted else ""

    def review(self, parent: QWidget | None, view: ImportPreviewView) -> ImportDecision:
        dialog = ImportReviewDialog(view, parent)
        self._active_review = dialog
        try:
            dialog.exec()
        finally:
            self._active_review = None
        return dialog.decision()

    def close_review(self) -> None:
        """Dismiss an open review, for a session abandoned out from under it."""
        dialog = self._active_review
        if dialog is not None:
            dialog.reject()

    def show_error(self, parent: QWidget | None, title: str, message: str) -> None:
        QMessageBox.warning(parent, title, message)


__all__ = [
    "GITHUB_URL_HINT",
    "ImportPrompts",
    "ImportReviewDialog",
    "InstallScopeDialog",
]
