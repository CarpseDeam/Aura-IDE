"""Focused tests for terminal command outcome classification.

Covers classify_terminal_run, classify_command_outcome, and the
propagation of structured metadata through ValidationRunResult.
"""

from __future__ import annotations

from aura.conversation.validation_orchestrator import (
    ENVIRONMENT_ERROR,
    PASSED,
    PRODUCT_VALIDATION_FAILED,
    TERMINAL_COMMAND_FAILED,
    TERMINAL_EXECUTION_FAILED,
    TERMINAL_PASSED,
    TERMINAL_ROLE_COMMAND,
    TERMINAL_ROLE_SEARCH,
    TERMINAL_SEARCH_NO_MATCH,
    TIMEOUT,
    TRACEBACK_DETECTED,
    CommandOutcome,
    TerminalRunClassification,
    ValidationRunResult,
    classify_command_outcome,
    classify_terminal_run,
    classify_validation_run,
    parse_validation_command,
)
from aura.sandbox import WatchResult, classify_watch_outcome


def test_versioned_godot_executable_is_a_runnable_validation_command() -> None:
    command = (
        '"C:\\Users\\Kori\\Desktop\\Godot_v4.6.3-stable_win64.exe" '
        '--headless --path "C:\\Projects\\Game" --check-only '
        '--script "res://scripts/player.gd"'
    )

    parsed = parse_validation_command(command)

    assert parsed.malformed is False
    assert parsed.command == command


def test_godot_validation_alias_and_bare_probe_are_validation_intent() -> None:
    from aura.conversation.validation_orchestrator import looks_like_validation_command

    prefix = (
        '"C:\\Users\\Kori\\Desktop\\Godot_v4.6.3-stable_win64.exe" '
        '--headless --path "C:\\Projects\\Game"'
    )

    assert looks_like_validation_command(prefix + " --validate-project") is True
    assert looks_like_validation_command(prefix) is True

# =========================================================================
# classify_terminal_run — enhanced with output/timeout
# =========================================================================


class TestClassifyTerminalRun:
    def test_exit_0_is_passed(self) -> None:
        result = classify_terminal_run("pytest", exit_code=0)
        assert result == TerminalRunClassification(
            role=TERMINAL_ROLE_COMMAND,
            classification=TERMINAL_PASSED,
            command_success=True,
        )

    def test_exit_0_with_traceback_still_traceback_detected(self) -> None:
        """Even with exit 0, a traceback in output is flagged."""
        result = classify_terminal_run(
            "python app.py",
            exit_code=0,
            output="Traceback (most recent call last):\n  File x.py, line 1\nok",
        )
        assert result.classification == TRACEBACK_DETECTED
        assert result.command_success is True
        assert result.traceback_detected is True

    def test_exit_0_traceback_search_command_passed(self) -> None:
        """Search commands that exit 0 are 'passed' even if output looks scary."""
        result = classify_terminal_run(
            "rg some_pattern",
            exit_code=0,
            output="# Traceback (most recent call last): is in a comment, not real\n",
        )
        assert result.classification == TERMINAL_PASSED
        assert result.command_success is True

    def test_search_exit_1_is_no_match(self) -> None:
        result = classify_terminal_run(
            "rg missing_pattern",
            exit_code=1,
        )
        assert result == TerminalRunClassification(
            role=TERMINAL_ROLE_SEARCH,
            classification=TERMINAL_SEARCH_NO_MATCH,
            command_success=False,
            no_matches=True,
        )

    def test_grep_exit_1_is_no_match(self) -> None:
        result = classify_terminal_run(
            "grep -r 'notfound' .",
            exit_code=1,
        )
        assert result.classification == TERMINAL_SEARCH_NO_MATCH
        assert result.no_matches is True

    def test_non_search_exit_1_is_command_failed(self) -> None:
        result = classify_terminal_run("npm test", exit_code=1)
        assert result.classification == TERMINAL_COMMAND_FAILED
        assert result.command_success is False
        assert result.no_matches is False

    def test_exit_124_is_timeout(self) -> None:
        result = classify_terminal_run("pytest", exit_code=124)
        assert result.classification == TIMEOUT
        assert result.was_timeout is True

    def test_was_timeout_flag(self) -> None:
        result = classify_terminal_run("pytest", exit_code=None, was_timeout=True)
        assert result.classification == TIMEOUT
        assert result.was_timeout is True

    def test_exit_minus_1_is_execution_failed(self) -> None:
        result = classify_terminal_run("foo", exit_code=-1)
        assert result.classification == TERMINAL_EXECUTION_FAILED

    def test_exit_none_is_execution_failed(self) -> None:
        result = classify_terminal_run("foo", exit_code=None)
        assert result.classification == TERMINAL_EXECUTION_FAILED

    def test_nonzero_with_traceback(self) -> None:
        result = classify_terminal_run(
            "python -c 'import nonexistent'",
            exit_code=1,
            output="Traceback (most recent call last):\n  File ...",
        )
        assert result.classification == TRACEBACK_DETECTED
        assert result.traceback_detected is True
        assert result.command_success is False

    def test_role_search_is_set(self) -> None:
        result = classify_terminal_run("rg foo", exit_code=1)
        assert result.role == TERMINAL_ROLE_SEARCH

    def test_role_command_is_default(self) -> None:
        result = classify_terminal_run("pytest", exit_code=0)
        assert result.role == TERMINAL_ROLE_COMMAND

    def test_backward_compatible_defaults(self) -> None:
        """Calling without output/was_timeout must still work."""
        result = classify_terminal_run("pytest", exit_code=0)
        assert result.classification == TERMINAL_PASSED
        assert result.traceback_detected is False
        assert result.was_timeout is False


