"""Summary card shown after a worker dispatch completes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BG_ALT, DANGER, FG, FG_DIM, SUCCESS, WARN


class WorkerSummaryCard(QFrame):
    """A card displayed in the chat after a worker finishes execution.

    Shows a status header (success/failure icon), the original goal,
    and a rendered summary of what the worker accomplished.
    """

    def __init__(
        self,
        tool_call_id: str,
        goal: str,
        ok: bool,
        summary: str,
        needs_followup: bool = False,
        parent=None,
        status: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.tool_call_id = tool_call_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        self._header = QLabel(self)
        layout.addWidget(self._header)

        self._goal_label = QLabel(self)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(f"color: {FG_DIM}; font-style: italic;")
        layout.addWidget(self._goal_label)

        self._body = QLabel(self)
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._body)

        self.update_summary(goal, ok, summary, needs_followup=needs_followup, status=status)

    def update_summary(
        self,
        goal: str,
        ok: bool,
        summary: str,
        *,
        needs_followup: bool = False,
        status: str | None = None,
    ) -> None:
        """Update this card in place for repeated results with the same ID."""
        self._status = status
        header_text, header_color = self._status_label(ok, needs_followup, summary, status)
        self._header.setText(header_text)
        self._header.setStyleSheet(f"color: {header_color}; font-weight: 700; font-size: 12px;")

        self.setObjectName("workerSummaryCard")
        self.setStyleSheet(
            f"QFrame#workerSummaryCard {{ background: {BG_ALT}; "
            f"border: 1px solid rgba(255, 255, 255, 0.08); "
            f"border-left: 3px solid {header_color}; "
            f"border-radius: 8px; }}"
        )

        self._goal_label.setText(goal)
        self._goal_label.setVisible(bool(goal))

        sanitized = self._sanitize_summary(summary)
        self._body.setText(_render_markdown_with_code(sanitized, color=FG))
        self._body.setVisible(bool(summary))

    @staticmethod
    def _status_label(
        ok: bool,
        needs_followup: bool = False,
        summary: str = "",
        status: str | None = None,
    ) -> tuple[str, str]:
        if status is not None:
            from aura.conversation.dispatch import WorkerOutcomeStatus

            mapping = {
                WorkerOutcomeStatus.completed.value: ("Completed", SUCCESS),
                WorkerOutcomeStatus.completed_with_caveats.value: ("Completed with caveats", WARN),
                WorkerOutcomeStatus.needs_followup.value: ("Needs follow-up", WARN),
                WorkerOutcomeStatus.validation_failed.value: ("Validation failed", DANGER),
                WorkerOutcomeStatus.edit_mechanics_blocked.value: ("Edit mechanics blocked", WARN),
                WorkerOutcomeStatus.craft_blocked.value: ("Craft blocked", DANGER),
                WorkerOutcomeStatus.craft_rejected.value: ("Craft rejected", DANGER),
                WorkerOutcomeStatus.scope_mismatch.value: ("Scope mismatch", WARN),
                WorkerOutcomeStatus.approval_rejected.value: ("Approval rejected", DANGER),
                WorkerOutcomeStatus.cancelled.value: ("Cancelled", "#6b7280"),
                WorkerOutcomeStatus.harness_error.value: ("Harness error", DANGER),
            }
            return mapping.get(status, ("Unknown", "#6b7280"))
        # Fallback to legacy inference
        if "Craft blocked" in summary:
            return "Craft blocked", DANGER
        if "Waiting for approval" in summary:
            return "Waiting for approval", WARN
        if "Repairing patch" in summary:
            return "Repairing patch", WARN
        if ok:
            return "Completed", SUCCESS
        if summary.startswith("Harness error"):
            return "Harness error", DANGER
        if summary.startswith("Validation failed"):
            return "Validation failed", WARN
        if summary.startswith("Worker needs follow-up"):
            return "Worker needs follow-up", WARN
        if needs_followup:
            return "Worker needs follow-up", WARN
        return "Worker needs follow-up", WARN

    @staticmethod
    def _sanitize_summary(summary: str) -> str:
        """Strip raw internal detail from worker summary text before rendering."""
        if summary.strip() == "Worker made no changes.":
            return summary

        SECTION_HEADERS = {
            "Modified files:", "Validation:", "Validation failures:",
            "Harness errors:", "Errors:", "Caveats:", "Failed writes:",
            "Summary:", "Remaining work:",
        }

        lines = summary.splitlines()
        out: list[str] = []
        skip_section = False

        for line in lines:
            stripped = line.strip()

            # Detect section headers to skip
            if any(stripped.startswith(h) for h in SECTION_HEADERS):
                skip_section = True
                continue

            if skip_section:
                # End section on blank line or border
                if not stripped:
                    skip_section = False
                    continue
                if stripped.startswith("═══") or stripped.startswith("───"):
                    skip_section = False
                    out.append(line)
                continue

            # Garbage patterns
            if "CONTRACT_MISSING_SYMBOL" in line:
                continue
            if "Write tool" in line and "failed" in line.lower():
                continue
            if "Exceeded max tool rounds" in line:
                continue
            out.append(line)

        result = "\n".join(out).strip()
        if not result:
            return summary

        if len(result) > 600:
            result = result[:597] + "..."

        if "Details are available in Worker Log." not in result:
            result += "\n\nDetails are available in Worker Log."

        return result
