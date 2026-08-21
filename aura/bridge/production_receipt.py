"""Completion receipt for one direct production execution turn.

The receipt is built from structured execution evidence collected by
``ExecutionEventRelay`` (the authoritative execution ledger) — applied and
rejected writes, commands run, validation classification, external process
exit status, cancellation, and the final model response.

It never infers success from "the stream ended" and never treats a syntax
probe as proof.  A validation command that failed and was then rerun
successfully is reported as a repair, with both events represented.

The receipt is an *execution* artifact.  The assistant's final answer belongs
to the chat transcript, so the visible receipt never restates it; the text is
retained under ``metadata["final_response"]`` only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.conversation.validation_truth import (
    validation_payload_failed,
    validation_payload_passed,
)

# Terminal outcome statuses for a production turn.
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_UNVERIFIED = "completed_unverified"
STATUS_VALIDATION_FAILED = "validation_failed"
STATUS_CANCELLED = "cancelled"
STATUS_BLOCKED = "blocked"
STATUS_HARNESS_ERROR = "harness_error"

_BORDER = "═" * 38
_DIVIDER = "─" * 38

_HEADER_LABELS = {
    STATUS_COMPLETED: "Production Report",
    STATUS_COMPLETED_UNVERIFIED: "Production Report (unverified)",
    STATUS_VALIDATION_FAILED: "Validation failed",
    STATUS_CANCELLED: "Cancelled",
    STATUS_BLOCKED: "Blocked",
    STATUS_HARNESS_ERROR: "Production Error",
}


@dataclass
class ProductionRunEvidence:
    """Structured facts observed during one production turn."""

    run_id: str = ""
    model: str = ""
    write_results: list[dict[str, Any]] = field(default_factory=list)
    not_applied_writes: list[dict[str, Any]] = field(default_factory=list)
    terminal_results: list[dict[str, Any]] = field(default_factory=list)
    validation_results: list[dict[str, Any]] = field(default_factory=list)
    process_results: list[dict[str, Any]] = field(default_factory=list)
    api_errors: list[str] = field(default_factory=list)
    failed_tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_response: str = ""
    cancelled: bool = False
    blocked_reason: str = ""
    #: Whether the model called ``report_already_satisfied`` and it succeeded.
    #: Observed evidence only — never inferred from "no write happened" and
    #: never from assistant prose.
    already_satisfied: bool = False


@dataclass(frozen=True)
class ValidationOutcome:
    """Per-command validation history for the run."""

    command: str
    attempts: int
    passed: bool
    repaired: bool  # failed at least once, then passed
    last_exit_code: Any = None
    first_failure_output: str = ""


@dataclass(frozen=True)
class ProductionReceipt:
    ok: bool
    status: str
    text: str
    needs_followup: bool
    metadata: dict[str, Any]


def _normalize_command(value: Any) -> str:
    return " ".join(str(value or "").split())


def _write_paths(writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate write records by path, keeping the final observed record."""
    by_path: dict[str, dict[str, Any]] = {}
    for record in writes:
        path = str(record.get("path") or record.get("rel_path") or "").strip()
        if not path:
            continue
        by_path[path] = record
    return [{"path": path, **record} for path, record in by_path.items()]


def summarize_validation(
    validation_results: list[dict[str, Any]],
) -> list[ValidationOutcome]:
    """Collapse the validation ledger into one outcome per command.

    Preserves the failure→repair→rerun story: a command that failed and later
    passed is reported with ``repaired=True`` and keeps its first failure
    output, so both events stay visible in the same turn.
    """
    ordered: list[str] = []
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in validation_results:
        command = _normalize_command(record.get("command"))
        if command not in grouped:
            grouped[command] = []
            ordered.append(command)
        grouped[command].append(record)

    outcomes: list[ValidationOutcome] = []
    for command in ordered:
        records = grouped[command]
        any_failed = any(_record_failed(record) for record in records)
        final_passed = _record_passed(records[-1])
        first_failure_output = ""
        for record in records:
            if _record_failed(record):
                raw = str(record.get("output") or record.get("output_preview") or "")
                first_failure_output = raw.strip().split("\n")[0][:200] if raw else ""
                break
        outcomes.append(
            ValidationOutcome(
                command=command,
                attempts=len(records),
                passed=final_passed,
                repaired=bool(any_failed and final_passed),
                last_exit_code=records[-1].get("exit_code"),
                first_failure_output=first_failure_output,
            )
        )
    return outcomes


