"""Worker dispatch spec — collapsible cockpit-style Plan Ready card.

After dispatch, the buttons collapse into a status header.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from aura.conversation.workflow_state import WorkflowState, WorkflowStatus
from aura.gui.cards._collapsible import _CollapsibleSection
from aura.gui.cards._helpers import _MarkdownTextBlock
from aura.gui.cards.dispatch_status_labels import spec_finished_label, spec_replay_finished_label
from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import ACCENT, BG_ALT, BG_RAISED, BORDER, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

_CHIP_STYLE = (
    f"background: {BG_RAISED}; color: {FG_DIM}; "
    f"border: 1px solid {BORDER}; border-radius: 4px; "
    f"padding: 2px 8px; font-size: 10px; font-weight: 600;"
)

_RISKY_KEYWORDS = [
    "auth", "subprocess", "thread", "qthread", "git",
    "delete", "destructive", "reset", "credentials", "token",
    "migration", "database", "security",
]


class SpecCard(QFrame):
    """Worker dispatch spec — collapsible, with Dispatch/Edit/Cancel buttons.

    After dispatch, the buttons collapse into a status header.
    """

    dispatch_clicked = Signal(str)  # tool_call_id (with current spec values)
    edit_clicked = Signal(str)
    cancel_clicked = Signal(str)

    def __init__(
        self,
        tool_call_id: str,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
        steps: list[dict[str, Any]] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        self._tool_call_id = tool_call_id
        self._goal = goal
        self._files = files
        self._spec = spec
        self._acceptance = acceptance
        self._summary = summary
        self._steps = [dict(step) for step in (steps or []) if isinstance(step, dict)]
        self._is_campaign = bool(self._steps)
        self._dispatched = False
        self._cancelled = False
        self._worker_running = False

        self.setStyleSheet(
            f"QFrame#card {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {ACCENT}; border-radius: 8px; }}"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(8)

        # ---- Header row: "Plan Ready" + chips ----
        header_row = self._build_header()
        outer.addLayout(header_row)

        # ---- Goal ----
        self._goal_label = self._build_goal_section()
        outer.addWidget(self._goal_label)

        outer.addSpacing(6)
        self._strategy_label: _MarkdownTextBlock | None = None
        self._acceptance_label: _MarkdownTextBlock | None = None
        self._campaign_label: _MarkdownTextBlock | None = None
        if self._is_campaign:
            campaign_header, self._campaign_label = self._build_campaign_section()
            outer.addWidget(campaign_header)
            outer.addWidget(self._campaign_label)
        else:
            # ---- STRATEGY section ----
            strategy_header, self._strategy_label = self._build_strategy_section()
            outer.addWidget(strategy_header)
            outer.addWidget(self._strategy_label)

        # ---- SCOPE section ----
        outer.addSpacing(6)
        files_header, self._files_container = self._build_scope_section()
        outer.addWidget(files_header)
        outer.addWidget(self._files_container)

        if not self._is_campaign:
            # ---- VALIDATION section ----
            outer.addSpacing(6)
            acc_header, self._acceptance_label = self._build_validation_section()
            outer.addWidget(acc_header)
            outer.addWidget(self._acceptance_label)

        # ---- FULL WORKER SPEC collapsible section ----
        outer.addSpacing(6)
        self._raw_spec_section = self._build_full_spec_section()
        outer.addWidget(self._raw_spec_section)

        # ---- Buttons row ----
        (
            self._buttons_row,
            self._dispatch_btn,
            self._edit_btn,
            self._cancel_btn,
        ) = self._build_button_row()
        outer.addWidget(self._buttons_row)

        # ---- Status section ----
        self._status_label = self._build_status_section()
        outer.addWidget(self._status_label)


        if not self._dispatched:
            self._status_label.setText("Plan ready — waiting for dispatch approval.")
            self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
            self._status_label.setVisible(True)

        # ---- Initial chip computation ----
        self._compute_chips()

    # Private layout helpers

    def _build_header(self) -> QHBoxLayout:
        """Return the header row layout: label + stretch + mode/risk/scope chips."""
        header_row = QHBoxLayout()
        header_label = QLabel("⚡ Plan Ready", parent=self)
        header_label.setStyleSheet(f"color: {ACCENT}; font-weight: 700; font-size: 12px;")
        header_row.addWidget(header_label)
        header_row.addStretch(1)

        self._mode_chip = self._make_chip("Fast Plan")
        header_row.addWidget(self._mode_chip)

        self._risk_chip = self._make_chip("Low Risk")
        header_row.addWidget(self._risk_chip)

        self._scope_chip = self._make_chip("0 files")
        header_row.addWidget(self._scope_chip)

        return header_row

    def _build_goal_section(self) -> _MarkdownTextBlock:
        """Create and return the goal label."""
        label = _MarkdownTextBlock(_render_markdown_with_code(self._goal), parent=self)
        label.setStyleSheet(
            f"background: transparent; border: none; color: {FG}; font-size: 14px;"
        )
        return label

    def _build_strategy_section(self) -> tuple[QLabel, _MarkdownTextBlock]:
        """Create STRATEGY header label and strategy text block. Returns both."""
        strategy_header = self._make_section_header("STRATEGY")
        strategy_text = self._compute_strategy_text()
        strategy_label = _MarkdownTextBlock(
            _render_markdown_with_code(strategy_text), parent=self
        )
        strategy_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG};"
        )
        return strategy_header, strategy_label

    def _build_scope_section(self) -> tuple[QLabel, QWidget]:
        """Create SCOPE header and files container widget. Returns both."""
        files_header = self._make_section_header("SCOPE")
        self._files_container = QWidget(self)
        files_layout = QVBoxLayout(self._files_container)
        files_layout.setContentsMargins(0, 0, 0, 0)
        files_layout.setSpacing(4)
        self._refresh_files_list(files_layout)
        return files_header, self._files_container

    def _build_validation_section(self) -> tuple[QLabel, _MarkdownTextBlock]:
        """Create VALIDATION header and acceptance text block. Returns both."""
        acc_header = self._make_section_header("VALIDATION")
        acceptance_label = _MarkdownTextBlock(
            _render_markdown_with_code(self._acceptance), parent=self
        )
        acceptance_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG_DIM};"
        )
        return acc_header, acceptance_label

    def _build_campaign_section(self) -> tuple[QLabel, _MarkdownTextBlock]:
        """Create CAMPAIGN header and ordered step list. Returns both."""
        campaign_header = self._make_section_header("CAMPAIGN")
        campaign_label = _MarkdownTextBlock(
            _render_markdown_with_code(self._format_campaign_steps()), parent=self
        )
        campaign_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG};"
        )
        return campaign_header, campaign_label

    def _build_full_spec_section(self) -> _CollapsibleSection:
        """Create the FULL WORKER SPEC collapsible section.

        Always wrapped in _CollapsibleSection with start_open=False.
        """
        self._spec_body_label = _MarkdownTextBlock(
            _render_markdown_with_code(self._spec), parent=self
        )
        self._spec_body_label.setStyleSheet(
            f"background: transparent; border: none; color: {FG};"
        )

        section = _CollapsibleSection(
            "Show Full Worker Spec", self._spec_body_label,
            start_open=False, prominent=False,
        )
        # After the section's own toggle runs, update the toggle title.
        section._toggle.clicked.connect(
            lambda: section.set_title(
                "Hide Full Worker Spec" if section._open else "Show Full Worker Spec"
            )
        )
        return section

    def _build_button_row(self) -> tuple[QWidget, QPushButton, QPushButton | None, QPushButton]:
        """Create the button row widget. Returns (row_widget, dispatch_btn, edit_btn, cancel_btn)."""
        buttons_row = QWidget(self)
        btn_layout = QHBoxLayout(buttons_row)
        btn_layout.setContentsMargins(0, 8, 0, 0)
        btn_layout.setSpacing(10)

        dispatch_btn = QPushButton("Dispatch", parent=buttons_row)
        dispatch_btn.setObjectName("primary")
        dispatch_btn.setMinimumHeight(34)
        dispatch_btn.setMinimumWidth(128)
        dispatch_btn.clicked.connect(self._on_dispatch)
        btn_layout.addWidget(dispatch_btn)

        edit_btn: QPushButton | None = None
        if not self._is_campaign:
            edit_btn = QPushButton("Edit Spec", parent=buttons_row)
            edit_btn.setMinimumHeight(32)
            edit_btn.setMinimumWidth(96)
            edit_btn.clicked.connect(lambda: self.edit_clicked.emit(self._tool_call_id))
            btn_layout.addWidget(edit_btn)

        btn_layout.addStretch(1)

        cancel_btn = QPushButton("Cancel", parent=buttons_row)
        cancel_btn.setObjectName("danger")
        cancel_btn.setMinimumHeight(32)
        cancel_btn.setMinimumWidth(88)
        cancel_btn.clicked.connect(self._on_cancel)
        btn_layout.addWidget(cancel_btn)

        return buttons_row, dispatch_btn, edit_btn, cancel_btn

    def _build_status_section(self) -> QLabel:
        """Create the status label (hidden by default)."""
        status_label = QLabel("", parent=self)
        status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        status_label.setVisible(False)
        return status_label

    # Static helpers

    @staticmethod
    def _make_section_header(text: str) -> QLabel:
        """Create a section header QLabel with muted styling."""
        header = QLabel(text)
        header.setStyleSheet(
            f"color: {FG_MUTED}; font-weight: 700; font-size: 10px;"
        )
        return header

    @staticmethod
    def _make_chip(text: str, color: str | None = None) -> QLabel:
        """Create a chip-style QLabel. If color is provided, override text color."""
        style = _CHIP_STYLE
        if color:
            style = style.replace(f"color: {FG_DIM}", f"color: {color}")
        chip = QLabel(text)
        chip.setStyleSheet(style)
        return chip

    # Chip computation

    def _compute_chips(self) -> None:
        """Update mode, risk, and scope chip text and styling."""
        # --- Mode chip ---
        if self._is_campaign:
            mode_text = "Campaign"
            mode_color = WARN
        elif len(self._files) <= 2 and len(self._spec) < 800:
            mode_text = "Fast Plan"
            mode_color = SUCCESS
        else:
            mode_text = "Careful Plan"
            mode_color = WARN
        self._mode_chip.setText(mode_text)
        self._mode_chip.setStyleSheet(
            _CHIP_STYLE.replace(f"color: {FG_DIM}", f"color: {mode_color}")
        )

        # --- Risk chip ---
        combined = f"{self._goal} {self._spec} {self._summary}".lower()
        has_risky = any(kw in combined for kw in _RISKY_KEYWORDS)

        if has_risky:
            risk_text = "High Risk"
            risk_color = DANGER
        elif len(self._files) <= 1:
            risk_text = "Low Risk"
            risk_color = SUCCESS
        else:
            risk_text = "Medium Risk"
            risk_color = WARN
        self._risk_chip.setText(risk_text)
        self._risk_chip.setStyleSheet(
            _CHIP_STYLE.replace(f"color: {FG_DIM}", f"color: {risk_color}")
        )

        # --- Scope chip ---
        if self._files:
            n = len(self._files)
            scope_text = f"{n} file{'s' if n != 1 else ''}"
            scope_color = FG_DIM
        else:
            scope_text = "No files"
            scope_color = FG_MUTED
        self._scope_chip.setText(scope_text)
        self._scope_chip.setStyleSheet(
            _CHIP_STYLE.replace(f"color: {FG_DIM}", f"color: {scope_color}")
        )

    # Content refresh

    def _refresh_files_list(self, layout: QVBoxLayout) -> None:
        """Clear and rebuild the files list with polished chip styling."""
        # Clear layout
        while layout.count():
            child = layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()

        if not self._files:
            lbl = QLabel("Files will be discovered as needed", parent=self._files_container)
            lbl.setStyleSheet(
                f"color: {FG_MUTED}; font-style: italic; font-size: 11px;"
            )
            layout.addWidget(lbl)
            return

        for path in self._files:
            lbl = QLabel(f"• {path}", parent=self._files_container)
            lbl.setStyleSheet(
                f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
                f"border-radius: 4px; padding: 2px 8px; "
                f"color: {FG_DIM}; font-family: 'Geist Mono', 'JetBrains Mono', monospace; "
                f"font-size: 11px;"
            )
            lbl.setToolTip(path)
            layout.addWidget(lbl)

    @staticmethod
    def _format_files(files: list[str]) -> str:
        """Format file list for display in plain text contexts."""
        if not files:
            return "(files discovered as needed)"
        return "  ".join(f"• {p}" for p in files)

    def _compute_strategy_text(self) -> str:
        """Return the text to display in the STRATEGY section.

        Never returns the full spec. Prefers self._summary, then derives
        a compact preview from self._spec.
        """
        if self._summary:
            return self._summary
        # Derive a compact preview from spec lines
        for line in self._spec.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                # Found first non-empty, non-heading line
                if len(line) > 300:
                    return line[:300] + "…"
                return line
        return "No summary available."

    def _format_campaign_steps(self) -> str:
        lines: list[str] = []
        for index, step in enumerate(self._steps, start=1):
            title = str(step.get("title") or step.get("goal") or f"Step {index}").strip()
            files = step.get("files") if isinstance(step.get("files"), list) else []
            file_text = ", ".join(str(path) for path in files if str(path).strip())
            lines.append(f"{index}. **{title}**")
            if file_text:
                lines.append(f"   Files: {file_text}")
            else:
                lines.append("   Files: discovered as needed")
        return "\n".join(lines) if lines else "No campaign steps provided."

    def _refresh_all_content(self) -> None:
        """Refresh all derived display: markdown blocks, files list, chips."""
        self._goal_label.setHtml(_render_markdown_with_code(self._goal))
        if self._campaign_label is not None:
            self._campaign_label.setHtml(_render_markdown_with_code(self._format_campaign_steps()))
        if self._strategy_label is not None:
            strategy_text = self._compute_strategy_text()
            self._strategy_label.setHtml(_render_markdown_with_code(strategy_text))
        if self._acceptance_label is not None:
            self._acceptance_label.setHtml(_render_markdown_with_code(self._acceptance))
        self._spec_body_label.setHtml(_render_markdown_with_code(self._spec))
        if hasattr(self, "_files_container"):
            self._refresh_files_list(self._files_container.layout())
        self._compute_chips()

    # Public API

    def update_spec(
        self,
        goal: str,
        files: list[str],
        spec: str,
        acceptance: str,
        summary: str = "",
        steps: list[dict[str, Any]] | None = None,
    ) -> None:
        """Update all stored values and refresh all derived display."""
        self._goal = goal
        self._files = list(files)
        self._spec = spec
        self._acceptance = acceptance
        self._summary = summary
        if steps is not None:
            self._steps = [dict(step) for step in steps if isinstance(step, dict)]
        self._refresh_all_content()

    def current_spec(self) -> tuple[str, list[str], str, str, str]:
        """Return (goal, files, spec, acceptance, summary)."""
        return (self._goal, list(self._files), self._spec, self._acceptance, self._summary)

    def tool_call_id(self) -> str:
        """Return the tool call ID for this card."""
        return self._tool_call_id

    # ---- button handlers -------------------------------------------------

    def _on_dispatch(self) -> None:
        self._dispatched = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Dispatch requested…")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.dispatch_clicked.emit(self._tool_call_id)

    def _on_cancel(self) -> None:
        self._cancelled = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Cancelled")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)
        self.cancel_clicked.emit(self._tool_call_id)

    def mark_dispatched(self) -> None:
        """Reflect a modal approval without emitting another dispatch signal."""
        self._dispatched = True
        self._worker_running = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Worker running...")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(True)

    def mark_worker_running(self) -> None:
        """Update status to indicate worker is running."""
        self._dispatched = True
        self._worker_running = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Worker running...")
        self._status_label.setStyleSheet(f"color: {FG_DIM}; font-size: 11px;")
        self._status_label.setVisible(True)

    def mark_stale(self) -> None:
        """Update status to indicate the card is stale/non-pending."""
        self._dispatched = False
        self._worker_running = False
        self._buttons_row.setVisible(False)
        self._status_label.setText("Stale plan — not pending")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)

    def mark_dispatch_expired(self) -> None:
        """Update status when dispatch is no longer pending (stale card button)."""
        self._dispatched = False
        self._worker_running = False
        self._buttons_row.setVisible(False)
        self._status_label.setText("Plan expired — click Dispatch again or Cancel")
        self._status_label.setStyleSheet(f"color: {WARN}; font-size: 11px;")
        self._status_label.setVisible(True)

    def mark_cancelled(self) -> None:
        """Reflect a modal cancellation without emitting another cancel signal."""
        self._cancelled = True
        self._buttons_row.setVisible(False)
        self._status_label.setText("Cancelled")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")
        self._status_label.setVisible(True)

    def disable_buttons(self) -> None:
        """Disable all buttons on the card."""
        self._dispatch_btn.setEnabled(False)
        if self._edit_btn is not None:
            self._edit_btn.setEnabled(False)
        self._cancel_btn.setEnabled(False)

    def worker_finished(self, ok: bool, summary: str, status: str | None = None, *, is_internal: bool = False) -> None:
        """Update status when worker completes."""
        self._worker_running = False
        verb, color = spec_finished_label(ok, status, is_internal=is_internal)
        self._status_label.setText(verb)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 11px;")

    @staticmethod
    def _finished_status_label(ok: bool, status: str | None = None) -> tuple[str, str]:
        """Canonical finished label via ``dispatch_status_labels.spec_finished_label``."""
        return spec_finished_label(ok, status)

    def worker_cancelled(self) -> None:
        """Update status when worker is cancelled during execution."""
        self._worker_running = False
        self._status_label.setText("Cancelled")
        self._status_label.setStyleSheet(f"color: {DANGER}; font-size: 11px;")

    def update_workflow_state(self, state: WorkflowState) -> None:
        """Render the authoritative active Worker state on the plan card."""
        label, color = self._workflow_status_label(state.status)
        self._status_label.setText(label)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._status_label.setVisible(True)

        if state.status in {
            WorkflowStatus.dispatched,
            WorkflowStatus.editing,
            WorkflowStatus.validating,
            WorkflowStatus.blocked,
        }:
            self._buttons_row.setVisible(False)
        elif state.status in {
            WorkflowStatus.done,
            WorkflowStatus.cancelled,
            WorkflowStatus.planner_resolving,
            WorkflowStatus.failed_retryable,
            WorkflowStatus.failed_nonrecoverable,
        }:
            self._buttons_row.setVisible(False)



    @staticmethod
    def _workflow_status_label(status: WorkflowStatus) -> tuple[str, str]:
        mapping = {
            WorkflowStatus.intent_captured: ("Intent captured", FG_DIM),
            WorkflowStatus.plan_ready: ("Awaiting dispatch", FG_DIM),
            WorkflowStatus.planner_resolving: ("Continuing", FG_DIM),
            WorkflowStatus.dispatched: ("Running", FG_DIM),
            WorkflowStatus.editing: ("Editing", ACCENT),
            WorkflowStatus.validating: ("Validating", WARN),
            WorkflowStatus.blocked: ("Needs attention", WARN),
            WorkflowStatus.failed_retryable: ("Continuing", FG_DIM),
            WorkflowStatus.failed_nonrecoverable: ("Failed", DANGER),
            WorkflowStatus.done: ("Done", SUCCESS),
            WorkflowStatus.cancelled: ("Cancelled", DANGER),
        }
        return mapping[status]





    def set_dispatched_and_finished(self, ok: bool, *, is_internal: bool = False) -> None:
        """Force the card into a read-only finished state (for history replay)."""
        self._dispatched = True
        self._worker_running = False
        self._buttons_row.setVisible(False)
        verb, color = spec_replay_finished_label(ok, is_internal=is_internal)
        self._status_label.setText(verb)
        self._status_label.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._status_label.setVisible(True)
