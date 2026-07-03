"""WorkerEventHandler lifecycle guard tests.

Validates that:
- Duplicate workerStarted for the same active campaign does NOT call begin_assistant
- A truly new campaign (different tool_call_id) DOES call begin_assistant
- _active_worker_tool_call_id is cleared only on workerFinished, not between steps
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from aura.conversation.workflow_state import WorkflowState, WorkflowStatus


# ── Helpers ─────────────────────────────────────────────────────────────

def _make_handler():
    """Build a WorkerEventHandler with fully mocked dependencies."""
    from aura.gui.worker_handler import WorkerEventHandler

    bridge = MagicMock()
    chat = MagicMock()
    playground = MagicMock()
    # AppSettings mock — just need it to exist
    settings = MagicMock()
    parent = None

    handler = WorkerEventHandler(
        bridge=bridge,
        chat=chat,
        playground=playground,
        settings=settings,
        parent=parent,
    )
    return handler, bridge, chat, playground, settings


def _workflow_state(tool_call_id="dispatch-1", status=WorkflowStatus.dispatched):
    return WorkflowState.intent_captured(
        tool_call_id, "Test goal", summary="Test summary"
    ).with_status(status)


def _flush_pending_finish(handler):
    pending = handler._pending_worker_finish
    assert pending is not None
    handler._flush_pending_worker_finish(pending.tool_call_id, pending.generation)


# ── Tests ───────────────────────────────────────────────────────────────


class TestWorkerEventHandlerDuplicateGuard:
    """Duplicate workerStarted events must not call begin_assistant twice."""

    def test_first_worker_started_calls_begin_assistant(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")

        assert handler._active_worker_tool_call_id == "dispatch-1"
        playground.begin_assistant.assert_called_once()
        playground.set_glow_state.assert_called_once_with("coding")

    def test_duplicate_worker_started_skips_begin_assistant(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        # First call sets up
        handler._on_worker_started("dispatch-1")
        playground.begin_assistant.reset_mock()
        playground.set_glow_state.reset_mock()

        # Second call with same tool_call_id — must skip
        handler._on_worker_started("dispatch-1")

        playground.begin_assistant.assert_not_called()
        playground.set_glow_state.assert_not_called()
        # Active id should not change
        assert handler._active_worker_tool_call_id == "dispatch-1"

    def test_new_campaign_calls_begin_assistant_again(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        # First campaign
        handler._on_worker_started("dispatch-1")
        assert handler._active_worker_tool_call_id == "dispatch-1"
        playground.begin_assistant.assert_called_once()

        # Finish first campaign
        handler._on_worker_finished(
            "dispatch-1", ok=True, summary="Done", needs_followup=False, status="completed"
        )
        _flush_pending_finish(handler)
        assert handler._active_worker_tool_call_id is None

        # Second campaign — should call begin_assistant again
        playground.begin_assistant.reset_mock()
        handler._on_worker_started("dispatch-2")

        assert handler._active_worker_tool_call_id == "dispatch-2"
        playground.begin_assistant.assert_called_once()

    def test_active_id_cleared_only_on_worker_finished(self):
        handler, _bridge, _chat, _playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")
        assert handler._active_worker_tool_call_id == "dispatch-1"

        # Simulate multiple duplicate calls (internal step transitions)
        for _ in range(3):
            handler._on_worker_started("dispatch-1")
            assert handler._active_worker_tool_call_id == "dispatch-1"

        # workerFinished clears the guard
        handler._on_worker_finished(
            "dispatch-1", ok=True, summary="Done", needs_followup=False, status="completed"
        )
        _flush_pending_finish(handler)
        assert handler._active_worker_tool_call_id is None

    def test_internal_finish_followed_by_same_campaign_start_does_not_present(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()
        handler._finish_presenter.present = MagicMock()

        handler._on_worker_started("dispatch-1")
        handler._on_worker_finished(
            "dispatch-1",
            ok=True,
            summary="Internal step complete.",
            needs_followup=False,
            status="completed",
        )
        pending = handler._pending_worker_finish
        assert pending is not None

        handler._on_worker_started("dispatch-1")
        handler._flush_pending_worker_finish(pending.tool_call_id, pending.generation)

        handler._finish_presenter.present.assert_not_called()
        playground.begin_assistant.assert_called_once()
        assert handler._active_worker_tool_call_id == "dispatch-1"

    def test_aggregate_finish_presents_when_no_same_campaign_restart_arrives(self):
        handler, _bridge, _chat, _playground, _settings = _make_handler()
        handler._finish_presenter.present = MagicMock()

        handler._on_worker_started("dispatch-1")
        handler._on_worker_finished(
            "dispatch-1",
            ok=True,
            summary="Campaign complete.",
            needs_followup=False,
            status="completed",
        )
        _flush_pending_finish(handler)

        handler._finish_presenter.present.assert_called_once()
        assert handler._active_worker_tool_call_id is None

    def test_recoverable_worker_finish_does_not_clear_visible_spec_card(self):
        handler, _bridge, _chat, _playground, _settings = _make_handler()
        handler._dispatch_ui.clear_active_spec_card = MagicMock()
        handler._finish_presenter.present = MagicMock(
            return_value=SimpleNamespace(
                outcome=SimpleNamespace(should_clear_dispatch_card=False)
            )
        )

        handler._on_worker_started("dispatch-1")
        handler._on_worker_finished(
            "dispatch-1",
            ok=False,
            summary="Worker Error",
            needs_followup=True,
            status="harness_error",
        )
        _flush_pending_finish(handler)

        handler._dispatch_ui.clear_active_spec_card.assert_not_called()

    def test_terminal_worker_success_clears_visible_spec_card(self):
        handler, _bridge, _chat, _playground, _settings = _make_handler()
        handler._dispatch_ui.clear_active_spec_card = MagicMock()
        handler._finish_presenter.present = MagicMock(
            return_value=SimpleNamespace(
                outcome=SimpleNamespace(should_clear_dispatch_card=True)
            )
        )

        handler._on_worker_started("dispatch-1")
        handler._on_worker_finished(
            "dispatch-1",
            ok=True,
            summary="Done",
            needs_followup=False,
            status="completed",
        )
        _flush_pending_finish(handler)

        handler._dispatch_ui.clear_active_spec_card.assert_called_once_with("dispatch-1")

    def test_worker_api_error_ends_running_state_without_clearing_spec_card(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()
        handler._dispatch_ui.clear_active_spec_card = MagicMock()

        handler._on_worker_started("dispatch-1")
        handler._on_worker_api_error("dispatch-1", 500, "temporary failure")

        playground.add_error.assert_called_once()
        playground.stop_aura.assert_called_once()
        playground.set_worker_running.assert_called_with(False)
        handler._dispatch_ui.clear_active_spec_card.assert_not_called()
        assert handler._active_worker_tool_call_id is None

    def test_cancelled_clears_active_id(self):
        handler, _bridge, _chat, _playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")
        assert handler._active_worker_tool_call_id == "dispatch-1"

        handler._on_worker_cancelled("dispatch-1")
        assert handler._active_worker_tool_call_id is None

    def test_worker_finished_different_id_does_not_clear(self):
        """If a stale workerFinished arrives for a different id, don't clear
        the active guard for the current campaign."""
        handler, _bridge, _chat, _playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")
        assert handler._active_worker_tool_call_id == "dispatch-1"

        # A stale workerFinished for a different campaign
        handler._on_worker_finished(
            "dispatch-old", ok=True, summary="Old", needs_followup=False, status="completed"
        )
        # Guard must stay — we're still in dispatch-1
        assert handler._active_worker_tool_call_id == "dispatch-1"


class TestWorkerEventHandlerTodoStepLocal:
    """Worker TODO is step-local — replacement between steps is expected."""

    def test_todo_updated_routes_to_playground(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        items = [
            {"id": "a", "text": "Task A", "status": "done"},
            {"id": "b", "text": "Task B", "status": "active"},
            {"id": "c", "text": "Task C", "status": "pending"},
        ]
        handler._on_worker_todo_updated("dispatch-1", items)

        playground.update_worker_todo.assert_called_once_with(items, "dispatch-1")

    def test_todo_updated_does_not_trigger_begin_assistant(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")
        playground.begin_assistant.reset_mock()

        # TODO updates during campaign must not reset anything
        items = [
            {"id": "x", "text": "Step 2 task X", "status": "pending"},
            {"id": "y", "text": "Step 2 task Y", "status": "active"},
            {"id": "z", "text": "Step 2 task Z", "status": "pending"},
        ]
        handler._on_worker_todo_updated("dispatch-1", items)

        playground.begin_assistant.assert_not_called()
        playground.clear.assert_not_called()


class TestWorkerEventHandlerActivityAppendOnly:
    """Activity updates are append-only and do not reset the playground."""

    def test_activity_updated_does_not_trigger_begin_assistant(self):
        handler, _bridge, _chat, playground, _settings = _make_handler()

        handler._on_worker_started("dispatch-1")
        playground.begin_assistant.reset_mock()

        entries = [{"kind": "tool_started", "message": "Tool started: write_file"}]
        handler._on_worker_activity_updated("dispatch-1", entries)

        playground.begin_assistant.assert_not_called()
        playground.clear.assert_not_called()
