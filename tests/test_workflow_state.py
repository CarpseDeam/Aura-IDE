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
    assert "continue" in finished.pending_user_action


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


def test_workflow_finish_maps_needs_planner_resolution() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix mismatch")

    finished = state.finish(
        ok=False,
        summary="Worker found a mismatch",
        needs_followup=True,
        status=WorkerOutcomeStatus.needs_planner_resolution.value,
    )

    assert finished.status == WorkflowStatus.planner_resolving
    assert finished.follow_up_required is True
    assert "Planner is resolving" in finished.pending_user_action


def test_workflow_finish_maps_planner_resolution_needed_extras() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix mismatch via extras")

    finished = state.finish(
        ok=False,
        summary="Worker found a mismatch via extras",
        needs_followup=True,
        status=WorkerOutcomeStatus.needs_followup.value,
        extras={"planner_resolution_needed": True},
    )

    assert finished.status == WorkflowStatus.planner_resolving
    assert finished.follow_up_required is True


def test_mismatch_question_becomes_blocker_reason() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix mismatch with question")

    finished = state.finish(
        ok=False,
        summary="Mismatch summary",
        needs_followup=True,
        status=WorkerOutcomeStatus.needs_planner_resolution.value,
        extras={"mismatch_question": "Should we use X or Y?"},
    )

    assert finished.status == WorkflowStatus.planner_resolving
    assert finished.blocker_reason == "Should we use X or Y?"
    assert finished.failure_reason == ""


def test_normal_needs_followup_still_maps_to_failed_retryable() -> None:
    state = WorkflowState.intent_captured("tc1", "Fix login")

    finished = state.finish(
        ok=False,
        summary="Validation still failing",
        needs_followup=True,
        status=WorkerOutcomeStatus.needs_followup.value,
    )

    assert finished.status == WorkflowStatus.failed_retryable
