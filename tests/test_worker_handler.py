"""Tests for WorkerEventHandler — worker lifecycle signal forwarding.

All Qt dependencies are mocked; no QApplication needed.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, patch

import pytest

from aura.gui.worker_handler import WorkerEventHandler


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Worker lifecycle delegation
# ---------------------------------------------------------------------------


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
        playground.worker_finished.assert_called_once_with(True, "done")

    def test_worker_cancelled_delegates_to_playground(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_cancelled("tc1")
        playground.worker_cancelled.assert_called_once_with()


# ---------------------------------------------------------------------------
# Worker content / reasoning
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Worker tool call flow
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Worker diff / error / view
# ---------------------------------------------------------------------------


class TestWorkerDiffError:
    """Verify diff, error, and placeholder slots."""

    def test_worker_diff_decided_delegates(
        self, handler: WorkerEventHandler, playground: Mock,
    ) -> None:
        handler._on_worker_diff_decided(
            "p1", "w1", "accept", "src/main.py", "old", "new", True,
        )
        playground.add_diff_card.assert_called_once_with(
            "w1", "src/main.py", "old", "new", "accept", True,
        )

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

    def test_view_worker_clicked_is_noop(
        self, handler: WorkerEventHandler,
    ) -> None:
        # Should not raise and do nothing
        handler._on_view_worker_clicked("tc1")


# ---------------------------------------------------------------------------
# Usage accumulation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dispatch paths
# ---------------------------------------------------------------------------


class TestDispatch:
    """Auto-dispatch vs dialog-based dispatch."""

    def test_auto_dispatch_path(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        bridge.auto_dispatch = True

        with patch("aura.gui.spec_edit_dialog.SpecApprovalDialog") as mock_dlg:
            handler._on_worker_dispatch_requested(
                "tc1", "goal text", ["f.py"], "spec text", "acc text",
            )

        bridge.user_dispatched.assert_called_once_with(
            "tc1", "goal text", ["f.py"], "spec text", "acc text",
        )
        mock_dlg.assert_not_called()

    def test_dialog_dispatch_path_accepted(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        bridge.auto_dispatch = False

        with patch("aura.gui.spec_edit_dialog.SpecApprovalDialog") as mock_dlg:
            dlg_instance = mock_dlg.return_value
            from PySide6.QtWidgets import QDialog
            dlg_instance.exec.return_value = QDialog.DialogCode.Accepted

            dlg_instance.goal.return_value = "edited goal"
            dlg_instance.files.return_value = ["f1.py", "f2.py"]
            dlg_instance.spec.return_value = "edited spec"
            dlg_instance.acceptance.return_value = "edited acceptance"

            handler._on_worker_dispatch_requested(
                "tc1", "goal text", ["f.py"], "spec text", "acc text",
            )

        mock_dlg.assert_called_once_with(
            "goal text", ["f.py"], "spec text", "acc text", parent=handler,
        )
        bridge.user_dispatched.assert_called_once_with(
            "tc1", "edited goal", ["f1.py", "f2.py"], "edited spec", "edited acceptance",
        )
        bridge.user_cancelled_dispatch.assert_not_called()

    def test_dialog_dispatch_path_rejected(
        self, handler: WorkerEventHandler, bridge: Mock,
    ) -> None:
        bridge.auto_dispatch = False

        with patch("aura.gui.spec_edit_dialog.SpecApprovalDialog") as mock_dlg:
            dlg_instance = mock_dlg.return_value
            dlg_instance.exec.return_value = 0  # Rejected

            handler._on_worker_dispatch_requested(
                "tc1", "goal text", ["f.py"], "spec text", "acc text",
            )

        bridge.user_dispatched.assert_not_called()
        bridge.user_cancelled_dispatch.assert_called_once_with("tc1")


# ---------------------------------------------------------------------------
# Signal wiring
# ---------------------------------------------------------------------------


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
            "terminalOutput",
        ]
        for sig_name in expected_signals:
            sig = getattr(bridge, sig_name)
            sig.connect.assert_called_once()


# ---------------------------------------------------------------------------
# Terminal output routing
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Dispatch click / edit / cancel
# ---------------------------------------------------------------------------


class TestDispatchActions:
    """Verify dispatch-related slot actions."""

    def test_dispatch_clicked(
        self, handler: WorkerEventHandler, chat: Mock, bridge: Mock,
    ) -> None:
        card = Mock()
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc")
        chat.get_spec_card.return_value = card

        handler._on_dispatch_clicked("tc1")
        chat.get_spec_card.assert_called_once_with("tc1")
        bridge.user_dispatched.assert_called_once_with("tc1", "goal", ["f.py"], "spec", "acc")

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
        card.current_spec.return_value = ("goal", ["f.py"], "spec", "acc")
        chat.get_spec_card.return_value = card

        with patch("aura.gui.spec_edit_dialog.SpecEditDialog") as mock_dlg:
            dlg_instance = mock_dlg.return_value
            from PySide6.QtWidgets import QDialog
            dlg_instance.exec.return_value = QDialog.DialogCode.Accepted
            dlg_instance.goal.return_value = "new goal"
            dlg_instance.files.return_value = ["new.py"]
            dlg_instance.spec.return_value = "new spec"
            dlg_instance.acceptance.return_value = "new acc"

            handler._on_edit_spec_clicked("tc1")

        card.update_spec.assert_called_once_with(
            "new goal", ["new.py"], "new spec", "new acc"
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
