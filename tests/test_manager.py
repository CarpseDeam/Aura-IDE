"""Comprehensive unit tests for ConversationManager with mocked dependencies."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, PropertyMock, patch

import pytest

from aura.client.events import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
    TerminalOutput,
    WorkerDispatchRequested,
)
from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.registry import (
    ApprovalDecision,
    ApprovalRequest,
    ToolExecResult,
    ToolRegistry,
)
from aura.sandbox import SandboxResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_done(content: str | None = None,
               tool_calls: list[dict] | None = None,
               reasoning: str | None = None) -> Done:
    """Build a Done event with the given content/tool_calls."""
    msg: dict = {"role": "assistant"}
    msg["content"] = content
    if reasoning:
        msg["reasoning_content"] = reasoning
    else:
        msg["reasoning_content"] = None
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return Done(finish_reason="tool_calls" if tool_calls else "stop",
                full_message=msg)


def _tool_call(id: str, name: str, args: dict) -> dict:
    """Build a tool-call dict as returned by the API."""
    return {
        "id": id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(args),
        },
    }


def _make_approval_cb(decision: str = "approve") -> MagicMock:
    """Return an approval callback that returns a fixed decision."""
    cb = MagicMock()
    cb.return_value = ApprovalDecision(decision)
    return cb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def cancel_event() -> threading.Event:
    """A threading.Event that is *not* set by default."""
    return threading.Event()


@pytest.fixture
def captured_events() -> list[Event]:
    """A list that captures every event fired by on_event."""
    return []


@pytest.fixture
def on_event(captured_events):
    """An EventCallback that appends to captured_events."""
    return lambda ev: captured_events.append(ev)


@pytest.fixture
def history() -> History:
    """A real History instance — no external deps."""
    return History()


@pytest.fixture
def mock_client():
    """A MagicMock for DeepSeekClient.  Override stream() per test."""
    client = MagicMock()
    return client


@pytest.fixture
def mock_tools(tmp_path):
    """A MagicMock for ToolRegistry with sensible defaults."""
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    tools.execute.return_value = ToolExecResult(ok=True, payload={"ok": True})
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    return tools


@pytest.fixture
def manager(mock_client, history, mock_tools) -> ConversationManager:
    """A ConversationManager with all three deps mocked/real."""
    return ConversationManager(
        client=mock_client,
        history=history,
        tool_registry=mock_tools,
    )


# ===================================================================
# 1. Normal flow — no tool calls
# ===================================================================

class TestNormalFlow:
    """Basic assistant response without any tool calls."""

    def test_simple_response(self, manager, mock_client, on_event,
                             captured_events, cancel_event, history):
        mock_client.stream.return_value = [
            ContentDelta(text="Hello"),
            Done(finish_reason="stop", full_message={
                "role": "assistant",
                "content": "Hello",
                "reasoning_content": None,
            }),
        ]
        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Exactly one assistant message
        assert len(history.messages) == 1
        assert history.messages[0]["role"] == "assistant"
        assert history.messages[0]["content"] == "Hello"

        # Events include ContentDelta and Done
        event_types = [type(e).__name__ for e in captured_events]
        assert "ContentDelta" in event_types
        assert "Done" in event_types


# ===================================================================
# 2. Single tool call round
# ===================================================================

class TestSingleToolCall:
    """One tool call followed by a final content response."""

    def test_one_tool_round(self, manager, mock_client, mock_tools, on_event,
                            captured_events, cancel_event, history):
        tool_id = "call1"
        tc = _tool_call(tool_id, "write_file", {"path": "test.py", "content": "ok"})

        # First stream: tool call
        mock_client.stream.side_effect = [
            iter([
                ToolCallStart(index=0, id=tool_id, name="write_file"),
                ToolCallArgsDelta(index=0, args_chunk='{"path":'),
                ToolCallArgsDelta(index=0, args_chunk='"test.py", "content": "ok"}'),
                ToolCallEnd(index=0),
                _make_done(content="", tool_calls=[tc]),
            ]),
            # Second stream: final content
            iter([
                ContentDelta(text="Done"),
                _make_done(content="Done"),
            ]),
        ]

        mock_tools.execute.return_value = ToolExecResult(
            ok=True, payload={"ok": True, "path": "test.py"}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # tools.execute() was called once with name="write_file"
        mock_tools.execute.assert_called_once()
        call_kwargs = mock_tools.execute.call_args[1]
        assert call_kwargs["name"] == "write_file"

        # History has 3 messages: assistant(tool), tool_result, assistant(content)
        assert len(history.messages) == 3
        assert history.messages[0]["role"] == "assistant"
        assert history.messages[1]["role"] == "tool"
        assert history.messages[2]["role"] == "assistant"
        assert history.messages[2]["content"] == "Done"

        # Events include ToolCallStart, ToolCallEnd, ToolResult, ContentDelta, Done
        event_types = [type(e).__name__ for e in captured_events]
        assert "ToolCallStart" in event_types
        assert "ToolCallEnd" in event_types
        assert "ToolResult" in event_types
        assert "ContentDelta" in event_types


# ===================================================================
# 3. Max tool rounds reached
# ===================================================================

class TestMaxToolRounds:
    """When every round produces a tool call, we hit the limit."""

    @patch("aura.conversation.manager.MAX_TOOL_ROUNDS", 2)
    def test_max_rounds_reached(self, manager, mock_client, mock_tools,
                                on_event, captured_events, cancel_event,
                                history):
        tool_id_template = "call{}"
        tc_template = lambda i: _tool_call(
            tool_id_template.format(i), "write_file",
            {"path": f"test_{i}.py", "content": "data"}
        )

        # Return tool calls for each round
        side_effects = []
        for i in range(3):  # 3 rounds but max is 2
            side_effects.append(iter([
                _make_done(content="", tool_calls=[tc_template(i)]),
            ]))
        mock_client.stream.side_effect = side_effects

        mock_tools.execute.return_value = ToolExecResult(
            ok=True, payload={"ok": True}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # After max rounds, an ApiError should be fired
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert len(api_errors) >= 1
        assert "max tool rounds" in api_errors[-1].message.lower()


# ===================================================================
# 4. Cancel during stream (has content)
# ===================================================================

class TestCancelDuringStream:
    """Cancel event set mid-stream; partial content is kept."""

    def test_cancel_with_content(self, manager, mock_client, on_event,
                                 captured_events, cancel_event, history):
        mock_client.stream.return_value = [
            ContentDelta(text="Partial "),
            # After yielding ContentDelta, we set cancel_event
        ]

        # We need to set cancel_event *after* the stream starts yielding.
        # Wrap stream to inject the set() call.
        original_iter = iter(mock_client.stream.return_value)

        def _controlled_stream(**kwargs):
            for ev in original_iter:
                yield ev
                if isinstance(ev, ContentDelta):
                    cancel_event.set()
            yield _make_done(content="Partial ")

        mock_client.stream.side_effect = None
        mock_client.stream.side_effect = _controlled_stream

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # The partial content should be kept in history
        assert len(history.messages) == 1
        assert history.messages[0]["role"] == "assistant"
        assert history.messages[0]["content"] == "Partial "

        # When cancel is set and we have partial content, manager keeps it
        # (does NOT fire ApiError — no cleanup needed)
        # Verify no ApiError was fired
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert len(api_errors) == 0


# ===================================================================
# 5. Cancel before stream (no content)
# ===================================================================

class TestCancelBeforeStream:
    """Cancel event is set before send() is called — no assistant message."""

    def test_cancel_before_send(self, manager, mock_client, on_event,
                                captured_events, cancel_event, history):
        # Set cancel before sending
        cancel_event.set()

        mock_client.stream.return_value = [
            _make_done(content="Should not appear"),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # No assistant message should be appended
        assert len(history.messages) == 0

        # ApiError with "Cancelled."
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert len(api_errors) >= 1
        assert "cancelled" in api_errors[-1].message.lower()


# ===================================================================
# 6. Cancel with content and tool calls
# ===================================================================

class TestCancelWithToolCalls:
    """Cancel is set after Done yields; tool_calls should be stripped."""

    def test_cancel_strips_tool_calls(self, manager, mock_client, on_event,
                                      captured_events, cancel_event, history):
        tc = _tool_call("call1", "write_file", {"path": "test.py", "content": "data"})

        # Stream yields a Done with both content and tool_calls
        done_ev = _make_done(content="some", tool_calls=[tc])

        # We need cancel_event to be set *after* the for-loop consumes Done,
        # but before the cancel check at the end of the round.
        # Use a controlled generator.
        def _controlled(**kwargs):
            yield done_ev
            # After yielding Done, set cancel_event
            cancel_event.set()

        mock_client.stream.side_effect = _controlled

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # History should have 1 assistant message with content "some"
        # and NO tool_calls (cancel strips them but keeps content)
        assert len(history.messages) == 1
        assert history.messages[0]["role"] == "assistant"
        assert history.messages[0]["content"] == "some"
        assert "tool_calls" not in history.messages[0] or not history.messages[0].get("tool_calls")

        # No ApiError — manager keeps valid partial content
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert len(api_errors) == 0


# ===================================================================
# 7. dispatch_to_worker with dispatch_cb
# ===================================================================

class TestDispatchToWorker:
    """Planner dispatches a task to a worker via dispatch_cb."""

    def test_dispatch_ok(self, manager, mock_client, mock_tools, on_event,
                         captured_events, cancel_event, history):
        tc = _tool_call("dispatch1", "dispatch_to_worker", {
            "goal": "Fix bug",
            "files": ["test.py"],
            "spec": "Change X to Y",
            "acceptance": "Tests pass",
        })

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        dispatch_cb = MagicMock()
        dispatch_cb.return_value = WorkerDispatchResult(
            ok=True, summary="done", cancelled=False
        )

        # Need a second stream call for the follow-up round
        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),  # first round (dispatch)
            iter([                                 # second round (result)
                ContentDelta(text="All done"),
                _make_done(content="All done"),
            ]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )

        # dispatch_cb was called
        dispatch_cb.assert_called_once()
        call_args = dispatch_cb.call_args[0]
        assert call_args[0] == "dispatch1"
        assert isinstance(call_args[1], WorkerDispatchRequest)
        assert call_args[1].goal == "Fix bug"

        # WorkerDispatchRequested event was fired
        assert any(isinstance(e, WorkerDispatchRequested) for e in captured_events)

        # History has tool result with ok=True
        tool_msgs = [m for m in history.messages if m["role"] == "tool"]
        assert len(tool_msgs) >= 1
        payload = json.loads(tool_msgs[0]["content"])
        assert payload["ok"] is True

    def test_dispatch_no_callback(self, manager, mock_client, on_event,
                                  captured_events, cancel_event, history):
        tc = _tool_call("dispatch1", "dispatch_to_worker", {
            "goal": "Fix bug",
            "files": ["test.py"],
            "spec": "Change X to Y",
            "acceptance": "Tests pass",
        })

        # Stream first yields the tool call, then yields a content-only response
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=None,
        )

        # ToolResult with ok=False
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        dispatch_results = [tr for tr in tool_results if tr.name == "dispatch_to_worker"]
        assert len(dispatch_results) == 1
        assert dispatch_results[0].ok is False
        assert "not enabled" in dispatch_results[0].result.lower()

    def test_dispatch_cb_raises(self, manager, mock_client, on_event,
                                captured_events, cancel_event, history):
        tc = _tool_call("dispatch1", "dispatch_to_worker", {
            "goal": "Fix bug",
            "files": ["test.py"],
            "spec": "Change X to Y",
            "acceptance": "Tests pass",
        })

        def _raising_cb(tool_call_id, req):
            raise RuntimeError("boom")

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=_raising_cb,
        )

        # ToolResult with ok=False and "RuntimeError: boom" in result
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        dispatch_results = [tr for tr in tool_results if tr.name == "dispatch_to_worker"]
        assert len(dispatch_results) >= 1
        assert dispatch_results[-1].ok is False
        assert "RuntimeError" in dispatch_results[-1].result


# ===================================================================
# 10. run_terminal_command basic
# ===================================================================

class TestRunTerminalCommand:
    """Terminal command execution via SandboxExecutor."""

    @patch("aura.conversation.manager.SandboxExecutor")
    @patch("aura.config.load_settings")
    def test_terminal_ok(self, mock_load_settings, mock_sandbox_cls,
                         manager, mock_client, mock_tools, on_event,
                         captured_events, cancel_event, history, tmp_path):
        # Mock settings
        fake_settings = MagicMock()
        fake_settings.sandbox_mode = "host"
        mock_load_settings.return_value = fake_settings

        # Mock SandboxExecutor instance
        mock_sandbox_instance = MagicMock()
        mock_sandbox_cls.return_value = mock_sandbox_instance
        mock_sandbox_instance.run_terminal_command.return_value = SandboxResult(
            ok=True, stdout="hello\n", stderr="", exit_code=0,
        )
        type(mock_tools).workspace_root = PropertyMock(return_value=tmp_path)

        tc = _tool_call("term1", "run_terminal_command",
                        {"command": "echo hello", "timeout": 30})

        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # SandboxExecutor was created with correct args
        mock_sandbox_cls.assert_called_once()
        _, kwargs = mock_sandbox_cls.call_args
        assert kwargs["mode"] == "host"
        assert kwargs["workspace_root"] == tmp_path

        # run_terminal_command was called
        mock_sandbox_instance.run_terminal_command.assert_called_once()

        # ToolResult with ok=True
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        term_results = [tr for tr in tool_results if tr.name == "run_terminal_command"]
        assert len(term_results) >= 1
        assert term_results[-1].ok is True

    @patch("aura.conversation.manager.SandboxExecutor")
    @patch("aura.config.load_settings")
    def test_terminal_missing_command(self, mock_load_settings, mock_sandbox_cls,
                                      manager, mock_client, on_event,
                                      captured_events, cancel_event, history,
                                      tmp_path):
        fake_settings = MagicMock()
        fake_settings.sandbox_mode = "host"
        mock_load_settings.return_value = fake_settings

        tc = _tool_call("term1", "run_terminal_command", {})  # no command

        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # ToolResult with ok=False and error about command required
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        term_results = [tr for tr in tool_results if tr.name == "run_terminal_command"]
        assert len(term_results) >= 1
        assert term_results[-1].ok is False
        assert "command is required" in term_results[-1].result.lower()


# ===================================================================
# 12. run_research basic
# ===================================================================

class TestRunResearch:
    """Research sub-agent flow."""

    @patch("aura.conversation.manager.ToolRegistry")
    def test_research_ok(self, mock_tool_registry_cls, manager, mock_client,
                         mock_tools, on_event, captured_events, cancel_event,
                         history, tmp_path):
        type(mock_tools).workspace_root = PropertyMock(return_value=tmp_path)

        # Mock the ToolRegistry created inside _handle_research
        mock_res_tools = MagicMock(spec=ToolRegistry)
        mock_res_tools.tool_defs.return_value = []
        mock_tool_registry_cls.return_value = mock_res_tools

        tc = _tool_call("res1", "run_research", {"objective": "test query"})

        # First stream: outer loop yields research tool call
        # Second stream: research loop yields content directly (no tool calls)
        # Third stream: outer loop's next round yields content -> loop ends
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Report content"),
                  _make_done(content="Research complete. Here is the report.")]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # ToolResult with ok=True and report content
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        research_results = [tr for tr in tool_results if tr.name == "run_research"]
        assert len(research_results) >= 1
        assert research_results[-1].ok is True
        payload = json.loads(research_results[-1].result)
        assert payload["ok"] is True
        assert "report" in payload

        # ToolRegistry was constructed with mode="researcher"
        mock_tool_registry_cls.assert_called_once()
        _, kwargs = mock_tool_registry_cls.call_args
        assert kwargs["mode"] == "researcher"

    @patch("aura.conversation.manager.ToolRegistry")
    def test_research_with_tool_calls(self, mock_tool_registry_cls, manager,
                                       mock_client, mock_tools, on_event,
                                       captured_events, cancel_event, history,
                                       tmp_path):
        """Research sub-agent with web_search tool calls in inner loop."""
        type(mock_tools).workspace_root = PropertyMock(return_value=tmp_path)

        # Mock ToolRegistry for research sub-agent
        mock_res_tools = MagicMock(spec=ToolRegistry)
        mock_res_tools.tool_defs.return_value = [{"name": "web_search"}]
        mock_res_tools.execute.return_value = ToolExecResult(
            ok=True, payload={"ok": True, "result": "search results"}
        )
        mock_tool_registry_cls.return_value = mock_res_tools

        tc = _tool_call("res1", "run_research", {"objective": "test query"})

        # Outer stream: tool call
        # Research stream: tool call (web_search) -> content
        # Outer stream: content
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([
                _make_done(content="", tool_calls=[
                    _tool_call("ws1", "web_search", {"query": "test"})
                ]),
            ]),
            iter([ContentDelta(text="Report..."),
                  _make_done(content="Research report content.")]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Verify the research sub-agent executed the web_search tool
        mock_res_tools.execute.assert_called_once()
        # execute is called as execute(name, args, approval_cb=...)
        assert mock_res_tools.execute.call_args[0][0] == "web_search"

        # ToolResult should have ok=True with a report
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        research_results = [tr for tr in tool_results if tr.name == "run_research"]
        assert len(research_results) >= 1
        assert research_results[-1].ok is True
        payload = json.loads(research_results[-1].result)
        assert payload["ok"] is True
        assert "report" in payload

    @patch("aura.conversation.manager.ToolRegistry")
    def test_research_error(self, mock_tool_registry_cls, manager, mock_client,
                            mock_tools, on_event, captured_events, cancel_event,
                            history, tmp_path):
        type(mock_tools).workspace_root = PropertyMock(return_value=tmp_path)

        mock_res_tools = MagicMock(spec=ToolRegistry)
        mock_res_tools.tool_defs.return_value = []
        mock_tool_registry_cls.return_value = mock_res_tools

        tc = _tool_call("res1", "run_research", {"objective": "test query"})

        # Research stream raises ApiError -> caught by try/except in _handle_research
        # Third stream: outer loop's next round
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ApiError(status_code=500, message="API failure")]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # ToolResult with ok=False and error message
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        research_results = [tr for tr in tool_results if tr.name == "run_research"]
        assert len(research_results) >= 1
        assert research_results[-1].ok is False
        payload = json.loads(research_results[-1].result)
        assert payload["ok"] is False
        assert "API failure" in payload.get("error", "")

    @patch("aura.conversation.manager.ToolRegistry")
    def test_research_no_objective(self, mock_tool_registry_cls, manager,
                                   mock_client, on_event, captured_events,
                                   cancel_event, history, tmp_path):
        """run_research with missing objective."""
        type(manager._tools).workspace_root = PropertyMock(return_value=tmp_path)

        tc = _tool_call("res1", "run_research", {})  # no objective

        # Only needs one stream — error path returns immediately
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        research_results = [tr for tr in tool_results if tr.name == "run_research"]
        assert len(research_results) >= 1
        assert research_results[-1].ok is False
        assert "objective is required" in research_results[-1].result.lower()


# ===================================================================
# 13. ApiError from stream
# ===================================================================

class TestApiError:
    """Stream yielding an ApiError instead of Done."""

    def test_api_error_from_stream(self, manager, mock_client, on_event,
                                   captured_events, cancel_event, history):
        mock_client.stream.return_value = [
            ApiError(status_code=500, message="Server error"),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # No assistant message appended
        assert len(history.messages) == 0

        # The ApiError is in captured_events
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert len(api_errors) >= 1
        assert api_errors[-1].message == "Server error"


# ===================================================================
# 14 + 15. Circuit breaker
# ===================================================================

class TestCircuitBreaker:
    """Repetitive tool failures trigger circuit breaker warnings."""

    def three_failures(self, manager, mock_client, mock_tools, on_event,
                       captured_events, cancel_event, history):
        """Three identical tool call failures trigger the circuit breaker."""
        tc = _tool_call("cb1", "write_file", {"path": "test.py", "content": "data"})

        # Each round returns another tool call
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        # Always fail with identical result
        mock_tools.execute.return_value = ToolExecResult(
            ok=False, payload={"ok": False, "error": "fail"}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Find ToolResult events from write_file
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)
                        and e.name == "write_file"]
        # The first two should NOT have circuit breaker, the third should
        # Find which one has circuit breaker
        cb_results = [tr for tr in tool_results if "CIRCUIT BREAKER" in tr.result]
        assert len(cb_results) >= 1

    def test_success_resets_counter(self, manager, mock_client, mock_tools,
                                     on_event, captured_events, cancel_event,
                                     history):
        """A success resets the failure counter for a given tool+args key."""
        tc_fail = _tool_call("cb1", "write_file", {"path": "test.py", "content": "data"})
        tc_success = _tool_call("cb2", "write_file", {"path": "other.py", "content": "ok"})

        # Round 1: tool call -> fail
        # Round 2: tool call (different args) -> succeed (resets counter for the original key)
        # Round 3: original tool call -> fail (count=1, no circuit breaker)
        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc_fail])]),
            iter([_make_done(content="", tool_calls=[tc_success])]),
            iter([_make_done(content="", tool_calls=[tc_fail])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        mock_tools.execute.side_effect = [
            ToolExecResult(ok=False, payload={"ok": False, "error": "fail"}),
            ToolExecResult(ok=True, payload={"ok": True}),
            ToolExecResult(ok=False, payload={"ok": False, "error": "fail"}),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Find write_file tool results in order
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)
                        and e.name == "write_file"]
        assert len(tool_results) == 3

        # First: fail, no circuit breaker
        assert tool_results[0].ok is False
        assert "CIRCUIT BREAKER" not in tool_results[0].result

        # Second: success, no circuit breaker
        assert tool_results[1].ok is True
        assert "CIRCUIT BREAKER" not in tool_results[1].result

        # Third: fail again, circuit breaker should NOT fire yet
        # because the success reset the counter for the first key
        assert tool_results[2].ok is False
        assert "CIRCUIT BREAKER" not in tool_results[2].result

    def test_three_identical_failures(self, manager, mock_client, mock_tools,
                                       on_event, captured_events, cancel_event,
                                       history):
        """Three identical failures trigger circuit breaker."""
        tc = _tool_call("cb1", "write_file", {"path": "test.py", "content": "data"})

        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        mock_tools.execute.return_value = ToolExecResult(
            ok=False, payload={"ok": False, "error": "fail"}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        tool_results = [e for e in captured_events if isinstance(e, ToolResult)
                        and e.name == "write_file"]
        assert len(tool_results) == 3

        # First two should NOT have circuit breaker
        assert "CIRCUIT BREAKER" not in tool_results[0].result
        assert "CIRCUIT BREAKER" not in tool_results[1].result
        # Third should
        assert "CIRCUIT BREAKER" in tool_results[2].result


# ===================================================================
# 16. reject_all_for_turn propagation
# ===================================================================

class TestRejectAll:
    """reject_all approval decision propagates to subsequent write calls."""

    def test_reject_all_propagation(self, manager, mock_client, mock_tools,
                                    on_event, captured_events, cancel_event,
                                    history):
        tc1 = _tool_call("w1", "write_file", {"path": "a.py", "content": "1"})
        tc2 = _tool_call("w2", "write_file", {"path": "b.py", "content": "2"})

        # First stream returns two tool calls
        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]

        # execute returns reject_all on the first call
        mock_tools.execute.return_value = ToolExecResult(
            ok=False,
            payload={"ok": False, "error": "rejected"},
            extras={"approval": "reject_all", "rel_path": "a.py"},
        )

        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # tools.execute() should only be called ONCE (for tc1)
        assert mock_tools.execute.call_count == 1

        # History should have: assistant, tool_result (for tc1), tool_result (for tc2, auto-rejected)
        tool_msgs = [m for m in history.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2

        # The second tool result should contain "rejected all"
        assert "rejected all" in tool_msgs[1]["content"].lower()


# ===================================================================
# 17. JSON parse error in tool arguments
# ===================================================================

class TestJsonParseError:
    """Invalid JSON in tool arguments yields an error ToolResult."""

    def test_invalid_json(self, manager, mock_client, on_event,
                          captured_events, cancel_event, history):
        tc = {
            "id": "badjson",
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": "not valid json",
            },
        }

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # ToolResult with ok=False and error about JSON parse
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        assert len(tool_results) >= 1
        assert tool_results[0].ok is False
        assert "failed to parse tool arguments" in tool_results[0].result.lower()


# ===================================================================
# 18. _cleanup_cancelled
# ===================================================================

class TestCleanupCancelled:
    """Direct test of the cleanup logic for cancelled turns."""

    def test_cleanup_removes_last_assistant_with_tool_calls(self, manager):
        """If the last message is assistant with tool calls, _cleanup_cancelled pops it."""
        history = manager._history
        history.append_assistant({
            "role": "assistant",
            "content": "some",
            "reasoning_content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}],
        })
        assert len(history.messages) == 1

        on_event = MagicMock()
        manager._cleanup_cancelled(on_event)

        assert len(history.messages) == 0
        on_event.assert_called_once()
        assert isinstance(on_event.call_args[0][0], ApiError)

    def test_cleanup_removes_empty_assistant(self, manager):
        """If the last assistant message has no content and no tool_calls, it's popped."""
        history = manager._history
        history.append_assistant({
            "role": "assistant",
            "content": None,
            "reasoning_content": None,
        })
        assert len(history.messages) == 1

        on_event = MagicMock()
        manager._cleanup_cancelled(on_event)

        assert len(history.messages) == 0

    def test_cleanup_keeps_valid_assistant(self, manager):
        """If the last assistant has content but no tool_calls, keep it."""
        history = manager._history
        history.append_assistant({
            "role": "assistant",
            "content": "Hello",
            "reasoning_content": None,
        })
        assert len(history.messages) == 1

        on_event = MagicMock()
        manager._cleanup_cancelled(on_event)

        assert len(history.messages) == 1  # kept


