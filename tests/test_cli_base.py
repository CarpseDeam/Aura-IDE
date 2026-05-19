"""Tests for CLI backend process event streaming."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch
import json

from aura.backends.cli_base import CLIAgentBackend
from aura.backends.cli_protocol import CLIEventAdapter
from aura.client.events import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    ContentDelta,
    Done,
    Event,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
)
from aura.sandbox import SandboxResult


class DummyCLIBackend(CLIAgentBackend):
    def stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str,
        thinking: str,
        cancel_event: Any = None,
        temperature: float = 0.7,
    ) -> Iterator[Event]:
        return iter(())


def exhaust_with_return(iterator):
    events = []
    while True:
        try:
            events.append(next(iterator))
        except StopIteration as stop:
            return events, stop.value


def test_run_cli_agent_command_streams_process_events(tmp_path: Path) -> None:
    backend = DummyCLIBackend(workspace_root=tmp_path)
    expected = SandboxResult(ok=True, stdout="first\nsecond\n", stderr="", exit_code=0)

    def fake_run_terminal_command(
        command: str,
        timeout: int,
        cancel_event: Any,
        on_output: Any,
        input_data: str | None = None,
    ) -> SandboxResult:
        on_output("first\n")
        on_output("second\n")
        return expected

    with patch(
        "aura.backends.cli_base.SandboxExecutor.run_terminal_command",
        side_effect=fake_run_terminal_command,
    ):
        events, result = exhaust_with_return(
            backend._run_cli_agent_command(
                command="dummy run",
                label="Dummy",
                input_data="prompt",
            )
        )

    assert result == expected
    assert isinstance(events[0], AgentProcessStarted)
    assert events[0].label == "Dummy"
    assert events[0].command == "dummy run"
    assert [event.text for event in events if isinstance(event, AgentProcessOutput)] == [
        "first\n",
        "second\n",
    ]
    assert isinstance(events[-1], AgentProcessFinished)
    assert events[-1].process_id == events[0].process_id
    assert events[-1].exit_code == 0


def test_cli_event_adapter_parses_tool_calls() -> None:
    adapter = CLIEventAdapter()
    events = []

    # Simulate chunked stdout emitting AURA_EVENT tool calls
    events.extend(adapter.feed("AURA_EVENT {\"type\":\"tool_call_start\",\"id\":\"call-123\",\"name\":\"update_todo_list\",\"index\":0}\n"))
    events.extend(adapter.feed("AURA_EVENT {\"type\":\"tool_call_args\",\"index\":0,\"args_chunk\":\"{\\\"task\\\":\"}\n"))
    events.extend(adapter.feed("AURA_EVENT {\"type\":\"tool_call_args\",\"index\":0,\"args_chunk\":\"\\\"Read file\\\"}\"}\n"))
    events.extend(adapter.feed("AURA_EVENT {\"type\":\"tool_call_end\",\"index\":0}\n"))
    events.extend(adapter.feed("Some conversational text\n"))

    events.extend(adapter.finish(exit_code=0, stdout="", stderr=""))

    assert any(isinstance(e, ToolCallStart) and e.name == "update_todo_list" for e in events)
    assert any(isinstance(e, ToolCallArgsDelta) and e.args_chunk == "{\"task\":" for e in events)
    assert any(isinstance(e, ToolCallEnd) and e.index == 0 for e in events)
    assert any(isinstance(e, ContentDelta) and e.text == "Some conversational text\n" for e in events)

    # Check Done event has accumulated tool_calls
    done_event = next(e for e in events if isinstance(e, Done))
    assert done_event.finish_reason == "tool_calls"
    assert done_event.full_message["role"] == "assistant"
    assert "Some conversational text" in done_event.full_message["content"]
    
    assert len(done_event.full_message["tool_calls"]) == 1
    call = done_event.full_message["tool_calls"][0]
    assert call["id"] == "call-123"
    assert call["type"] == "function"
    assert call["function"]["name"] == "update_todo_list"
    assert call["function"]["arguments"] == "{\"task\":\"Read file\"}"

def test_cli_event_adapter_nonzero_exit() -> None:
    from aura.client.events import ApiError
    adapter = CLIEventAdapter()
    events = list(adapter.finish(exit_code=1, stdout="failed\n", stderr="some error\n"))

    assert len(events) == 1
    assert isinstance(events[0], ApiError)
    assert events[0].status_code == 1
    assert "some error" in events[0].message


def test_cli_backend_end_to_end_execution(tmp_path: Path) -> None:
    from aura.conversation.manager import ConversationManager
    from aura.conversation.history import History
    from aura.conversation.tools.registry import ToolRegistry
    from aura.client.events import ToolResult
    import threading

    class MockToolCLIBackend(CLIAgentBackend):
        def stream(
            self,
            messages: list[dict[str, Any]],
            tools: list[dict[str, Any]] | None,
            model: str,
            thinking: str,
            cancel_event: Any = None,
            temperature: float = 0.7,
        ) -> Iterator[Event]:
            adapter = CLIEventAdapter()
            yield from adapter.feed('AURA_EVENT {"type":"tool_call_start","id":"call-mock","name":"mock_tool","index":0}\n')
            yield from adapter.feed('AURA_EVENT {"type":"tool_call_args","index":0,"args_chunk":"{\\"foo\\":\\"bar\\"}"}\n')
            yield from adapter.feed('AURA_EVENT {"type":"tool_call_end","index":0}\n')
            yield from adapter.finish(0, "", "")

    backend = MockToolCLIBackend(workspace_root=tmp_path)
    history = History()
    history.append_user_text("Do the thing")

    registry = ToolRegistry(tmp_path)

    with patch.object(registry, "execute") as mock_exec:
        from aura.client.events import ToolResult as MockToolRes
        class DummyToolResult:
            def __init__(self, ok, result, extras=None):
                self.ok = ok
                self.result = result
                self.extras = extras or {}
            def to_tool_message_content(self):
                return self.result

        # When ConversationManager calls execute, return a successful result
        mock_exec.return_value = DummyToolResult(ok=True, result="mock success")

        manager = ConversationManager(history, registry)
        emitted_events = []

        def on_event(ev: Event) -> None:
            emitted_events.append(ev)

        cancel_event = threading.Event()

        # Patch trigger so it bypasses Qt hooks and yields directly from our backend
        with patch("aura.conversation.manager.hooks.trigger") as mock_trigger:
            mock_trigger.return_value = backend.stream(
                messages=history.for_api(),
                tools=registry.tool_defs(),
                model="dummy",
                thinking="off",
            )

            # This will consume the backend stream, extract the tool_calls from Done,
            # execute the tool, and emit a ToolResult.
            manager.send(
                on_event=on_event,
                approval_cb=lambda r: None,  # auto approve
                cancel_event=cancel_event,
                model="dummy",
                thinking="off",
            )

        # Verify that ConversationManager successfully executed the tool
        tool_results = [e for e in emitted_events if isinstance(e, ToolResult)]
        assert len(tool_results) == 1
        assert tool_results[0].name == "mock_tool"
        assert tool_results[0].ok is True
        assert tool_results[0].result == "mock success"


def test_cli_event_adapter_parses_gemini_format() -> None:
    adapter = CLIEventAdapter()
    events = []

    gemini_msg = {
        "type": "tool_use",
        "tool_id": "gemini-call-1",
        "tool_name": "run_shell_command",
        "parameters": {"command": "ls -l"}
    }
    events.extend(adapter.feed(json.dumps(gemini_msg) + "\n"))
    events.extend(adapter.finish(exit_code=0, stdout="", stderr=""))

    assert any(isinstance(e, ToolCallStart) and e.name == "run_terminal_command" for e in events)
    
    done_event = next(e for e in events if isinstance(e, Done))
    assert done_event.finish_reason == "tool_calls"
    call = done_event.full_message["tool_calls"][0]
    assert call["id"] == "gemini-call-1"
    assert call["function"]["name"] == "run_terminal_command"
    assert "ls -l" in call["function"]["arguments"]
