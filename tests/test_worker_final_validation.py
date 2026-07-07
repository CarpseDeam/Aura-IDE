"""Focused tests for aura/conversation/worker_final_validation.py.

Covers sandbox exception handling, malformed command handling, and
infra-classified failure iteration — the regression gaps identified in
Track 1 of the harness simplification plan.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from aura.conversation.worker_final_validation import (
    WorkerFinalValidationResult,
    run_explicit_validation_commands,
)


# ---------------------------------------------------------------------------
# Sandbox exception
# ---------------------------------------------------------------------------


class TestSandboxException:
    """When SandboxExecutor.run_and_watch raises, the result must carry the
    exception type and message, and must return ok=False."""

    def test_sandbox_exception_returns_ok_false(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """An exception from run_and_watch produces ok=False."""
        result = self._run_with_exception(
            tmp_path, monkeypatch, RuntimeError("connection refused"),
        )
        assert result.ok is False

    def test_sandbox_exception_includes_exception_type_and_message(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Diagnostics must include the exception type name and message text."""
        result = self._run_with_exception(
            tmp_path, monkeypatch, ValueError("invalid cwd: /nonexistent"),
        )
        assert "ValueError" in result.diagnostics
        assert "invalid cwd: /nonexistent" in result.diagnostics

    def test_sandbox_exception_has_one_run(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The result contains exactly one run with ok=False."""
        result = self._run_with_exception(
            tmp_path, monkeypatch, RuntimeError("timeout"),
        )
        assert result.runs is not None
        assert len(result.runs) == 1
        assert result.runs[0].ok is False

    def test_sandbox_exception_has_command_in_result(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The failing command is preserved in the result."""
        result = self._run_with_exception(
            tmp_path, monkeypatch, RuntimeError("OOM"),
        )
        assert result.command == "pytest"

    def test_sandbox_exception_runs_list_contains_diagnostics(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """The run's output contains the exception diagnostics."""
        result = self._run_with_exception(
            tmp_path, monkeypatch, RuntimeError("Killed"),
        )
        assert result.runs is not None
        assert "RuntimeError" in result.runs[0].output
        assert "Killed" in result.runs[0].output

    @staticmethod
    def _run_with_exception(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        exception: Exception,
    ) -> WorkerFinalValidationResult:
        """Run a single validation command with a sandbox that raises."""

        class _RaisingSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, *_args, **_kwargs) -> None:
                raise exception

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _RaisingSandbox,
        )
        return run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["pytest"],
        )


# ---------------------------------------------------------------------------
# Malformed / unrunnable commands
# ---------------------------------------------------------------------------


class TestMalformedCommand:
    """A validation command that cannot be parsed as runnable must produce a
    failed run, not a crash or silent pass."""

    def test_empty_command_returns_ok_false(self, tmp_path: Path) -> None:
        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=[""],
        )
        assert result.ok is False

    def test_empty_command_run_is_not_product_failure(self, tmp_path: Path) -> None:
        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=[""],
        )
        assert result.counts_as_product_failure is False
        assert result.infra_only is True

    def test_empty_command_run_has_diagnostics(self, tmp_path: Path) -> None:
        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=[""],
        )
        assert result.runs is not None
        assert len(result.runs) == 1
        assert result.runs[0].ok is False
        assert result.runs[0].classification == "malformed_validation_command"

    def test_malformed_command_does_not_crash(self, tmp_path: Path) -> None:
        """A string that is not a valid command should not raise."""
        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["  "],
        )
        # Should produce a non-ok result without raising
        assert result.ok is False


# ---------------------------------------------------------------------------
# Infra-classified failures continue iteration
# ---------------------------------------------------------------------------