# =========================================================================
# classify_command_outcome — all branches
# =========================================================================


class TestClassifyCommandOutcome:
    def test_passed(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=0, output="ok",
        )
        assert outcome == CommandOutcome(
            classification=PASSED, command_success=True,
        )

    def test_validation_command_passed(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=0, output="ok",
            is_validation_command=True,
        )
        assert outcome.classification == PASSED
        assert outcome.counts_as_validation is True
        assert outcome.counts_as_product_failure is False

    def test_timeout(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=124, output="",
        )
        assert outcome.classification == TIMEOUT
        assert outcome.was_timeout is True
        assert outcome.command_success is False

    def test_timeout_was_timeout_flag(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=None, output="",
            was_timeout=True,
        )
        assert outcome.classification == TIMEOUT
        assert outcome.was_timeout is True

    def test_search_no_matches(self) -> None:
        outcome = classify_command_outcome(
            "rg nonexistent", exit_code=1, output="",
        )
        assert outcome.classification == TERMINAL_SEARCH_NO_MATCH
        assert outcome.no_matches is True
        assert outcome.command_success is False
        assert outcome.counts_as_product_failure is False
        assert outcome.counts_as_validation is False

    def test_grep_no_matches(self) -> None:
        outcome = classify_command_outcome(
            "grep -r 'missing' .", exit_code=1, output="",
        )
        assert outcome.classification == TERMINAL_SEARCH_NO_MATCH
        assert outcome.no_matches is True

    def test_product_validation_failed(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=1, output="FAILED test_foo.py::test_bar",
            is_validation_command=True,
        )
        assert outcome.classification == PRODUCT_VALIDATION_FAILED
        assert outcome.counts_as_validation is True
        assert outcome.counts_as_product_failure is True

    def test_product_validation_failed_with_traceback(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=1,
            output="Traceback (most recent call last):\n  assert False",
            is_validation_command=True,
        )
        assert outcome.classification == PRODUCT_VALIDATION_FAILED
        assert outcome.traceback_detected is True
        assert outcome.counts_as_product_failure is True

    def test_launch_watch_traceback_detected(self) -> None:
        """Launch watch with traceback output -> traceback_detected, product failure."""
        outcome = classify_command_outcome(
            "python app.py", exit_code=0,
            output="Traceback (most recent call last):\n  boom",
            is_launch_watch=True,
        )
        assert outcome.classification == TRACEBACK_DETECTED
        assert outcome.traceback_detected is True
        assert outcome.counts_as_product_failure is True
        assert outcome.command_success is True

    def test_launch_watch_traceback_nonzero_exit(self) -> None:
        """Launch watch that exits nonzero with traceback -> product failure."""
        outcome = classify_command_outcome(
            "python app.py", exit_code=1,
            output="Traceback (most recent call last):\n  boom",
            is_launch_watch=True,
        )
        assert outcome.classification == TRACEBACK_DETECTED
        assert outcome.traceback_detected is True
        assert outcome.counts_as_product_failure is True
        assert outcome.command_success is False

    def test_intermediate_traceback_validation_passes(self) -> None:
        """Validation command exits 0 despite intermediate traceback -> passed.

        This is the key case: fallback branches may produce traceback
        text, but the final command exit code of 0 means success.
        """
        outcome = classify_command_outcome(
            "python -m compileall docs/",
            exit_code=0,
            output=(
                "Traceback (most recent call last):\n"
                "  File 'compileall.py', line 1, in _walk_dir\n"
                "    compile(file, doraise=True)\n"
                "py_compile: 1 errors in 1 files\n"
                "fallback: retrying without doraise...\n"
                "Success: no issues found\n"
            ),
            is_validation_command=True,
        )
        assert outcome.classification == PASSED
        assert outcome.command_success is True
        # Traceback was present in intermediate output but the final
        # validation result is clean — classification is 'passed'.
        assert outcome.traceback_detected is False  # not a product failure

    def test_environment_error_none_exit(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=None, output="[ERROR: OSError]",
        )
        assert outcome.classification == ENVIRONMENT_ERROR
        assert outcome.command_success is False

    def test_environment_error_minus_one(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=-1, output="[ERROR]",
        )
        assert outcome.classification == ENVIRONMENT_ERROR

    def test_validation_command_environment_error(self) -> None:
        """Validation command that couldn't run -> environment_error."""
        outcome = classify_command_outcome(
            "pytest", exit_code=-1, output="Docker unavailable",
            is_validation_command=True,
        )
        assert outcome.classification == ENVIRONMENT_ERROR
        assert outcome.counts_as_validation is False
        assert outcome.counts_as_product_failure is False

    def test_generic_command_failure(self) -> None:
        outcome = classify_command_outcome(
            "some_tool", exit_code=2, output="unrecognized argument",
        )
        assert outcome.classification == TERMINAL_COMMAND_FAILED
        assert outcome.command_success is False

    def test_traceback_detected_non_validation_non_launch(self) -> None:
        """Traceback in a generic command -> traceback_detected, not product failure."""
        outcome = classify_command_outcome(
            "python script.py", exit_code=1,
            output="Traceback (most recent call last):\n  ValueError",
        )
        assert outcome.classification == TRACEBACK_DETECTED
        assert outcome.traceback_detected is True
        assert outcome.counts_as_product_failure is False
        assert outcome.counts_as_validation is False

    def test_validation_command_failure_no_traceback(self) -> None:
        """Validation fails with an error message (no traceback)."""
        outcome = classify_command_outcome(
            "pytest", exit_code=1, output="FAILURES: test_foo failed",
            is_validation_command=True,
        )
        assert outcome.classification == PRODUCT_VALIDATION_FAILED
        assert outcome.counts_as_product_failure is True
        assert outcome.traceback_detected is False


