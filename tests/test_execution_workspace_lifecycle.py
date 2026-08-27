"""Lazy workspace activation for the desktop Aura lifecycle.

``ProductionExecutionSession`` emits ``executionStarted`` for every production
turn, a text-only answer such as "Hi Aura" included. That run-level signal is
therefore not proof of workspace activity, and ``ExecutionEventHandler`` must
not treat it as such. Proves:

1. A text-only start/finish never touches or activates the workspace.
2. The first real workspace activity activates it exactly once.
3. Repeated activity does not re-prepare the playground or restart the fade.
4. An activated normal completion writes its truthful terminal status and
   stops the workspace Aura.
5. Cancellation and API/harness errors stop it safely and clear the state.
6. No workspace-activity route can be wired in a way that bypasses lazy
   activation, and mere provider/process startup never activates.
"""

from __future__ import annotations

import inspect
import sys

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from aura.gui.execution_handler import ExecutionEventHandler
from aura.gui.execution_tool_event_router import ExecutionToolEventRouter


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    yield app


class _FakeBridge(QObject):
    """Mirrors the ConversationBridge signals ExecutionEventHandler wires."""

    executionStarted = Signal(str)
    executionFinished = Signal(str, bool, str)
    executionCancelled = Signal(str)
    executionToolCallStart = Signal(str, str, str)
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

    production_run_id = ""


