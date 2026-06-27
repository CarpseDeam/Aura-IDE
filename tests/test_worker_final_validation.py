from __future__ import annotations

from unittest.mock import MagicMock, patch

from aura.conversation.validation_orchestrator import (
    MALFORMED_VALIDATION_COMMAND,
    PASSED,
    PRODUCT_VALIDATION_FAILED,
)
from aura.conversation.worker_final_validation import run_explicit_validation_commands
from aura.sandbox import WatchResult


def test_explicit_validation_runs_corrected_pytest_command(tmp_workspace) -> None:
    sandbox = MagicMock()
    sandbox.run_and_watch.return_value = WatchResult(
        ok=True,
        survived_window=False,
        exited_early=True,
        error_detected=False,
        exit_code=0,
        output="1 passed\n",
    )

    with patch("aura.conversation.worker_final_validation.SandboxExecutor", return_value=sandbox):
        result = run_explicit_validation_commands(
            workspace_root=tmp_workspace,
            commands=[
                "python -m pytest tests/test_worker_summary_card.py "
                "-k worker_summary_card_inserted_by_default -x passes"
            ],
        )

    assert result.ok is True
    sandbox.run_and_watch.assert_called_once_with(
        "python -m pytest tests/test_worker_summary_card.py "
        "-k worker_summary_card_inserted_by_default -x",
        window_seconds=20,
    )
    assert result.runs
    assert result.runs[0].classification == PASSED
    assert result.runs[0].normalized is True
    assert result.runs[0].expected_outcome == "passes"


def test_explicit_validation_malformed_prose_does_not_run(tmp_workspace) -> None:
    sandbox = MagicMock()

    with patch("aura.conversation.worker_final_validation.SandboxExecutor", return_value=sandbox):
        result = run_explicit_validation_commands(
            workspace_root=tmp_workspace,
            commands=["Run pytest and make sure it passes"],
        )

    sandbox.run_and_watch.assert_not_called()
    assert result.ok is True
    assert result.runs
    assert result.runs[0].classification == MALFORMED_VALIDATION_COMMAND
    assert result.runs[0].counts_as_product_failure is False


def test_explicit_validation_product_failure_still_blocks(tmp_workspace) -> None:
    sandbox = MagicMock()
    sandbox.run_and_watch.return_value = WatchResult(
        ok=False,
        survived_window=False,
        exited_early=True,
        error_detected=False,
        exit_code=1,
        output="FAILED tests/test_x.py::test_a - AssertionError\n",
    )

    with patch("aura.conversation.worker_final_validation.SandboxExecutor", return_value=sandbox):
        result = run_explicit_validation_commands(
            workspace_root=tmp_workspace,
            commands=["python -m pytest tests/test_x.py -x"],
        )

    assert result.ok is False
    assert result.counts_as_product_failure is True
    assert result.runs
    assert result.runs[0].classification == PRODUCT_VALIDATION_FAILED
