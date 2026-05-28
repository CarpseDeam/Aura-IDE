from __future__ import annotations

import json

from aura.conversation.dispatch import WorkerOutcomeStatus
from aura.conversation.workflow_state import (
    ValidationStatus,
    WorkflowState,
    WorkflowStatus,
)


def test_workflow_tracks_write_and_validation_result() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix login").with_status(
        WorkflowStatus.plan_ready
    )

    state = state.absorb_worker_tool_result(
        "write_file",
        True,
        json.dumps({"ok": True, "path": "aura/auth.py", "applied": True}),
    )
    state = state.absorb_worker_tool_result(
        "run_terminal_command",
        True,
        json.dumps({
            "command": "python -m py_compile aura/auth.py",
            "ok": True,
            "exit_code": 0,
        }),
    )

    assert state.status == WorkflowStatus.validating
    assert state.changed_files == ("aura/auth.py",)
    assert state.validation_status == ValidationStatus.passed
    assert state.validation_commands_run[0].command == "python -m py_compile aura/auth.py"


def test_workflow_finish_maps_retryable_failure() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix login")

    finished = state.finish(
        ok=False,
        summary="Validation failed - python -m py_compile aura/auth.py",
        needs_followup=True,
        status=WorkerOutcomeStatus.validation_failed.value,
    )

    assert finished.status == WorkflowStatus.failed_retryable
    assert finished.follow_up_required is True
    assert "follow-up" in finished.pending_user_action


def test_workflow_finish_maps_nonrecoverable_failure() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix login")

    finished = state.finish(
        ok=False,
        summary="Harness error - internal failure",
        needs_followup=False,
        status=WorkerOutcomeStatus.harness_error.value,
    )

    assert finished.status == WorkflowStatus.failed_nonrecoverable
    assert finished.follow_up_required is False
    assert finished.failure_reason.startswith("Harness error")
