"""Phase 4B regression coverage: checklist-only Progress pane.

Replaces the Execution Log's QPlainTextEdit stream, activity heartbeat, and
dynamic DiffCard/ErrorCard area with a pane that shows only the live Task
Checklist, a truthful terminal status chip, and the Stop button. Proves:

1. Checklist snapshots still render and update through the full
   ``ProductionExecutionSession.taskChecklistUpdated`` -> ``ExecutionEventHandler``
   -> ``AuraPlayground`` -> ``InfoHubPane`` -> ``TaskChecklistWidget`` path.
2. Completion/cancellation/error do not clear the checklist.
3. The next execution (``begin_assistant``) or an explicit ``clear()`` clears
   the prior checklist normally.
4. Stop still emits the cancellation request.
5. Execution reasoning/activity/diff-decided routing methods and connections
   were removed, so those events can no longer create log/card content.
6. Non-success terminal outcomes (cancellation, API/harness errors, failed
   finishes) remain visibly surfaced in chat; a plain success does not add a
   duplicate receipt.
"""

from __future__ import annotations

import sys

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPlainTextEdit

from aura.gui.execution_finish_outcome import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_ERROR,
    terminal_status_label,
)
from aura.gui.execution_finish_presenter import ExecutionFinishPresenter
from aura.gui.execution_handler import ExecutionEventHandler
from aura.gui.execution_tool_event_router import ExecutionToolEventRouter
from aura.gui.info_hub_pane import InfoHubPane
from aura.gui.playground import AuraPlayground


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


CHECKLIST_ITEMS = [
    {"id": "1", "text": "Read the spec", "status": "done"},
    {"id": "2", "text": "Write the code", "status": "active"},
    {"id": "3", "text": "Ship it", "status": "pending"},
]


class _FakeBridge(QObject):
    """Mirrors the subset of ConversationBridge signals ExecutionEventHandler wires."""

    executionStarted = Signal(str)
    executionFinished = Signal(str, bool, str, str)
    executionCancelled = Signal(str)
    executionToolCallArgs = Signal(str, str, str)
    executionToolCallEnd = Signal(str, str)
    executionToolResult = Signal(str, str, str, bool, str, dict)
    executionFileEditLifecycle = Signal(str, str, str, str, list, str)
    executionWorkspaceReconcileRequested = Signal(str, str)
    executionApiError = Signal(str, int, str)
    executionUsage = Signal(str, str, int, int, int, int)
    taskChecklistUpdated = Signal(str, list)
    executionTerminalCommandStarted = Signal(str, str, str, str)
    executionTerminalOutput = Signal(str, str, str)
    executionAgentProcessStarted = Signal(str, str, str, str)
    executionAgentProcessOutput = Signal(str, str, str)
    executionAgentProcessFinished = Signal(str, str, object)
    terminalOutput = Signal(str, str)

    production_run_id = ""

    def execution_result_metadata(self, tool_call_id: str) -> dict:
        return {}


class _RecordingChat:
    def __init__(self) -> None:
        self.info_calls: list[tuple] = []
        self.error_calls: list[tuple] = []

    def add_info(self, title: str, message: str) -> None:
        self.info_calls.append((title, message))

    def add_error(self, title: str, message: str, show_retry: bool = False, persist: bool = True) -> None:
        self.error_calls.append((title, message, show_retry, persist))


def _make_handler(qapp) -> tuple[_FakeBridge, _RecordingChat, AuraPlayground, ExecutionEventHandler]:
    bridge = _FakeBridge()
    chat = _RecordingChat()
    playground = AuraPlayground()
    handler = ExecutionEventHandler(bridge=bridge, chat=chat, playground=playground, settings=object())
    handler.connect_bridge_signals()
    return bridge, chat, playground, handler


# ---- 1. full checklist routing path ----------------------------------------


def test_checklist_routes_end_to_end_through_bridge_signal(qapp) -> None:
    bridge, _chat, playground, _handler = _make_handler(qapp)

    bridge.taskChecklistUpdated.emit("run-1", CHECKLIST_ITEMS)

    widget = playground._info_hub._todo_widget
    assert set(widget._rows.keys()) == {"1", "2", "3"}
    assert widget._title.text() == "TASK CHECKLIST · 1/3"
    assert widget.isHidden() is False

    updated = [dict(item) for item in CHECKLIST_ITEMS]
    updated[1]["status"] = "done"
    bridge.taskChecklistUpdated.emit("run-1", updated)

    assert widget._title.text() == "TASK CHECKLIST · 2/3"