class TestInfraFailureContinues:
    """Infra-classified validation failures must not stop the command list
    unless they are product failures. Remaining commands should still run."""

    def test_infra_failure_does_not_stop_iteration(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When the first command fails with an infra-only error, the second
        command still runs."""
        calls: list[str] = []

        class _TrackingSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, command: str, **_kwargs) -> MagicMock:
                calls.append(str(command))
                watch = MagicMock()
                watch.exited_early = True
                watch.exit_code = 1 if "fail" in command else 0
                # Use ModuleNotFoundError so classify_validation_run treats
                # the failure as infra-only (missing dependency), not product.
                watch.output = (
                    f"ModuleNotFoundError: no module named '{command}'"
                    if "fail" in command
                    else f"output from {command}"
                )
                watch.survived_window = False
                return watch

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _TrackingSandbox,
        )

        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["python fail.py", "python ok.py"],
        )

        # Both commands should have run (infra-only failure doesn't stop)
        assert len(calls) == 2
        assert "python fail.py" in calls
        assert "python ok.py" in calls

        # The overall result should have both runs
        assert result.runs is not None
        assert len(result.runs) == 2

    def test_infra_only_overall_result_when_all_failures_infra(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When all failures are infra-only, the overall result is infra_only."""
        class _InfraSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, command: str, **_kwargs) -> MagicMock:
                watch = MagicMock()
                watch.exited_early = True
                watch.exit_code = 1  # Both fail
                watch.output = f"ModuleNotFoundError: no module '{command}'"
                watch.survived_window = False
                return watch

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _InfraSandbox,
        )

        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["pytest", "ruff"],
        )

        assert result.ok is False
        assert result.infra_only is True
        assert result.counts_as_product_failure is False

    def test_mixed_infra_and_product_failure_not_infra_only(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """When the first command is product failure, iteration stops and
        the result is not infra_only."""
        calls: list[str] = []

        class _MixedSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, command: str, **_kwargs) -> MagicMock:
                calls.append(str(command))
                watch = MagicMock()
                watch.exited_early = True
                watch.exit_code = 1
                watch.output = f"FAILED {command}"
                watch.survived_window = False
                return watch

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _MixedSandbox,
        )

        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["pytest", "ruff"],
        )

        # Only the first command should have run (product failure stops iteration)
        assert len(calls) == 1
        assert result.ok is False
        assert result.infra_only is False
        assert result.counts_as_product_failure is True

    def test_all_pass_returns_ok_true(self, tmp_path: Path, monkeypatch) -> None:
        """When all commands pass, the result is ok=True."""
        class _PassSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, command: str, **_kwargs) -> MagicMock:
                watch = MagicMock()
                watch.exited_early = True
                watch.exit_code = 0
                watch.output = f"passed: {command}"
                watch.survived_window = False
                return watch

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _PassSandbox,
        )

        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["pytest", "ruff"],
        )

        assert result.ok is True
        assert result.infra_only is False
        assert result.counts_as_product_failure is False

    def test_first_product_failure_stops_iteration(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """A product failure on the first command prevents the second from running."""
        class _ProductFailSandbox:
            def __init__(self, **_kwargs) -> None:
                pass

            def run_and_watch(self, command: str, **_kwargs) -> MagicMock:
                watch = MagicMock()
                watch.exited_early = True
                watch.exit_code = 1
                watch.output = f"FAILED {command}: test_bar failed"
                watch.survived_window = False
                return watch

        monkeypatch.setattr(
            "aura.conversation.worker_final_validation.SandboxExecutor",
            _ProductFailSandbox,
        )

        result = run_explicit_validation_commands(
            workspace_root=tmp_path,
            commands=["pytest", "ruff"],
        )

        assert result.ok is False
        assert result.counts_as_product_failure is True


# ---------------------------------------------------------------------------
# Property integration
# ---------------------------------------------------------------------------


class TestWorkerFinalValidationResultProperties:
    """WorkerFinalValidationResult property contract."""

    def test_ok_empty_runs_none(self) -> None:
        """A default-constructed result (runs=None) is ok directly, not
        infra_only, not a product failure."""
        r = WorkerFinalValidationResult(ok=True)
        assert r.counts_as_product_failure is False
        assert r.infra_only is False

    def test_ok_false_no_runs_not_infra(self) -> None:
        """With no runs and ok=False, infra_only is False because there
        are no failing runs to inspect."""
        r = WorkerFinalValidationResult(ok=False)
        assert r.infra_only is False

    def test_infra_only_true_when_all_failing_runs_are_infra(self) -> None:
        from aura.conversation.validation_orchestrator import (
            ENVIRONMENT_ERROR,
            ValidationRunResult,
        )
        infra_run = ValidationRunResult(
            command="pytest", raw_text="pytest", exit_code=1,
            classification=ENVIRONMENT_ERROR, counts_as_product_failure=False,
            output="ModuleNotFoundError",
        )
        r = WorkerFinalValidationResult(
            ok=False, diagnostics="infra fail", command="pytest",
            runs=[infra_run],
        )
        assert r.infra_only is True
        assert r.counts_as_product_failure is False

    def test_infra_only_false_when_any_run_is_product_failure(self) -> None:
        from aura.conversation.validation_orchestrator import (
            PRODUCT_VALIDATION_FAILED,
            ValidationRunResult,
        )
        product_run = ValidationRunResult(
            command="pytest", raw_text="pytest", exit_code=1,
            classification=PRODUCT_VALIDATION_FAILED, counts_as_product_failure=True,
            output="AssertionError: expected 3 got 5",
        )
        r = WorkerFinalValidationResult(
            ok=False, diagnostics="product fail", command="pytest",
            runs=[product_run],
        )
        assert r.infra_only is False
        assert r.counts_as_product_failure is True