def _record_passed(record: dict[str, Any]) -> bool:
    if "validation_ok" in record:
        return bool(record["validation_ok"])
    return validation_payload_passed(record)


def _record_failed(record: dict[str, Any]) -> bool:
    if "validation_ok" in record:
        return not bool(record["validation_ok"])
    return validation_payload_failed(record)


def _resolve_status(
    evidence: ProductionRunEvidence,
    outcomes: list[ValidationOutcome],
) -> str:
    """Project the turn's final status from its structured execution evidence.

    A passive summary of what was observed. Precedence is fixed: cancellation,
    harness error, a reported blocker, and failing validation all outrank
    success; changes made without any validation are reported as unverified
    rather than proven. Nothing here judges whether the turn was *allowed* to
    end the way it did.
    """
    if evidence.cancelled:
        return STATUS_CANCELLED
    if evidence.api_errors:
        return STATUS_HARNESS_ERROR
    if evidence.blocked_reason:
        return STATUS_BLOCKED
    if any(not outcome.passed for outcome in outcomes):
        return STATUS_VALIDATION_FAILED
    if outcomes:
        return STATUS_COMPLETED
    # No validation evidence. A turn that changed files without running any
    # meaningful validation is explicitly NOT reported as proven.
    if evidence.write_results:
        return STATUS_COMPLETED_UNVERIFIED
    # ``report_already_satisfied`` evidence is observed, never inferred from
    # the absence of a write and never from assistant prose.
    if evidence.already_satisfied:
        return STATUS_COMPLETED
    return STATUS_COMPLETED


