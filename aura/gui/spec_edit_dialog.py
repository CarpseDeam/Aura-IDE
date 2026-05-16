"""Modal dialog for editing a worker dispatch spec before sending it."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import (
    ACCENT,
    BG,
    BORDER,
    FG,
    FG_DIM,
)


class SpecEditDialog(QDialog):
    """Edit goal / files / spec / acceptance / summary before dispatching."""

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
        self.setWindowTitle("Edit Plan Spec")
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

        # Button row (outside scroll area)
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


class SpecApprovalDialog(QDialog):
    """Modal dialog for reviewing and confirming a worker dispatch."""

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
        self.setWindowTitle("Review Plan")
        self.setModal(True)
        self.resize(800, 680)

        # Store initial values for editing.
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._summary = summary

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

        # Header
        header = QLabel("⚡ Review Plan")
        header.setStyleSheet(
            f"color: {ACCENT}; font-weight: bold; font-size: 14px;"
        )
        outer.addWidget(header)

        # Goal
        self._goal_label = QLabel(self._goal)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        outer.addWidget(self._goal_label)

        # Files
        self._files_label = QLabel(self._format_files(self._files))
        self._files_label.setWordWrap(True)
        self._files_label.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Cascadia Mono', Consolas, monospace; "
            "font-size: 11px;"
        )
        outer.addWidget(self._files_label)

        # Spec view
        self._spec_view = QPlainTextEdit()
        self._spec_view.setReadOnly(True)
        self._spec_view.setPlainText(self._spec)
        mono = QFont("Cascadia Mono, Consolas, Menlo, monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        mono.setFixedPitch(True)
        mono.setPointSize(10)
        self._spec_view.setFont(mono)
        self._spec_view.setMinimumHeight(200)
        self._spec_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 8px; }}"
        )
        outer.addWidget(self._spec_view, 1)

        # Acceptance view
        self._acceptance_view = QPlainTextEdit()
        self._acceptance_view.setReadOnly(True)
        self._acceptance_view.setPlainText(self._acceptance)
        self._acceptance_view.setFont(mono)
        self._acceptance_view.setMinimumHeight(80)
        self._acceptance_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 8px; }}"
        )
        outer.addWidget(self._acceptance_view)

        # Summary view
        self._summary_view = QPlainTextEdit()
        self._summary_view.setReadOnly(True)
        self._summary_view.setPlainText(self._summary)
        self._summary_view.setFont(mono)
        self._summary_view.setMinimumHeight(60)
        self._summary_view.setStyleSheet(
            f"QPlainTextEdit {{ background: {BG}; color: {FG_DIM}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; padding: 8px; font-style: italic; }}"
        )
        outer.addWidget(self._summary_view)

        # Button row (outside scroll area)
        btn_container = QWidget(self)
        btn_container.setStyleSheet(f"background: {BG}; border-top: 1px solid {BORDER};")
        button_row = QHBoxLayout(btn_container)
        button_row.setContentsMargins(18, 12, 18, 12)
        button_row.setSpacing(8)
        button_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setObjectName("danger")
        cancel_btn.clicked.connect(self.reject)
        button_row.addWidget(cancel_btn)

        edit_btn = QPushButton("Edit Spec")
        edit_btn.clicked.connect(self._on_edit_spec)
        button_row.addWidget(edit_btn)

        dispatch_btn = QPushButton("Dispatch")
        dispatch_btn.setObjectName("success")
        dispatch_btn.setDefault(True)
        dispatch_btn.clicked.connect(self.accept)
        button_row.addWidget(dispatch_btn)

        main_layout.addWidget(btn_container)

    @staticmethod
    def _format_files(files: list[str]) -> str:
        if not files:
            return "(no files listed)"
        return "  ".join(f"• {p}" for p in files)

    def _on_edit_spec(self) -> None:
        dlg = SpecEditDialog(
            self._goal, list(self._files), self._spec, self._acceptance, self._summary, parent=self
        )
        if dlg.exec() == SpecEditDialog.DialogCode.Accepted:
            self._goal = dlg.goal()
            self._files = dlg.files()
            self._spec = dlg.spec()
            self._acceptance = dlg.acceptance()
            self._summary = dlg.summary()
            self._goal_label.setText(self._goal)
            self._files_label.setText(self._format_files(self._files))
            self._spec_view.setPlainText(self._spec)
            self._acceptance_view.setPlainText(self._acceptance)
            self._summary_view.setPlainText(self._summary)

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)

    # --- Public accessors ---

    def goal(self) -> str:
        return self._goal

    def files(self) -> list[str]:
        return list(self._files)

    def spec(self) -> str:
        return self._spec

    def acceptance(self) -> str:
        return self._acceptance

    def summary(self) -> str:
        return self._summary

    def dispatched(self) -> bool:
        return self.result() == QDialog.DialogCode.Accepted
