"""Summary card shown after a worker dispatch completes.

Provides a compact completion card with a receipt parser and
a pure function to extract structured data from the receipt text.
"""

from __future__ import annotations

import re
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from aura.gui.markdown_renderer import _render_markdown_with_code
from aura.gui.theme import BG_ALT, BG_RAISED, DANGER, FG, FG_DIM, FG_MUTED, SUCCESS, WARN

# ── Receipt parser (pure, no Qt imports) ──────────────────────────────────

# Known non-user-facing caveats to filter out
_FILTERED_CAVEATS: frozenset[str] = frozenset([
    "Broad/multi-file task did not use update_todo_list — consider a visible plan next time.",
])


def parse_worker_summary_receipt(summary: str) -> dict[str, Any]:
    """Parse a structured worker receipt into a compact dict.

    The receipt is built by ``_build_worker_summary`` in
    ``aura/bridge/dispatch.py``.

    Returns
    -------
    dict with keys:
        summary_text (str)  — first line after "Summary:" section, stripped.
        files_changed (str) — the full "Files changed   : ..." line, or "".
        validation (str)    — the full "Validation      : ..." line, or "".
        caveats (list[str]) — caveat items with known nags filtered out.
        file_counts (dict)  — {"total": N, "edited": N, "new": N, "deleted": N}.
        has_box_borders (bool) — True if ══ or ── borders detected.
    """
    result: dict[str, Any] = {
        "summary_text": "",
        "files_changed": "",
        "validation": "",
        "caveats": [],
        "file_counts": {"total": 0, "edited": 0, "new": 0, "deleted": 0},
        "has_box_borders": False,
    }

    if not summary:
        return result

    lines = summary.splitlines()

    # Detect box borders
    result["has_box_borders"] = any(
        line.startswith("═══") or line.startswith("───") for line in lines
    )

    # Extract glance lines
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("Files changed"):
            result["files_changed"] = stripped
            result["file_counts"] = _parse_file_counts(stripped)
        elif stripped.startswith("Validation") and "✓" in stripped:
            result["validation"] = stripped

    # Extract caveats
    in_caveats = False
    for line in lines:
        stripped = line.strip()
        if stripped == "Caveats:":
            in_caveats = True
            continue
        if in_caveats:
            if not stripped or stripped.startswith("═══") or stripped.startswith("───"):
                in_caveats = False
                continue
            if stripped.startswith("•") or stripped.startswith("-"):
                item = stripped.lstrip("•- ").strip()
                if item and item not in _FILTERED_CAVEATS:
                    result["caveats"].append(item)

    # Extract summary text
    in_summary = False
    summary_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped == "Summary:":
            in_summary = True
            continue
        if in_summary:
            if not stripped or stripped.startswith("═══") or stripped.startswith("───"):
                break
            if stripped.startswith("Remaining work:"):
                break
            summary_lines.append(stripped)

    if summary_lines:
        result["summary_text"] = summary_lines[0]

    return result


def _parse_file_counts(line: str) -> dict[str, int]:
    """Parse a 'Files changed   : N (M edited, K new)' line into counts."""
    counts: dict[str, int] = {"total": 0, "edited": 0, "new": 0, "deleted": 0}

    # Extract total
    total_match = re.search(r"Files changed\s*:\s*(\d+)", line)
    if total_match:
        counts["total"] = int(total_match.group(1))

    # Extract parts inside parentheses
    paren_match = re.search(r"\((.+?)\)", line)
    if paren_match:
        parts = paren_match.group(1).split(",")
        for part in parts:
            part = part.strip()
            m = re.match(r"(\d+)\s+(.+)", part)
            if m:
                num = int(m.group(1))
                label = m.group(2).strip()
                if label == "edited":
                    counts["edited"] = num
                elif label == "new":
                    counts["new"] = num
                elif label == "deleted":
                    counts["deleted"] = num

    return counts


# ── Card widget ────────────────────────────────────────────────────────────


