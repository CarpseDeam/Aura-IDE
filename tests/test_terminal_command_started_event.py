from __future__ import annotations

import threading
from pathlib import Path

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import TerminalCommandStarted, ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.tool_runner import ToolRunner
from aura.events import EXECUTION_COMMAND_STARTED, EventBus


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def test_command_start_is_owned_by_real_start_event() -> None:
    bus = EventBus()
    bus_events: list[tuple[str, dict]] = []
    bus.subscribe("*", lambda event: bus_events.append((event.topic, event.payload)))
    relay = ExecutionEventRelay(_ApprovalProxy(), bus)
    starts: list[tuple[str, str, str, str]] = []
    relay.terminalCommandStarted.connect(lambda *args: starts.append(args))

    relay.relay("run-1", ToolCallStart(index=0, id="call-1", name="run_terminal_command"))
    relay.relay("run-1", ToolCallArgsDelta(index=0, args_chunk='{"command":"partial"}'))
    relay.relay("run-1", ToolCallEnd(index=0))
    relay.relay(
        "run-1",
        TerminalCommandStarted(
            tool_call_id="call-1",
            command="Set-Location -LiteralPath 'C:/workspace'\necho actual",
            cwd="C:/workspace",
        ),
    )

    command_events = [payload for topic, payload in bus_events if topic == EXECUTION_COMMAND_STARTED]
    assert len(command_events) == 1
    assert command_events[0]["command"].endswith("echo actual")
    assert starts == [
        (
            "run-1",
            "call-1",
            "Set-Location -LiteralPath 'C:/workspace'\necho actual",
            "C:/workspace",
        )
    ]


def test_run_and_watch_preexisting_cancellation_has_no_start(tmp_path: Path) -> None:
    runner = ToolRunner(History(), tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()
    events: list[object] = []

    runner.handle_run_and_watch(
        "watch-cancelled",
        {},
        events.append,
        cancel_event,
        "python -c \"print('not launched')\"",
    )

    assert len(events) == 1
    assert isinstance(events[0], ToolResult)


def test_run_and_watch_launch_failure_has_no_start(tmp_path: Path, monkeypatch) -> None:
    def fail_launch(*_args, **_kwargs):
        raise FileNotFoundError("launch failed")

    monkeypatch.setattr("aura.sandbox.SandboxExecutor._launch_host_command", fail_launch)
    runner = ToolRunner(History(), tmp_path)
    events: list[object] = []

    runner.handle_run_and_watch(
        "watch-failed",
        {},
        events.append,
        threading.Event(),
        "python -c \"print('not launched')\"",
    )

    assert not any(isinstance(event, TerminalCommandStarted) for event in events)
    assert len(events) == 1 and isinstance(events[0], ToolResult)
