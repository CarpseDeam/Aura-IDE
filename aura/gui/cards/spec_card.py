"""Worker dispatch spec — collapsible, with Dispatch/Edit/Cancel buttons."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.theme import ACCENT, BG_ALT, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS


class SpecCard(QFrame):
    """Worker dispatch spec — collapsible, with Dispatch/Edit/Cancel buttons.

    After dispatch, the buttons collapse into a status header and a "View Worker"
    button appears to open the pop-out WorkerWindow.
    """

    dispatch_clicked = Signal(str)  # tool_call_id (with current spec values)
    edit_clicked = Signal(str)
    cancel_clicked = Signal(str)
    view_worker_clicked = Signal(str)  # tool_call_id

    def __init__(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
    ) -> None:
        super().__init__()
        self.setObjectName("specCard")
        self._tool_call_id = tool_call_id
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._dispatched = False
        self._cancelled = False
        self._worker_running = False

        self.setStyleSheet(
            f"QFrame#specCard {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {ACCENT}; border-radius: 8px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(8)

        header = QLabel("⚡ Dispatch to Worker")
        header.setStyleSheet(f"color: {ACCENT}; font-weight: 700; font-size: 12px;")
        outer.addWidget(header)

        self._goal_label = QLabel(self._goal)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(f"color: {FG}; font-weight: 600;")
        outer.addWidget(self._goal_label)

        self._files_label = QLabel(self._format_files(self._files))
        self._files_label.setWordWrap(True)
        self._files_label.setStyleSheet(
            f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
            "font-size: 11px;"
        )
        outer.addWidget(self._files_label)

        # Spec body (collapsible if long).
        self._spec_label = QLabel(self._spec)
        self._spec_label.setWordWrap(True)
        self._spec_label.setStyleSheet(f"color: {FG};")
        self._spec_label.setTextFormat(Qt.TextFormat.PlainText)

        self._spec_section: _CollapsibleSection | None = None
        if self._spec.count("\n") > 6 or len(self._spec) > 600:
            section = _CollapsibleSection(
                "Spec", self._spec_label, start_open=False, prominent=False
            )
            self._spec_section = section
            outer.addWidget(section)
        else:
            outer.addWidget(self._spec_label)

        self._acceptance_label = QLabel(f"Acceptance: {self._acceptance}")
        self._acceptance_label.setWordWrap(True)
        self._acceptance_label.setStyleSheet(
            f"color: {FG_MUTED}; font-style: italic;"
        )
        outer.addWidget(self._acceptance_label)

        # Buttons row.
        self._buttons_row = QWidget()
        btn_layout = QHBoxLayout(self._buttons_row)
        btn_layout.setContentsMargins(0, 4, 0, 0)
        btn_layout.setSpacing(8)

        self._dispatch_btn = QPushButton("Dispatch")
        self._dispatch_btn.setObjectName("primary")
        self._dispatch_btn.clicked.connect(self._on_dispatch)
        btn_layout.addWidget(self._dispatch_btn)

        self._edit_btn = QPushButton("Edit Spec")
        self._edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._tool_call_id))
        btn_layout.addWidget(self._edit_btn)

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setObjectName("danger")
        self._cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(self._cancel_btn)

        btn_layout.addStretch(1)

        outer.addWidget(self._buttons_row)

        # "View Worker" button — hidden until dispatch.
        self._view_worker_btn = QPushButton("View Worker")
        self._view_worker_btn.setVisible(False)
        self._view_worker_btn.clicked.connect(
            lambda: self.view_worker_clicked.emit(self._tool_call_id)
        )
        outer.addWidget(self._view_worker_btn)

        # Status label, hidden until dispatch/cancel.
        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(False)
        outer.addWidget(self._status_label)

    # ---- helpers ---------------------------------------------------------

    @staticmethod
    def _format_files(files: list[str]) -> str:
        if not files:
            return "(no files listed)"
        return "  ".join(f"• {p}" for p in files)

    def update_spec(
        self, goal: str, files: list[str], spec: str, acceptance: str
    ) -> None:
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._goal_label.setText(self._goal)
        self._files_label.setText(self._format_files(self._files))
        self._spec_label.setText(self._spec)
        self._acceptance_label.setText(f"Acceptance: {self._acceptance}")

    def current_spec(self) -> tuple[str, list[str], str, str]:
        return (self._goal, list(self._files), self._spec, self._acceptance)

    def tool_call_id(self) -> str:
        return self._tool_call_id

    # ---- button handlers -------------------------------------------------

    def _on_dispatch(self) -> None:
        self._dispatched = True
        self._worker_running = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Dispatched — worker running…")
        self._status_label.setVisible(True)
        self._view_worker_btn.setVisible(True)
        self.dispatch_clicked.emit(self._tool_call_id)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Cancelled.")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.cancel_clicked.emit(self._tool_call_id)

    def disable_buttons(self) -> None:
        self._dispatch_btn.setEnabled(False)
        self._edit_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)

    def worker_finished(self, ok: bool, summary: str) -> None:
        self._worker_running = False
        verb = "Completed" if ok else "Completed with errors"
        color = SUCCESS if ok else DANGER
        self._status_label.setText(verb)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        # Keep "View Worker" button visible for later review.