class WorkerSummaryCard(QFrame):
    """A card displayed in the chat after a worker finishes execution.

    Shows a compact completion card with status header, goal, summary line,
    stats chips, and a footer directing to Worker Log for details.
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
        context_gearbox: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(parent)
        self.tool_call_id = tool_call_id

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(8)

        # Header
        self._header = QLabel(self)
        layout.addWidget(self._header)

        # Goal
        self._goal_label = QLabel(self)
        self._goal_label.setWordWrap(True)
        self._goal_label.setStyleSheet(f"color: {FG_DIM}; font-style: italic;")
        layout.addWidget(self._goal_label)

        # Summary line (compact one-liner)
        self._summary_line = QLabel(self)
        self._summary_line.setWordWrap(True)
        self._summary_line.setTextFormat(Qt.TextFormat.RichText)
        self._summary_line.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._summary_line)

        # Stats row (chips)
        self._stats_layout = QHBoxLayout()
        self._stats_layout.setSpacing(6)
        layout.addLayout(self._stats_layout)

        # Footer
        self._footer = QLabel(self)
        self._footer.setWordWrap(True)
        self._footer.setStyleSheet(f"color: {FG_MUTED}; font-size: 11px;")
        layout.addWidget(self._footer)

        # Body (fallback for non-receipt summaries)
        self._body = QLabel(self)
        self._body.setWordWrap(True)
        self._body.setTextFormat(Qt.TextFormat.RichText)
        self._body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._body)

        self.update_summary(
            goal,
            ok,
            summary,
            needs_followup=needs_followup,
            status=status,
            context_gearbox=context_gearbox,
        )

    def update_summary(
        self,
        goal: str,
        ok: bool,
        summary: str,
        *,
        needs_followup: bool = False,
        status: str | None = None,
        context_gearbox: dict[str, Any] | None = None,
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

        # Parse receipt and render compact view
        parsed = parse_worker_summary_receipt(summary)
        summary_text = parsed["summary_text"]
        file_counts = parsed["file_counts"]
        validation = parsed["validation"]
        caveats = parsed["caveats"]
        has_box_borders = parsed["has_box_borders"]

        if summary_text:
            self._summary_line.setText(_render_markdown_with_code(summary_text, color=FG))
            self._summary_line.setVisible(True)
            self._body.clear()
            self._body.setVisible(False)
        elif has_box_borders or self._looks_like_full_receipt(summary):
            fallback = self._fallback_text(summary)
            self._summary_line.setText(_render_markdown_with_code(fallback, color=FG))
            self._summary_line.setVisible(True)
            self._body.clear()
            self._body.setVisible(False)
        else:
            sanitized = self._sanitize_summary(summary)
            self._body.setText(_render_markdown_with_code(sanitized, color=FG))
            self._body.setVisible(bool(summary))
            self._summary_line.clear()
            self._summary_line.setVisible(False)

        # Rebuild stats chips
        self._rebuild_stats(file_counts, validation, context_gearbox)

        # Footer
        footer_parts: list[str] = []
        footer_parts.append("Details are in Worker Log.")
        if caveats:
            footer_parts.append("Review caveats in Worker Log.")
        self._footer.setText(" ".join(footer_parts))
        self._footer.setVisible(bool(summary))

    def _rebuild_stats(
        self,
        file_counts: dict[str, int],
        validation: str,
        context_gearbox: dict[str, Any] | None = None,
    ) -> None:
        """Rebuild the stats chip row from parsed file counts and validation."""
        # Clear existing chips
        while self._stats_layout.count():
            item = self._stats_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        total = file_counts.get("total", 0)
        edited = file_counts.get("edited", 0)
        new_count = file_counts.get("new", 0)
        deleted = file_counts.get("deleted", 0)

        if total > 0:
            self._stats_layout.addWidget(self._build_chip(f"{total} file{'s' if total != 1 else ''}"))
        if edited > 0:
            self._stats_layout.addWidget(self._build_chip(f"{edited} edited"))
        if new_count > 0:
            self._stats_layout.addWidget(self._build_chip(f"{new_count} new"))
        if deleted > 0:
            self._stats_layout.addWidget(self._build_chip(f"{deleted} deleted"))

        # Validation chip
        if validation:
            # Shorten validation string for chip display
            val_short = validation
            if "(" in validation:
                val_short = validation.split("(")[1].rstrip(")")
            self._stats_layout.addWidget(self._build_chip(val_short))

        context_chip = self._context_chip_text(context_gearbox)
        if context_chip:
            self._stats_layout.addWidget(self._build_chip(context_chip))

        self._stats_layout.addStretch()

    @staticmethod
    def _context_chip_text(context_gearbox: dict[str, Any] | None) -> str:
        if not isinstance(context_gearbox, dict):
            return ""
        summary = context_gearbox.get("summary")
        if not isinstance(summary, dict):
            return ""
        loaded = summary.get("loaded_count")
        skipped = summary.get("skipped_count")
        if not isinstance(loaded, int) or not isinstance(skipped, int):
            return ""
        return f"Context {loaded}/{skipped}"

    @staticmethod
    def _build_chip(text: str) -> QLabel:
        """Create a small chip label with raised background and dim text."""
        chip = QLabel(text)
        chip.setStyleSheet(
            f"background: {BG_RAISED}; color: {FG_DIM}; "
            f"border-radius: 4px; padding: 2px 8px; font-size: 11px;"
        )
        return chip

    @staticmethod
    def _looks_like_full_receipt(summary: str) -> bool:
        """Heuristic: check for receipt section headers."""
        markers = ["Files changed", "Modified files:", "Caveats:", "Summary:"]
        return any(marker in summary for marker in markers)

    @staticmethod
    def _fallback_text(summary: str) -> str:
        """Extract first meaningful line after Summary: section, capped at 250 chars."""
        lines = summary.splitlines()
        in_summary = False
        for line in lines:
            stripped = line.strip()
            if stripped == "Summary:":
                in_summary = True
                continue
            if in_summary:
                if stripped and not stripped.startswith("═══") and not stripped.startswith("───"):
                    if len(stripped) > 250:
                        return stripped[:247] + "..."
                    return stripped
        # Fallback: first non-status, non-glance, non-border line
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("═══") or stripped.startswith("───"):
                continue
            if "✅" in stripped or "❌" in stripped or "⚠️" in stripped or "🔶" in stripped:
                continue
            if stripped.startswith("Files changed") or stripped.startswith("Validation"):
                continue
            if len(stripped) > 250:
                return stripped[:247] + "..."
            return stripped
        return ""

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
                WorkerOutcomeStatus.completed.value: ("✅ Done", SUCCESS),
                WorkerOutcomeStatus.completed_with_caveats.value: ("✅ Done with caveats", WARN),
                WorkerOutcomeStatus.needs_followup.value: ("⚠️ Needs follow-up", WARN),
                WorkerOutcomeStatus.validation_failed.value: ("❌ Failed validation", DANGER),
                WorkerOutcomeStatus.edit_mechanics_blocked.value: ("⚠️ Edit mechanics blocked", WARN),
                WorkerOutcomeStatus.craft_blocked.value: ("❌ Craft blocked", DANGER),
                WorkerOutcomeStatus.craft_rejected.value: ("❌ Craft rejected", DANGER),
                WorkerOutcomeStatus.scope_mismatch.value: ("⚠️ Scope mismatch", WARN),
                WorkerOutcomeStatus.approval_rejected.value: ("❌ Approval rejected", DANGER),
                WorkerOutcomeStatus.cancelled.value: ("🔶 Cancelled", "#6b7280"),
                WorkerOutcomeStatus.harness_error.value: ("❌ Harness error", DANGER),
            }
            return mapping.get(status, ("Unknown", "#6b7280"))
        # Fallback to legacy inference
        if "Craft blocked" in summary:
            return "❌ Craft blocked", DANGER
        if "Waiting for approval" in summary:
            return "⚠️ Waiting for approval", WARN
        if "Repairing patch" in summary:
            return "⚠️ Repairing patch", WARN
        if ok:
            return "✅ Done", SUCCESS
        if summary.startswith("Harness error"):
            return "❌ Harness error", DANGER
        if summary.startswith("Validation failed"):
            return "❌ Failed validation", WARN
        if summary.startswith("Worker needs follow-up"):
            return "⚠️ Worker needs follow-up", WARN
        if needs_followup:
            return "⚠️ Worker needs follow-up", WARN
        return "⚠️ Worker needs follow-up", WARN

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

        if len(result) > 300:
            result = result[:297] + "..."

        if "Details are available in Worker Log." not in result:
            result += "\n\nDetails are available in Worker Log."

        return result
