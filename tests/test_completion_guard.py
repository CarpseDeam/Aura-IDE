"""Unit tests for worker_dispatch_is_terminal."""
from __future__ import annotations

from aura.conversation.completion_guard import worker_dispatch_is_terminal
from aura.conversation.dispatch import WorkerDispatchResult


def test_none_returns_false():
    assert worker_dispatch_is_terminal(None) is False


def test_completed_returns_terminal():
    result = WorkerDispatchResult(
        ok=True, summary="done", status="completed",
    )
    assert worker_dispatch_is_terminal(result) is True


def test_completed_with_caveats_returns_terminal():
    result = WorkerDispatchResult(
        ok=True,
        summary="done with caveats",
        status="completed_with_caveats",
    )
    assert worker_dispatch_is_terminal(result) is True


def test_cancelled_returns_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="cancelled", cancelled=True, status="cancelled",
    )
    assert worker_dispatch_is_terminal(result) is True


def test_cancelled_boolean_returns_terminal():
    """cancelled=True even without explicit status should be terminal."""
    result = WorkerDispatchResult(
        ok=False, summary="cancelled", cancelled=True,
    )
    assert worker_dispatch_is_terminal(result) is True


def test_approval_rejected_returns_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="rejected", status="approval_rejected",
    )
    assert worker_dispatch_is_terminal(result) is True


def test_nonrecoverable_harness_error_returns_terminal():
    result = WorkerDispatchResult(
        ok=False,
        summary="harness error",
        recoverable=False,
        extras={"worker_internal_error": True},
    )
    assert worker_dispatch_is_terminal(result) is True


def test_needs_followup_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="needs followup", needs_followup=True, recoverable=True,
    )
    assert worker_dispatch_is_terminal(result) is False


def test_recoverable_validation_failed_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="validation failed", recoverable=True,
        status="validation_failed",
    )
    assert worker_dispatch_is_terminal(result) is False


def test_phase_boundary_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="phase boundary", needs_followup=True,
        phase_boundary=True, recoverable=True,
    )
    assert worker_dispatch_is_terminal(result) is False


def test_recoverable_edit_mechanics_blocked_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="edit blocked", recoverable=True,
        status="edit_mechanics_blocked",
    )
    assert worker_dispatch_is_terminal(result) is False


def test_recoverable_craft_blocked_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="craft blocked", recoverable=True,
        status="craft_blocked",
    )
    assert worker_dispatch_is_terminal(result) is False


def test_scope_mismatch_returns_non_terminal():
    result = WorkerDispatchResult(
        ok=False, summary="scope mismatch", status="scope_mismatch",
    )
    assert worker_dispatch_is_terminal(result) is False


def test_happy_path_without_explicit_status_returns_terminal():
    """DispatchResult with ok=True and no explicit status infers as completed."""
    result = WorkerDispatchResult(
        ok=True, summary="done",
    )
    assert worker_dispatch_is_terminal(result) is True


def test_fallback_ok_false_unknown_status_returns_non_terminal():
    """Unknown status with ok=False falls back to result.ok."""
    result = WorkerDispatchResult(
        ok=False, summary="unknown", status="some_crazy_status",
    )
    assert worker_dispatch_is_terminal(result) is False
