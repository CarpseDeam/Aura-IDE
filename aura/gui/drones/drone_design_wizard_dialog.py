from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from aura.gui.theme import ACCENT, BG, BG_ALT, BG_RAISED, BORDER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN


@dataclass
class DesignedDroneDraft:
    name: str
    instructions: str
    output_contract: str
    write_policy: str


_WRITE_POLICY_LABELS: dict[str, str] = {
    "read_only": "No — read-only",
    "ask_before_writes": "Yes — ask before each write",
    "normal_diff_approval": "Yes — normal diff approval",
}

_POLICY_BADGE_COLORS: dict[str, str] = {
    "read_only": WARN,
    "ask_before_writes": ACCENT,
    "normal_diff_approval": SUCCESS,
}

_PAGE_COUNT = 5


class DroneDesignWizardDialog(QDialog):
    """A multi-page wizard that guides the user through designing a Drone."""

    def __init__(self, workspace_root: Path | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._workspace_root = workspace_root
        self._draft: DesignedDroneDraft | None = None

        self.setWindowTitle("Design a Drone")
        self.setMinimumWidth(540)
        self.setModal(True)
        self.setStyleSheet(
            f"QDialog {{ background: {BG_ALT}; }}"
        )

        self._build_ui()

    # -- Public API --

    @property
    def draft(self) -> DesignedDroneDraft | None:
        return self._draft

    # -- UI construction --

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 16)
        layout.setSpacing(0)

        # Stacked pages
        self._stack = QStackedWidget(self)
        self._stack.setStyleSheet("background: transparent;")

        self._pages: list[QWidget] = []
        self._pages.append(self._build_intro_page())         # 0
        self._pages.append(self._build_job_description_page())  # 1
        self._pages.append(self._build_write_policy_page())     # 2
        self._pages.append(self._build_output_contract_page())  # 3
        self._pages.append(self._build_preview_page())          # 4

        for page in self._pages:
            self._stack.addWidget(page)

        layout.addWidget(self._stack, 1)

        # Navigation buttons
        nav_layout = QHBoxLayout()
        nav_layout.setContentsMargins(0, 16, 0, 0)
        nav_layout.setSpacing(8)

        self._back_btn = QPushButton("Back")
        self._back_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; "
            f"padding: 6px 18px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; border-color: {ACCENT}; }}"
        )
        self._back_btn.clicked.connect(self._go_back)

        self._next_btn = QPushButton("Next")
        self._next_btn.setObjectName("primary")
        self._next_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 20px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton#primary:hover {{ background: #94b6ff; }}"
        )
        self._next_btn.clicked.connect(self._go_next)

        self._accept_btn = QPushButton("Open in Editor")
        self._accept_btn.setObjectName("primary")
        self._accept_btn.setStyleSheet(
            f"QPushButton#primary {{ background: {ACCENT}; color: {BG}; "
            f"border: 1px solid {ACCENT}; border-radius: 6px; "
            f"padding: 6px 20px; font-weight: 600; font-size: 13px; }}"
            f"QPushButton#primary:hover {{ background: #94b6ff; }}"
        )
        self._accept_btn.clicked.connect(self._on_accept)
        self._accept_btn.hide()

        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {FG_DIM}; "
            f"border: 1px solid {BORDER}; border-radius: 6px; "
            f"padding: 6px 18px; font-size: 13px; }}"
            f"QPushButton:hover {{ background: {BG_RAISED}; color: {FG}; }}"
        )
        self._cancel_btn.clicked.connect(self.reject)

        nav_layout.addWidget(self._back_btn)
        nav_layout.addStretch(1)
        nav_layout.addWidget(self._cancel_btn)
        nav_layout.addWidget(self._next_btn)
        nav_layout.addWidget(self._accept_btn)

        layout.addLayout(nav_layout)

        self._update_nav(0)

    # -- Pages --

    def _build_intro_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        spacer = QWidget()
        spacer.setFixedHeight(40)
        layout.addWidget(spacer)

        title = QLabel("Design a Drone")
        title.setStyleSheet(
            f"font-size: 22px; font-weight: 700; color: {FG}; background: transparent;"
        )
        layout.addWidget(title)

        desc = QLabel(
            "Describe the job. Aura will turn it into a reusable Drone draft."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(
            f"font-size: 14px; color: {FG_DIM}; background: transparent; "
            f"line-height: 1.5;"
        )
        layout.addWidget(desc)

        layout.addStretch(1)
        return page

    def _build_job_description_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("What job should this Drone do for this project?")
        label.setWordWrap(True)
        label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {FG}; background: transparent;"
        )
        layout.addWidget(label)

        self._job_edit = QPlainTextEdit()
        self._job_edit.setPlaceholderText(
            "Example: Review recent changes and write concise release notes."
        )
        self._job_edit.setMinimumHeight(120)
        self._job_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 8px; color: {FG}; "
            f"font-size: 13px;"
        )
        layout.addWidget(self._job_edit, 1)

        return page

    def _build_write_policy_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("Should this Drone edit files?")
        label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {FG}; background: transparent;"
        )
        layout.addWidget(label)

        self._policy_readonly = QRadioButton("No — read-only")
        self._policy_readonly.setChecked(True)
        self._policy_ask = QRadioButton("Yes — ask before each write")
        self._policy_approval = QRadioButton("Yes — normal diff approval")

        for rb in (self._policy_readonly, self._policy_ask, self._policy_approval):
            rb.setStyleSheet(
                f"QRadioButton {{ color: {FG}; font-size: 13px; padding: 4px 0; "
                f"background: transparent; }}"
                f"QRadioButton::indicator {{ width: 16px; height: 16px; }}"
            )

        layout.addWidget(self._policy_readonly)
        layout.addWidget(self._policy_ask)
        layout.addWidget(self._policy_approval)

        layout.addStretch(1)
        return page

    def _build_output_contract_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        label = QLabel("What should it bring back?")
        label.setStyleSheet(
            f"font-size: 14px; font-weight: 600; color: {FG}; background: transparent;"
        )
        layout.addWidget(label)

        self._contract_edit = QPlainTextEdit()
        self._contract_edit.setPlainText(
            "Return: 1. Summary of findings 2. Files inspected 3. Recommendations"
        )
        self._contract_edit.setMinimumHeight(100)
        self._contract_edit.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 6px; padding: 8px; color: {FG}; "
            f"font-size: 13px;"
        )
        layout.addWidget(self._contract_edit, 1)

        return page

    def _build_preview_page(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        preview_title = QLabel("Preview")
        preview_title.setStyleSheet(
            f"font-size: 18px; font-weight: 700; color: {FG}; background: transparent;"
        )
        layout.addWidget(preview_title)

        # Summary card
        card = QWidget()
        card.setStyleSheet(
            f"background: {BG_RAISED}; border: 1px solid {BORDER}; "
            f"border-radius: 8px; padding: 4px;"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(14, 12, 14, 12)
        card_layout.setSpacing(8)

        # Name
        self._preview_name = QLabel("")
        self._preview_name.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {FG}; background: transparent;"
        )
        card_layout.addWidget(self._preview_name)

        # Policy badge
        self._preview_policy = QLabel("")
        self._preview_policy.setStyleSheet(
            f"font-size: 11px; font-weight: 600; background: transparent;"
        )
        card_layout.addWidget(self._preview_policy)

        # Instructions (truncated)
        instr_label = QLabel("Instructions")
        instr_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {FG_DIM}; "
            f"background: transparent; letter-spacing: 0.04em; "
            f"padding: 4px 0 0 0;"
        )
        card_layout.addWidget(instr_label)

        self._preview_instructions = QLabel("")
        self._preview_instructions.setWordWrap(True)
        self._preview_instructions.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent; "
            f"line-height: 1.4;"
        )
        card_layout.addWidget(self._preview_instructions)

        # Output contract
        contract_label = QLabel("Output Contract")
        contract_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {FG_DIM}; "
            f"background: transparent; letter-spacing: 0.04em; "
            f"padding: 4px 0 0 0;"
        )
        card_layout.addWidget(contract_label)

        self._preview_contract = QLabel("")
        self._preview_contract.setWordWrap(True)
        self._preview_contract.setStyleSheet(
            f"font-size: 12px; color: {FG_DIM}; background: transparent;"
        )
        card_layout.addWidget(self._preview_contract)

        layout.addWidget(card, 1)

        note = QLabel("Review the draft below. Use Back to refine, or Open in Editor to tweak further.")
        note.setWordWrap(True)
        note.setStyleSheet(
            f"font-size: 12px; color: {FG_MUTED}; background: transparent; "
            f"padding: 0 2px;"
        )
        layout.addWidget(note)

        return page

    # -- Navigation --

    def _go_back(self) -> None:
        current = self._stack.currentIndex()
        if current > 0:
            self._stack.setCurrentIndex(current - 1)
            self._update_nav(current - 1)

    def _go_next(self) -> None:
        current = self._stack.currentIndex()

        # Validate current page before advancing
        if current == 1:  # Job description
            if not self._job_edit.toPlainText().strip():
                return
        elif current == 3:  # Output contract
            if not self._contract_edit.toPlainText().strip():
                return

        if current < _PAGE_COUNT - 1:
            # If moving to preview page, build the draft
            if current + 1 == _PAGE_COUNT - 1:
                self._build_draft()

            self._stack.setCurrentIndex(current + 1)
            self._update_nav(current + 1)

    def _update_nav(self, index: int) -> None:
        self._back_btn.setVisible(index > 0)
        is_last = index == _PAGE_COUNT - 1
        self._next_btn.setVisible(not is_last)
        self._accept_btn.setVisible(is_last)
        self._cancel_btn.setVisible(not is_last)

    # -- Draft building --

    def _build_draft(self) -> None:
        job_text = self._job_edit.toPlainText().strip()

        # Derive name from job description
        name = self._derive_name(job_text)

        # Determine write policy
        if self._policy_readonly.isChecked():
            write_policy = "read_only"
        elif self._policy_ask.isChecked():
            write_policy = "ask_before_writes"
        else:
            write_policy = "normal_diff_approval"

        # Output contract
        output_contract = self._contract_edit.toPlainText().strip()

        # Build instructions
        instructions = self._build_instructions(job_text, write_policy, output_contract)

        self._draft = DesignedDroneDraft(
            name=name,
            instructions=instructions,
            output_contract=output_contract,
            write_policy=write_policy,
        )

        self._update_preview()

    @staticmethod
    def _derive_name(job_text: str) -> str:
        """Derive a short readable name from the job description."""
        # Use first sentence or first ~50 chars
        name = ""
        for sep in (". ", ".\n", "\n", "."):
            if sep in job_text:
                candidate = job_text.split(sep, 1)[0]
                if candidate.strip():
                    name = candidate.strip()
                    break
        if not name:
            name = job_text[:50].strip()

        # Clean up trailing punctuation
        name = name.rstrip(".,;:!?")
        # Title case for nice names
        name = name.title()
        return name[:60]

    @staticmethod
    def _build_instructions(job_text: str, write_policy: str, output_contract: str) -> str:
        lines: list[str] = []

        # Job section
        lines.append("## Job")
        lines.append("")
        lines.append(job_text)
        lines.append("")

        # Operating rules
        lines.append("## Operating Rules")
        lines.append("")
        lines.append("- Be focused on the specific job described above.")
        lines.append("- Use only the tools available to you.")
        lines.append("- Read relevant files to understand context before acting.")
        lines.append("- Report progress honestly and concisely.")
        lines.append("- If you encounter errors or blockers, report them clearly.")

        # Write policy guidance
        lines.append("")
        if write_policy == "read_only":
            lines.append("- Do not modify any files. This is a read-only investigation.")
        elif write_policy == "ask_before_writes":
            lines.append(
                "- You have write access but must ask before each write. "
                "Aura's approval flow will handle the decision."
            )
        else:
            lines.append(
                "- You have write access. Use Aura's approval and validation flow for all changes. "
                "After making changes, summarize what files were modified."
            )

        lines.append("")

        # Expected output
        lines.append("## Expected Output")
        lines.append("")
        lines.append(output_contract)

        return "\n".join(lines)

    def _update_preview(self) -> None:
        if not self._draft:
            return

        self._preview_name.setText(self._draft.name)

        policy_color = _POLICY_BADGE_COLORS.get(self._draft.write_policy, FG_DIM)
        policy_label_text = _WRITE_POLICY_LABELS.get(self._draft.write_policy, self._draft.write_policy)
        self._preview_policy.setText(policy_label_text)
        self._preview_policy.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {policy_color}; "
            f"background: transparent;"
        )

        # Show first ~300 chars of instructions
        instr = self._draft.instructions
        if len(instr) > 300:
            instr = instr[:297] + "..."
        self._preview_instructions.setText(instr)

        contract = self._draft.output_contract
        if len(contract) > 200:
            contract = contract[:197] + "..."
        self._preview_contract.setText(contract)

    def _on_accept(self) -> None:
        if self._draft is not None:
            self.accept()