# ---- 2. terminal outcomes preserve the checklist ----------------------------


def test_completion_does_not_clear_checklist(qapp) -> None:
    playground = AuraPlayground()
    playground.update_task_checklist(CHECKLIST_ITEMS)

    playground.execution_finished(True, "All done.")

    widget = playground._info_hub._todo_widget
    assert set(widget._rows.keys()) == {"1", "2", "3"}
    assert playground._info_hub._status_chip.text() == STATUS_DONE


def test_cancellation_does_not_clear_checklist(qapp) -> None:
    playground = AuraPlayground()
    playground.update_task_checklist(CHECKLIST_ITEMS)

    playground.execution_cancelled()

    widget = playground._info_hub._todo_widget
    assert set(widget._rows.keys()) == {"1", "2", "3"}
    assert playground._info_hub._status_chip.text() == STATUS_CANCELLED


def test_api_error_does_not_clear_checklist(qapp) -> None:
    playground = AuraPlayground()
    playground.update_task_checklist(CHECKLIST_ITEMS)

    playground.mark_execution_error()

    widget = playground._info_hub._todo_widget
    assert set(widget._rows.keys()) == {"1", "2", "3"}
    assert playground._info_hub._status_chip.text() == STATUS_ERROR


# ---- 3. next execution / explicit clear reset the checklist ----------------


def test_next_execution_clears_prior_checklist(qapp) -> None:
    playground = AuraPlayground()
    playground.update_task_checklist(CHECKLIST_ITEMS)
    playground.execution_finished(True, "All done.")

    playground.begin_assistant()

    widget = playground._info_hub._todo_widget
    assert widget._rows == {}
    assert widget.isHidden() is True
    assert playground._info_hub._status_chip.text() == "Idle"


def test_explicit_clear_clears_prior_checklist(qapp) -> None:
    playground = AuraPlayground()
    playground.update_task_checklist(CHECKLIST_ITEMS)
    playground.execution_finished(False, "Blocked.", status="validation_failed")

    playground.clear()

    widget = playground._info_hub._todo_widget
    assert widget._rows == {}


# ---- 4. Stop still emits the cancellation request ---------------------------


def test_stop_button_emits_cancellation_request(qapp) -> None:
    pane = InfoHubPane()
    received: list[bool] = []
    pane.stop_execution_requested.connect(lambda: received.append(True))
    pane.set_execution_running(True)

    pane._stop_execution_btn.click()

    assert received == [True]
    assert pane._stop_execution_btn.text() == "Stopping..."
    assert pane._stop_execution_btn.isEnabled() is False


def test_playground_stop_signal_forwards_to_info_hub(qapp) -> None:
    playground = AuraPlayground()
    received: list[bool] = []
    playground.stop_execution_requested.connect(lambda: received.append(True))

    playground._info_hub.stop_execution_requested.emit()

    assert received == [True]


# ---- 5. removed reasoning/activity/diff routing -----------------------------


def test_progress_pane_has_no_execution_log_widgets(qapp) -> None:
    pane = InfoHubPane()
    assert pane.findChild(QPlainTextEdit) is None
    for removed in (
        "append_reasoning",
        "append_content",
        "update_activity",
        "add_diff_card",
        "add_error",
        "show_final_summary",
        "flush_execution_log",
        "mark_execution_log_boundary",
    ):
        assert not hasattr(pane, removed), removed


def test_playground_no_longer_exposes_removed_execution_log_forwarders() -> None:
    for removed in (
        "append_reasoning",
        "append_content",
        "update_activity",
        "add_diff_card",
        "add_error",
        "add_tool_call",
    ):
        assert not hasattr(AuraPlayground, removed), removed


def test_router_no_longer_forwards_tool_call_start_or_diff_decided() -> None:
    for removed in ("on_execution_tool_call_start", "on_execution_diff_decided"):
        assert not hasattr(ExecutionToolEventRouter, removed), removed


