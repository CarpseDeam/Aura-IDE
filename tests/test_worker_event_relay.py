"""Tests for WorkerEventRelay final-report Activity suppression.

Verifies that the explicit ``suppress_final_report_activity`` flag is
threaded correctly and that internal DispatchSession steps do not emit
``WORKER_FINAL_REPORT_STARTED`` / ``WORKER_FINAL_REPORT_FINISHED`` on
the event bus, while non-internal steps still do.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_activity import WorkerActivityController
from aura.bridge.worker_relay_factory import create_worker_relay
from aura.client import Done
from aura.events import (
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    AuraEvent,
    EventBus,
)


class _ApprovalProxy:
    """Minimal approval proxy stub (no Qt, no real approval flow)."""
    def consume_last_event(self):
        return None


@pytest.fixture
def approval_proxy() -> _ApprovalProxy:
    return _ApprovalProxy()


@pytest.fixture
def event_bus() -> EventBus:
    return EventBus()


# ── suppress_final_report_activity = True ────────────────────────────────


class TestSuppressFinalReportActivity:
    """Done(stop) with suppress_final_report_activity=True must NOT emit
    WORKER_FINAL_REPORT_STARTED / WORKER_FINAL_REPORT_FINISHED."""

    def test_suppressed_flag_blocks_final_report_activity(
        self, approval_proxy, event_bus
    ) -> None:
        """When suppress_final_report_activity=True, Done(stop) does not
        emit final-report activity events."""
        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_final_report_activity=True,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Task complete"},
        ))

        entries = ctrl.snapshot()
        final_report_kinds = {"final_report_started", "final_report_completed", "final_report_failed"}
        match = [e for e in entries if e.kind in final_report_kinds]
        assert not match, (
            f"Expected no final-report activity entries when "
            f"suppress_final_report_activity=True, got: {[e.kind for e in match]}"
        )

    def test_suppressed_flag_still_records_final_report_text(
        self, approval_proxy, event_bus
    ) -> None:
        """Even when suppress_final_report_activity=True, the final report
        content is still captured (only Activity emission is suppressed)."""
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_final_report_activity=True,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "I finished the work."},
        ))

        assert relay.final_report_text == "I finished the work.", (
            "final_report_text must still be captured even when "
            "final-report Activity is suppressed"
        )

    def test_non_stop_done_unaffected_by_suppression(
        self, approval_proxy, event_bus
    ) -> None:
        """Done with finish_reason='tool_calls' should never emit final-report
        activity, regardless of the suppression flag."""
        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_final_report_activity=True,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="tool_calls",
            full_message={"role": "assistant", "content": None, "tool_calls": []},
        ))

        entries = ctrl.snapshot()
        final_report_kinds = {"final_report_started", "final_report_completed", "final_report_failed"}
        match = [e for e in entries if e.kind in final_report_kinds]
        assert not match, (
            f"Done(tool_calls) must not emit final-report activity even "
            f"when suppression flag is True, got: {[e.kind for e in match]}"
        )


# ── suppress_final_report_activity = False (default) ─────────────────────


class TestDefaultFinalReportActivity:
    """With default suppress_final_report_activity=False, Done(stop) MUST
    emit final-report activity entries."""

    def test_default_emits_final_report_activity(
        self, approval_proxy, event_bus
    ) -> None:
        """Done(stop) with default settings emits both started and finished
        final-report activity entries."""
        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Done."},
        ))

        entries = ctrl.snapshot()
        kinds = [e.kind for e in entries]

        assert "final_report_started" in kinds, (
            "Expected final_report_started in activity entries when "
            "suppress_final_report_activity=False (default)"
        )
        assert "final_report_completed" in kinds, (
            "Expected final_report_completed in activity entries when "
            "suppress_final_report_activity=False (default)"
        )

    def test_explicit_false_emits_final_report_activity(
        self, approval_proxy, event_bus
    ) -> None:
        """Same as above but with explicit suppress_final_report_activity=False."""
        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_final_report_activity=False,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Work complete."},
        ))

        entries = ctrl.snapshot()
        kinds = [e.kind for e in entries]

        assert "final_report_started" in kinds
        assert "final_report_completed" in kinds

    def test_explicit_false_still_emits_ok_finish_reason(
        self, approval_proxy, event_bus
    ) -> None:
        """The payload of the FINISHED event includes the finish_reason."""
        received_events: list[AuraEvent] = []

        def capture(ev: AuraEvent) -> None:
            received_events.append(ev)

        event_bus.subscribe(WORKER_FINAL_REPORT_FINISHED, capture)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_final_report_activity=False,
            event_bus=event_bus,
        )

        relay.relay("tool-call-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "all good"},
        ))

        finished_events = [
            e for e in received_events
            if e.topic == WORKER_FINAL_REPORT_FINISHED
        ]
        assert len(finished_events) == 1
        payload = finished_events[0].payload
        assert payload.get("ok") is True
        assert payload.get("finish_reason") == "stop"


# ── Flag threading ───────────────────────────────────────────────────────


class TestFlagThreading:
    """Verify suppress_final_report_activity reaches WorkerEventRelay."""

    def test_flag_threaded_via_create_worker_relay(self, approval_proxy) -> None:
        """create_worker_relay passes suppress_final_report_activity to
        WorkerEventRelay."""
        dispatch_proxy = MagicMock()
        todo_relay_callback = MagicMock()

        relay = create_worker_relay(
            approval_proxy=approval_proxy,
            worker_model="test-model",
            dispatch_proxy=dispatch_proxy,
            todo_relay_callback=todo_relay_callback,
            suppress_todo_updates=False,
            suppress_final_report_activity=True,
            event_bus=None,
        )

        # Verify the flag reached the relay via behavioral test:
        bus = EventBus()
        ctrl = WorkerActivityController(bus)
        relay._event_bus = bus
        relay.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "no final report"},
        ))
        kinds = {e.kind for e in ctrl.snapshot()}
        assert "final_report_started" not in kinds, (
            "create_worker_relay with suppress_final_report_activity=True "
            "must create a relay that suppresses final-report Activity"
        )

    def test_flag_defaults_to_false(self, approval_proxy) -> None:
        """suppress_final_report_activity defaults to False in all callers."""
        # Behavioral test: default relay does emit final-report activity
        bus = EventBus()
        ctrl = WorkerActivityController(bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=bus,
        )
        relay.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "x"},
        ))
        kinds = [e.kind for e in ctrl.snapshot()]
        assert "final_report_started" in kinds, (
            "Default must emit final-report activity"
        )


# ── suppress_todo_updates decoupling ─────────────────────────────────────


class TestTodoDecoupling:
    """Verify suppress_todo_updates and suppress_final_report_activity are
    independent flags."""

    def test_suppress_todo_alone_does_not_block_final_report_activity(
        self, approval_proxy, event_bus
    ) -> None:
        """suppress_todo_updates=True alone must NOT block final-report
        Activity — only suppress_final_report_activity should do that."""
        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_todo_updates=True,        # TODO suppressed
            suppress_final_report_activity=False,  # final-report NOT suppressed
            event_bus=event_bus,
        )

        relay.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "work done"},
        ))

        kinds = [e.kind for e in ctrl.snapshot()]
        assert "final_report_started" in kinds, (
            "suppress_todo_updates alone must not suppress final-report Activity"
        )
        assert "final_report_completed" in kinds

    def test_final_report_flag_alone_does_blocks_final_report(
        self, approval_proxy, event_bus
    ) -> None:
        """suppress_final_report_activity=True alone (without
        suppress_todo_updates) blocks final-report Activity."""

        ctrl = WorkerActivityController(event_bus)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_todo_updates=False,             # TODO NOT suppressed
            suppress_final_report_activity=True,      # final-report suppressed
            event_bus=event_bus,
        )

        relay.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "work done"},
        ))

        kinds = {e.kind for e in ctrl.snapshot()}
        assert "final_report_started" not in kinds, (
            "suppress_final_report_activity=True must block final-report "
            "Activity even when suppress_todo_updates=False"
        )
        assert "final_report_completed" not in kinds


# ── EventBus integration: both flags in combination ──────────────────────


class TestEventBusIntegration:
    """End-to-end verification that internal dispatch steps don't leak
    final-report Activity onto the event bus."""

    def test_three_step_campaign_no_leak(self, approval_proxy) -> None:
        """Simulate a 3-step campaign: internal steps do not emit
        final-report Activity, only the final one does."""
        bus = EventBus()
        ctrl = WorkerActivityController(bus)

        # Step 1 (internal)
        relay1 = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_todo_updates=True,
            suppress_final_report_activity=True,
            event_bus=bus,
        )
        relay1.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Step 1 done"},
        ))

        # Step 2 (internal)
        relay2 = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_todo_updates=True,
            suppress_final_report_activity=True,
            event_bus=bus,
        )
        relay2.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Step 2 done"},
        ))

        # Step 3 (final — no suppression flags)
        relay3 = WorkerEventRelay(
            approval_proxy=approval_proxy,
            suppress_todo_updates=False,
            suppress_final_report_activity=False,
            event_bus=bus,
        )
        relay3.relay("tc-1", Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Step 3 done"},
        ))

        # Check: only one set of final-report entries
        entries = ctrl.snapshot()
        final_report_events = [
            e for e in entries
            if e.kind in ("final_report_started", "final_report_completed", "final_report_failed")
        ]
        assert len(final_report_events) == 2, (
            f"Expected exactly 2 final-report entries (started + completed) "
            f"across 3 steps, got {len(final_report_events)}: "
            f"{[e.kind for e in final_report_events]}"
        )
        assert final_report_events[0].kind == "final_report_started"
        assert final_report_events[1].kind == "final_report_completed"


# ── WorkerEventHandler lifecycle idempotency ─────────────────────────────


class TestWorkerEventHandlerIdempotency:
    """Verify that WorkerEventHandler lifecycle guards fire exactly once
    per tool_call_id.

    These tests use MagicMock for Qt dependencies and only exercise the
    idempotency logic, not full signal routing.
    """

    @pytest.fixture
    def handler(self):
        """Create a WorkerEventHandler with mocked dependencies."""
        from unittest.mock import MagicMock
        from aura.gui.worker_handler import WorkerEventHandler

        bridge = MagicMock()
        chat = MagicMock()
        playground = MagicMock()
        settings = MagicMock()

        h = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            parent=None,
        )
        # Playpen for counting calls — wrap real methods with MagicMock
        playground.begin_assistant = MagicMock()
        playground.worker_finished = MagicMock()
        chat._remove_plan_writer_card = MagicMock()
        chat.get_plan_writer_card = MagicMock(return_value=None)
        # Wrap finish_presenter.present with a MagicMock so we can count calls
        h._finish_presenter.present = MagicMock()
        # Wrap dispatch_ui methods used in tests
        h._dispatch_ui.get_spec_card = MagicMock(return_value=None)
        return h

    def test_on_worker_started_idempotent(self, handler) -> None:
        """_on_worker_started calls playground.begin_assistant only once
        per tool_call_id."""
        handler._on_worker_started("tc-1")
        handler._on_worker_started("tc-1")  # second call — same id

        assert handler._playground.begin_assistant.call_count == 1, (
            "begin_assistant must be called exactly once for the same tool_call_id"
        )
        assert "tc-1" in handler._initialized_worker_campaigns

    def test_on_worker_started_different_ids_not_blocked(self, handler) -> None:
        """_on_worker_started for different tool_call_ids both proceed."""
        handler._on_worker_started("tc-1")
        handler._on_worker_started("tc-2")

        assert handler._playground.begin_assistant.call_count == 2, (
            "begin_assistant must be called for each distinct tool_call_id"
        )

    def test_on_worker_finished_idempotent(self, handler) -> None:
        """_on_worker_finished calls finish_presenter.present only once
        per tool_call_id."""
        # Must mark as initialized first (the guard requires it)
        handler._initialized_worker_campaigns.add("tc-1")

        handler._on_worker_finished("tc-1", True, "all good")
        handler._on_worker_finished("tc-1", True, "all good")  # second call

        assert handler._finish_presenter.present.call_count == 1, (
            "present must be called exactly once for the same tool_call_id"
        )
        assert "tc-1" in handler._finalized_worker_campaigns

    def test_on_worker_finished_different_ids_not_blocked(self, handler) -> None:
        """_on_worker_finished for different tool_call_ids both proceed."""
        handler._initialized_worker_campaigns.add("tc-1")
        handler._initialized_worker_campaigns.add("tc-2")

        handler._on_worker_finished("tc-1", True, "done")
        handler._on_worker_finished("tc-2", True, "done")

        assert handler._finish_presenter.present.call_count == 2, (
            "present must be called for each distinct tool_call_id"
        )

    def test_workflow_state_plan_writer_guard(self, handler) -> None:
        """WorkflowState changes after Worker start must not update
        PlanWriterCard for that tool_call_id."""
        from aura.conversation.workflow_state import WorkflowState, WorkflowStatus

        plan_card = MagicMock()
        plan_card.update_workflow_state = MagicMock()
        handler._dispatch_ui.get_spec_card = MagicMock(return_value=None)
        handler._chat.get_plan_writer_card = MagicMock(return_value=plan_card)

        # Before Worker start — PlanWriterCard should be updated
        state_before = WorkflowState(
            tool_call_id="tc-1",
            task_title="test",
            user_intent_summary="test",
            status=WorkflowStatus.plan_ready,
        )
        handler._on_workflow_state_changed(state_before)

        assert plan_card.update_workflow_state.call_count == 1, (
            "PlanWriterCard must be updated before Worker start"
        )

        # Simulate Worker start
        handler._initialized_worker_campaigns.add("tc-1")

        # After Worker start — PlanWriterCard must NOT be updated
        state_after = WorkflowState(
            tool_call_id="tc-1",
            task_title="test",
            user_intent_summary="test",
            status=WorkflowStatus.dispatched,
        )
        handler._on_workflow_state_changed(state_after)

        assert plan_card.update_workflow_state.call_count == 1, (
            "PlanWriterCard must NOT be updated after Worker start for same tool_call_id"
        )

    def test_workflow_state_diff_tool_call_id_not_blocked(self, handler) -> None:
        """WorkflowState for a different tool_call_id must update
        PlanWriterCard even if one tool_call_id has started."""
        from aura.conversation.workflow_state import WorkflowState, WorkflowStatus

        plan_card_b = MagicMock()
        plan_card_b.update_workflow_state = MagicMock()

        def _get_plan_card(tid: str):
            return {"tc-b": plan_card_b}.get(tid)

        handler._chat.get_plan_writer_card = _get_plan_card

        # Worker started for tc-a
        handler._initialized_worker_campaigns.add("tc-a")

        # WorkflowState for tc-b (different id) — should still update
        state_b = WorkflowState(
            tool_call_id="tc-b",
            task_title="other task",
            user_intent_summary="other",
            status=WorkflowStatus.plan_ready,
        )
        handler._on_workflow_state_changed(state_b)

        assert plan_card_b.update_workflow_state.call_count == 1, (
            "PlanWriterCard for tc-b must be updated even after tc-a has started"
        )

    def test_reset_session_usage_clears_lifecycle_guards(self, handler) -> None:
        """reset_session_usage() clears both lifecycle guard sets so
        a new conversation can reuse tool_call_ids."""
        handler._initialized_worker_campaigns.add("tc-1")
        handler._finalized_worker_campaigns.add("tc-1")

        handler.reset_session_usage()

        assert len(handler._initialized_worker_campaigns) == 0, (
            "reset_session_usage must clear _initialized_worker_campaigns"
        )
        assert len(handler._finalized_worker_campaigns) == 0, (
            "reset_session_usage must clear _finalized_worker_campaigns"
        )