# =========================================================================
# Classification metadata propagation
# =========================================================================


class TestValidationRunResultMetadata:
    def test_new_fields_carried_in_metadata(self) -> None:
        """ValidationRunResult.metadata() includes terminal outcome fields."""
        run = ValidationRunResult(
            command="pytest",
            raw_text="pytest",
            exit_code=1,
            output="FAILED",
            classification=PRODUCT_VALIDATION_FAILED,
            counts_as_validation=True,
            counts_as_product_failure=True,
            user_action="fix_code",
            command_outcome_classification=TRACEBACK_DETECTED,
            traceback_detected=True,
        )
        meta = run.metadata()
        assert meta["command_outcome_classification"] == TRACEBACK_DETECTED
        assert meta["validation_traceback_detected"] is True
        # Existing fields must still be present
        assert meta["validation_classification"] == PRODUCT_VALIDATION_FAILED
        assert meta["counts_as_product_failure"] is True

    def test_no_terminal_metadata_when_empty(self) -> None:
        """Fields left at defaults should not appear in metadata."""
        run = ValidationRunResult(
            command="pytest",
            raw_text="pytest",
            exit_code=1,
            output="FAILED",
            classification=PRODUCT_VALIDATION_FAILED,
            counts_as_validation=True,
            counts_as_product_failure=True,
            user_action="fix_code",
        )
        meta = run.metadata()
        assert "command_outcome_classification" not in meta
        assert "validation_traceback_detected" not in meta

    def test_false_fields_omitted(self) -> None:
        """False boolean fields should not appear in metadata."""
        run = ValidationRunResult(
            command="pytest",
            raw_text="pytest",
            exit_code=0,
            output="ok",
            classification=PASSED,
            counts_as_validation=True,
            user_action="none",
            command_outcome_classification=PASSED,
        )
        meta = run.metadata()
        assert meta["command_outcome_classification"] == PASSED
        assert "validation_traceback_detected" not in meta
        assert "validation_was_timeout" not in meta


