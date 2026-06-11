"""Tests for WorkerEventHandler — worker lifecycle signal forwarding.

All Qt dependencies are mocked; no QApplication needed.
"""

from __future__ import annotations

from unittest.mock import Mock, patch

import pytest

from aura.conversation.workflow_state import WorkflowStatus
from aura.gui.worker_handler import WorkerEventHandler


# Fixtures


@pytest.fixture(scope="session")
def qapp():
    """Ensure a QApplication exists for widget tests."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def bridge() -> Mock:
    b = Mock()
    b.auto_dispatch = False
    # All worker signals as Mocks so .connect() can be tracked
    for sig_name in (
        "workerDispatchRequested",
        "workerStarted",
        "workerFinished",
        "workerCancelled",
        "workerReasoningDelta",
        "workerContentDelta",
        "workerToolCallStart",
        "workerToolCallArgs",
        "workerToolCallEnd",
        "workerToolResult",
        "workerDiffDecided",
        "workerApiError",
        "workerUsage",
        "workerTodoListUpdated",
        "workerTerminalOutput",
        "workerAgentProcessStarted",
        "workerAgentProcessOutput",
        "workerAgentProcessFinished",
        "terminalOutput",
    ):
        setattr(b, sig_name, Mock())
    return b


@pytest.fixture
def chat() -> Mock:
    return Mock()


@pytest.fixture
def playground() -> Mock:
    return Mock()


@pytest.fixture
def settings() -> Mock:
    return Mock()


@pytest.fixture
def spec_host() -> Mock:
    return Mock()


@pytest.fixture
def handler(
    bridge: Mock, chat: Mock, playground: Mock, settings: Mock
) -> WorkerEventHandler:
    return WorkerEventHandler(
        bridge=bridge,
        chat=chat,
        playground=playground,
        settings=settings,
        parent=None,
    )


def _mock_spec_card() -> Mock:
    card = Mock()
    for signal_name in (
        "dispatch_clicked",
        "edit_clicked",
        "cancel_clicked",
    ):
        setattr(card, signal_name, Mock(connect=Mock()))
    return card


# Worker lifecycle delegation


class TestWorkerLifecycle:
    """Verify that lifecycle signals are forwarded to chat/playground."""

    def test_worker_started_stops_chat_aura_and_starts_playground(
        self, handler: WorkerEventHandler, chat: Mock, playground: Mock
    ) -> None:
        handler._on_worker_started("tc1")
        chat.stop_current_aura.assert_called_once_with()
        playground.begin_assistant.assert_called_once_with()

    def test_worker_started_emits_worker_started_signal(
        self, handler: WorkerEventHandler,
    ) -> None:
        callback = Mock()
        handler.worker_started.connect(callback)
        handler._on_worker_started("tc1")
        callback.assert_called_once_with()

    def test_worker_finished_delegates_to_playground(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_finished("tc1", True, "done")
        playground.worker_finished.assert_called_once_with(True, "done", status=None)

    def test_recoverable_worker_followup_does_not_add_main_summary(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock, playground: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {"extras": {"recoverable": True}}

        handler._on_worker_finished(
            "tc1",
            False,
            "Worker needs one more pass.",
            needs_followup=True,
            status="needs_followup",
        )

        playground.worker_finished.assert_called_once_with(
            False,
            "Worker needs one more pass.",
            needs_followup=True,
            status="needs_followup",
        )
        chat.add_worker_summary.assert_not_called()

    def test_unrecoverable_worker_followup_still_adds_main_summary(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {"extras": {"recoverable": False}}

        handler._on_worker_finished(
            "tc1",
            False,
            "Worker blocked.",
            needs_followup=True,
            status="needs_followup",
        )

        chat.add_worker_summary.assert_called_once()

    def test_worker_cancelled_delegates_to_playground(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_cancelled("tc1")
        playground.worker_cancelled.assert_called_once_with()


# Worker content / reasoning


class TestWorkerContent:
    """Verify reasoning and content deltas are forwarded."""

    def test_worker_reasoning_delegates(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_reasoning("tc1", "thinking...")
        playground.append_reasoning.assert_called_once_with("thinking...")

    def test_worker_content_delegates(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_content("tc1", "some code")
        playground.append_content.assert_called_once_with("some code")


# Worker tool call flow


class TestWorkerToolCalls:
    """Verify tool call lifecycle methods are forwarded."""

    def test_worker_tool_call_flow_delegates(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        # Start
        handler._on_worker_tool_call_start("tc1", "wt1", "read_file")
        playground.add_tool_call.assert_called_once_with("wt1", "read_file")

        # Args
        handler._on_worker_tool_args("tc1", "wt1", '{"path":')
        playground.append_tool_args.assert_called_once_with("wt1", '{"path":')

        # Result
        handler._on_worker_tool_result("tc1", "wt1", "read_file", True, "content", {})
        playground.set_tool_result.assert_called_once_with("wt1", True, "content")


# Worker diff / error


class TestWorkerDiffError:
    """Verify diff, error slots."""

    def test_worker_diff_decided_delegates(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_diff_decided(
            "p1", "w1", "accept", "src/main.py", "old", "new", True,
        )
        playground.show_code_diff.assert_called_once_with(
            "w1", "src/main.py", "old", "new", "accept",
        )
        playground.add_diff_card.assert_not_called()

    def test_worker_api_error_formats_title(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_api_error("tc1", 429, "rate limited")
        playground.add_error.assert_called_once_with("API Error 429: rate limited")

    def test_worker_api_error_zero_status(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_api_error("tc1", 0, "connection failed")
        playground.add_error.assert_called_once_with("Worker Error: connection failed")


# Usage accumulation


class TestWorkerUsage:
    """Verify session usage accumulation and reset."""

    def test_worker_usage_accumulates_and_emits(
        self, handler: WorkerEventHandler,
    ) -> None:
        callback = Mock()
        handler.usage_updated.connect(callback)

        handler._on_worker_usage("tc1", "gpt-4", 100, 50, 10, 5)
        assert handler.session_usage == {"gpt-4": {"hit": 10, "miss": 5, "out": 50}}
        assert callback.call_count == 1

        handler._on_worker_usage("tc1", "gpt-4", 200, 100, 20, 10)
        assert handler.session_usage == {"gpt-4": {"hit": 30, "miss": 15, "out": 150}}
        assert callback.call_count == 2

        handler._on_worker_usage("tc1", "claude-3", 50, 25, 0, 0)
        assert handler.session_usage == {
            "gpt-4": {"hit": 30, "miss": 15, "out": 150},
            "claude-3": {"hit": 0, "miss": 50, "out": 25},
        }
        assert callback.call_count == 3

    def test_worker_usage_fallback(
        self, handler: WorkerEventHandler,
    ) -> None:
        """When hit=0 and miss=0, miss should default to prompt tokens."""
        handler._on_worker_usage("tc1", "model-x", 100, 30, 0, 0)
        assert handler.session_usage["model-x"]["miss"] == 100

    def test_reset_session_usage_clears_and_emits(
        self, handler: WorkerEventHandler,
    ) -> None:
        callback = Mock()
        handler.usage_updated.connect(callback)

        # Pre-populate
        handler._on_worker_usage("tc1", "gpt-4", 100, 50, 10, 5)
        assert len(handler.session_usage) == 1
        assert callback.call_count == 1

        handler.reset_session_usage()
        assert handler.session_usage == {}
        assert callback.call_count == 2


# Dispatch paths


class TestDispatch:
    """Auto-dispatch vs dialog-based dispatch."""

    def test_auto_dispatch_path(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        bridge.auto_dispatch = True
        handler._on_worker_dispatch_requested(
            "tc1", "goal text", ["f.py"], "spec text", "acc text", "",
        )
        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal text", ["f.py"], "spec text", "acc text", "",
        )

    def test_non_auto_dispatch_shows_card_and_waits(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.auto_dispatch = False
        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "",
        )
        chat.add_spec_card.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "",
        )
        bridge.user_dispatched.assert_not_called()
        bridge.user_cancelled_dispatch.assert_not_called()

    def test_non_auto_dispatch_then_dispatch_clicked(
        self, handler: WorkerEventHandler, chat: Mock, bridge: Mock,
    ) -> None:
        card = Mock()
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc", "")
        chat.get_spec_card.return_value = card
        handler._on_dispatch_clicked("tc1")
        chat.get_spec_card.assert_called_once_with("tc1")
        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "",
        )

    def test_non_auto_dispatch_then_cancel_clicked(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        handler._on_cancel_dispatch_clicked("tc1")
        bridge.user_cancelled_dispatch.assert_called_once_with("tc1")

    def test_spec_card_render_failure_unblocks_auto_dispatch(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.auto_dispatch = True
        chat.add_spec_card.side_effect = RuntimeError("bad markdown")

        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )

        chat.add_error.assert_called_once()
        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )
        bridge.user_cancelled_dispatch.assert_not_called()

    def test_spec_card_render_failure_cancels_manual_dispatch(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.auto_dispatch = False
        chat.add_spec_card.side_effect = RuntimeError("bad markdown")

        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )

        chat.add_error.assert_called_once()
        bridge.user_dispatched.assert_not_called()
        bridge.user_cancelled_dispatch.assert_called_once_with("tc1")


class TestActiveSpecHost:
    """Verify active Worker plans use the pinned host when available."""

    def test_dispatch_requested_uses_active_host_not_chat_footer(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock, spec_host: Mock
    ) -> None:
        card = _mock_spec_card()
        spec_host.add_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            spec_host=spec_host,
            parent=None,
        )

        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )

        chat.prepare_spec_card.assert_called_once_with("tc1")
        spec_host.add_spec_card.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )
        chat.add_spec_card.assert_not_called()
        card.dispatch_clicked.connect.assert_called_once_with(handler._on_dispatch_clicked)
        card.edit_clicked.connect.assert_called_once_with(handler._on_edit_spec_clicked)
        card.cancel_clicked.connect.assert_called_once_with(handler._on_cancel_dispatch_clicked)

    def test_dispatch_edit_cancel_wiring_still_uses_same_active_card(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock, spec_host: Mock
    ) -> None:
        card = _mock_spec_card()
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc", "")
        spec_host.get_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            spec_host=spec_host,
            parent=None,
        )

        handler._on_dispatch_clicked("tc1")
        handler._on_cancel_dispatch_clicked("tc1")

        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "",
        )
        bridge.user_cancelled_dispatch.assert_called_once_with("tc1")
        spec_host.get_spec_card.assert_called_with("tc1")

    def test_worker_lifecycle_updates_active_card(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock, spec_host: Mock
    ) -> None:
        card = _mock_spec_card()
        spec_host.get_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            spec_host=spec_host,
            parent=None,
        )

        handler._on_worker_started("tc1")
        handler._on_worker_finished("tc1", True, "done", status="completed")

        card.mark_worker_running.assert_called_once_with()
        card.worker_finished.assert_called_once_with(True, "done", status="completed")
        spec_host.remove_spec_card.assert_called_once_with("tc1")

    def test_auto_dispatch_from_active_host_still_dispatches(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock, spec_host: Mock
    ) -> None:
        bridge.auto_dispatch = True
        card = _mock_spec_card()
        spec_host.add_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            spec_host=spec_host,
            parent=None,
        )

        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )

        card.mark_dispatched.assert_called_once_with()
        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )

    def test_terminal_states_clear_active_host_card(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock, spec_host: Mock
    ) -> None:
        card = _mock_spec_card()
        spec_host.get_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            spec_host=spec_host,
            parent=None,
        )

        handler._on_worker_cancelled("tc1")
        handler._on_worker_api_error("tc2", 500, "boom")

        spec_host.remove_spec_card.assert_any_call("tc1")
        spec_host.remove_spec_card.assert_any_call("tc2")


# Signal wiring


class TestBridgeWiring:
    """Verify connect_bridge_signals wires all expected signals."""

    def test_connect_bridge_signals_wires_all(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        handler.connect_bridge_signals()

        expected_signals = [
            "workerDispatchRequested",
            "workerStarted",
            "workerFinished",
            "workerCancelled",
            "workerReasoningDelta",
            "workerContentDelta",
            "workerToolCallStart",
            "workerToolCallArgs",
            "workerToolCallEnd",
            "workerToolResult",
            "workerDiffDecided",
            "workerApiError",
            "workerUsage",
            "workerTodoListUpdated",
            "workerTerminalOutput",
            "workerAgentProcessStarted",
            "workerAgentProcessOutput",
            "workerAgentProcessFinished",
            "terminalOutput",
        ]
        for sig_name in expected_signals:
            sig = getattr(bridge, sig_name)
            sig.connect.assert_called_once()


# Terminal output routing


class TestTerminalOutput:
    """Verify terminal output routes to the correct component."""

    def test_terminal_output_routes_to_chat(
        self, handler: WorkerEventHandler, chat: Mock,
    ) -> None:
        handler._on_terminal_output("tc1", "build output")
        chat.append_terminal_output.assert_called_once_with("tc1", "build output")

    def test_worker_terminal_output_routes_to_playground(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_terminal_output("ptc", "wtc", "worker output")
        playground.append_terminal_output.assert_called_once_with("wtc", "worker output")

    def test_worker_agent_process_routes_to_playground_terminal(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_agent_process_started("ptc", "proc1", "Codex", "codex exec hi")
        handler._on_worker_agent_process_output("ptc", "proc1", "stream chunk")
        handler._on_worker_agent_process_finished("ptc", "proc1", 0)

        playground.start_terminal_process.assert_called_once_with("proc1", "codex exec hi")
        playground.append_terminal_output.assert_called_once_with("proc1", "stream chunk")
        playground.finish_terminal_process.assert_called_once_with("proc1", 0)


# Dispatch click / edit / cancel


class TestDispatchActions:
    """Verify dispatch-related slot actions."""

    def test_dispatch_clicked(
        self, handler: WorkerEventHandler, chat: Mock, bridge: Mock,
    ) -> None:
        card = Mock()
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc", "")
        chat.get_spec_card.return_value = card

        handler._on_dispatch_clicked("tc1")
        chat.get_spec_card.assert_called_once_with("tc1")
        bridge.user_dispatched.assert_called_once_with("tc1", "goal", ["f.py"], "spec", "acc", "")

    def test_dispatch_clicked_no_card(
        self, handler: WorkerEventHandler, chat: Mock, bridge: Mock,
    ) -> None:
        chat.get_spec_card.return_value = None
        handler._on_dispatch_clicked("tc1")
        bridge.user_dispatched.assert_not_called()

    def test_edit_spec_clicked(
        self, handler: WorkerEventHandler, chat: Mock,
    ) -> None:
        card = Mock()
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc", "")
        chat.get_spec_card.return_value = card

        with patch("aura.gui.spec_edit_dialog.SpecEditDialog") as mock_dlg:
            from PySide6.QtWidgets import QDialog
            mock_dlg.DialogCode.Accepted = QDialog.DialogCode.Accepted
            dlg_instance = mock_dlg.return_value
            dlg_instance.exec.return_value = QDialog.DialogCode.Accepted
            dlg_instance.goal.return_value = "new goal"
            dlg_instance.files.return_value = ["new.py"]
            dlg_instance.spec.return_value = "new spec"
            dlg_instance.acceptance.return_value = "new acc"
            dlg_instance.summary.return_value = ""

            handler._on_edit_spec_clicked("tc1")

        card.update_spec.assert_called_once_with(
            "new goal", ["new.py"], "new spec", "new acc", ""
        )

    def test_edit_spec_clicked_no_card(
        self, handler: WorkerEventHandler, chat: Mock,
    ) -> None:
        chat.get_spec_card.return_value = None
        handler._on_edit_spec_clicked("tc1")
        # Should not raise

    def test_cancel_dispatch_clicked(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        handler._on_cancel_dispatch_clicked("tc1")
        bridge.user_cancelled_dispatch.assert_called_once_with("tc1")

    def test_worker_todo_list_updated(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        tasks = [{"id": "1", "desc": "Task 1"}]
        handler._on_worker_todo_list_updated("tc1", tasks)
        playground.update_todo_list.assert_called_once_with(tasks)

    def test_active_workflow_tracks_worker_progress(
        self, bridge: Mock, chat: Mock, playground: Mock, settings: Mock,
    ) -> None:
        card = _mock_spec_card()
        card.current_spec.return_value = (
            "Fix auth",
            ["aura/auth.py"],
            "spec",
            "python -m py_compile aura/auth.py",
            "summary",
        )
        chat.add_spec_card.return_value = card
        chat.get_spec_card.return_value = card
        handler = WorkerEventHandler(
            bridge=bridge,
            chat=chat,
            playground=playground,
            settings=settings,
            parent=None,
        )

        handler._on_worker_dispatch_requested(
            "tc1",
            "Fix auth",
            ["aura/auth.py"],
            "spec",
            "python -m py_compile aura/auth.py",
            "summary",
        )
        assert handler.active_workflow is not None
        assert handler.active_workflow.status == WorkflowStatus.plan_ready

        handler._on_dispatch_clicked("tc1")
        assert handler.active_workflow.status == WorkflowStatus.dispatched

        handler._on_worker_tool_result(
            "tc1",
            "wt1",
            "write_file",
            True,
            '{"ok": true, "path": "aura/auth.py", "applied": true}',
            {},
        )
        assert handler.active_workflow.changed_files == ("aura/auth.py",)

        handler._on_worker_finished("tc1", True, "done", needs_followup=False, status="completed")
        assert handler.active_workflow.status == WorkflowStatus.done

    def test_current_spec_returns_five_values(self, qapp) -> None:
        """SpecCard.current_spec must return (goal, files, spec, acceptance, summary)."""
        from aura.gui.cards.spec_card import SpecCard
        card = SpecCard("tid", "goal", ["f.py"], "spec", "acc", summary="sum")
        result = card.current_spec()
        assert len(result) == 5
        assert result == ("goal", ["f.py"], "spec", "acc", "sum")

    def test_update_spec_updates_summary(self, qapp) -> None:
        """update_spec should update the summary field."""
        from aura.gui.cards.spec_card import SpecCard
        card = SpecCard("tid", "goal", ["f.py"], "spec", "acc", summary="old")
        card.update_spec("new goal", ["g.py"], "new spec", "new acc", summary="new sum")
        _, _, _, _, summary = card.current_spec()
        assert summary == "new sum"

    def test_spec_card_workflow_status_labels_match_active_states(self, qapp) -> None:
        from aura.gui.cards.spec_card import SpecCard

        assert (
            SpecCard._workflow_status_label(WorkflowStatus.plan_ready)[0]
            == "Awaiting dispatch"
        )
        assert SpecCard._workflow_status_label(WorkflowStatus.dispatched)[0] == "Running"
        assert SpecCard._workflow_status_label(WorkflowStatus.editing)[0] == "Editing"
        assert SpecCard._workflow_status_label(WorkflowStatus.validating)[0] == "Validating"
        assert SpecCard._workflow_status_label(WorkflowStatus.blocked)[0] == "Blocked"
        assert SpecCard._workflow_status_label(WorkflowStatus.planner_resolving)[0] == "Planner resolving mismatch"
        assert SpecCard._workflow_status_label(WorkflowStatus.failed_retryable)[0] == "Failed"
        assert SpecCard._workflow_status_label(WorkflowStatus.done)[0] == "Done"

    def test_finished_status_label_supports_needs_planner_resolution(self, qapp) -> None:
        from aura.conversation.dispatch import WorkerOutcomeStatus
        from aura.gui.cards.spec_card import SpecCard

        label, color = SpecCard._finished_status_label(False, "needs_planner_resolution")
        assert label == "Planner resolving mismatch"


class TestMismatchResolutionWiring:
    """Verify mismatch resolution card is wired correctly."""

    def test_mismatch_worker_finish_adds_resolution_card(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {
                "planner_resolution_needed": True,
                "mismatch_kind": "contract_gate",
                "mismatch_question": "Which field?",
            }
        }
        handler._on_worker_finished(
            "tc1", False, "Mismatch detected", needs_followup=True,
            status="needs_planner_resolution",
        )
        chat.add_mismatch_resolution_card.assert_called_once_with(
            "tc1", "contract_gate", "Which field?"
        )
        assert handler._active_mismatch_card_id == "tc1"

    def test_mismatch_worker_finish_suppresses_worker_summary(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {
                "planner_resolution_needed": True,
                "mismatch_kind": "contract_gate",
                "mismatch_question": "Which field?",
            }
        }
        handler._on_worker_finished(
            "tc1", False, "Mismatch", needs_followup=True,
            status="needs_planner_resolution",
        )
        chat.add_worker_summary.assert_not_called()

    def test_mismatch_via_status_string_only(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {}
        handler._on_worker_finished(
            "tc1", False, "Mismatch via status", needs_followup=True,
            status="needs_planner_resolution",
        )
        chat.add_mismatch_resolution_card.assert_called_once_with(
            "tc1", "", ""
        )
        assert handler._active_mismatch_card_id == "tc1"

    def test_mismatch_via_extras_flag_only(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {
                "planner_resolution_needed": True,
                "mismatch_kind": "missing_field",
            }
        }
        handler._on_worker_finished(
            "tc1", False, "Mismatch via extras", needs_followup=True,
            status="needs_followup",
        )
        chat.add_mismatch_resolution_card.assert_called_once_with(
            "tc1", "missing_field", ""
        )
        assert handler._active_mismatch_card_id == "tc1"

    def test_normal_needs_followup_does_not_add_mismatch_card(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {"recoverable": True}
        }
        handler._on_worker_finished(
            "tc1", False, "Needs another pass", needs_followup=True,
            status="needs_followup",
        )
        chat.add_mismatch_resolution_card.assert_not_called()
        assert handler._active_mismatch_card_id is None

    def test_normal_completed_does_not_add_mismatch_card(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {}
        handler._on_worker_finished(
            "tc1", True, "Done", needs_followup=False, status="completed",
        )
        chat.add_mismatch_resolution_card.assert_not_called()
        assert handler._active_mismatch_card_id is None

    def test_next_dispatch_resolves_previous_mismatch_card(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {
                "planner_resolution_needed": True,
                "mismatch_kind": "X",
                "mismatch_question": "?",
            }
        }
        handler._on_worker_finished(
            "tc1", False, "Mismatch", needs_followup=True,
            status="needs_planner_resolution",
        )
        assert handler._active_mismatch_card_id == "tc1"
        chat.mark_mismatch_resolved.reset_mock()
        handler._on_worker_dispatch_requested(
            "tc2", "goal", ["f.py"], "spec", "acc", "summary",
        )
        chat.mark_mismatch_resolved.assert_called_once_with("tc1")
        assert handler._active_mismatch_card_id is None

    def test_dispatch_without_mismatch_card_does_not_call_mark_resolved(
        self, handler: WorkerEventHandler, chat: Mock,
    ) -> None:
        assert handler._active_mismatch_card_id is None
        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )
        chat.mark_mismatch_resolved.assert_not_called()

    def test_mismatch_finish_calls_begin_planner_resolution_aura(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {
                "planner_resolution_needed": True,
                "mismatch_kind": "X",
                "mismatch_question": "?",
            }
        }
        handler._on_worker_finished(
            "tc1", False, "Mismatch", needs_followup=True,
            status="needs_planner_resolution",
        )
        assert chat.begin_planner_resolution_aura.called

    def test_normal_finish_does_not_call_begin_planner_resolution_aura(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {}
        handler._on_worker_finished(
            "tc1", True, "Done", needs_followup=False, status="completed",
        )
        assert not chat.begin_planner_resolution_aura.called

    def test_next_dispatch_after_mismatch_stops_chat_aura(
        self, handler: WorkerEventHandler, bridge: Mock, chat: Mock,
    ) -> None:
        bridge.worker_result_metadata.return_value = {
            "extras": {"planner_resolution_needed": True}
        }
        handler._on_worker_finished(
            "tc1", False, "M", needs_followup=True,
            status="needs_planner_resolution",
        )
        chat.stop_current_aura.reset_mock()
        handler._on_worker_dispatch_requested(
            "tc2", "goal", ["f.py"], "spec", "acc", "summary",
        )
        assert chat.stop_current_aura.called
        assert handler._active_mismatch_card_id is None

    def test_dispatch_without_mismatch_does_not_stop_chat_aura(
        self, handler: WorkerEventHandler, chat: Mock,
    ) -> None:
        assert handler._active_mismatch_card_id is None
        handler._on_worker_dispatch_requested(
            "tc1", "goal", ["f.py"], "spec", "acc", "summary",
        )
        chat.stop_current_aura.assert_not_called()
