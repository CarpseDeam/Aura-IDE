"""Structured Planner replan memory from a failed WorkerDispatchResult.

Exports frozen dataclasses and three public functions for building and
rendering attempt briefs — compact structured memory the Planner receives
after a failed Worker dispatch so it can replan the next attempt.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.conversation.dispatch import WorkerDispatchResult

__all__ = [
    "AttemptBrief",
    "FailedClauseBrief",
    "build_attempt_brief",
    "render_for_planner",
    "render_prior_attempt_lines",
]


@dataclass(frozen=True)
class FailedClauseBrief:
    """A single critic finding that contributed to a failed dispatch."""

    clause: str
    file: str
    message: str
    suggested_action: str


@dataclass(frozen=True)
class AttemptBrief:
    """Structured memory of one failed Worker dispatch attempt."""

    failed_clauses: tuple[FailedClauseBrief, ...]
    mismatch_observed: str
    planner_question: str
    modified_files: tuple[str, ...]
    validation_tail: tuple[str, ...]
    attempt_number: int


def _normalize_finding(raw: Any) -> FailedClauseBrief | None:
    """Convert a raw dict-like finding into a FailedClauseBrief, or None."""
    if not isinstance(raw, dict):
        return None
    clause = str(raw.get("clause", "") or "")
    file = str(raw.get("file", "") or "")
    message = str(raw.get("message", "") or "")
    suggested_action = str(raw.get("suggested_action", "") or "")
    if not clause and not message:
        return None
    return FailedClauseBrief(
        clause=clause,
        file=file,
        message=message,
        suggested_action=suggested_action,
    )


def _extract_failed_clauses(result: WorkerDispatchResult) -> tuple[FailedClauseBrief, ...]:
    """Extract FailedClauseBrief items from result extras."""
    raw_findings = result.extras.get("critic_findings")
    if not isinstance(raw_findings, list):
        return ()
    findings: list[FailedClauseBrief] = []
    for item in raw_findings:
        brief = _normalize_finding(item)
        if brief is not None:
            findings.append(brief)
    return tuple(findings)


def _extract_mismatch_observed(result: WorkerDispatchResult) -> str:
    """Extract mismatch observed text, or empty string."""
    if result.mismatch is not None:
        return result.mismatch.observed or ""
    return ""


def _extract_planner_question(result: WorkerDispatchResult) -> str:
    """Extract question for planner, or empty string."""
    if result.mismatch is not None:
        return result.mismatch.question_for_planner or ""
    return ""


def _extract_validation_tail(result: WorkerDispatchResult) -> tuple[str, ...]:
    """Extract trailing 30 lines of validation output, if any."""
    if not result.validation:
        return ()
    lines = result.validation.splitlines()
    tail = lines[-30:]
    return tuple(tail)


def _extract_attempt_number(result: WorkerDispatchResult) -> int:
    """Extract attempt_number from extras, defaulting to 1."""
    raw = result.extras.get("attempt_number")
    if isinstance(raw, int):
        return raw
    return 1


def _has_meaningful_data(
    failed_clauses: tuple[FailedClauseBrief, ...],
    mismatch_observed: str,
    modified_files: tuple[str, ...],
    validation_tail: tuple[str, ...],
) -> bool:
    """Return True if any of the provided fields carry extractable data."""
    if failed_clauses:
        return True
    if mismatch_observed:
        return True
    if modified_files:
        return True
    if validation_tail:
        return True
    return False


def build_attempt_brief(result: WorkerDispatchResult) -> AttemptBrief | None:
    """Build an AttemptBrief from a failed WorkerDispatchResult.

    Returns None when nothing meaningful is extractable (no failed clauses,
    no mismatch, no modified files, no validation tail).
    """
    failed_clauses = _extract_failed_clauses(result)
    mismatch_observed = _extract_mismatch_observed(result)
    planner_question = _extract_planner_question(result)
    modified_files = tuple(result.modified_files)
    validation_tail = _extract_validation_tail(result)
    attempt_number = _extract_attempt_number(result)

    if not _has_meaningful_data(failed_clauses, mismatch_observed, modified_files, validation_tail):
        return None

    return AttemptBrief(
        failed_clauses=failed_clauses,
        mismatch_observed=mismatch_observed,
        planner_question=planner_question,
        modified_files=modified_files,
        validation_tail=validation_tail,
        attempt_number=attempt_number,
    )


def _render_failed_clauses(brief: AttemptBrief) -> str:
    """Render failed clauses grouped by file."""
    if not brief.failed_clauses:
        return ""

    # Group by file
    by_file: dict[str, list[FailedClauseBrief]] = {}
    for fc in brief.failed_clauses:
        by_file.setdefault(fc.file, []).append(fc)

    parts: list[str] = []
    for filepath, clauses in by_file.items():
        parts.append(f"  File: {filepath}")
        for fc in clauses:
            parts.append(f"    - clause: {fc.clause}")
            parts.append(f"      message: {fc.message}")
            if fc.suggested_action:
                parts.append(f"      suggested_action: {fc.suggested_action}")
    return "\n".join(parts)


def render_for_planner(brief: AttemptBrief) -> str:
    """Produce a compact structured internal block for Planner replan history.

    The block begins with "CONSTRAINT FOR NEXT ATTEMPT:" and lists only
    populated sections.
    """
    lines: list[str] = ["CONSTRAINT FOR NEXT ATTEMPT:"]

    if brief.failed_clauses:
        lines.append("  Critic findings:")
        rendered = _render_failed_clauses(brief)
        for line in rendered.splitlines():
            lines.append(f"  {line}")

    if brief.mismatch_observed:
        lines.append(f"  Mismatch observed: {brief.mismatch_observed}")

    if brief.planner_question:
        lines.append(f"  Planner question: {brief.planner_question}")

    if brief.modified_files:
        lines.append("  Modified files:")
        for fp in brief.modified_files:
            lines.append(f"    - {fp}")

    if brief.validation_tail:
        lines.append("  Validation tail:")
        for vline in brief.validation_tail:
            lines.append(f"    {vline}")

    return "\n".join(lines)


def render_prior_attempt_lines(brief: AttemptBrief) -> list[str]:
    """Return concise lines for a future Worker 'Prior Attempt' section.

    No current caller should use this outside this module in this task.
    """
    result: list[str] = [
        f"[Prior attempt #{brief.attempt_number}]",
    ]
    if brief.failed_clauses:
        result.append(f"  Critic findings: {len(brief.failed_clauses)} item(s)")
    if brief.mismatch_observed:
        result.append(f"  Mismatch: {brief.mismatch_observed[:200]}")
    if brief.modified_files:
        result.append(f"  Modified: {', '.join(brief.modified_files)}")
    if brief.validation_tail:
        result.append(f"  Validation: {len(brief.validation_tail)} line(s)")
    return result