# =========================================================================
# Test the existing worker logic: validation vs. launch strictness
# =========================================================================


class TestWorkerFinalValidationBehavior:
    """Regression: watch/traceback behavior for validation vs launch."""

    def test_validation_passes_with_intermediate_traceback(self) -> None:
        """A watch result with exit_code=0 and traceback -> validation passes.

        This simulates what worker_final_validation.run_explicit_validation_commands
        does: ok = bool(watch.exited_early and watch.exit_code == 0).
        """
        # Simulate a sandbox watch where an intermediate command
        # produced a traceback, but the final exit was 0.
        watch = WatchResult(
            ok=False,          # watch's own traceback heuristic
            survived_window=False,
            exited_early=True,
            error_detected=True,
            exit_code=0,
            output="Traceback (most recent call last):\n  fallback error\nok\n",
        )

        # The explicit validation logic overrides the watch's heuristic:
        ok = bool(watch.exited_early and watch.exit_code == 0)
        assert ok is True  # validation passes

        # classify_command_outcome should reflect the same:
        outcome = classify_command_outcome(
            "python -m py_compile foo.py",
            exit_code=watch.exit_code,
            output=watch.output,
            is_validation_command=True,
        )
        assert outcome.classification == PASSED
        assert outcome.command_success is True
        assert outcome.counts_as_product_failure is False

    def test_launch_stays_strict_on_traceback(self) -> None:
        """classify_watch_outcome still detects tracebacks for launch."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=0,
            output="Traceback (most recent call last):\nboom\n",
            window_seconds=10,
        )
        assert result.ok is False
        assert result.error_detected is True

    def test_launch_watch_traceback_product_failure(self) -> None:
        outcome = classify_command_outcome(
            "python app.py",
            exit_code=0,
            output="Traceback (most recent call last):\nboom",
            is_launch_watch=True,
        )
        assert outcome.classification == TRACEBACK_DETECTED
        assert outcome.counts_as_product_failure is True

    def test_run_and_watch_exit_0_no_traceback_passes(self) -> None:
        """Launch watch with clean output and exit 0 passes."""
        result = classify_watch_outcome(
            still_running=False,
            exit_code=0,
            output="Server started on port 8080\n",
            window_seconds=10,
            require_survive_window=False,
        )
        assert result.ok is True
        assert result.error_detected is False


class TestClassifyValidationRunInteraction:
    """Existing classify_validation_run behavior is unchanged."""

    def test_validation_run_passed(self) -> None:
        vc = parse_validation_command("pytest tests/ passed")
        run = classify_validation_run(vc, exit_code=0, output="passed", ok=True)
        assert run.classification == PASSED
        assert run.ok is True

    def test_validation_run_product_failure(self) -> None:
        vc = parse_validation_command("pytest tests/")
        run = classify_validation_run(vc, exit_code=1, output="FAILED", ok=False)
        assert run.classification == PRODUCT_VALIDATION_FAILED
        assert run.counts_as_product_failure is True

    def test_validation_run_missing_dependency(self) -> None:
        vc = parse_validation_command("pytest tests/")
        run = classify_validation_run(
            vc, exit_code=1, output="ModuleNotFoundError: no module named 'xyz'", ok=False,
        )
        assert run.classification == "missing_dependency"

    def test_validation_run_malformed_command(self) -> None:
        vc = parse_validation_command("")
        run = classify_validation_run(vc, exit_code=None, output="", ok=False)
        assert run.classification == "malformed_validation_command"
        assert run.counts_as_product_failure is False
        assert run.ok is False

    def test_validation_run_timeout(self) -> None:
        vc = parse_validation_command("pytest tests/")
        run = classify_validation_run(vc, exit_code=124, output="timed out", ok=False)
        assert run.classification == TIMEOUT


class TestTerminalRunClassificationMetadata:
    """Verify metadata dict keys match expected naming."""

    def test_metadata_keys_passed(self) -> None:
        trc = classify_terminal_run("pytest", exit_code=0)
        meta = trc.metadata()
        assert meta["terminal_command_role"] == TERMINAL_ROLE_COMMAND
        assert meta["terminal_classification"] == TERMINAL_PASSED
        assert meta["command_success"] is True
        assert meta["terminal_no_matches"] is False
        assert meta["terminal_traceback_detected"] is False
        assert meta["terminal_was_timeout"] is False

    def test_metadata_no_match_search(self) -> None:
        trc = classify_terminal_run("rg foo", exit_code=1)
        meta = trc.metadata()
        assert meta["terminal_command_role"] == TERMINAL_ROLE_SEARCH
        assert meta["terminal_classification"] == TERMINAL_SEARCH_NO_MATCH
        assert meta["terminal_no_matches"] is True

    def test_metadata_traceback_detected(self) -> None:
        trc = classify_terminal_run(
            "python app.py", exit_code=0,
            output="Traceback (most recent call last):\n  boom",
        )
        meta = trc.metadata()
        assert meta["terminal_classification"] == TRACEBACK_DETECTED
        assert meta["terminal_traceback_detected"] is True

    def test_metadata_timeout(self) -> None:
        trc = classify_terminal_run("pytest", exit_code=124)
        meta = trc.metadata()
        assert meta["terminal_classification"] == TIMEOUT
        assert meta["terminal_was_timeout"] is True


class TestCommandOutcomeMetadata:
    """Verify CommandOutcome metadata dict keys."""

    def test_metadata_keys_present(self) -> None:
        outcome = classify_command_outcome(
            "pytest", exit_code=0, output="ok",
            is_validation_command=True,
        )
        meta = outcome.metadata()
        assert meta["command_outcome_classification"] == PASSED
        assert meta["command_success"] is True
        assert meta["counts_as_validation"] is True
        assert meta["command_traceback_detected"] is False
        assert meta["command_no_matches"] is False
        assert meta["command_was_timeout"] is False

    def test_inspection_no_match_metadata(self) -> None:
        outcome = classify_command_outcome(
            "rg missing", exit_code=1, output="",
        )
        meta = outcome.metadata()
        assert meta["command_outcome_classification"] == TERMINAL_SEARCH_NO_MATCH
        assert meta["command_no_matches"] is True
        assert meta["counts_as_product_failure"] is False


# =========================================================================
# Integration: tool_runner payload_dict merging (contextual override)
# =========================================================================
# These simulate the aggregation that tool_runner.py performs in
# handle_run_terminal_command: classify_terminal_run → add raw metadata
# → classify_validation_run → add validation metadata → classify_command_outcome
# → override raw terminal fields with contextual outcome.


class TestToolRunnerValidationPayloadAggregation:
    """Simulate the payload_dict merging logic from tool_runner.py.

    Validates that contextual classify_command_outcome overrides raw
    terminal_classification for validation commands, so Workers do not
    see a scary terminal_classification alongside passed validation.
    """

    def _simulate_payload(
        self,
        command: str,
        exit_code: int | None,
        output: str,
        ok: bool = True,
        looks_like_validation: bool = True,
    ) -> dict[str, object]:
        """Aggregate metadata the same way tool_runner.handle_run_terminal_command does."""
        pd: dict[str, object] = {"exit_code": exit_code, "output": output, "command": command}

        # Step 1: raw terminal classification
        trc = classify_terminal_run(command, exit_code=exit_code, output=output)
        pd.update(trc.metadata())

        if looks_like_validation:
            # Step 2: validation-level classification
            vc = parse_validation_command(command)
            run = classify_validation_run(vc, exit_code=exit_code, output=output, ok=ok)
            pd.update(run.metadata())

            # Step 3: contextual command outcome & override
            outcome = classify_command_outcome(
                command,
                exit_code=exit_code,
                output=output,
                is_validation_command=True,
            )
            pd.update(outcome.metadata())
            pd["terminal_classification"] = outcome.classification
            pd["terminal_traceback_detected"] = outcome.traceback_detected

        return pd

    # --- Intermediate traceback (the key case) -----------------------------

    def test_intermediate_traceback_validation_passes_contextual(self) -> None:
        """Validation command with intermediate traceback, exit 0.

        Raw terminal_classification would be traceback_detected, but
        contextual override must set it to passed.
        """
        output = (
            "Traceback (most recent call last):\n"
            "  File 'compileall.py', line 1\n"
            "    compile(file, doraise=True)\n"
            "py_compile: 1 errors in 1 files\n"
            "fallback: retrying without doraise...\n"
            "Success: no issues found\n"
        )
        pd = self._simulate_payload(
            "python -m compileall docs/", exit_code=0, output=output,
        )
        # Raw terminal sees traceback
        assert pd["terminal_traceback_detected"] is False  # overridden by outcome
        # Contextual override must say PASSED (not traceback_detected)
        assert pd["terminal_classification"] == PASSED
        assert pd["command_outcome_classification"] == PASSED
        # Validation classification also says passed
        assert pd["validation_classification"] == PASSED
        assert pd["counts_as_product_failure"] is False
        assert pd["command_success"] is True

    def test_intermediate_traceback_raw_terminal_is_scary(self) -> None:
        """The raw terminal classification (without override) IS traceback_detected."""
        trc = classify_terminal_run(
            "python -m compileall docs/",
            exit_code=0,
            output=(
                "Traceback (most recent call last):\n"
                "  File 'compileall.py', line 1\n"
                "Success: no issues found\n"
            ),
        )
        assert trc.classification == TRACEBACK_DETECTED  # raw is scary
        assert trc.traceback_detected is True

    # --- Validation passes cleanly ----------------------------------------

    def test_clean_validation_passes_through(self) -> None:
        """Clean validation pass — terminal stays passed."""
        pd = self._simulate_payload(
            "pytest tests/", exit_code=0, output="=== 1 passed ===",
        )
        assert pd["terminal_classification"] == PASSED
        assert pd["validation_classification"] == PASSED
        assert pd["command_outcome_classification"] == PASSED
        assert pd["command_success"] is True

    # --- Validation product failure ---------------------------------------

    def test_validation_failure_contextual(self) -> None:
        """Validation that fails with exit 1 -> product_validation_failed."""
        pd = self._simulate_payload(
            "pytest tests/", exit_code=1,
            output="FAILED test_foo.py::test_bar",
            ok=False,
        )
        # Raw terminal would say command_failed; contextual uses product_validation_failed
        assert pd["terminal_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["validation_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["command_outcome_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["counts_as_product_failure"] is True

    def test_validation_failure_with_traceback_contextual(self) -> None:
        """Validation failure with traceback -> product_validation_failed."""
        pd = self._simulate_payload(
            "pytest tests/", exit_code=1,
            output="Traceback (most recent call last):\n  assert False",
            ok=False,
        )
        assert pd["terminal_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["validation_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["command_outcome_classification"] == PRODUCT_VALIDATION_FAILED
        assert pd["command_traceback_detected"] is True
        # Override leaves terminal_traceback_detected in sync with outcome
        assert pd["terminal_traceback_detected"] is True

    # --- Timeout ----------------------------------------------------------

    def test_validation_timeout_contextual(self) -> None:
        """Timeout with validation classification -> timeout."""
        pd = self._simulate_payload(
            "pytest tests/", exit_code=124, output="timed out",
            ok=False,
        )
        assert pd["terminal_classification"] == TIMEOUT
        assert pd["validation_classification"] == TIMEOUT
        assert pd["command_outcome_classification"] == TIMEOUT
        assert pd["command_was_timeout"] is True
        assert pd["command_success"] is False

    # --- Search / no-match not affected (not validation) ------------------

    def test_search_not_validation_no_override(self) -> None:
        """Search commands not classified as validation pass through raw."""
        pd = self._simulate_payload(
            "rg missing_func", exit_code=1, output="",
            looks_like_validation=False,
        )
        # Without validation classification, terminal runs raw:
        assert pd["terminal_classification"] == TERMINAL_SEARCH_NO_MATCH
        assert pd["terminal_no_matches"] is True
        # No validation or outcome keys added
        assert "validation_classification" not in pd
        assert "command_outcome_classification" not in pd

    # --- Non-validation generic command -----------------------------------

    def test_generic_command_not_validation(self) -> None:
        """Generic command unrelated to validation is not overridden."""
        pd = self._simulate_payload(
            "npm install", exit_code=0, output="added 10 packages",
            looks_like_validation=False,
        )
        assert pd["terminal_classification"] == PASSED
        assert pd["command_success"] is True
        assert "validation_classification" not in pd