# ===================================================================
# 19. run_research error handling (already covered in test_research_error)
# ===================================================================

# Additional edge cases

class TestEdgeCases:
    """Additional edge-case coverage."""

    def test_empty_tool_calls_list(self, manager, mock_client, on_event,
                                   captured_events, cancel_event, history):
        """An assistant message with an empty tool_calls list should not loop."""
        mock_client.stream.return_value = [
            _make_done(content="No tools", tool_calls=[]),
        ]
        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )
        assert len(history.messages) == 1
        assert history.messages[0]["content"] == "No tools"

    def test_done_with_reasoning(self, manager, mock_client, on_event,
                                 captured_events, cancel_event, history):
        """Assistant response with reasoning_content is stored correctly."""
        mock_client.stream.return_value = [
            ReasoningDelta(text="Thinking..."),
            ContentDelta(text="Answer"),
            _make_done(content="Answer", reasoning="Thinking..."),
        ]
        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="high",
        )
        assert len(history.messages) == 1
        assert history.messages[0]["reasoning_content"] == "Thinking..."

    def test_multiple_tool_calls_same_round(self, manager, mock_client,
                                            mock_tools, on_event,
                                            captured_events, cancel_event,
                                            history):
        """Multiple tool calls in one round are all executed."""
        tc1 = _tool_call("c1", "write_file", {"path": "a.py", "content": "1"})
        tc2 = _tool_call("c2", "edit_file", {"path": "b.py", "old_str": "x", "new_str": "y"})

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]
        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        mock_tools.execute.return_value = ToolExecResult(
            ok=True, payload={"ok": True}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        assert mock_tools.execute.call_count == 2
        # History should have: assistant, tool, tool, assistant
        assert len(history.messages) == 4

    def test_cancel_during_tool_processing(self, manager, mock_client,
                                           mock_tools, on_event,
                                           captured_events, cancel_event,
                                           history):
        """Cancel is set while iterating over tool calls in a round."""
        tc1 = _tool_call("c1", "write_file", {"path": "a.py", "content": "1"})
        tc2 = _tool_call("c2", "write_file", {"path": "b.py", "content": "2"})

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]

        # Set cancel after first tool call — the second is never processed
        def _execute_and_cancel(**kwargs):
            cancel_event.set()
            return ToolExecResult(ok=True, payload={"ok": True})

        mock_tools.execute.side_effect = _execute_and_cancel

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Only one tool call processed (the first one)
        assert mock_tools.execute.call_count == 1

        # History has assistant + tool_result for the first tool call.
        # The cancel check happens before the second tool call is processed.
        # _cleanup_cancelled only pops if last msg is assistant with tool_calls,
        # but the last msg is a tool_result, so it stays.
        assert len(history.messages) == 2
        assert history.messages[0]["role"] == "assistant"
        assert history.messages[1]["role"] == "tool"

    def test_full_message_none(self, manager, mock_client, on_event,
                               captured_events, cancel_event, history):
        """Stream that yields no Done event at all."""
        mock_client.stream.return_value = [
            ContentDelta(text="Hello"),
            # No Done event
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # No assistant message since full_message was None
        assert len(history.messages) == 0

    def test_cancel_empty_message(self, manager, mock_client, on_event,
                                  captured_events, cancel_event, history):
        """Cancel set during a round where full_message has no content and no tool_calls."""
        def _controlled(**kwargs):
            yield _make_done(content=None)
            cancel_event.set()

        mock_client.stream.side_effect = _controlled

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        assert len(history.messages) == 0
        api_errors = [e for e in captured_events if isinstance(e, ApiError)]
        assert any("cancelled" in e.message.lower() for e in api_errors)

    def test_circuit_breaker_non_json_payload(self, manager, mock_client,
                                               mock_tools, on_event,
                                               captured_events, cancel_event,
                                               history):
        """Circuit breaker handling of non-JSON payload — appends warning to string."""
        tc = _tool_call("cb1", "write_file", {"path": "test.py", "content": "data"})

        mock_client.stream.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        # Create a ToolExecResult with a monkeypatched to_tool_message_content
        from types import MethodType
        non_json_result = ToolExecResult(ok=False, payload={"ok": False})
        non_json_result.to_tool_message_content = MethodType(
            lambda self: "Not JSON at all", non_json_result
        )

        mock_tools.execute.return_value = non_json_result

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        tool_results = [e for e in captured_events if isinstance(e, ToolResult)
                        and e.name == "write_file"]
        assert len(tool_results) == 3
        # The third result should have circuit breaker text appended
        assert "CIRCUIT BREAKER" in tool_results[2].result

    def test_full_message_none_not_cancelled(self, manager, mock_client,
                                              on_event, captured_events,
                                              cancel_event, history):
        """Stream ends without yielding Done — full_message stays None, method returns."""
        mock_client.stream.return_value = [
            ContentDelta(text="Partial"),
            # No Done event at all
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # No assistant message should be in history since full_message was None
        assert len(history.messages) == 0

    def test_reject_all_edit_file(self, manager, mock_client, mock_tools,
                                   on_event, captured_events, cancel_event,
                                   history):
        """reject_all also applies to edit_file."""
        tc1 = _tool_call("e1", "edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"})
        tc2 = _tool_call("e2", "edit_file", {"path": "b.py", "old_str": "a", "new_str": "b"})

        mock_client.stream.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]

        mock_tools.execute.return_value = ToolExecResult(
            ok=False,
            payload={"ok": False, "error": "rejected"},
            extras={"approval": "reject_all", "rel_path": "a.py"},
        )

        mock_client.stream.side_effect = [
            iter(mock_client.stream.return_value),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        # Only one execute call (for the first edit_file)
        assert mock_tools.execute.call_count == 1

        # Both edit_files produce tool results
        tool_msgs = [m for m in history.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        # The second should be "rejected all"
        assert "rejected all" in tool_msgs[1]["content"].lower()