class _RecordingPlayground:
    """Records every playground call the handler makes, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name: str):
        def _record(*args, **kwargs) -> None:
            self.calls.append((name, args, kwargs))

        return _record

    def names(self) -> list[str]:
        return [name for name, _args, _kwargs in self.calls]


class _RecordingCard:
    def __init__(self) -> None:
        self.thinking_messages: list[str] = []

    def show_thinking_message(self, message: str) -> None:
        self.thinking_messages.append(message)


class _RecordingChat:
    def __init__(self) -> None:
        self.card = _RecordingCard()
        self.info_calls: list[tuple] = []

    def current_assistant(self) -> _RecordingCard:
        return self.card

    def add_info(self, title: str, message: str) -> None:
        self.info_calls.append((title, message))


#: Playground calls that can only happen because the workspace was activated.
WORKSPACE_LIFECYCLE_CALLS = frozenset({
    "begin_assistant",
    "start_aura",
    "stop_aura",
    "execution_finished",
    "execution_cancelled",
    "mark_execution_error",
    "clear",
})

#: One representative bridge emission per workspace-activity route.
ACTIVITY_EMISSIONS: dict[str, tuple[str, tuple]] = {
    "on_execution_tool_result": (
        "executionToolResult", ("run-1", "call-1", "read_file", True, "{}", {}),
    ),
    "on_execution_file_edit_lifecycle": (
        "executionFileEditLifecycle", ("run-1", "call-1", "write_file", "applied", [], ""),
    ),
    "on_execution_workspace_reconcile_requested": (
        "executionWorkspaceReconcileRequested", ("run-1", "call-1"),
    ),
    "on_execution_terminal_command_started": (
        "executionTerminalCommandStarted", ("run-1", "call-1", "pytest -q", "/repo"),
    ),
    "on_execution_terminal_output": (
        "executionTerminalOutput", ("run-1", "call-1", "ok\n"),
    ),
    "on_execution_agent_process_output": (
        "executionAgentProcessOutput", ("run-1", "proc-1", "line\n"),
    ),
    "on_execution_agent_process_finished": (
        "executionAgentProcessFinished", ("run-1", "proc-1", 0),
    ),
}


@pytest.fixture
def wired(qapp):
    bridge = _FakeBridge()
    chat = _RecordingChat()
    playground = _RecordingPlayground()
    handler = ExecutionEventHandler(
        bridge=bridge, chat=chat, playground=playground, settings=object()
    )
    handler.connect_bridge_signals()
    return bridge, chat, playground, handler


# ---- 1. a text-only turn never touches the workspace ------------------------


def test_text_only_run_never_activates_or_touches_the_workspace(wired) -> None:
    bridge, chat, playground, handler = wired

    bridge.executionStarted.emit("run-1")
    bridge.executionFinished.emit("run-1", True, "completed")

    assert playground.calls == []
    assert chat.card.thinking_messages == []
    assert chat.info_calls == []
    assert handler._workspace_activated is False


def test_text_only_start_still_owns_run_and_status_state(wired) -> None:
    """Run/input/status state is unchanged — only the workspace went lazy."""
    bridge, _chat, _playground, handler = wired
    started: list[bool] = []
    running: list[bool] = []
    handler.execution_started.connect(lambda: started.append(True))
    handler.execution_running_changed.connect(running.append)

    bridge.executionStarted.emit("run-1")
    assert started == [True]
    assert running == [True]
    assert handler._active_execution_tool_call_id == "run-1"

    bridge.executionFinished.emit("run-1", True, "completed")
    assert running == [True, False]
    assert handler._active_execution_tool_call_id is None


# ---- 2. the first real workspace activity activates exactly once ------------


def test_first_tool_call_start_activates_the_workspace_once(wired) -> None:
    bridge, chat, playground, handler = wired

    bridge.executionStarted.emit("run-1")
    assert playground.calls == []

    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")

    assert playground.calls == [
        ("begin_assistant", (), {}),
        ("set_execution_running", (True,), {}),
        ("start_aura", (), {}),
    ]
    assert chat.card.thinking_messages == ["Working in the workspace"]
    assert handler._workspace_activated is True


def test_activation_prepares_the_playground_before_the_event_is_projected(wired) -> None:
    """begin_assistant closes execution tabs, so it must land first."""
    bridge, _chat, playground, _handler = wired
    bridge.executionStarted.emit("run-1")

    bridge.executionFileEditLifecycle.emit("run-1", "call-1", "write_file", "applied", [], "")

    assert playground.names() == [
        "begin_assistant",
        "set_execution_running",
        "start_aura",
        "handle_file_edit_lifecycle",
    ]


def test_activation_re_asserts_the_live_status_the_pane_reset(wired) -> None:
    """begin_assistant clears the chip to Idle mid-run, so Live is restored."""
    bridge, _chat, playground, _handler = wired
    bridge.executionStarted.emit("run-1")

    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")

    running_calls = [
        args for name, args, _kwargs in playground.calls
        if name == "set_execution_running"
    ]
    assert running_calls == [(True,)]
    assert playground.names().index("set_execution_running") > playground.names().index(
        "begin_assistant"
    )


# ---- 3. repeated activity does not reinitialize the workspace ---------------


def test_repeated_activity_does_not_reinitialize_or_restart_the_fade(wired) -> None:
    bridge, chat, playground, _handler = wired
    bridge.executionStarted.emit("run-1")

    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")
    bridge.executionToolCallStart.emit("run-1", "call-2", "run_terminal")
    bridge.executionTerminalCommandStarted.emit("run-1", "call-2", "pytest -q", "/repo")
    bridge.executionTerminalOutput.emit("run-1", "call-2", "ok\n")
    bridge.executionToolResult.emit("run-1", "call-2", "run_terminal", True, "{}", {})

    assert playground.names().count("begin_assistant") == 1
    assert playground.names().count("start_aura") == 1
    assert chat.card.thinking_messages == ["Working in the workspace"]


def test_a_second_run_activates_again_after_the_first_finished(wired) -> None:
    bridge, _chat, playground, handler = wired

    bridge.executionStarted.emit("run-1")
    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")
    bridge.executionFinished.emit("run-1", True, "completed")
    assert handler._workspace_activated is False

    bridge.executionStarted.emit("run-2")
    bridge.executionToolCallStart.emit("run-2", "call-2", "write_file")

    assert playground.names().count("begin_assistant") == 2
    assert playground.names().count("start_aura") == 2


# ---- 4. an activated normal completion stops the workspace Aura -------------


def test_activated_completion_sets_status_then_stops_the_workspace_aura(wired) -> None:
    bridge, _chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")
    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")
    playground.calls.clear()

    bridge.executionFinished.emit("run-1", False, "validation_failed")

    assert playground.calls == [
        ("execution_finished", (False,), {"status": "validation_failed"}),
        ("stop_aura", (), {}),
    ]
    assert handler._workspace_activated is False


# ---- 5. cancellation and API/harness errors stop it safely ------------------


def test_cancellation_stops_the_workspace_aura_and_clears_activation(wired) -> None:
    bridge, chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")
    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")
    playground.calls.clear()

    bridge.executionCancelled.emit("run-1")

    assert playground.names() == ["stop_aura", "execution_cancelled"]
    assert chat.info_calls == [("Execution", "Stopped by user.")]
    assert handler._workspace_activated is False


def test_api_error_stops_the_workspace_aura_and_clears_activation(wired) -> None:
    bridge, _chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")
    bridge.executionToolCallStart.emit("run-1", "call-1", "write_file")
    playground.calls.clear()

    bridge.executionApiError.emit("run-1", 500, "boom")

    assert playground.names() == [
        "mark_execution_error",
        "stop_aura",
        "set_execution_running",
    ]
    assert handler._workspace_activated is False


@pytest.mark.parametrize("signal_name, args", [
    ("executionCancelled", ("run-1",)),
    ("executionApiError", ("run-1", 500, "boom")),
])
def test_cancellation_and_error_are_safe_on_a_never_activated_run(
    wired, signal_name: str, args: tuple
) -> None:
    """Stopping an Aura that never started must not raise or leave state set."""
    bridge, _chat, _playground, handler = wired
    bridge.executionStarted.emit("run-1")

    getattr(bridge, signal_name).emit(*args)

    assert handler._workspace_activated is False
    assert handler._active_execution_tool_call_id is None


# ---- 6. no workspace-activity route can bypass lazy activation -------------


def test_every_router_forward_is_classified() -> None:
    """A new router method must be classified, or connecting it raises."""
    routed = {
        name
        for name, _member in inspect.getmembers(
            ExecutionToolEventRouter, inspect.isfunction
        )
        if name.startswith("on_execution_")
    }
    classified = (
        ExecutionEventHandler.WORKSPACE_ACTIVITY_ROUTES
        | ExecutionEventHandler.PROVIDER_STARTUP_ROUTES
    )
    assert routed == classified
    assert routed == set(ExecutionEventHandler._BRIDGE_SIGNAL_BY_ROUTE)


def test_activity_emissions_cover_every_workspace_activity_route() -> None:
    assert set(ACTIVITY_EMISSIONS) == ExecutionEventHandler.WORKSPACE_ACTIVITY_ROUTES


@pytest.mark.parametrize(
    "route", sorted(ExecutionEventHandler.WORKSPACE_ACTIVITY_ROUTES)
)
def test_each_workspace_activity_route_activates_lazily(wired, route: str) -> None:
    bridge, _chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")

    signal_name, args = ACTIVITY_EMISSIONS[route]
    getattr(bridge, signal_name).emit(*args)

    assert handler._workspace_activated is True, route
    assert playground.names()[:3] == [
        "begin_assistant",
        "set_execution_running",
        "start_aura",
    ], route


def test_provider_process_startup_is_not_workspace_activity(wired) -> None:
    """Spawning a backend process is startup, not proof of workspace work."""
    bridge, chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")

    bridge.executionAgentProcessStarted.emit("run-1", "proc-1", "claude", "claude -p")

    assert handler._workspace_activated is False
    assert chat.card.thinking_messages == []
    assert not WORKSPACE_LIFECYCLE_CALLS.intersection(playground.names())
    # The event is still projected — only the activation decision is withheld.
    assert playground.names() == ["start_terminal_process"]


def test_task_checklist_updates_do_not_activate_the_workspace(wired) -> None:
    """A checklist snapshot is a status lens, not proof of workspace work."""
    bridge, _chat, playground, handler = wired
    bridge.executionStarted.emit("run-1")

    bridge.taskChecklistUpdated.emit("run-1", [])

    assert handler._workspace_activated is False
    assert playground.names() == ["update_task_checklist"]
