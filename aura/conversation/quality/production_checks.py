from __future__ import annotations

import re

from aura.conversation.quality.diff_parser import DiffFile
from aura.conversation.quality.models import QualityFinding
from aura.conversation.quality.path_policy import (
    is_production_path,
    normalize_changed_files,
    normalize_path,
)

_RUNTIME_DEBUG_PATTERNS = (
    ("print(", re.compile(r"\bprint\s*\(")),
    ("breakpoint()", re.compile(r"\bbreakpoint\s*\(\s*\)")),
    ("pdb.set_trace", re.compile(r"\bpdb\.set_trace\b")),
)
_TEMP_TEXT_PATTERNS = (
    ("DIAGNOSTIC", re.compile(r"\bDIAGNOSTIC\b")),
    ("debug probe", re.compile(r"debug probe", re.IGNORECASE)),
    ("event probe", re.compile(r"event probe", re.IGNORECASE)),
    ("temporary probe", re.compile(r"temporary probe", re.IGNORECASE)),
    ("TODO: remove", re.compile(r"TODO:\s*remove", re.IGNORECASE)),
    ("HACK", re.compile(r"\bHACK\b")),
    ("XXX", re.compile(r"\bXXX\b")),
)
_PLACEHOLDER_PATTERNS = (
    ("NotImplementedError", re.compile(r"\bNotImplementedError\b")),
    ("TODO: implement", re.compile(r"todo:\s*implement", re.IGNORECASE)),
    ("placeholder", re.compile(r"\bplaceholder\b", re.IGNORECASE)),
    ("stub", re.compile(r"\bstub\b", re.IGNORECASE)),
    ("return None  # TODO", re.compile(r"\breturn\s+None\s*#\s*TODO\b", re.IGNORECASE)),
)
_EXCEPT_RE = re.compile(r"^\s*except(?:\s+Exception)?\s*:")
_SWALLOW_RE = re.compile(r"^\s*(?:pass|return(?:\s+None)?|continue)\s*(?:#.*)?$")
_HANDLING_RE = re.compile(
    r"\b(?:log|logger|logging|receipt|error|failure|raise|report|on_event)\b",
    re.IGNORECASE,
)


def unexpected_production_file_findings(
    changed_files: list[str],
    expected_files: list[str] | None,
) -> list[QualityFinding]:
    if expected_files is None:
        return []
    expected = set(normalize_changed_files(expected_files))
    findings: list[QualityFinding] = []
    for path in normalize_changed_files(changed_files):
        if not is_production_path(path) or path in expected:
            continue
        findings.append(
            QualityFinding(
                kind="unexpected_production_file",
                severity="error",
                file=path,
                line=None,
                message=f"Production file {path} was changed outside the dispatch scope.",
                suggested_action=(
                    "Remove the unrelated production edit or update the dispatch scope before release."
                ),
                evidence={"expected_files": sorted(expected)},
            )
        )
    return findings


def temporary_production_code_findings(
    diff_files: dict[str, DiffFile],
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for path, line_number, text in _iter_added_production_lines(diff_files):
        runtime_marker = _runtime_debug_marker(text)
        if runtime_marker:
            findings.append(
                QualityFinding(
                    kind="temporary_production_code",
                    severity="error",
                    file=path,
                    line=line_number,
                    message=f"Runtime debug statement added to production code: {runtime_marker}.",
                    suggested_action="Remove the debug statement before final release.",
                    evidence={"line_text": text.strip(), "marker": runtime_marker},
                )
            )
            continue
        text_marker = _temporary_text_marker(text)
        if text_marker:
            findings.append(
                QualityFinding(
                    kind="temporary_production_code",
                    severity="warning",
                    file=path,
                    line=line_number,
                    message=f"Temporary/debug marker added to production code: {text_marker}.",
                    suggested_action=(
                        "Remove temporary instrumentation or replace it with production-owned behavior."
                    ),
                    evidence={"line_text": text.strip(), "marker": text_marker},
                )
            )
    return findings


def placeholder_production_code_findings(
    diff_files: dict[str, DiffFile],
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for path, line_number, text in _iter_added_production_lines(diff_files):
        marker = _placeholder_marker(text)
        if not marker:
            continue
        findings.append(
            QualityFinding(
                kind="placeholder_production_code",
                severity="error",
                file=path,
                line=line_number,
                message=f"Placeholder implementation marker added to production code: {marker}.",
                suggested_action="Replace the placeholder with the actual implementation before release.",
                evidence={"line_text": text.strip(), "marker": marker},
            )
        )
    return findings


def swallowed_exception_findings(
    diff_files: dict[str, DiffFile],
) -> list[QualityFinding]:
    findings: list[QualityFinding] = []
    for path, diff_file in sorted(diff_files.items()):
        normalized_path = normalize_path(path)
        if not is_production_path(normalized_path):
            continue
        added = diff_file.added
        for index, (line_number, text) in enumerate(added):
            if not _EXCEPT_RE.search(text):
                continue
            nearby = [candidate for _, candidate in added[index:index + 6]]
            if any(_HANDLING_RE.search(candidate) for candidate in nearby):
                continue
            swallow_line = _first_swallow_line(added[index + 1:index + 6])
            if swallow_line is None:
                continue
            swallow_line_number, swallow_text = swallow_line
            findings.append(
                QualityFinding(
                    kind="swallowed_exception",
                    severity="error",
                    file=normalized_path,
                    line=swallow_line_number or line_number,
                    message="Broad exception handler added without nearby error handling.",
                    suggested_action=(
                        "Handle the exception explicitly, log/report it, or let it propagate."
                    ),
                    evidence={
                        "except_line": text.strip(),
                        "swallow_line": swallow_text.strip(),
                    },
                )
            )
    return findings


def _iter_added_production_lines(
    diff_files: dict[str, DiffFile],
) -> list[tuple[str, int | None, str]]:
    lines: list[tuple[str, int | None, str]] = []
    for path, diff_file in sorted(diff_files.items()):
        normalized_path = normalize_path(path)
        if not is_production_path(normalized_path):
            continue
        for line_number, text in diff_file.added:
            lines.append((normalized_path, line_number, text))
    return lines


def _runtime_debug_marker(text: str) -> str:
    for marker, pattern in _RUNTIME_DEBUG_PATTERNS:
        if pattern.search(text):
            return marker
    return ""


def _temporary_text_marker(text: str) -> str:
    for marker, pattern in _TEMP_TEXT_PATTERNS:
        if pattern.search(text):
            return marker
    return ""


def _placeholder_marker(text: str) -> str:
    for marker, pattern in _PLACEHOLDER_PATTERNS:
        if pattern.search(text):
            return marker
    return ""


def _first_swallow_line(
    candidates: list[tuple[int | None, str]],
) -> tuple[int | None, str] | None:
    for line_number, text in candidates:
        stripped = text.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _SWALLOW_RE.search(text):
            return line_number, text
        return None
    return None
