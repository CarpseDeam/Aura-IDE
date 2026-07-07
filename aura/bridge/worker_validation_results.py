"""Validation-result collection and filtering helpers for bridge dispatch.

Extracted from worker_completion_result.py to reduce module size.
"""

from __future__ import annotations

import re
from typing import Any

from aura.conversation.detected_validation import (
    behavioral_required_commands,
)
from aura.conversation.detected_validation import (
    normalize_command as _normalize_command,
)
from aura.conversation.path_utils import (
    is_validation_scratch_path as _is_validation_scratch_path,
)
from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    MISSING_DEPENDENCY,
    MISSING_EXECUTABLE,
    NO_TESTS_COLLECTED,
    POLICY_BLOCKED,
    TEST_SELECTION_EMPTY,
    TIMEOUT,
    validation_issue_message,
)
from aura.conversation.worker_completion._shell_pipeline import (
    _is_benign_search_no_match,
)

__all__ = [
    "_VALIDATION_COMMAND_ISSUE_CLASSES",
    "_assess_required_behavioral_validation",
    "_filter_scratch_validation_results",
    "_later_family_passes",
    "_later_py_compile_passes",
    "_normalize_py_compile_path",
    "_py_compile_targets",
    "_unrecovered_validation_failures",
    "_validation_command_issues_for_task",
    "_validation_family",
    "_validation_results_for_task",
]


def _assess_required_behavioral_validation(
    validation_commands: list[str],
    validation_results: list[dict[str, Any]],
    validation_command_issues: list[dict[str, Any]],
) -> dict[str, Any]:
    """Categorise each required behavioural command against what actually ran.

    Returns a dict with four lists:
        * ``passed`` — commands that ran and reported ``ok=True``.
        * ``failed`` — commands that ran and reported ``counts_as_product_failure=True``
          (existing validation-failure logic remains in charge).
        * ``skipped`` — commands for which there is no result *and* no issue record.
        * ``could_not_run`` — commands for which there is no result but there
          *is* a validation-command issue (e.g. missing executable, timeout).
    """
    required = behavioral_required_commands(validation_commands)
    passed: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    skipped: list[str] = []
    could_not_run: list[dict[str, Any]] = []

    for cmd in required:
        norm = _normalize_command(cmd)

        # 1. Check validation results.
        matching_results = [
            r for r in validation_results
            if _normalize_command(str(r.get("command") or "")) == norm
        ]
        if matching_results:
            if any(r.get("ok") for r in matching_results):
                passed.append({"command": cmd})
                continue
            if any(r.get("counts_as_product_failure") for r in matching_results):
                failed.append({"command": cmd})
                continue
            # Ran but failed with *non*-product outcome (e.g. infra error).
            # Treat as could-not-run — the result record's status is the reason.
            # (A timeout, missing-dep, etc. that the terminal classified as
            # non-product-failure will also have a matching issue entry, but we
            # already have the direct result so prefer it.)
            reason = "command failed with non-product outcome"
            could_not_run.append({"command": cmd, "reason": reason})
            continue

        # 2. Check validation-command issues (command never ran successfully).
        matching_issues = [
            i for i in validation_command_issues
            if _normalize_command(str(i.get("command") or "")) == norm
        ]
        if matching_issues:
            issue = matching_issues[0]
            classification = str(
                issue.get("validation_classification") or issue.get("classification") or ""
            )
            # Prefer a human-readable reason from the issue record.
            reason = validation_issue_message(issue) or classification
            could_not_run.append({"command": cmd, "reason": reason})
            continue

        # 3. No result, no issue → skipped.
        skipped.append(cmd)

    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "could_not_run": could_not_run,
    }


def _validation_family(command: str) -> str:
    """Classify a validation command into a family for cross-run recovery."""
    normalized = command.lower()
    if "py_compile" in normalized:
        return "py_compile"
    if "ruff" in normalized and "check" in normalized:
        return "ruff_check"
    if "pytest" in normalized:
        return "pytest"
    if "python -m aura --selfcheck" in normalized or "aura --selfcheck" in normalized:
        return "aura_selfcheck"
    return ""


def _later_family_passes(results: list[dict[str, Any]], family: str) -> bool:
    """Check if a later result in the same validation family passed."""
    if not family:
        return False
    for result in results:
        if not result.get("ok"):
            continue
        if _validation_family(str(result.get("command") or "")) == family:
            return True
    return False


def _unrecovered_validation_failures(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        if result.get("ok"):
            continue
        if result.get("counts_as_product_failure") is False:
            continue
        if _is_benign_search_no_match(result):
            continue
        command = str(result.get("command", ""))
        targets = set(_py_compile_targets(command))
        if targets and _later_py_compile_passes(results[index + 1:], targets):
            continue
        family = _validation_family(command)
        if family and _later_family_passes(results[index + 1:], family):
            continue
        failures.append(result)
    return failures


_VALIDATION_COMMAND_ISSUE_CLASSES = {
    MALFORMED_VALIDATION_COMMAND,
    NO_TESTS_COLLECTED,
    TEST_SELECTION_EMPTY,
    MISSING_DEPENDENCY,
    MISSING_EXECUTABLE,
    POLICY_BLOCKED,
    TIMEOUT,
}


def _validation_command_issues_for_task(
    terminal_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for record in terminal_results:
        classification = str(record.get("validation_classification") or record.get("classification") or "")
        normalized = bool(record.get("validation_command_normalized") or record.get("normalized"))
        if (
            classification not in _VALIDATION_COMMAND_ISSUE_CLASSES
            and not normalized
        ):
            continue
        if record.get("counts_as_product_failure") is True:
            continue
        key = (
            str(record.get("validation_raw_text") or record.get("raw_text") or ""),
            str(record.get("command") or ""),
            classification,
        )
        if key in seen:
            continue
        seen.add(key)
        issues.append(record)
    return issues


def _validation_results_for_task(
    validation_results: list[dict[str, Any]],
    terminal_results: list[dict[str, Any]],
    explicit_commands: list[str],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen: set[tuple[str, Any, Any]] = set()

    def add(record: dict[str, Any]) -> None:
        key = (str(record.get("command") or ""), record.get("exit_code"), record.get("ok"))
        if key not in seen:
            records.append(record)
            seen.add(key)

    for record in validation_results:
        add(record)

    explicit = {command.strip() for command in explicit_commands if command.strip()}
    if explicit:
        for record in terminal_results:
            if str(record.get("command") or "").strip() in explicit:
                add(record)
    return records


def _later_py_compile_passes(results: list[dict[str, Any]], targets: set[str]) -> bool:
    for result in results:
        if not result.get("ok"):
            continue
        later_targets = set(_py_compile_targets(str(result.get("command", ""))))
        if targets and targets.issubset(later_targets):
            return True
    return False


def _py_compile_targets(command: str) -> list[str]:
    if "py_compile" not in command:
        return []
    matches = re.findall(r"(?<![\\w.-])([A-Za-z0-9_./\\\\:\\-]+\\.py)(?![\\w.-])", command)
    return [_normalize_py_compile_path(m) for m in matches if not m.endswith("py_compile.py")]


def _normalize_py_compile_path(raw: str) -> str:
    p = raw.strip().replace("\\\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _filter_scratch_validation_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for result in results:
        command = str(result.get("command") or "")
        targets = _py_compile_targets(command)
        if targets and all(_is_validation_scratch_path(target) for target in targets):
            continue
        filtered.append(result)
    return filtered
