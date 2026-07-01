"""Format a structured task spec as a user message for the worker."""

from __future__ import annotations

from typing import Any

from aura.conversation import WorkerDispatchRequest, WorkerTaskSpec, normalize_worker_task
from aura.conversation.task_shape import task_shape_contract_lines

__all__ = [
    "_format_spec_as_user_message",
]


def _format_spec_as_user_message(task: WorkerTaskSpec | WorkerDispatchRequest) -> str:
    """Format a structured task spec (or raw dispatch request) as a user message
    for the worker. Accepts both types for backward compatibility."""
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
        "Allowed Responsibilities",
        _lines(task.allowed_responsibilities),
        "",
        "Forbidden Responsibilities",
        _lines(task.forbidden_responsibilities),
        "",
        "Required Outputs",
        _lines(task.required_outputs),
        "",
        "Non-Goals",
        _lines(task.non_goals),
        "",
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
        "Worker Contract",
        "- Read each target before editing it.",
        "- Small files can use read_file or read_files.",
        "- Large files and scoped targets use read_file_outline to navigate, then read_file_range around the edit region.",
        "- Use the returned content_hash from the latest successful read_file, read_files, or read_file_range result as expected_file_hash on patch_file.",
        "- If read_file returns truncated: true, treat it as navigation context only; read the actual edit region with read_file_range and use that range read's content_hash before patch_file.",
        "- Do not move unrelated behavior into entry points.",
        "- Do not create demo, prototype, or phase files unless explicitly requested.",
        "- Do not invent broad architecture outside the task scope.",
        "- Do not hide failure behind success-looking output.",
        "- Do not satisfy acceptance with placeholder behavior.",
        "- If a requested responsibility does not belong in a listed file, inspect and choose the smallest correct neighboring module, or report the mismatch.",

        "- Use patch_file for existing-file edits after reading the file or target range.",
        "- Use write_file only for new files or intentional full-file replacement.",
        "- Use delete_file for intentional file removals; do not use terminal rm/del as the primary deletion path.",
        "- If patch_file reports a hash mismatch or hunk failure, re-read the file and retry patch_file once with the new expected_file_hash; do not switch between edit tools.",
        "- Build the smallest complete implementation.",
        "- Own exact edits, validation, and code-quality decisions.",
        "- Use grep_search for discovery; use read_file or read_file_range for exact known-file verification.",
        "- For absent-pattern validation, make intended no-match exit 0.",
        "- Code must work and be easy to work on.",
        "- Avoid public-library, tutorial, or demo ceremony unless requested.",
        "- Avoid module summary docstrings and Args/Returns/Raises in normal app/tool code.",
        "- Do not add fake architecture.",
        "- Helpers return values or raise; CLI/UI/app boundary reports.",
        "- Validate actual behavior when practical.",
        "- Do not report Done unless acceptance passed.",
        "",
        "Begin. Read the listed files first, then make the change(s).",
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
