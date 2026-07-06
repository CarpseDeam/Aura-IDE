"""Worker prompt and report formatting helpers."""

from __future__ import annotations

from typing import Any

from aura.conversation import History, WorkerDispatchRequest, WorkerTaskSpec, normalize_worker_task
from aura.conversation.task_shape import task_shape_contract_lines
from aura.conversation.validation_orchestrator import validation_issue_message
from aura.conversation.worker_completion._summary_formatters import (
    _final_report_claims_failure,
    _final_report_claims_validation,
    _format_recoverable_write_failure,
    _format_structured_worker_failure,
    _format_worker_write_failure,
    _parse_structured_worker_failure,
)

__all__ = [
    "_format_spec_as_user_message",
    "_build_worker_summary",
    "_dedupe_summary_writes",
    "_final_report_claims_failure",
    "_final_report_claims_validation",
    "_parse_structured_worker_failure",
    "_format_structured_worker_failure",
    "_format_worker_write_failure",
    "_format_recoverable_write_failure",
]


def _format_spec_as_user_message(
    task: WorkerTaskSpec | WorkerDispatchRequest,
    *,
    artifact_item_index: int | None = None,
    artifact_item_total: int | None = None,
) -> str:
    """Format a structured task spec (or raw dispatch request) as a user message
    for the worker. Accepts both types for backward compatibility.

    When *artifact_item_index* is set, the message uses WorkArtifact item
    wording. Otherwise it uses flat/standalone Worker job wording.
    """
    if isinstance(task, WorkerDispatchRequest):
        task = normalize_worker_task(task)

    def _lines(items: list[str], default: str = "(none listed)") -> str:
        if not items:
            return default
        return "\n".join(f"- {item}" for item in items)

    def _target_region_lines(regions: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for region in regions:
            if not isinstance(region, dict):
                continue
            path = str(region.get("path") or "").strip()
            symbol = str(region.get("symbol") or "").strip()
            note = str(region.get("note") or "").strip()
            start_line = _positive_line_number(region.get("start_line"))
            end_line = _positive_line_number(region.get("end_line"))
            line_text = _target_line_text(start_line, end_line)

            detail_parts = [part for part in (symbol, line_text) if part]
            detail = " ".join(detail_parts)
            if path and detail:
                line = f"{path} :: {detail}" if symbol else f"{path} {detail}"
            else:
                line = path or detail
            if note:
                line = f"{line} \u2014 {note}" if line else note
            if line:
                lines.append(line)
        return lines

    parts: list[str] = []

    # ---- Project Profile (injected by dispatch) -----------------------------
    if task.project_profile is not None:
        summary = task.project_profile.summarize()
        parts.append("\u2500\u2500 Project Profile " + "\u2500" * 42)
        for line in summary.split("\n"):
            parts.append(line)
        parts.append("\u2500" * 60)
        parts.append("")

    if task.task_shape is not None:
        parts.extend([*task_shape_contract_lines(task.task_shape), ""])

    if artifact_item_index is not None:
        # Artifact item — bounded internal execution unit
        parts.extend([
            "Approved WorkArtifact Item",
            (
                "This is one bounded item inside an already approved "
                "WorkArtifact job. Complete only this bounded item. "
                "Do not execute other artifact items. "
                "Aura will continue the approved job after this item succeeds."
            ),
            "",
        ])
    else:
        # Flat / single-item dispatch — standalone Worker job
        parts.extend([
            "Approved Worker Job",
            "Complete the approved job described below.",
            "",
        ])

    parts.extend([
        "Goal",
        task.goal,
        "",
        "Files",
        _lines(task.files),
        "",
    ])

    target_regions = _target_region_lines(task.target_regions)
    if target_regions:
        parts.extend([
            "Target Regions",
            _lines(target_regions),
            "",
        ])

    parts.extend([
        "Builder Note",
        task.builder_note,
        "",
    ])

    if task.allowed_responsibilities:
        parts.extend([
            "Allowed Responsibilities",
            _lines(task.allowed_responsibilities),
            "",
        ])

    if task.forbidden_responsibilities:
        parts.extend([
            "Forbidden Responsibilities",
            _lines(task.forbidden_responsibilities),
            "",
        ])

    if task.required_outputs:
        parts.extend([
            "Required Outputs",
            _lines(task.required_outputs),
            "",
        ])

    if task.non_goals:
        parts.extend([
            "Non-Goals",
            _lines(task.non_goals),
            "",
        ])

    parts.extend([
        "Acceptance / Validation",
        task.acceptance,
    ])

    if task.validation_commands:
        parts.extend([
            "",
            "Validation Commands",
            "```",
            "\n".join(task.validation_commands),
            "```",
        ])

    parts.extend([
        "",
        "Begin.",
    ])

    return "\n".join(parts)


def _positive_line_number(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        line = value
    elif isinstance(value, str) and value.strip().isdigit():
        line = int(value.strip())
    else:
        return None
    return line if line > 0 else None


def _target_line_text(start_line: int | None, end_line: int | None) -> str:
    if start_line is not None and end_line is not None:
        if start_line == end_line:
            return f"line {start_line}"
        return f"lines {start_line}-{end_line}"
    if start_line is not None:
        return f"line {start_line}"
    if end_line is not None:
        return f"through line {end_line}"
    return ""


def _render_required_behavioral_summary(
    lines: list[str],
    behavioral: dict[str, Any],
) -> None:
    """Append required behavioral validation status lines to *lines*."""
    passed = behavioral.get("passed", [])
    skipped = behavioral.get("skipped", [])
    could_not_run = behavioral.get("could_not_run", [])

    if not passed and not skipped and not could_not_run:
        return

    lines.append("")
    lines.append(" Required behavioral checks:")
    for entry in passed:
        cmd = entry.get("command", "")
        lines.append(f"  • {cmd}  →  passed")
    for entry in could_not_run:
        cmd = entry.get("command", "")
        reason = entry.get("reason", "")
        suffix = f" ({reason})" if reason else ""
        lines.append(f"  • {cmd}  →  could not run{suffix}")
    for cmd in skipped:
        lines.append(f"  • {cmd}  →  required but skipped")


def _build_worker_summary(
    req: WorkerDispatchRequest,
    history: History,
    writes: list[dict[str, Any]],
    errors: list[str],
    continuation: dict[str, Any] | None = None,
    caveats: list[str] | None = None,
    validation_results: list[dict[str, Any]] | None = None,
    validation_command_issues: list[dict[str, Any]] | None = None,
    not_applied_writes: list[dict[str, Any]] | None = None,
    status: str | None = None,
    internal_error: str | None = None,
    behavioral_validation: dict[str, Any] | None = None,
) -> str:
    continuation = continuation or {}
    caveats = caveats or []
    validation_results = validation_results or []
    validation_command_issues = validation_command_issues or []
    not_applied_writes = not_applied_writes or []
    behavioral_validation = behavioral_validation or {}

    # Derive status if not provided (backward compat for callers without status)
    if not status:
        if errors:
            if _is_internal_error_summary(errors[0]):
                status = "harness_error"
            elif errors[0].startswith("Validation command failed"):
                status = "validation_failed"
            else:
                status = "harness_error"
        elif caveats:
            status = "completed_with_caveats"
        else:
            status = "completed"

    HEADER_LABELS = {
        "completed": "Worker Report",
        "completed_with_caveats": "Worker Report",
        "validation_failed": "Worker Report",
        "edit_mechanics_blocked": "Worker Report",
        "scope_mismatch": "Worker Report",
        "approval_rejected": "Changes rejected",
        "cancelled": "Cancelled",
        "harness_error": "Worker Error",
    }

    header_label = HEADER_LABELS.get(status, "\u2753  Unknown outcome")

    BORDER = "\u2550" * 38
    DIVIDER = "\u2500" * 38

    lines: list[str] = []
    displayed_writes = _dedupe_summary_writes(writes)

    # === Files changed count ===
    deleted_count = sum(1 for w in displayed_writes if w.get("deleted"))
    edited_count = sum(1 for w in displayed_writes if not w.get("is_new_file") and not w.get("deleted"))
    new_count = sum(1 for w in displayed_writes if w.get("is_new_file"))
    total_count = len(displayed_writes)
    if total_count > 0:
        parts = []
        if edited_count:
            parts.append(f"{edited_count} edited")
        if new_count:
            parts.append(f"{new_count} new")
        if deleted_count:
            parts.append(f"{deleted_count} deleted")
        files_changed_str = f"{total_count} ({', '.join(parts)})"
    else:
        files_changed_str = "0"

    # === Validation glance ===
    py_compile_results = [v for v in validation_results if "py_compile" in str(v.get("command", ""))]
    if not validation_results:
        validation_str = "\u2014 (not yet verified)"
    elif py_compile_results:
        pc_passed = sum(1 for v in py_compile_results if v.get("ok"))
        pc_total = len(py_compile_results)
        pc_ok = pc_passed == pc_total
        pc_prefix = "\u2713" if pc_ok else "\u2717"
        validation_str = f"{pc_prefix} py_compile ({pc_passed}/{pc_total} passed)"
    else:
        passed = sum(1 for v in validation_results if v.get("ok"))
        total = len(validation_results)
        ok = passed == total
        prefix = "\u2713" if ok else "\u2717"
        validation_str = f"{prefix} {passed}/{total} passed"

    # === Top section ===
    lines.append(BORDER)
    lines.append(f" {header_label}")
    lines.append(DIVIDER)

    # Glance line
    lines.append(f" Files changed   : {files_changed_str}")
    lines.append(f" Validation      : {validation_str}")
    lines.append(DIVIDER)

    # === Modified files ===
    if displayed_writes:
        lines.append("")
        lines.append(" Modified files:")
        for w in displayed_writes:
            tag = "(deleted)" if w.get("deleted") else ("(new)" if w.get("is_new_file") else "(edit)")
            path = str(w.get("path") or "").strip()
            lines.append(f"  \u2022 {path}   {tag}")
    else:
        lines.append("")
        lines.append(" Worker made no changes.")

    # === Validation detail ===
    if validation_results:
        passed_v = [v for v in validation_results if v.get("ok")]
        failed_v = [
            v for v in validation_results
            if not v.get("ok") and v.get("counts_as_product_failure") is not False
        ]

        if passed_v:
            lines.append("")
            lines.append(" Validated:")
            for v in passed_v:
                cmd = str(v.get("command") or "")
                lines.append(f"  \u2022 {cmd}  \u2192  passed")

        if failed_v:
            lines.append("")
            lines.append(" Product failures:")
            for v in failed_v:
                cmd = str(v.get("command") or "")
                exit_code = v.get("exit_code")
                exit_str = f" (exit {exit_code})" if exit_code is not None else ""
                lines.append(f"  \u2022 {cmd}  \u2192  failed{exit_str}")
                output = v.get("output") or v.get("output_preview") or ""
                if output:
                    first_line = output.strip().split("\n")[0][:200]
                    if first_line:
                        lines.append(f"    {first_line}")

    if validation_command_issues:
        lines.append("")
        lines.append(" Validation command issues:")
        for issue in validation_command_issues[:5]:
            lines.append(f"  \u2022 {validation_issue_message(issue)}")

    # \u2500\u2500 Required behavioral validation \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500
    _render_required_behavioral_summary(lines, behavioral_validation)

    # === Harness errors ===
    if internal_error:
        lines.append("")
        lines.append(" Harness errors:")
        lines.append(f"  \u2022 {internal_error}")

    # === Other errors (filter harness prefix to avoid duplication) ===
    other_errors: list[str] = []
    for err in errors:
        if internal_error and ("Harness error" in err or "internal Worker exception" in err):
            continue
        other_errors.append(err)
    if other_errors:
        lines.append("")
        lines.append(" Errors:")
        for err in other_errors:
            lines.append(f"  \u2022 {err}")

    # === Caveats ===
    if caveats:
        lines.append("")
        lines.append(" Caveats:")
        for c in caveats:
            lines.append(f"  \u2022 {c}")

    # === Failed writes ===
    if not_applied_writes:
        lines.append("")
        lines.append(" Failed writes:")
        deduped_not_applied = _dedupe_summary_writes(not_applied_writes)
        for w in deduped_not_applied[:5]:
            path = str(w.get("path") or "(unknown path)")
            failure = str(w.get("failure_class") or "")
            lines.append(f"  \u2022 {path}   ({failure})")

    # === Summary ===
    if req.summary:
        lines.append("")
        lines.append(" Summary:")
        for s_line in req.summary.strip().split("\n"):
            lines.append(f" {s_line}")

    # === Remaining work ===
    remaining = continuation.get("remaining", [])
    if remaining:
        lines.append("")
        lines.append(" Remaining work:")
        for item in remaining:
            lines.append(f"  \u2022 {item}")

    lines.append(BORDER)
    return "\n".join(lines)


def _dedupe_summary_writes(writes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    by_path: dict[str, dict[str, Any]] = {}
    for write in writes:
        path = str(write.get("path") or "").strip()
        if not path:
            continue
        existing = by_path.get(path)
        if existing is None:
            record = dict(write)
            record["path"] = path
            deduped.append(record)
            by_path[path] = record
            continue
        if write.get("is_new_file"):
            existing["is_new_file"] = True
        if write.get("deleted"):
            existing["deleted"] = True
    return deduped


def _is_internal_error_summary(error: str) -> bool:
    text = error.lower()
    return (
        text.startswith("harness error")
        or "internal worker exception" in text
        or "internal worker dispatch exception" in text
        or "worker_internal_error" in text
        or "internal_error" in text
    )
