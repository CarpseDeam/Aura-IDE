"""Focused tests for required behavioral validation enforcement in completion.

Covers the classification and result-assembly behaviour when required
behavioral validation commands are passed, skipped, or could-not-run.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import pytest

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_completion_result import (
    _assess_required_behavioral_validation,
    prepare_worker_completion_result,
)
from aura.client import ToolResult
from aura.conversation import History, WorkerDispatchRequest, normalize_worker_task
from aura.conversation.worker_outcome import WorkerOutcomeStatus
from aura.events import EventBus


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def _make_result(
    *,
    validation_commands: list[str] | None = None,
    validation_results: list[dict] | None = None,
    terminal_results: list[dict] | None = None,
    write_results: list[dict] | None = None,
    touched: list[str] | None = None,
    report_text: str = "<status>completed</status><validation>passed</validation>",
) -> dict:
    """Build a full completion result and return its outcome fields."""
    req = WorkerDispatchRequest(
        goal="Make a change",
        files=[],
        spec="A thing",
        acceptance="it should work",
    )
    if validation_commands is not None:
        req = replace(req, validation_commands=validation_commands)

    history = History()
    history.append_assistant(
        {
            "role": "assistant",
            "content": report_text,
            "reasoning_content": None,
        }
    )
    task_spec = normalize_worker_task(req)

    relay = WorkerEventRelay(_ApprovalProxy(), event_bus=EventBus())
    if write_results:
        for w in write_results:
            relay.relay(
                "worker-parent",
                ToolResult(
                    tool_call_id="write-1",
                    name="write_file",
                    ok=True,
                    result=json.dumps(w),
                ),
            )
    # Relay validation/terminal results through the classification pipeline.
    if validation_results:
        for v in validation_results:
            relay.relay(
                "worker-parent",
                ToolResult(
                    tool_call_id="val-1",
                    name="run_terminal_command",
                    ok=True,
                    result=json.dumps(v),
                ),
            )
    if terminal_results:
        for t in terminal_results:
            relay.relay(
                "worker-parent",
                ToolResult(
                    tool_call_id="term-1",
                    name="run_terminal_command",
                    ok=True,
                    result=json.dumps(t),
                ),
            )
    if touched:
        relay.touched_files = set(touched)

    completion = prepare_worker_completion_result(
        req=req,
        worker_history=history,
        task_spec=task_spec,
        relay=relay,
        context_gearbox={},
        internal_error=None,
        cleaned_scratch_files=[],
        final_validation_commands=list(task_spec.validation_commands),
        workspace_root=None,
        preserve_scratch_records=False,
    )
    cr = completion.build_result(validation_selector=None)
    return {
        "ok": cr.result.ok,
        "recoverable": cr.result.recoverable,
        "needs_followup": cr.result.needs_followup,
        "status": cr.result.status,
        "extras": cr.extras,
    }


# ---------------------------------------------------------------------------
# Direct unit tests for _assess_required_behavioral_validation
# ---------------------------------------------------------------------------


class TestAssessRequiredBehavioralValidation:
    """Tests for the assessment function directly, bypassing the classification
    pipeline so we can precisely control the input shapes."""

    def test_ran_and_passed(self) -> None:
        result = _assess_required_behavioral_validation(
            validation_commands=["pytest"],
            validation_results=[
                {"command": "pytest", "ok": True, "counts_as_product_failure": False}
            ],
            validation_command_issues=[],
        )
        assert len(result["passed"]) == 1
        assert result["passed"][0]["command"] == "pytest"
        assert not result["skipped"]
        assert not result["could_not_run"]
        assert not result["failed"]

    def test_failed_as_product_failure(self) -> None:
        result = _assess_required_behavioral_validation(
            validation_commands=["pytest"],
            validation_results=[
                {"command": "pytest", "ok": False, "counts_as_product_failure": True}
            ],
            validation_command_issues=[],
        )
        assert len(result["failed"]) == 1
        assert result["failed"][0]["command"] == "pytest"

    def test_skipped_no_result_no_issue(self) -> None:
        result = _assess_required_behavioral_validation(
            validation_commands=["pytest"],
            validation_results=[],
            validation_command_issues=[],
        )
        assert "pytest" in result["skipped"]

    def test_could_not_run_with_issue(self) -> None:
        result = _assess_required_behavioral_validation(
            validation_commands=["pytest"],
            validation_results=[],
            validation_command_issues=[
                {
                    "command": "pytest",
                    "validation_classification": "missing_executable",
                    "classification": "missing_executable",
                }
            ],
        )
        assert len(result["could_not_run"]) == 1
        assert result["could_not_run"][0]["command"] == "pytest"

    def test_mixed(self) -> None:
        result = _assess_required_behavioral_validation(
            validation_commands=["pytest", "npm run build", "cargo test"],
            validation_results=[
                {"command": "npm run build", "ok": True, "counts_as_product_failure": False}
            ],
            validation_command_issues=[
                {"command": "cargo test", "validation_classification": "missing_executable"}
            ],
        )
        assert len(result["passed"]) == 1
        assert result["passed"][0]["command"] == "npm run build"
        assert "pytest" in result["skipped"]
        assert len(result["could_not_run"]) == 1
        assert result["could_not_run"][0]["command"] == "cargo test"

    def test_behavioral_only(self) -> None:
        """Non-behavioral commands are excluded."""
        result = _assess_required_behavioral_validation(
            validation_commands=["ruff check", "mypy"],
            validation_results=[],
            validation_command_issues=[],
        )
        assert not result["passed"]
        assert not result["skipped"]
        assert not result["could_not_run"]
        assert not result["failed"]


# ---------------------------------------------------------------------------
# Integration tests through the full completion pipeline
# ---------------------------------------------------------------------------


class TestIntegrationBehavioralValidation:
    """End-to-end tests using the classification pipeline.

    Note: the relay's ``validation_results`` only contain ``command``,
    ``ok``, ``exit_code``, ``output``, and ``output_preview`` -- any
    additional fields such as ``counts_as_product_failure`` are stripped
    during relay processing.  Use these tests for pipeline-level checks;
    the precise assessment logic is tested separately via
    ``TestAssessRequiredBehavioralValidation``.
    """

    def test_behavioral_required_ran_and_passed(self) -> None:
        """A required behavioral command that ran and passed does not block."""
        result = _make_result(
            validation_commands=["pytest"],
            validation_results=[
                {
                    "ok": True,
                    "command": "pytest",
                    "exit_code": 0,
                    "output": "passed",
                }
            ],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        assert result["ok"] is True
        assert result["recoverable"] is False
        assert result["needs_followup"] is False
        assert result["status"] == WorkerOutcomeStatus.completed.value
        bv = result["extras"].get("required_behavioral_validation", {})
        assert len(bv.get("passed", [])) == 1
        assert bv["passed"][0]["command"] == "pytest"

    def test_behavioral_required_skipped_blocks_recoverable(self) -> None:
        """A required behavioral command with no result and no issue blocks with recoverable=True."""
        result = _make_result(
            validation_commands=["pytest"],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        assert result["ok"] is False
        assert result["recoverable"] is True
        assert result["needs_followup"] is True
        assert result["status"] == WorkerOutcomeStatus.validation_failed.value
        bv = result["extras"].get("required_behavioral_validation", {})
        assert "pytest" in bv.get("skipped", [])

    def test_behavioral_required_skipped_no_writes_recoverable(self) -> None:
        """Behavioral skipped with no writes still returns recoverable=True."""
        result = _make_result(
            validation_commands=["npm test"],
        )
        assert result["ok"] is False
        assert result["recoverable"] is True
        assert result["needs_followup"] is True
        assert result["status"] == WorkerOutcomeStatus.validation_failed.value
        bv = result["extras"].get("required_behavioral_validation", {})
        assert "npm test" in bv.get("skipped", [])

    def test_behavioral_required_skipped_has_result_error(self) -> None:
        """Skipped behavioral command appears in result_errors."""
        result = _make_result(
            validation_commands=["pytest"],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        errors = result["extras"].get("errors", [])
        assert any("Required behavioral validation skipped" in e for e in errors)

    def test_non_behavioral_ignored(self) -> None:
        """Non-behavioral commands (ruff, mypy) do not trigger behavioral enforcement.

        Validation results ARE provided for the non-behavioral commands so
        that ``validation_not_run`` is not triggered separately.
        """
        result = _make_result(
            validation_commands=["ruff check", "mypy"],
            validation_results=[
                {
                    "ok": True,
                    "command": "ruff check",
                    "exit_code": 0,
                    "output": "",
                }
            ],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        # No behavioral commands in the list → no enforcement triggered.
        bv = result["extras"].get("required_behavioral_validation", {})
        assert len(bv.get("skipped", [])) == 0

    def test_mixed_skipped_and_passed(self) -> None:
        """With both a skipped and a passed behavioral command, the skipped one
        still causes recoverable failure."""
        result = _make_result(
            validation_commands=["pytest", "npm run build"],
            validation_results=[
                {
                    "ok": True,
                    "command": "npm run build",
                    "exit_code": 0,
                    "output": "built",
                }
            ],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        assert result["ok"] is False
        assert result["recoverable"] is True
        assert result["status"] == WorkerOutcomeStatus.validation_failed.value
        bv = result["extras"].get("required_behavioral_validation", {})
        assert len(bv.get("passed", [])) == 1
        assert bv["passed"][0]["command"] == "npm run build"
        assert "pytest" in bv.get("skipped", [])

    def test_not_internal_harness_error(self) -> None:
        """Skipped behavioral commands must NOT be classified as internal/harness error."""
        result = _make_result(
            validation_commands=["pytest"],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        assert result["status"] != WorkerOutcomeStatus.harness_error.value
        assert result["recoverable"] is True

    def test_non_runnable_placeholder_excluded_from_enforcement(self) -> None:
        """Non-runnable placeholders such as 'python -m py_compile (touched files)'
        are excluded from behavioral required set."""
        result = _make_result(
            validation_commands=["python -m py_compile (touched files)", "pytest"],
            validation_results=[
                {
                    "ok": True,
                    "command": "pytest",
                    "exit_code": 0,
                    "output": "passed",
                }
            ],
            write_results=[{"path": "x.py", "applied": True, "is_new_file": True}],
            touched=["x.py"],
        )
        # py_compile placeholder should be filtered out; pytest passed
        assert result["ok"] is True
