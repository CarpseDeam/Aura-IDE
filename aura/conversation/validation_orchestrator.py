"""Structured parsing and classification for Worker validation commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aura.conversation._parse_helpers import (
    _clean_token,
    _contains_timeout,
    _is_missing_dependency,
    _is_missing_executable,
    _is_pytest_tokens,
    _is_shell_syntax_error,
    _looks_like_command,
    _pytest_missing_path,
    _pytest_no_tests_collected,
    _pytest_selection_empty,
    _select_command_line,
    _split_tokens,
    _strip_prompt_prefix,
    _strip_shell_comment_outcome,
    _strip_trailing_outcome_token,
)

PASSED = "passed"
PRODUCT_VALIDATION_FAILED = "product_validation_failed"
MALFORMED_VALIDATION_COMMAND = "malformed_validation_command"
NO_TESTS_COLLECTED = "no_tests_collected"
TEST_SELECTION_EMPTY = "test_selection_empty"
MISSING_DEPENDENCY = "missing_dependency"
MISSING_EXECUTABLE = "missing_executable"
POLICY_BLOCKED = "policy_blocked"
TIMEOUT = "timeout"
ENVIRONMENT_ERROR = "environment_error"
UNKNOWN_FAILURE = "unknown_failure"

ACTION_NONE = "none"
ACTION_FIX_CODE = "fix_code"
ACTION_FIX_VALIDATION_COMMAND = "fix_validation_command"
ACTION_INSTALL_DEPENDENCY = "install_dependency"
ACTION_RETRY = "retry"


@dataclass(frozen=True)
class ValidationCommand:
    raw_text: str
    command: str
    expected_outcome: str = ""
    source: str = "worker_command"
    normalized: bool = False
    normalization_reason: str = ""

    @property
    def malformed(self) -> bool:
        return not self.command.strip()

    def metadata(self) -> dict[str, Any]:
        payload = {
            "validation_raw_text": self.raw_text,
            "raw_text": self.raw_text,
            "expected_outcome": self.expected_outcome,
            "validation_source": self.source,
            "validation_command_normalized": self.normalized,
            "normalized": self.normalized,
        }
        if self.normalization_reason:
            payload["normalization_reason"] = self.normalization_reason
        return payload


@dataclass(frozen=True)
class ValidationRunResult:
    command: str
    raw_text: str
    exit_code: int | None
    output: str = ""
    classification: str = UNKNOWN_FAILURE
    counts_as_validation: bool = False
    counts_as_product_failure: bool = False
    user_action: str = ACTION_RETRY
    expected_outcome: str = ""
    source: str = "worker_command"
    normalized: bool = False
    normalization_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.classification == PASSED

    def metadata(self) -> dict[str, Any]:
        payload = {
            "validation_classification": self.classification,
            "classification": self.classification,
            "counts_as_validation": self.counts_as_validation,
            "counts_as_product_failure": self.counts_as_product_failure,
            "user_action": self.user_action,
            "validation_raw_text": self.raw_text,
            "raw_text": self.raw_text,
            "validation_source": self.source,
            "expected_outcome": self.expected_outcome,
            "validation_command_normalized": self.normalized,
            "normalized": self.normalized,
        }
        if self.normalization_reason:
            payload["normalization_reason"] = self.normalization_reason
        return payload


def parse_validation_command(raw_text: str, *, source: str = "worker_command") -> ValidationCommand:
    raw = str(raw_text or "").strip()
    if not raw:
        return ValidationCommand(raw_text=raw, command="", source=source, normalization_reason="empty validation text")

    line = _select_command_line(raw)
    if not line:
        return ValidationCommand(raw_text=raw, command="", source=source, normalization_reason="no runnable command found")

    line = _strip_prompt_prefix(line)
    command, expected, reason = _strip_shell_comment_outcome(line)
    if not _looks_like_command(command):
        return ValidationCommand(
            raw_text=raw,
            command="",
            expected_outcome=expected,
            source=source,
            normalized=bool(expected),
            normalization_reason="validation text is prose, not a runnable command",
        )

    tokens = _split_tokens(command)
    if _is_pytest_tokens(tokens):
        stripped = _strip_trailing_outcome_token(command, tokens)
        if stripped is not None:
            stripped_command, outcome = stripped
            return ValidationCommand(
                raw_text=raw,
                command=stripped_command,
                expected_outcome=outcome,
                source=source,
                normalized=True,
                normalization_reason="trailing outcome prose token",
            )

    return ValidationCommand(
        raw_text=raw,
        command=command,
        expected_outcome=expected,
        source=source,
        normalized=bool(expected or reason),
        normalization_reason=reason,
    )


def classify_validation_run(
    validation_command: ValidationCommand,
    *,
    exit_code: int | None,
    output: str,
    ok: bool,
    failure_class: str = "",
) -> ValidationRunResult:
    output_text = str(output or "")
    if validation_command.malformed:
        return _result(
            validation_command,
            exit_code,
            output_text,
            MALFORMED_VALIDATION_COMMAND,
            counts_as_validation=False,
            counts_as_product_failure=False,
            user_action=ACTION_FIX_VALIDATION_COMMAND,
        )

    if failure_class in {"source_inspection_command_blocked", "worker_terminal_not_validation"}:
        return _result(validation_command, exit_code, output_text, POLICY_BLOCKED, user_action=ACTION_FIX_VALIDATION_COMMAND)

    if exit_code == -1 or _contains_timeout(output_text):
        return _result(validation_command, exit_code, output_text, TIMEOUT, user_action=ACTION_RETRY)

    if ok and exit_code == 0:
        return _result(
            validation_command,
            exit_code,
            output_text,
            PASSED,
            counts_as_validation=True,
            counts_as_product_failure=False,
            user_action=ACTION_NONE,
        )

    lowered = output_text.lower()
    if _is_missing_executable(lowered):
        return _result(validation_command, exit_code, output_text, MISSING_EXECUTABLE, user_action=ACTION_INSTALL_DEPENDENCY)
    if _is_missing_dependency(lowered):
        return _result(validation_command, exit_code, output_text, MISSING_DEPENDENCY, user_action=ACTION_INSTALL_DEPENDENCY)
    if _is_shell_syntax_error(lowered):
        return _result(
            validation_command,
            exit_code,
            output_text,
            MALFORMED_VALIDATION_COMMAND,
            user_action=ACTION_FIX_VALIDATION_COMMAND,
        )

    tokens = _split_tokens(validation_command.command)
    if _is_pytest_tokens(tokens):
        missing_path = _pytest_missing_path(output_text)
        if missing_path:
            parsed_tokens = {_clean_token(token).lower() for token in tokens}
            missing_clean = _clean_token(missing_path).lower()
            expected = validation_command.expected_outcome.lower()
            if missing_clean == expected or missing_clean not in parsed_tokens:
                return _result(
                    validation_command,
                    exit_code,
                    output_text,
                    MALFORMED_VALIDATION_COMMAND,
                    counts_as_validation=False,
                    counts_as_product_failure=False,
                    user_action=ACTION_FIX_VALIDATION_COMMAND,
                )
        if _pytest_no_tests_collected(lowered):
            return _result(validation_command, exit_code, output_text, NO_TESTS_COLLECTED, user_action=ACTION_FIX_VALIDATION_COMMAND)
        if _pytest_selection_empty(lowered):
            return _result(validation_command, exit_code, output_text, TEST_SELECTION_EMPTY, user_action=ACTION_FIX_VALIDATION_COMMAND)
        return _result(
            validation_command,
            exit_code,
            output_text,
            PRODUCT_VALIDATION_FAILED,
            counts_as_validation=True,
            counts_as_product_failure=True,
            user_action=ACTION_FIX_CODE,
        )

    return _result(
        validation_command,
        exit_code,
        output_text,
        PRODUCT_VALIDATION_FAILED,
        counts_as_validation=True,
        counts_as_product_failure=True,
        user_action=ACTION_FIX_CODE,
    )


def classify_validation_payload(payload: dict[str, Any]) -> ValidationRunResult:
    raw_text = str(payload.get("validation_raw_text") or payload.get("raw_text") or payload.get("requested_command") or payload.get("command") or "")
    command_text = str(payload.get("command") or "")
    parsed = parse_validation_command(raw_text, source=str(payload.get("validation_source") or "worker_command"))
    if command_text:
        parsed = ValidationCommand(
            raw_text=parsed.raw_text or raw_text,
            command=command_text,
            expected_outcome=str(payload.get("expected_outcome") or parsed.expected_outcome),
            source=parsed.source,
            normalized=bool(payload.get("validation_command_normalized") or payload.get("normalized") or parsed.normalized),
            normalization_reason=str(payload.get("normalization_reason") or parsed.normalization_reason),
        )
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = None
    return classify_validation_run(
        parsed,
        exit_code=exit_code,
        output=str(payload.get("output") or payload.get("output_preview") or payload.get("error") or ""),
        ok=bool(payload.get("ok")),
        failure_class=str(payload.get("failure_class") or ""),
    )


def looks_like_validation_command(command: str) -> bool:
    normalized = " ".join(str(command or "").strip().lower().split())
    if not normalized:
        return False
    python_exe = r"(?:(?:\"[^\"]*python3?(?:\.exe)?\")|(?:'[^']*python3?(?:\.exe)?')|\S*python3?(?:\.exe)?|py)"
    patterns = (
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+py_compile\b",
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+(?:pytest|unittest|ruff|mypy)\b",
        r"(^|[;&|]\s*)pytest\b",
        r"(^|[;&|]\s*)unittest\b",
        r"(^|[;&|]\s*)ruff\s+(?:check|format\s+--check)\b",
        r"(^|[;&|]\s*)mypy\b",
        r"(^|[;&|]\s*)npm\s+(?:test|run\s+(?:test|build))\b",
        r"(^|[;&|]\s*)cargo\s+(?:test|build)\b",
        r"(^|[;&|]\s*)go\s+test\b",
    )
    return any(re.search(pattern, normalized) for pattern in patterns)


def validation_issue_message(record: dict[str, Any]) -> str:
    classification = str(record.get("validation_classification") or record.get("classification") or "")
    raw = str(record.get("validation_raw_text") or record.get("raw_text") or record.get("requested_command") or record.get("command") or "").strip()
    command = str(record.get("command") or "").strip()
    reason = str(record.get("normalization_reason") or "").strip()
    expected = str(record.get("expected_outcome") or "").strip()
    if expected and "outcome prose token" in reason:
        return f"Requested command had trailing prose token `{expected}`; runnable command was `{command}`."
    if classification == MALFORMED_VALIDATION_COMMAND and expected:
        return f"Requested command had trailing prose token `{expected}`; runnable command was `{command}`."
    if classification == MALFORMED_VALIDATION_COMMAND:
        return f"Requested validation command was malformed: `{raw}`."
    if classification in {NO_TESTS_COLLECTED, TEST_SELECTION_EMPTY}:
        return f"Validation command selected no tests: `{command or raw}`."
    if classification in {MISSING_DEPENDENCY, MISSING_EXECUTABLE}:
        return f"Validation environment issue for `{command or raw}`."
    if classification == POLICY_BLOCKED:
        return f"Validation command was blocked by policy: `{command or raw}`."
    if classification == TIMEOUT:
        return f"Validation command timed out: `{command or raw}`."
    if reason:
        return f"Requested validation command was normalized ({reason}): `{raw}` -> `{command}`."
    return f"Validation command issue: `{raw or command}`."


def _result(
    command: ValidationCommand,
    exit_code: int | None,
    output: str,
    classification: str,
    *,
    counts_as_validation: bool = False,
    counts_as_product_failure: bool = False,
    user_action: str,
) -> ValidationRunResult:
    return ValidationRunResult(
        command=command.command,
        raw_text=command.raw_text,
        exit_code=exit_code,
        output=output,
        classification=classification,
        counts_as_validation=counts_as_validation,
        counts_as_product_failure=counts_as_product_failure,
        user_action=user_action,
        expected_outcome=command.expected_outcome,
        source=command.source,
        normalized=command.normalized,
        normalization_reason=command.normalization_reason,
    )


__all__ = [
    "ACTION_FIX_CODE",
    "ACTION_FIX_VALIDATION_COMMAND",
    "ACTION_INSTALL_DEPENDENCY",
    "ACTION_NONE",
    "ACTION_RETRY",
    "ENVIRONMENT_ERROR",
    "MALFORMED_VALIDATION_COMMAND",
    "MISSING_DEPENDENCY",
    "MISSING_EXECUTABLE",
    "NO_TESTS_COLLECTED",
    "PASSED",
    "POLICY_BLOCKED",
    "PRODUCT_VALIDATION_FAILED",
    "TEST_SELECTION_EMPTY",
    "TIMEOUT",
    "UNKNOWN_FAILURE",
    "ValidationCommand",
    "ValidationRunResult",
    "classify_validation_payload",
    "classify_validation_run",
    "looks_like_validation_command",
    "parse_validation_command",
    "validation_issue_message",
]