def test_progress_header_title_is_progress(qapp) -> None:
    pane = InfoHubPane()
    titles = [lbl.text() for lbl in pane.findChildren(QLabel)]
    assert "PROGRESS" in titles
    assert "EXECUTION LOG" not in titles


# ---- 6. non-success outcomes stay visible in chat ---------------------------


def test_terminal_status_label_matrix() -> None:
    cases = [
        (True, None, STATUS_DONE),
        (True, "completed", STATUS_DONE),
        (True, "completed_with_caveats", STATUS_DONE),
        (False, "harness_error", STATUS_ERROR),
        (False, "cancelled", STATUS_CANCELLED),
        (True, "cancelled", STATUS_CANCELLED),
        (False, "validation_failed", STATUS_ERROR),
        (False, "approval_rejected", STATUS_ERROR),
        (False, None, STATUS_ERROR),
    ]
    for ok, status, expected in cases:
        assert terminal_status_label(ok=ok, status=status) == expected


def test_terminal_status_label_has_no_needs_followup_state() -> None:
    import aura.gui.execution_finish_outcome as execution_finish_outcome

    assert not hasattr(execution_finish_outcome, "STATUS_NEEDS_FOLLOWUP")


class _FakePresenterPlayground:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def stop_aura(self) -> None:
        self.calls.append(("stop_aura",))

    def execution_finished(self, ok, summary, status=None) -> None:
        self.calls.append(("execution_finished", ok, summary, status))

    def set_execution_running(self, running) -> None:
        self.calls.append(("set_execution_running", running))


def test_presenter_success_does_not_add_chat_receipt() -> None:
    chat = _RecordingChat()
    presenter = ExecutionFinishPresenter(chat, _FakePresenterPlayground())

    presenter.present(
        tool_call_id="t", ok=True, summary="All good.",
        status="completed", metadata={},
    )

    assert chat.error_calls == []


def test_presenter_failure_surfaces_error_in_chat() -> None:
    chat = _RecordingChat()
    presenter = ExecutionFinishPresenter(chat, _FakePresenterPlayground())

    presenter.present(
        tool_call_id="t", ok=False, summary="Validation failed: x",
        status="validation_failed", metadata={},
    )

    assert chat.error_calls == [(STATUS_ERROR, "Validation failed: x", False, True)]


def test_presenter_unverified_completion_shows_done_and_no_chat_card() -> None:
    """A successful-but-unverified production run must never surface the
    retired generic 'Needs follow-up' card — it shows Done, like any other
    successful completion."""
    chat = _RecordingChat()
    presenter = ExecutionFinishPresenter(chat, _FakePresenterPlayground())

    presenter.present(
        tool_call_id="t", ok=True, summary="Done, unverified.",
        status="completed_unverified", metadata={},
    )

    assert chat.error_calls == []


def test_handler_cancellation_surfaces_in_chat_and_sets_pane_status(qapp) -> None:
    bridge, chat, playground, handler = _make_handler(qapp)
    handler._active_execution_tool_call_id = "run-1"

    bridge.executionCancelled.emit("run-1")

    assert chat.info_calls == [("Execution", "Stopped by user.")]
    assert playground._info_hub._status_chip.text() == STATUS_CANCELLED


def test_handler_api_error_surfaces_in_chat_and_sets_pane_status(qapp) -> None:
    bridge, chat, playground, handler = _make_handler(qapp)
    handler._active_execution_tool_call_id = "run-1"

    bridge.executionApiError.emit("run-1", 500, "boom")

    assert chat.error_calls == [("API Error 500", "boom", False, True)]
    assert playground._info_hub._status_chip.text() == STATUS_ERROR


def test_handler_finished_success_no_chat_receipt(qapp) -> None:
    bridge, chat, playground, _handler = _make_handler(qapp)

    bridge.executionFinished.emit("run-1", True, "All good.", "completed")

    assert chat.error_calls == []
    assert playground._info_hub._status_chip.text() == STATUS_DONE


def test_handler_finished_failure_surfaces_in_chat(qapp) -> None:
    bridge, chat, playground, _handler = _make_handler(qapp)

    bridge.executionFinished.emit("run-1", False, "It broke.", "harness_error")

    assert chat.error_calls == [(STATUS_ERROR, "It broke.", False, True)]
    assert playground._info_hub._status_chip.text() == STATUS_ERROR
