from __future__ import annotations

import re
from pathlib import Path

from aura.conversation.quality.diff_parser import DiffFile
from aura.conversation.quality.models import QualityFinding

DUPLICATE_STRING_MIN_LENGTH = 16
LARGE_DIFF_LINE_THRESHOLD = 400
PROTECTED_CONTROL_FLOW_FILES = frozenset({
    "manager.py",
    "dispatch.py",
    "worker_flow.py",
})

_CONTROL_FLOW_RE = re.compile(
    r"^\s*(?:if|elif|else|for|while|try|except|finally|with|return|raise|break|continue|match|case)\b"
    r"|^\s*else\s*:",
)
_DOUBLE_QUOTED_RE = re.compile(r'(?:[rRuUbBfF]{0,3})"((?:\\.|[^"\\])*)"')
_SINGLE_QUOTED_RE = re.compile(r"(?:[rRuUbBfF]{0,3})'((?:\\.|[^'\\])*)'")


def duplicate_changed_string_findings(
    diff_files: dict[str, DiffFile],
) -> list[QualityFinding]:
    by_literal: dict[str, dict[str, list[int | None]]] = {}
    for path, diff_file in diff_files.items():
        for line_number, text in diff_file.added:
            for literal in _string_literals(text):
                stripped = literal.strip()
                if len(stripped) < DUPLICATE_STRING_MIN_LENGTH:
                    continue
                by_literal.setdefault(stripped, {}).setdefault(path, []).append(line_number)

    findings: list[QualityFinding] = []
    for literal, file_lines in sorted(by_literal.items()):
        if len(file_lines) < 2:
            continue
        files = sorted(file_lines)
        preview = literal[:80]
        line = _first_line(file_lines)
        findings.append(
            QualityFinding(
                kind="duplicate_changed_string",
                severity="warning",
                file=", ".join(files),
                line=line,
                message=(
                    "Same newly added string literal appears in multiple changed files: "
                    + ", ".join(files)
                ),
                suggested_action=(
                    "Replace the duplicated literal with an existing shared constant or "
                    "leave one local copy only if the repetition is intentional."
                ),
                evidence={
                    "literal_preview": preview,
                    "files": files,
                    "line_numbers": file_lines,
                },
            )
        )
    return findings


def large_diff_findings(diff_files: dict[str, DiffFile]) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for path, diff_file in sorted(diff_files.items()):
        if diff_file.new_file:
            continue
        changed_count = diff_file.changed_line_count
        if changed_count <= LARGE_DIFF_LINE_THRESHOLD:
            continue
        findings.append(
            QualityFinding(
                kind="large_diff_whole_file_rewrite",
                severity="warning",
                file=path,
                line=None,
                message=(
                    f"Changed line count is {changed_count}, above the "
                    f"{LARGE_DIFF_LINE_THRESHOLD} line review threshold."
                ),
                suggested_action=(
                    "Narrow the patch or confirm the broad rewrite is required by the task."
                ),
                evidence={
                    "added_lines": len(diff_file.added),
                    "removed_lines": len(diff_file.removed),
                    "threshold": LARGE_DIFF_LINE_THRESHOLD,
                },
            )
        )
    return findings


def protected_control_flow_findings(
    diff_files: dict[str, DiffFile],
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for path, diff_file in sorted(diff_files.items()):
        if Path(path).name not in PROTECTED_CONTROL_FLOW_FILES:
            continue
        touched = [
            (line_number, text)
            for line_number, text in [*diff_file.added, *diff_file.removed]
            if _CONTROL_FLOW_RE.search(text)
        ]
        if not touched:
            continue
        line, text = touched[0]
        findings.append(
            QualityFinding(
                kind="protected_file_controlflow",
                severity="warning",
                file=path,
                line=line,
                message=f"Control flow changed in protected file {path}.",
                suggested_action=(
                    "Review the branch change against the requested task and keep the smallest valid patch."
                ),
                evidence={
                    "protected_files": sorted(PROTECTED_CONTROL_FLOW_FILES),
                    "line_text": text.strip(),
                },
            )
        )
    return findings


def _string_literals(text: str) -> list[str]:
    literals: list[str] = []
    for regex in (_DOUBLE_QUOTED_RE, _SINGLE_QUOTED_RE):
        literals.extend(match.group(1) for match in regex.finditer(text))
    return literals


def _first_line(file_lines: dict[str, list[int | None]]) -> int | None:
    for line in sorted(
        (line for lines in file_lines.values() for line in lines if line is not None)
    ):
        return line
    return None
