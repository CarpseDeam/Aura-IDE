"""Tests for WorkerEventRelay final-report Activity suppression.

Verifies that the explicit ``suppress_final_report_activity`` flag is
threaded correctly and that internal DispatchSession steps do not emit
``WORKER_FINAL_REPORT_STARTED`` / ``WORKER_FINAL_REPORT_FINISHED`` on
the event bus, while non-internal steps still do.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aura.bridge.event_relay import WorkerEventRelay
from aura.bridge.worker_activity import WorkerActivityController
from aura.bridge.worker_relay_factory import create_worker_relay
from aura.client import (
    Done,
    ToolCallStart,
    ToolResult,
)
from aura.conversation import WorkerDispatchRequest, WorkerDispatchResult
from aura.events import (
    WORKER_FINAL_REPORT_FINISHED,
    WORKER_FINAL_REPORT_STARTED,
    WORKER_TOOL_FINISHED,
    WORKER_TOOL_STARTED,
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

        relay = create_worker_relay(
            approval_proxy=approval_proxy,
            worker_model="test-model",
            dispatch_proxy=dispatch_proxy,
            event_bus=EventBus(),
            suppress_final_report_activity=True,
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

    def test_workflow_projection_suppression_keeps_worker_ui_and_bus(
        self, approval_proxy,
    ) -> None:
        """Internal campaign steps suppress only WorkflowState projection."""
        bus = EventBus()
        received: list[AuraEvent] = []
        bus.subscribe(WORKER_TOOL_STARTED, received.append)
        bus.subscribe(WORKER_TOOL_FINISHED, received.append)
        dispatch_proxy = MagicMock()

        relay = create_worker_relay(
            approval_proxy=approval_proxy,
            worker_model="test-model",
            dispatch_proxy=dispatch_proxy,
            event_bus=bus,
            suppress_workflow_state_updates=True,
        )

        relay.relay("tc-1", ToolCallStart(
            index=0,
            id="worker-tool-1",
            name="write_file",
        ))
        relay.relay("tc-1", ToolResult(
            tool_call_id="worker-tool-1",
            name="write_file",
            ok=True,
            result='{"path": "a.py"}',
            extras={"path": "a.py"},
        ))

        dispatch_proxy.workerToolCallStart.assert_called_once()
        dispatch_proxy.workerToolResult.assert_called_once()
        dispatch_proxy._workflow_tool_started.assert_not_called()
        dispatch_proxy._workflow_tool_result.assert_not_called()
        assert [event.topic for event in received] == [
            WORKER_TOOL_STARTED,
            WORKER_TOOL_FINISHED,
        ]

    def test_workflow_projection_default_still_updates_workflow_callbacks(
        self, approval_proxy,
    ) -> None:
        """Single-step/default dispatch keeps Worker tool WorkflowState updates."""
        dispatch_proxy = MagicMock()
        relay = create_worker_relay(
            approval_proxy=approval_proxy,
            worker_model="test-model",
            dispatch_proxy=dispatch_proxy,
            event_bus=EventBus(),
        )

        relay.relay("tc-1", ToolCallStart(
            index=0,
            id="worker-tool-1",
            name="write_file",
        ))
        relay.relay("tc-1", ToolResult(
            tool_call_id="worker-tool-1",
            name="write_file",
            ok=True,
            result='{"path": "a.py"}',
            extras={"path": "a.py"},
        ))

        dispatch_proxy._workflow_tool_started.assert_called_once()
        dispatch_proxy._workflow_tool_result.assert_called_once()

    def test_run_worker_internal_threads_workflow_suppression(
        self, monkeypatch,
    ) -> None:
        """_run_worker_internal creates a runner with both internal-step flags."""
        from aura.bridge import dispatch as dispatch_module
        from aura.bridge.dispatch import _DispatchProxy

        captured: dict[str, object] = {}
        expected = WorkerDispatchResult(ok=True, summary="done")

        class DummyRunner:
            def __init__(self, **kwargs):
                captured.update(kwargs)

            def run_worker(self, tool_call_id, req, pending, record_replayable=True):
                captured["run_worker_args"] = (
                    tool_call_id,
                    req,
                    pending,
                    record_replayable,
                )
                return expected

        monkeypatch.setattr(dispatch_module, "WorkerDispatchRunner", DummyRunner)
        proxy = _DispatchProxy(
            parent_widget=None,
            registry_factory=MagicMock(),
            approval_proxy=MagicMock(),
        )
        req = WorkerDispatchRequest(
            goal="g",
            files=[],
            spec="s",
            acceptance="a",
            summary="summary",
        )
        pending = SimpleNamespace()

        result = proxy._run_worker_internal("tc-1", req, pending)

        assert result is expected
        assert captured["suppress_final_report_activity"] is True
        assert captured["suppress_workflow_state_updates"] is True
        assert captured["run_worker_args"] == ("tc-1", req, pending, False)


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


# ── WorkerEventHandler lifecycle handling ────────────────────────────────


class TestWorkerEventHandlerLifecycle:
    """Verify WorkerEventHandler lifecycle signal handling."""

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

    def test_on_worker_started_runs_start_choreography(self, handler) -> None:
        """_on_worker_started runs the visible start choreography."""
        handler._on_worker_started("tc-1")

        assert handler._playground.begin_assistant.call_count == 1, (
            "begin_assistant must be called for the workerStarted signal"
        )
        handler._chat._remove_plan_writer_card.assert_called_once_with("tc-1")

    def test_on_worker_started_duplicate_same_campaign_does_not_clear_again(self, handler) -> None:
        """Duplicate workerStarted for the active campaign is a no-op for reset choreography."""
        handler._on_worker_started("tc-1")
        handler._on_worker_started("tc-1")

        assert handler._playground.begin_assistant.call_count == 1
        assert handler._chat.stop_current_aura.call_count == 1
        assert handler._chat._remove_plan_writer_card.call_count == 1

    def test_on_worker_started_different_ids_not_blocked(self, handler) -> None:
        """_on_worker_started for different tool_call_ids both proceed."""
        handler._on_worker_started("tc-1")
        handler._on_worker_started("tc-2")

        assert handler._playground.begin_assistant.call_count == 2, (
            "begin_assistant must be called for each distinct tool_call_id"
        )

    def test_on_worker_finished_runs_finish_presentation(self, handler) -> None:
        """_on_worker_finished presents the worker result."""
        handler._on_worker_finished("tc-1", True, "all good")

        assert handler._finish_presenter.present.call_count == 1, (
            "present must be called for the workerFinished signal"
        )

    def test_on_worker_finished_different_ids_not_blocked(self, handler) -> None:
        """_on_worker_finished for different tool_call_ids both proceed."""
        handler._on_worker_finished("tc-1", True, "done")
        handler._on_worker_finished("tc-2", True, "done")

        assert handler._finish_presenter.present.call_count == 2, (
            "present must be called for each distinct tool_call_id"
        )

    def test_workflow_state_plan_writer_updates_only_before_dispatch(self, handler) -> None:
        """Only plan_ready WorkflowState snapshots update PlanWriterCard."""
        from aura.conversation.workflow_state import WorkflowState, WorkflowStatus

        plan_card = MagicMock()
        plan_card.update_workflow_state = MagicMock()
        handler._dispatch_ui.get_spec_card = MagicMock(return_value=None)
        handler._chat.get_plan_writer_card = MagicMock(return_value=plan_card)

        state_before = WorkflowState(
            tool_call_id="tc-1",
            task_title="test",
            user_intent_summary="test",
            status=WorkflowStatus.plan_ready,
        )
        handler._on_workflow_state_changed(state_before)

        assert plan_card.update_workflow_state.call_count == 1, (
            "PlanWriterCard must be updated for plan_ready snapshots"
        )

        state_after = WorkflowState(
            tool_call_id="tc-1",
            task_title="test",
            user_intent_summary="test",
            status=WorkflowStatus.dispatched,
        )
        handler._on_workflow_state_changed(state_after)

        assert plan_card.update_workflow_state.call_count == 1, (
            "PlanWriterCard must not be updated for dispatched snapshots"
        )

    def test_workflow_state_plan_ready_updates_matching_plan_card(self, handler) -> None:
        """plan_ready snapshots update the matching PlanWriterCard."""
        from aura.conversation.workflow_state import WorkflowState, WorkflowStatus

        plan_card_b = MagicMock()
        plan_card_b.update_workflow_state = MagicMock()

        def _get_plan_card(tid: str):
            return {"tc-b": plan_card_b}.get(tid)

        handler._chat.get_plan_writer_card = _get_plan_card

        state_b = WorkflowState(
            tool_call_id="tc-b",
            task_title="other task",
            user_intent_summary="other",
            status=WorkflowStatus.plan_ready,
        )
        handler._on_workflow_state_changed(state_b)

        assert plan_card_b.update_workflow_state.call_count == 1, (
            "PlanWriterCard for tc-b must be updated for its plan_ready snapshot"
        )


# ── Dispatch identity propagation ──────────────────────────────────────────


class TestDispatchIdentityPropagation:
    """WorkerEventRelay must propagate dispatch identity to EventBus facts.

    Every AuraEvent emitted by ``_emit_bus_event`` during ``relay()`` must
    carry the parent dispatch ``tool_call_id`` as ``run_id`` and
    ``campaign_id``, while preserving the inner Worker tool id in
    ``payload["tool_call_id"]``.
    """

    def test_tool_started_carries_dispatch_identity(
        self, approval_proxy, event_bus,
    ) -> None:
        """WORKER_TOOL_STARTED carries run_id/campaign_id == dispatch id."""
        received: list[AuraEvent] = []
        event_bus.subscribe(WORKER_TOOL_STARTED, received.append)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=event_bus,
        )

        relay.relay("dispatch-tc-1", ToolCallStart(
            index=0, id="worker-tool-1", name="read_file",
        ))

        assert len(received) == 1
        assert received[0].run_id == "dispatch-tc-1"
        assert received[0].campaign_id == "dispatch-tc-1"

    def test_tool_finished_carries_dispatch_identity(
        self, approval_proxy, event_bus,
    ) -> None:
        """WORKER_TOOL_FINISHED carries run_id/campaign_id == dispatch id."""
        received: list[AuraEvent] = []
        event_bus.subscribe(WORKER_TOOL_FINISHED, received.append)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=event_bus,
        )

        relay.relay("dispatch-tc-2", ToolResult(
            tool_call_id="worker-tool-2",
            name="write_file",
            ok=True,
            result='{"path": "a.py"}',
            extras={},
        ))

        assert len(received) == 1
        assert received[0].run_id == "dispatch-tc-2"
        assert received[0].campaign_id == "dispatch-tc-2"

    def test_payload_tool_call_id_is_worker_id_not_dispatch_id(
        self, approval_proxy, event_bus,
    ) -> None:
        """payload[tool_call_id] stays as the Worker tool id, not the
        dispatch id — the two namespaces are distinct."""
        received: list[AuraEvent] = []
        event_bus.subscribe(WORKER_TOOL_STARTED, received.append)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=event_bus,
        )

        relay.relay("dispatch-tc-3", ToolCallStart(
            index=0, id="worker-inner-id", name="run_terminal_command",
        ))

        assert len(received) == 1
        # payload["tool_call_id"] is the Worker tool id
        assert received[0].payload.get("tool_call_id") == "worker-inner-id"
        # run_id/campaign_id is the dispatch id
        assert received[0].run_id == "dispatch-tc-3"
        assert received[0].campaign_id == "dispatch-tc-3"

    def test_all_worker_topics_carry_identity(
        self, approval_proxy, event_bus,
    ) -> None:
        """Multiple relay() calls — each with a different dispatch id —
        carry the correct identity on their EventBus emissions."""
        received: list[AuraEvent] = []
        event_bus.subscribe(WORKER_TOOL_STARTED, received.append)
        event_bus.subscribe(WORKER_TOOL_FINISHED, received.append)
        relay = WorkerEventRelay(
            approval_proxy=approval_proxy,
            event_bus=event_bus,
        )

        relay.relay("dispatch-first", ToolCallStart(
            index=0, id="wt-1", name="read_file",
        ))
        relay.relay("dispatch-second", ToolCallStart(
            index=1, id="wt-2", name="write_file",
        ))

        assert len(received) == 2
        assert received[0].run_id == "dispatch-first"
        assert received[0].campaign_id == "dispatch-first"
        assert received[1].run_id == "dispatch-second"
        assert received[1].campaign_id == "dispatch-second"

        relay.relay("dispatch-second", ToolResult(
            tool_call_id="wt-2",
            name="write_file",
            ok=True,
            result='{"path": "b.py"}',
            extras={},
        ))
        assert len(received) == 3
        assert received[2].run_id == "dispatch-second"
        assert received[2].campaign_id == "dispatch-second"
