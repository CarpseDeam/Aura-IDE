"""Structured parsing and classification for Worker validation commands."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from aura.conversation._parse_helpers import (
    _clean_token,
    _extract_cd_wrapper,
    _extract_package_manager_cwd,
    _contains_timeout,
    _is_missing_dependency,
    _is_missing_executable,
    _is_package_manifest_missing,
    _is_pytest_tokens,
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
TRACEBACK_DETECTED = "traceback_detected"
UNKNOWN_FAILURE = "unknown_failure"
VALIDATION_COMMAND_UNRUNNABLE = "validation_command_unrunnable"
VALIDATION_WRONG_WORKING_DIRECTORY = "validation_wrong_working_directory"

ACTION_NONE = "none"
ACTION_FIX_CODE = "fix_code"
ACTION_FIX_VALIDATION_COMMAND = "fix_validation_command"
ACTION_INSTALL_DEPENDENCY = "install_dependency"
ACTION_RETRY = "retry"

TERMINAL_ROLE_COMMAND = "command"
TERMINAL_ROLE_SEARCH = "inspection_search"
TERMINAL_PASSED = "passed"
TERMINAL_SEARCH_NO_MATCH = "search_no_match"
TERMINAL_COMMAND_FAILED = "command_failed"
TERMINAL_EXECUTION_FAILED = "execution_failed"


@dataclass(frozen=True)
class ValidationCommand:
    raw_text: str
    command: str
    cwd: str = ""
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
            "cwd": self.cwd,
            "working_directory": self.cwd,
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
    cwd: str = ""
    output: str = ""
    classification: str = UNKNOWN_FAILURE
    counts_as_validation: bool = False
    counts_as_product_failure: bool = False
    user_action: str = ACTION_RETRY
    expected_outcome: str = ""
    source: str = "worker_command"
    normalized: bool = False
    normalization_reason: str = ""
    command_outcome_classification: str = ""
    traceback_detected: bool = False
    was_timeout: bool = False

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
            "cwd": self.cwd,
            "working_directory": self.cwd,
            "expected_outcome": self.expected_outcome,
            "validation_command_normalized": self.normalized,
            "normalized": self.normalized,
        }
        if self.command_outcome_classification:
            payload["command_outcome_classification"] = self.command_outcome_classification
        if self.traceback_detected:
            payload["validation_traceback_detected"] = self.traceback_detected
        if self.was_timeout:
            payload["validation_was_timeout"] = self.was_timeout
        if self.classification in {VALIDATION_COMMAND_UNRUNNABLE, VALIDATION_WRONG_WORKING_DIRECTORY}:
            payload.update(
                {
                    "recoverable": True,
                    "suggested_next_tool": "run_terminal_command",
                    "suggested_next_action": (
                        "Rerun the package-manager validation command from the "
                        "subproject/package root using cwd or working_directory."
                    ),
                }
            )
        if self.normalization_reason:
            payload["normalization_reason"] = self.normalization_reason
        return payload


@dataclass(frozen=True)
class TerminalRunClassification:
    role: str
    classification: str
    command_success: bool
    no_matches: bool = False
    traceback_detected: bool = False
    was_timeout: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "terminal_command_role": self.role,
            "terminal_classification": self.classification,
            "command_success": self.command_success,
            "terminal_no_matches": self.no_matches,
            "terminal_traceback_detected": self.traceback_detected,
            "terminal_was_timeout": self.was_timeout,
        }


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

    command_cwd = ""
    cd_normalized = _extract_cd_wrapper(command)
    if cd_normalized is not None:
        command_cwd, command = cd_normalized
        reason = _append_reason(reason, "cd wrapper")
        if not _looks_like_command(command):
            return ValidationCommand(
                raw_text=raw,
                command="",
                cwd=command_cwd,
                expected_outcome=expected,
                source=source,
                normalized=True,
                normalization_reason="cd wrapper did not contain a runnable command",
            )

    package_cwd = _extract_package_manager_cwd(command)
    if package_cwd is not None:
        extracted_cwd, command = package_cwd
        if command_cwd and _normalize_cwd(command_cwd) != _normalize_cwd(extracted_cwd):
            return ValidationCommand(
                raw_text=raw,
                command="",
                cwd=command_cwd,
                expected_outcome=expected,
                source=source,
                normalized=True,
                normalization_reason="conflicting command working directories",
            )
        command_cwd = command_cwd or extracted_cwd
        reason = _append_reason(reason, "package manager working directory flag")

    tokens = _split_tokens(command)
    if _is_pytest_tokens(tokens):
        stripped = _strip_trailing_outcome_token(command, tokens)
        if stripped is not None:
            stripped_command, outcome = stripped
            return ValidationCommand(
                raw_text=raw,
                command=stripped_command,
                cwd=command_cwd,
                expected_outcome=outcome,
                source=source,
                normalized=True,
                normalization_reason="trailing outcome prose token",
            )

    return ValidationCommand(
        raw_text=raw,
        command=command,
        cwd=command_cwd,
        expected_outcome=expected,
        source=source,
        normalized=bool(command_cwd or expected or reason),
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
    if failure_class == VALIDATION_COMMAND_UNRUNNABLE:
        return _result(
            validation_command,
            exit_code,
            output_text,
            VALIDATION_COMMAND_UNRUNNABLE,
            counts_as_validation=False,
            counts_as_product_failure=False,
            user_action=ACTION_FIX_VALIDATION_COMMAND,
        )

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
    tokens = _split_tokens(validation_command.command)
    if _is_package_manifest_missing(tokens, lowered):
        return _result(
            validation_command,
            exit_code,
            output_text,
            VALIDATION_WRONG_WORKING_DIRECTORY,
            counts_as_validation=False,
            counts_as_product_failure=False,
            user_action=ACTION_FIX_VALIDATION_COMMAND,
        )

    if _is_missing_executable(lowered):
        return _result(validation_command, exit_code, output_text, MISSING_EXECUTABLE, user_action=ACTION_INSTALL_DEPENDENCY)
    if _is_missing_dependency(lowered):
        return _result(validation_command, exit_code, output_text, MISSING_DEPENDENCY, user_action=ACTION_INSTALL_DEPENDENCY)

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
            cwd=str(payload.get("cwd") or payload.get("working_directory") or parsed.cwd),
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
        rf"(^|[;&|]\s*){python_exe}\s+-m\s+compileall\b",
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


def classify_terminal_run(
    command: str,
    *,
    exit_code: int | None,
    output: str = "",
    was_timeout: bool = False,
) -> TerminalRunClassification:
    role = (
        TERMINAL_ROLE_SEARCH
        if _is_search_command(command)
        else TERMINAL_ROLE_COMMAND
    )
    has_tb = "Traceback (most recent call last):" in output

    if was_timeout or exit_code == 124:
        return TerminalRunClassification(
            role=role,
            classification=TIMEOUT,
            command_success=False,
            was_timeout=True,
        )

    if exit_code == 0:
        if has_tb and role != TERMINAL_ROLE_SEARCH:
            return TerminalRunClassification(
                role=role,
                classification=TRACEBACK_DETECTED,
                command_success=True,
                traceback_detected=True,
            )
        return TerminalRunClassification(
            role=role,
            classification=TERMINAL_PASSED,
            command_success=True,
        )
    if role == TERMINAL_ROLE_SEARCH and exit_code == 1:
        return TerminalRunClassification(
            role=role,
            classification=TERMINAL_SEARCH_NO_MATCH,
            command_success=False,
            no_matches=True,
        )
    if has_tb:
        return TerminalRunClassification(
            role=role,
            classification=TRACEBACK_DETECTED,
            command_success=False,
            traceback_detected=True,
        )
    if exit_code is None or exit_code == -1:
        return TerminalRunClassification(
            role=role,
            classification=TERMINAL_EXECUTION_FAILED,
            command_success=False,
        )
    return TerminalRunClassification(
        role=role,
        classification=TERMINAL_COMMAND_FAILED,
        command_success=False,
    )


@dataclass(frozen=True)
class CommandOutcome:
    """Structured classification of a terminal command outcome.

    Provides stable, contextual classification that downstream logic
    (Worker, finalization, event relay) can use without re-parsing
    raw terminal output.

    Attributes:
        classification: Stable name from the constants above.
        command_success: Whether the underlying process exited with 0.
        counts_as_validation: Whether this outcome counts as a
            validation attempt.
        counts_as_product_failure: Whether this outcome should be
            treated as a product (code) failure.
        traceback_detected: Whether output contained a Python traceback.
        no_matches: Whether this was a search command with no matches.
        was_timeout: Whether the command was killed by timeout.
    """
    classification: str
    command_success: bool
    counts_as_validation: bool = False
    counts_as_product_failure: bool = False
    traceback_detected: bool = False
    no_matches: bool = False
    was_timeout: bool = False

    def metadata(self) -> dict[str, Any]:
        return {
            "command_outcome_classification": self.classification,
            "command_success": self.command_success,
            "counts_as_validation": self.counts_as_validation,
            "counts_as_product_failure": self.counts_as_product_failure,
            "command_traceback_detected": self.traceback_detected,
            "command_no_matches": self.no_matches,
            "command_was_timeout": self.was_timeout,
        }


def classify_command_outcome(
    command: str,
    *,
    exit_code: int | None,
    output: str,
    is_validation_command: bool = False,
    is_launch_watch: bool = False,
    was_timeout: bool = False,
) -> CommandOutcome:
    """Classify a terminal command outcome with structured metadata.

    This top-level classification accounts for the command's role and
    context (validation, launch watch, search/inspection) to produce
    a stable result that downstream logic can act on without parsing
    raw terminal output.

    Args:
        command: The command string that was executed.
        exit_code: The process exit code, or None / -1 for sandbox errors.
        output: The full merged stdout/stderr output.
        is_validation_command: True if this is a validation command
            (pytest, compileall, etc.).
        is_launch_watch: True if this is a launch-watch (run-and-watch)
            command where tracebacks are always product failures.
        was_timeout: True if the command was killed due to timeout.

    Returns:
        CommandOutcome with stable classification fields.
    """
    has_tb = "Traceback (most recent call last):" in output
    is_search = _is_search_command(command)

    # --- Timeout -----------------------------------------------------------
    if was_timeout or exit_code == 124:
        return CommandOutcome(
            classification=TIMEOUT,
            command_success=False,
            was_timeout=True,
        )

    # --- Exit code 0 (process-level success) --------------------------------
    if exit_code == 0:
        if is_launch_watch and has_tb:
            return CommandOutcome(
                classification=TRACEBACK_DETECTED,
                command_success=True,
                counts_as_product_failure=True,
                traceback_detected=True,
            )
        return CommandOutcome(
            classification=PASSED,
            command_success=True,
            counts_as_validation=is_validation_command,
        )

    # --- Non-zero exit (process-level failure) ------------------------------

    # Search / inspection: exit code 1 is typically "no matches"
    if is_search and exit_code == 1:
        return CommandOutcome(
            classification=TERMINAL_SEARCH_NO_MATCH,
            command_success=False,
            no_matches=True,
        )

    # Traceback in failed output
    if has_tb:
        if is_launch_watch:
            return CommandOutcome(
                classification=TRACEBACK_DETECTED,
                command_success=False,
                counts_as_product_failure=True,
                traceback_detected=True,
            )
        if is_validation_command:
            return CommandOutcome(
                classification=PRODUCT_VALIDATION_FAILED,
                command_success=False,
                counts_as_validation=True,
                counts_as_product_failure=True,
                traceback_detected=True,
            )
        return CommandOutcome(
            classification=TRACEBACK_DETECTED,
            command_success=False,
            traceback_detected=True,
        )

    # Sandbox / environment error
    if exit_code is None or exit_code == -1:
        return CommandOutcome(
            classification=ENVIRONMENT_ERROR,
            command_success=False,
        )

    # Generic command failure
    if is_validation_command:
        return CommandOutcome(
            classification=PRODUCT_VALIDATION_FAILED,
            command_success=False,
            counts_as_validation=True,
            counts_as_product_failure=True,
        )

    return CommandOutcome(
        classification=TERMINAL_COMMAND_FAILED,
        command_success=False,
    )


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
    if classification == VALIDATION_COMMAND_UNRUNNABLE:
        return f"Requested validation command is not runnable as specified: `{raw or command}`."
    if classification == VALIDATION_WRONG_WORKING_DIRECTORY:
        cwd = str(record.get("cwd") or record.get("working_directory") or "").strip()
        target = f" from `{cwd}`" if cwd else " from the subproject/package root"
        return f"Validation command must be rerun{target}: `{command or raw}`."
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


def _is_search_command(command: str) -> bool:
    tokens = _split_tokens(command)
    if not tokens:
        return False
    executable = _clean_token(tokens[0]).lower().replace("\\", "/").rsplit("/", 1)[-1]
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable in {"rg", "grep", "findstr"}


def _result(
    command: ValidationCommand,
    exit_code: int | None,
    output: str,
    classification: str,
    *,
    counts_as_validation: bool = False,
    counts_as_product_failure: bool = False,
    user_action: str,
    command_outcome_classification: str = "",
    traceback_detected: bool = False,
    was_timeout: bool = False,
) -> ValidationRunResult:
    return ValidationRunResult(
        command=command.command,
        raw_text=command.raw_text,
        exit_code=exit_code,
        cwd=command.cwd,
        output=output,
        classification=classification,
        counts_as_validation=counts_as_validation,
        counts_as_product_failure=counts_as_product_failure,
        user_action=user_action,
        expected_outcome=command.expected_outcome,
        source=command.source,
        normalized=command.normalized,
        normalization_reason=command.normalization_reason,
        command_outcome_classification=command_outcome_classification,
        traceback_detected=traceback_detected,
        was_timeout=was_timeout,
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
    "TRACEBACK_DETECTED",
    "TERMINAL_COMMAND_FAILED",
    "TERMINAL_EXECUTION_FAILED",
    "TERMINAL_PASSED",
    "TERMINAL_ROLE_COMMAND",
    "TERMINAL_ROLE_SEARCH",
    "TERMINAL_SEARCH_NO_MATCH",
    "UNKNOWN_FAILURE",
    "VALIDATION_COMMAND_UNRUNNABLE",
    "VALIDATION_WRONG_WORKING_DIRECTORY",
    "ValidationCommand",
    "ValidationRunResult",
    "TerminalRunClassification",
    "CommandOutcome",
    "classify_validation_payload",
    "classify_validation_run",
    "classify_terminal_run",
    "classify_command_outcome",
    "looks_like_validation_command",
    "parse_validation_command",
    "validation_issue_message",
]


def _append_reason(current: str, reason: str) -> str:
    if not current:
        return reason
    return f"{current}; {reason}"


def _normalize_cwd(cwd: str) -> str:
    return str(cwd or "").strip().replace("\\", "/").strip("/")
