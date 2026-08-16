from __future__ import annotations

from aura.bridge.execution_event_relay import ExecutionEventRelay
from aura.client import TerminalCommandStarted, ToolCallArgsDelta, ToolCallEnd, ToolCallStart
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

    relay.relay("run-1", ToolCallStart(index=0, id="call-1", name="shell"))
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
