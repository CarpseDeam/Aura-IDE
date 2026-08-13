"""Modal dialog for editing a Plan Review plan before implementing it.

Used only for the "Edit Plan" action; the primary experience is the inline
``PlanReviewCard`` in chat, not this dialog.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import BG, BORDER


class PlanEditDialog(QDialog):
    """Edit goal / files / spec / acceptance / summary before implementing."""

    def __init__(
        self,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit Plan")
        self.setModal(True)
        self.resize(720, 620)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setStyleSheet(f"background: {BG}; border: none;")
        main_layout.addWidget(scroll)

        scroll_content = QWidget()
        scroll_content.setStyleSheet(f"background: {BG};")
        scroll.setWidget(scroll_content)

        outer = QVBoxLayout(scroll_content)
        outer.setContentsMargins(18, 16, 18, 12)
        outer.setSpacing(10)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(8)

        self._goal_edit = QLineEdit(goal)
        form.addRow("Goal:", self._goal_edit)

        self._files_edit = QLineEdit(", ".join(files))
        self._files_edit.setPlaceholderText("comma-separated workspace-relative paths")
        form.addRow("Files:", self._files_edit)

        self._spec_edit = QPlainTextEdit(spec)
        self._spec_edit.setMinimumHeight(220)
        form.addRow("Spec:", self._spec_edit)

        self._acceptance_edit = QPlainTextEdit(acceptance)
        self._acceptance_edit.setMinimumHeight(80)
        form.addRow("Acceptance:", self._acceptance_edit)

        self._summary_edit = QPlainTextEdit(summary)
        self._summary_edit.setMinimumHeight(60)
        self._summary_edit.setPlaceholderText("Concise summary of intended changes for the user")
        form.addRow("Summary:", self._summary_edit)

        outer.addLayout(form)

        btn_container = QWidget(self)
        btn_container.setStyleSheet(f"background: {BG}; border-top: 1px solid {BORDER};")
        btn_layout = QHBoxLayout(btn_container)
        btn_layout.setContentsMargins(18, 12, 18, 12)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        btn_layout.addWidget(buttons)
        main_layout.addWidget(btn_container)

    def goal(self) -> str:
        return self._goal_edit.text().strip()

    def files(self) -> list[str]:
        raw = self._files_edit.text().strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def spec(self) -> str:
        return self._spec_edit.toPlainText().strip()

    def acceptance(self) -> str:
        return self._acceptance_edit.toPlainText().strip()

    def summary(self) -> str:
        return self._summary_edit.toPlainText().strip()


__all__ = ["PlanEditDialog"]