def build_production_receipt(
    evidence: ProductionRunEvidence,
) -> ProductionReceipt:
    """Build exactly one factual completion receipt from run evidence."""
    outcomes = summarize_validation(evidence.validation_results)
    status = _resolve_status(evidence, outcomes)
    ok = status in (STATUS_COMPLETED, STATUS_COMPLETED_UNVERIFIED)
    needs_followup = status in (
        STATUS_VALIDATION_FAILED,
        STATUS_BLOCKED,
        STATUS_HARNESS_ERROR,
    )

    writes = _write_paths(evidence.write_results)
    deleted = [w for w in writes if w.get("deleted")]
    created = [w for w in writes if w.get("is_new_file") and not w.get("deleted")]
    edited = [
        w for w in writes
        if not w.get("is_new_file") and not w.get("deleted")
    ]

    lines: list[str] = [_BORDER, f" {_HEADER_LABELS.get(status, status)}", _DIVIDER]

    if writes:
        parts: list[str] = []
        if edited:
            parts.append(f"{len(edited)} edited")
        if created:
            parts.append(f"{len(created)} new")
        if deleted:
            parts.append(f"{len(deleted)} deleted")
        lines.append(f" Files changed   : {len(writes)} ({', '.join(parts)})")
    else:
        lines.append(" Files changed   : 0")

    lines.append(f" Commands run    : {len(evidence.terminal_results)}")
    if outcomes:
        passed = sum(1 for outcome in outcomes if outcome.passed)
        repaired = sum(1 for outcome in outcomes if outcome.repaired)
        glance = f"{passed}/{len(outcomes)} passed"
        if repaired:
            glance += f" ({repaired} after repair)"
        mark = "✓" if passed == len(outcomes) else "✗"
        lines.append(f" Validation      : {mark} {glance}")
    else:
        lines.append(" Validation      : — not verified")
    lines.append(_DIVIDER)

    if writes:
        lines.append("")
        lines.append(" Files:")
        for record in writes:
            if record.get("deleted"):
                tag = "(deleted)"
            elif record.get("is_new_file"):
                tag = "(new)"
            else:
                tag = "(edit)"
            lines.append(f"  • {record['path']}   {tag}")

    if evidence.not_applied_writes:
        lines.append("")
        lines.append(" Writes not applied:")
        for record in _write_paths(evidence.not_applied_writes)[:5]:
            reason = str(
                record.get("failure_class")
                or record.get("write_outcome")
                or record.get("reason")
                or "not applied"
            )
            lines.append(f"  • {record['path']}   ({reason})")

    if outcomes:
        lines.append("")
        lines.append(" Validation:")
        for outcome in outcomes:
            if outcome.passed and outcome.repaired:
                lines.append(
                    f"  • {outcome.command}  →  failed, repaired, "
                    f"passed on rerun ({outcome.attempts} runs)"
                )
                if outcome.first_failure_output:
                    lines.append(f"    first failure: {outcome.first_failure_output}")
            elif outcome.passed:
                lines.append(f"  • {outcome.command}  →  passed")
            else:
                exit_str = (
                    f" (exit {outcome.last_exit_code})"
                    if outcome.last_exit_code is not None
                    else ""
                )
                lines.append(f"  • {outcome.command}  →  failed{exit_str}")
                if outcome.first_failure_output:
                    lines.append(f"    {outcome.first_failure_output}")

    finished_processes = [
        record for record in evidence.process_results
        if record.get("exit_code") is not None
    ]
    if finished_processes:
        lines.append("")
        lines.append(" Processes:")
        for record in finished_processes:
            label = str(record.get("label") or record.get("command") or record.get("process_id"))
            lines.append(f"  • {label}  →  exit {record.get('exit_code')}")

    if evidence.api_errors:
        lines.append("")
        lines.append(" Errors:")
        for message in evidence.api_errors[:5]:
            lines.append(f"  • {message}")

    if evidence.blocked_reason:
        lines.append("")
        lines.append(" Blocked:")
        lines.append(f"  • {evidence.blocked_reason}")

    if evidence.cancelled:
        lines.append("")
        lines.append(" Stopped by user before completion.")

    # The final assistant answer is chat-owned and has already been streamed
    # into the chat transcript.  The receipt reports execution facts only and
    # deliberately does not restate the prose; it stays in metadata for
    # diagnostics and downstream consumers.
    final_response = (evidence.final_response or "").strip()

    if status == STATUS_COMPLETED_UNVERIFIED:
        lines.append("")
        lines.append(" No validation command was run — changes are unverified.")

    if evidence.already_satisfied:
        lines.append("")
        lines.append(" Already satisfied — the requested state was already")
        lines.append(" present; no change was required.")

    lines.append(_BORDER)

    metadata: dict[str, Any] = {
        "run_id": evidence.run_id,
        "model": evidence.model,
        "status": status,
        "modified_files": [record["path"] for record in writes],
        "files_created": [record["path"] for record in created],
        "files_edited": [record["path"] for record in edited],
        "files_deleted": [record["path"] for record in deleted],
        "not_applied_writes": [
            record["path"] for record in _write_paths(evidence.not_applied_writes)
        ],
        "commands_run": [
            _normalize_command(record.get("command"))
            for record in evidence.terminal_results
        ],
        "validation": [
            {
                "command": outcome.command,
                "attempts": outcome.attempts,
                "passed": outcome.passed,
                "repaired": outcome.repaired,
                "exit_code": outcome.last_exit_code,
            }
            for outcome in outcomes
        ],
        "validation_repaired": any(outcome.repaired for outcome in outcomes),
        "processes": list(evidence.process_results),
        "api_errors": list(evidence.api_errors),
        "cancelled": bool(evidence.cancelled),
        "blocked_reason": evidence.blocked_reason,
        "already_satisfied": bool(evidence.already_satisfied),
        "final_response": final_response,
        "extras": {
            "production_run": True,
            "direct_production_execution": True,
        },
    }

    return ProductionReceipt(
        ok=ok,
        status=status,
        text="\n".join(lines),
        needs_followup=needs_followup,
        metadata=metadata,
    )


__all__ = [
    "ProductionReceipt",
    "ProductionRunEvidence",
    "ValidationOutcome",
    "build_production_receipt",
    "summarize_validation",
    "STATUS_BLOCKED",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_COMPLETED_UNVERIFIED",
    "STATUS_HARNESS_ERROR",
    "STATUS_VALIDATION_FAILED",
]
