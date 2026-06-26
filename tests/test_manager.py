"""Comprehensive unit tests for ConversationManager with mocked dependencies."""

from __future__ import annotations

import json
import threading
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
    WorkerDispatchRequested,
)
from aura.hooks import hooks
from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE
from aura.conversation.tools._types import (
    ApprovalDecision,
    ToolExecResult,
)
from aura.conversation.tools.registry import ToolRegistry
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


def _valid_dispatch_args(goal: str = "Fix bug", files: list[str] | None = None, core: str = "Change X to Y") -> dict:
    return {
        "goal": goal,
        "files": files or ["test.py"],
        "spec": (
            "Core Behavior\n"
            f"{core}\n\n"
            "Failure Behavior\n"
            "Preserve existing error behavior and do not add clever fallback behavior unless requested.\n\n"
            "Code Shape\n"
            "Implement the smallest complete change. Use direct app/tool code with no module summary "
            "docstrings or Args/Returns/Raises docstrings.\n\n"
            "File-by-File Implementation Plan\n"
            "- Read the listed files before editing.\n"
            "- Make only the requested change.\n\n"
            "Acceptance Checks\n"
            "- pytest tests/test_manager.py passes and the requested behavior is verified.\n\n"
            "Non-Goals\n"
            "- No unrelated refactors."
        ),
        "acceptance": (
            "Run `pytest tests/test_manager.py` and verify it passes with exit code 0. "
            "Confirm the requested behavior is verified."
        ),
    }


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
    """A MagicMock used as the backend handler registered on the hook."""
    return MagicMock()


@pytest.fixture(autouse=True)
def registered_backend(mock_client):
    """Register mock_client as the handler for both planner and worker hooks."""
    hooks.register('generate_planner_code', mock_client)
    hooks.register('generate_worker_code', mock_client)
    yield
    hooks.unregister('generate_planner_code')
    hooks.unregister('generate_worker_code')


@pytest.fixture
def mock_tools(tmp_path):
    """A MagicMock for ToolRegistry with sensible defaults."""
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    tools.execute.return_value = ToolExecResult(ok=True, payload={"ok": True})
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    type(tools).mode = PropertyMock(return_value="single")
    return tools


@pytest.fixture
def manager(history, mock_tools) -> ConversationManager:
    """A ConversationManager with all three deps mocked/real."""
    return ConversationManager(
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
        mock_client.return_value = [
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
        mock_client.side_effect = [
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

    def test_max_rounds_reached(self, manager, mock_client, mock_tools,
                                on_event, captured_events, cancel_event,
                                history):
        tool_id_template = "call{}"
        def tc_template(i):
            return _tool_call(
                tool_id_template.format(i), "write_file",
                {"path": f"test_{i}.py", "content": "data"}
            )

        # Return tool calls for each round
        side_effects = []
        for i in range(3):  # 3 rounds but max is 2
            side_effects.append(iter([
                _make_done(content="", tool_calls=[tc_template(i)]),
            ]))
        mock_client.side_effect = side_effects

        mock_tools.execute.return_value = ToolExecResult(
            ok=True, payload={"ok": True}
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            max_tool_rounds=2,
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
        mock_client.return_value = [
            ContentDelta(text="Partial "),
            # After yielding ContentDelta, we set cancel_event
        ]

        # We need to set cancel_event *after* the stream starts yielding.
        # Wrap stream to inject the set() call.
        original_iter = iter(mock_client.return_value)

        def _controlled_stream(**kwargs):
            for ev in original_iter:
                yield ev
                if isinstance(ev, ContentDelta):
                    cancel_event.set()
            yield _make_done(content="Partial ")

        mock_client.side_effect = None
        mock_client.side_effect = _controlled_stream

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

        mock_client.return_value = [
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

        mock_client.side_effect = _controlled
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
    """dispatch_to_worker tool integration tests."""

    def test_dispatch_ok(self, manager, mock_client, mock_tools, on_event,
                         captured_events, cancel_event, history):
        type(mock_tools).mode = PropertyMock(return_value="planner")
        tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        dispatch_cb = MagicMock()
        dispatch_cb.return_value = WorkerDispatchResult(
            ok=True, summary="done", cancelled=False
        )

        # Need a second stream call for the follow-up round
        mock_client.side_effect = [
            iter(mock_client.return_value),  # first round (dispatch)
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

def test_dispatch_no_callback(manager, mock_client, mock_tools, on_event,
                                  captured_events, cancel_event, history):
        type(mock_tools).mode = PropertyMock(return_value="planner")
        tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())

        # Stream first yields the tool call, then yields a content-only response
        mock_client.side_effect = [
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

def test_dispatch_allows_specs_without_quality_sections(manager, mock_client, mock_tools, on_event,
                                                        captured_events, cancel_event, history):
        type(mock_tools).mode = PropertyMock(return_value="planner")
        tc = _tool_call("dispatch1", "dispatch_to_worker", {
            "goal": "Fix bug",
            "files": ["test.py"],
            "spec": "Change X to Y",
            "acceptance": "Verify X changes to Y.",
        })
        dispatch_cb = MagicMock(return_value=WorkerDispatchResult(ok=True, summary="done"))

        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([ContentDelta(text="Done."), _make_done(content="Done.")]),
        ]

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )

        dispatch_cb.assert_called_once()
        assert any(isinstance(e, WorkerDispatchRequested) for e in captured_events)

        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        dispatch_results = [tr for tr in tool_results if tr.name == "dispatch_to_worker"]
        assert len(dispatch_results) == 1
        assert dispatch_results[0].ok is True
        parsed = json.loads(dispatch_results[0].result)
        assert parsed["ok"] is True
        assert history.messages[-1]["content"] == "Done."

def test_dispatch_cb_raises(manager, mock_client, mock_tools, on_event,
                                captured_events, cancel_event, history):
        type(mock_tools).mode = PropertyMock(return_value="planner")
        tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())

        def _raising_cb(tool_call_id, req):
            raise RuntimeError("boom")

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        mock_client.side_effect = [
            iter(mock_client.return_value),
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

        # ToolResult with ok=False and a generic internal-error summary
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        dispatch_results = [tr for tr in tool_results if tr.name == "dispatch_to_worker"]
        assert len(dispatch_results) >= 1
        assert dispatch_results[-1].ok is False
        parsed = json.loads(dispatch_results[-1].result)
        assert parsed["summary"] == "Harness error due to an internal Worker dispatch exception."
        assert parsed["extras"]["worker_internal_error"] is True
        assert "RuntimeError" not in parsed["summary"]

def test_dispatch_spec_rejection_is_plan_incomplete_not_worker_started(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    args = _valid_dispatch_args()
    args["acceptance"] = ""
    tc = _tool_call("dispatch1", "dispatch_to_worker", args)
    dispatch_cb = MagicMock()
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([ContentDelta(text="Plan fixed"), _make_done(content="Plan fixed")]),
    ]

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    dispatch_cb.assert_not_called()
    assert not any(isinstance(e, WorkerDispatchRequested) for e in captured_events)
    dispatch_result = next(
        e for e in captured_events
        if isinstance(e, ToolResult) and e.name == "dispatch_to_worker"
    )
    assert dispatch_result.ok is True
    parsed = json.loads(dispatch_result.result)
    assert parsed["ok"] is False
    assert parsed["summary"].startswith("Plan incomplete")
    assert parsed["extras"]["dispatch_not_started"] is True
    assert parsed["extras"]["dispatch_spec_rejected"] is True
    assert history.messages[-1]["content"] == "Plan fixed"

def test_recoverable_worker_phase_boundary_allows_planner_to_continue(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([ContentDelta(text="Continuing"), _make_done(content="Continuing")]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=False,
        summary="Worker pass limit reached.",
        needs_followup=True,
        phase_boundary=True,
        recoverable=True,
        followup_reason="worker_tool_call_limit_reached",
        completed=["Read files"],
        remaining=["Finish change"],
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert dispatch_cb.call_count == 1
    assert mock_client.call_count == 2
    api_errors = [e for e in captured_events if isinstance(e, ApiError)]
    assert api_errors == []
    assert history.messages[-1]["content"] == "Continuing"


def test_completed_worker_result_accepts_one_final_message(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([
            ContentDelta(text="All set. Validation passed."),
            _make_done(content="All set. Validation passed."),
        ]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=True,
        summary="done",
        needs_followup=False,
        status="completed",
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert mock_client.call_count == 2
    assert history.messages[-1]["content"] == "All set. Validation passed."
    assert any(
        isinstance(e, ContentDelta) and e.text == "All set. Validation passed."
        for e in captured_events
    )


def test_worker_modified_files_adds_planner_stale_read_notice(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([
            ContentDelta(text="All set. Worker finished."),
            _make_done(content="All set. Worker finished."),
        ]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=True,
        summary="done",
        needs_followup=False,
        status="completed",
        modified_files=["aura/foo.py"],
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    notices = [
        message for message in history.messages
        if message["role"] == "user"
        and "Planner stale-read invalidation" in message["content"]
    ]
    assert len(notices) == 1
    notice = notices[0]["content"]
    assert "- aura/foo.py" in notice
    assert "Any prior Planner reads of those paths are stale." in notice
    assert "Re-read the modified files before planning, dispatching, or reasoning" in notice
    assert "do not redispatch because of this notice" in notice

    tool_index = next(
        index for index, message in enumerate(history.messages)
        if message["role"] == "tool" and message["tool_call_id"] == "dispatch1"
    )
    notice_index = history.messages.index(notices[0])
    final_index = next(
        index for index, message in enumerate(history.messages)
        if message["role"] == "assistant" and message.get("content") == "All set. Worker finished."
    )
    assert tool_index < notice_index < final_index

    second_round_messages = mock_client.call_args_list[1].kwargs["messages"]
    assert any(
        message["role"] == "user"
        and "Any prior Planner reads of those paths are stale." in message["content"]
        for message in second_round_messages
    )


def test_completed_worker_result_stops_before_second_repetitive_final(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([
            ContentDelta(text="All set. Staged and ready."),
            _make_done(content="All set. Staged and ready."),
        ]),
        iter([
            ContentDelta(text="All set, staged and ready. Let me know if you need anything else."),
            _make_done(
                content="All set, staged and ready. Let me know if you need anything else."
            ),
        ]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=True,
        summary="done",
        needs_followup=False,
        status="completed",
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert mock_client.call_count == 2
    assert history.messages[-1]["content"] == "All set. Staged and ready."
    assert not any(
        isinstance(e, ContentDelta) and "Let me know" in e.text
        for e in captured_events
    )


def test_repeated_completion_chatter_does_not_stream_indefinitely(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([
            ContentDelta(text="All set. Everything else is in good shape."),
            _make_done(content="All set. Everything else is in good shape."),
        ]),
        iter([
            ContentDelta(text="All set. No further action needed. Let me know."),
            _make_done(content="All set. No further action needed. Let me know."),
        ]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=True,
        summary="done",
        needs_followup=False,
        status="completed_with_caveats",
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert mock_client.call_count == 2
    streamed_text = "".join(e.text for e in captured_events if isinstance(e, ContentDelta))
    assert "No further action needed" not in streamed_text


def test_normal_explanation_without_worker_completion_is_not_blocked(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    mock_client.side_effect = [
        iter([
            ContentDelta(text="All set means the task has no remaining work."),
            _make_done(content="All set means the task has no remaining work."),
        ]),
    ]

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=MagicMock(),
    )

    assert mock_client.call_count == 1
    assert history.messages[-1]["content"] == "All set means the task has no remaining work."


def test_successful_builtin_write_gets_one_final_message(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="single")
    tc = _tool_call("write1", "write_file", {"path": "a.txt", "content": "done"})
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc])]),
        iter([
            ContentDelta(text="All set. Wrote the file."),
            _make_done(content="All set. Wrote the file."),
        ]),
        iter([
            ContentDelta(text="All set, committed and done. Let me know if you need anything else."),
            _make_done(
                content="All set, committed and done. Let me know if you need anything else."
            ),
        ]),
    ]

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=MagicMock(),
    )

    assert mock_client.call_count == 2
    assert history.messages[-1]["content"] == "All set. Wrote the file."
    streamed_text = "".join(e.text for e in captured_events if isinstance(e, ContentDelta))
    assert "committed and done" not in streamed_text


def test_redispatch_counter_stops_runaway_followups(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    dispatches = [
        _tool_call(
            f"dispatch{i}",
            "dispatch_to_worker",
            _valid_dispatch_args(goal=f"Pass {i}", core=f"Do pass {i}"),
        )
        for i in range(3)
    ]
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[dispatches[0]])]),
        iter([_make_done(content="", tool_calls=[dispatches[1]])]),
        iter([_make_done(content="", tool_calls=[dispatches[2]])]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=False,
        summary="Worker pass limit reached.",
        needs_followup=True,
        phase_boundary=True,
        recoverable=True,
        followup_reason="worker_tool_call_limit_reached",
        completed=["Some work"],
        remaining=["More work"],
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert dispatch_cb.call_count == 2
    # Suppressed visible card: no ContentDelta, Done with empty content
    content_deltas = [ev for ev in captured_events if isinstance(ev, ContentDelta)]
    assert len(content_deltas) == 0
    done_events = [ev for ev in captured_events if isinstance(ev, Done)]
    assert len(done_events) >= 1
    # The last Done (from _append_dispatch_blocker_message) has empty content
    last_done = done_events[-1]
    assert last_done.full_message.get("content") == ""


def test_identical_dispatch_failure_stops_after_second_attempt(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    args = _valid_dispatch_args(goal="Same goal", core="Same change")
    dispatches = [
        _tool_call(f"dispatch{i}", "dispatch_to_worker", args)
        for i in range(3)
    ]
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[dispatches[0]])]),
        iter([_make_done(content="", tool_calls=[dispatches[1]])]),
        iter([_make_done(content="", tool_calls=[dispatches[2]])]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=False,
        summary="Worker still needs validation.",
        needs_followup=True,
        recoverable=True,
        remaining=["Run validation"],
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert dispatch_cb.call_count == 2
    # Suppressed visible card: no ContentDelta, Done with empty content
    content_deltas = [ev for ev in captured_events if isinstance(ev, ContentDelta)]
    assert len(content_deltas) == 0
    done_events = [ev for ev in captured_events if isinstance(ev, Done)]
    assert len(done_events) >= 1
    last_done = done_events[-1]
    assert last_done.full_message.get("content") == ""


def test_worker_internal_error_stops_without_redispatch(
    manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
):
    type(mock_tools).mode = PropertyMock(return_value="planner")
    tc1 = _tool_call("dispatch1", "dispatch_to_worker", _valid_dispatch_args())
    tc2 = _tool_call("dispatch2", "dispatch_to_worker", _valid_dispatch_args())
    mock_client.side_effect = [
        iter([_make_done(content="", tool_calls=[tc1])]),
        iter([_make_done(content="", tool_calls=[tc2])]),
    ]
    dispatch_cb = MagicMock(return_value=WorkerDispatchResult(
        ok=False,
        summary="Harness error due to an internal Worker dispatch exception.",
        recoverable=False,
        extras={"worker_internal_error": True, "internal_error": "AttributeError: hidden"},
    ))

    manager.send(
        on_event=on_event,
        approval_cb=_make_approval_cb(),
        cancel_event=cancel_event,
        model="deepseek-chat",
        thinking="off",
        dispatch_cb=dispatch_cb,
    )

    assert dispatch_cb.call_count == 1
    # Suppressed visible card: no ContentDelta, Done with empty content
    content_deltas = [ev for ev in captured_events if isinstance(ev, ContentDelta)]
    assert len(content_deltas) == 0
    done_events = [ev for ev in captured_events if isinstance(ev, Done)]
    assert len(done_events) >= 1
    last_done = done_events[-1]
    assert last_done.full_message.get("content") == ""


# ===================================================================
# 10. run_terminal_command basic
# ===================================================================

class TestRunTerminalCommand:
    """Terminal command execution via SandboxExecutor."""

    @patch("aura.conversation.tool_runner.SandboxExecutor")
    @patch("aura.conversation.tool_runner.load_settings")
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
        mock_client.side_effect = [
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

        # SandboxExecutor was created with correct args        mock_sandbox_cls.assert_called_once()
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

    @patch("aura.conversation.tool_runner.SandboxExecutor")
    @patch("aura.conversation.tool_runner.load_settings")
    def test_terminal_missing_command(self, mock_load_settings, mock_sandbox_cls,
                                      manager, mock_client, on_event,
                                      captured_events, cancel_event, history,
                                      tmp_path):
        fake_settings = MagicMock()
        fake_settings.sandbox_mode = "host"
        mock_load_settings.return_value = fake_settings

        tc = _tool_call("term1", "run_terminal_command", {})  # no command

        mock_client.side_effect = [
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

    @patch("aura.conversation.tool_runner.ToolRegistry")
    def test_research_ok(self, mock_tool_registry_cls, manager, mock_client,
                         mock_tools, on_event, captured_events, cancel_event,
                         history, tmp_path):
        type(mock_tools).workspace_root = PropertyMock(return_value=tmp_path)
        type(mock_tools).mode = PropertyMock(return_value="planner")

        # Mock the ToolRegistry created inside _handle_research
        mock_res_tools = MagicMock(spec=ToolRegistry)
        mock_res_tools.tool_defs.return_value = []
        mock_tool_registry_cls.return_value = mock_res_tools

        tc = _tool_call("res1", "run_research", {"objective": "test query"})

        # First stream: outer loop yields research tool call
        # Second stream: research loop yields content directly (no tool calls)
        # Third stream: outer loop's next round yields content -> loop ends
        mock_client.side_effect = [
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

    @patch("aura.conversation.tool_runner.ToolRegistry")
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
        mock_client.side_effect = [
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

    @patch("aura.conversation.tool_runner.ToolRegistry")
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
        mock_client.side_effect = [
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

    @patch("aura.conversation.tool_runner.ToolRegistry")
    def test_research_no_objective(self, mock_tool_registry_cls, manager,
                                   mock_client, on_event, captured_events,
                                   cancel_event, history, tmp_path):
        """run_research with missing objective."""
        type(manager._tools).workspace_root = PropertyMock(return_value=tmp_path)

        tc = _tool_call("res1", "run_research", {})  # no objective

        # Only needs one stream — error path returns immediately
        mock_client.side_effect = [
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
        mock_client.return_value = [
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
        mock_client.side_effect = [
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
        mock_client.side_effect = [
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

        mock_client.side_effect = [
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

    def test_worker_loop_detection_creates_phase_boundary(
        self, manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
    ):
        """Worker repeated failures stop the pass and request planner recovery."""
        type(mock_tools).mode = PropertyMock(return_value="worker")
        tc = _tool_call("cb1", "write_file", {"path": "test.py", "content": "data"})

        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([
                ContentDelta(text="<continuation_report>"),
                _make_done(content=(
                    "<continuation_report>\n"
                    "<status>needs_followup</status>\n"
                    "<reason>loop_detected</reason>\n"
                    "<completed>\n- Attempted write\n</completed>\n"
                    "<modified_files>\n</modified_files>\n"
                    "<validation>Not run</validation>\n"
                    "<remaining>\n- Planner should revise the approach\n</remaining>\n"
                    "<recommended_next_step>Dispatch with a different fix strategy.</recommended_next_step>\n"
                    "</continuation_report>"
                )),
            ]),
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

        assert mock_tools.execute.call_count == 3
        tool_results = [
            e for e in captured_events if isinstance(e, ToolResult) and e.name == "write_file"
        ]
        parsed = json.loads(tool_results[2].result)
        assert parsed["loop_detected"] is True
        assert parsed["recoverable"] is True
        assert parsed["phase_boundary"] is True
        assert parsed["reason"] == "loop_detected"
        assert history.messages[-1]["content"].startswith("<continuation_report>")

    def test_worker_repeated_todo_update_creates_phase_boundary(
        self, manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
    ):
        """Repeated identical TODO updates are treated as no-progress loops."""
        type(mock_tools).mode = PropertyMock(return_value="worker")
        args = {"tasks": [{"description": "Read files", "status": "active"}]}
        tc = _tool_call("todo1", "update_todo_list", args)

        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([_make_done(content="", tool_calls=[tc])]),
            iter([
                ContentDelta(text="<continuation_report>"),
                _make_done(content=(
                    "<continuation_report>\n"
                    "<status>needs_followup</status>\n"
                    "<reason>repeated_no_progress</reason>\n"
                    "<completed>\n- Updated TODO list\n</completed>\n"
                    "<modified_files>\n</modified_files>\n"
                    "<validation>Not run</validation>\n"
                    "<remaining>\n- Move beyond the repeated TODO update\n</remaining>\n"
                    "<recommended_next_step>Continue with implementation.</recommended_next_step>\n"
                    "</continuation_report>"
                )),
            ]),
        ]
        mock_tools.execute.return_value = ToolExecResult(
            ok=True,
            payload={"ok": True, "message": "TODO list updated", "tasks": args["tasks"]},
            extras={"is_todo_update": True, "tasks": args["tasks"]},
        )

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        assert mock_tools.execute.call_count == 3
        tool_results = [
            e for e in captured_events if isinstance(e, ToolResult) and e.name == "update_todo_list"
        ]
        parsed = json.loads(tool_results[2].result)
        assert parsed["loop_detected"] is True
        assert parsed["recoverable"] is True
        assert parsed["phase_boundary"] is True
        assert parsed["reason"] == "repeated_no_progress"
        assert history.messages[-1]["content"].startswith("<continuation_report>")


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
        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]

        # execute returns reject_all on the first call
        mock_tools.execute.return_value = ToolExecResult(
            ok=False,
            payload={"ok": False, "error": "rejected"},
            extras={"approval": "reject_all", "rel_path": "a.py"},
        )

        mock_client.side_effect = [
            iter(mock_client.return_value),
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

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc]),
        ]

        mock_client.side_effect = [
            iter(mock_client.return_value),
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
        mock_client.return_value = [
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
        mock_client.return_value = [
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

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]
        mock_client.side_effect = [
            iter(mock_client.return_value),
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

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]
        history.append_user_text("hello")

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

        # _cleanup_cancelled now identifies that some tool calls are missing results
        # and truncates the history back to before the assistant message.
        # It keeps the user message that started the turn.
        assert len(history.messages) == 1
        assert history.messages[0]["role"] == "user"

    def test_full_message_none(self, manager, mock_client, on_event,
                               captured_events, cancel_event, history):
        """Stream that yields no Done event at all."""
        mock_client.return_value = [
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

        mock_client.side_effect = _controlled

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

        mock_client.side_effect = [
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
        mock_client.return_value = [
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

        mock_client.return_value = [
            _make_done(content="", tool_calls=[tc1, tc2]),
        ]

        mock_tools.execute.return_value = ToolExecResult(
            ok=False,
            payload={"ok": False, "error": "rejected"},
            extras={"approval": "reject_all", "rel_path": "a.py"},
        )

        mock_client.side_effect = [
            iter(mock_client.return_value),
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


# ===================================================================
# Tool limit integration tests
# ===================================================================


class TestToolLimitIntegration:
    """Test that tool limits reject tools and create proper tool results."""

    def test_planner_allows_multiple_dispatches_in_same_round(self, manager, mock_client, mock_tools,
                                                              on_event, captured_events, cancel_event,
                                                              history):
        """Planner dispatch is not category-capped."""
        type(mock_tools).mode = PropertyMock(return_value="planner")
        tc1 = _tool_call(
            "d1",
            "dispatch_to_worker",
            _valid_dispatch_args(goal="Fix A", files=["a.py"], core="Change A"),
        )
        tc2 = _tool_call("d2", "dispatch_to_worker", {
            "goal": "Fix B",
            "files": ["b.py"],
            "spec": "Change B",
            "acceptance": "Verify B changed.",
        })
        dispatch_cb = MagicMock(return_value=WorkerDispatchResult(ok=True, summary="done"))
        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=[tc1, tc2])]),
            iter([ContentDelta(text="Done."), _make_done(content="Done.")]),
        ]
        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
            dispatch_cb=dispatch_cb,
        )
        assert dispatch_cb.call_count == 2
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        dispatch_results = [tr for tr in tool_results if tr.name == "dispatch_to_worker"]
        assert len(dispatch_results) == 2
        assert dispatch_results[0].ok is True
        assert dispatch_results[1].ok is True
        tool_msgs = [m for m in history.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        assert history.messages[-1]["content"] == "Done."

    def test_worker_limit_allows_many_reads(self, manager, mock_client, mock_tools,
                                            on_event, captured_events, cancel_event,
                                            history):
        """Worker tool limit allows many read_file calls below the cap."""
        type(mock_tools).mode = PropertyMock(return_value="worker")
        # Build 30 tool calls (all read_file) in one round
        tcs = [_tool_call(f"r{i}", "read_file", {"path": f"file{i}.py"}) for i in range(30)]
        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=tcs)]),
            iter([ContentDelta(text="Done"), _make_done(content="Done")]),
        ]
        mock_tools.execute.return_value = ToolExecResult(ok=True, payload={"ok": True, "content": "data"})
        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )
        # All 30 read calls should succeed
        assert mock_tools.execute.call_count == 30
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        assert len(tool_results) == 30
        assert all(tr.ok for tr in tool_results)

    def test_worker_emergency_limit_produces_phase_boundary_without_dangling_tools(
        self, manager, mock_client, mock_tools, on_event, captured_events, cancel_event, history
    ):
        """Worker emergency guard rejects cleanly and allows a final no-tool report."""
        type(mock_tools).mode = PropertyMock(return_value="worker")
        limit = MAX_TOOL_CALLS_BY_MODE["worker"]
        tcs = [_tool_call(f"r{i}", "read_file", {"path": f"file{i}.py"}) for i in range(limit + 1)]
        mock_client.side_effect = [
            iter([_make_done(content="", tool_calls=tcs)]),
            iter([
                ContentDelta(text="<continuation_report>"),
                _make_done(content=(
                    "<continuation_report>\n"
                    "<status>needs_followup</status>\n"
                    "<reason>tool_limit_reached</reason>\n"
                    "<completed>\n- Read many files\n</completed>\n"
                    "<modified_files>\n</modified_files>\n"
                    "<validation>Not run</validation>\n"
                    "<remaining>\n- Continue work\n</remaining>\n"
                    "<recommended_next_step>Dispatch a narrower pass</recommended_next_step>\n"
                    "</continuation_report>"
                )),
            ]),
        ]
        mock_tools.execute.return_value = ToolExecResult(ok=True, payload={"ok": True})

        manager.send(
            on_event=on_event,
            approval_cb=_make_approval_cb(),
            cancel_event=cancel_event,
            model="deepseek-chat",
            thinking="off",
        )

        assert mock_tools.execute.call_count == limit
        tool_results = [e for e in captured_events if isinstance(e, ToolResult)]
        rejected = [tr for tr in tool_results if tr.ok is False]
        assert len(rejected) == 1
        parsed = json.loads(rejected[0].result)
        assert parsed["recoverable"] is True
        assert parsed["phase_boundary"] is True
        assert parsed["reason"] == "worker_emergency_tool_call_limit_reached"
        assistant_tool_calls = [
            tc["id"]
            for msg in history.messages
            if msg.get("role") == "assistant"
            for tc in (msg.get("tool_calls") or [])
        ]
        tool_result_ids = [
            msg.get("tool_call_id") for msg in history.messages if msg.get("role") == "tool"
        ]
        assert set(assistant_tool_calls).issubset(set(tool_result_ids))


class TestDeleteFileRecovery:
    """Tests for delete_file recovery state handling in _update_worker_recovery_state."""

    def test_delete_file_does_not_add_to_syntax_validation(self, manager):
        """delete_file should not add path to syntax_validation_required."""
        edit_fallback_required: dict = {}
        line_range_reread_required: dict = {}
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        write_attempts_by_path: dict = {}
        worker_file_state: dict = {}
        patch_failed_cycles: dict = {}
        edit_failed_shapes: set[str] = set()

        manager._update_worker_recovery_state(
            name="delete_file",
            args={"path": "old.py"},
            ok=True,
            content='{"ok": true, "deleted": true, "path": "old.py"}',
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
        )

        assert "old.py" not in syntax_validation_required
        assert len(syntax_validation_required) == 0

    def test_delete_file_clears_existing_syntax_state(self, manager):
        """delete_file should clear pre-existing syntax state for the path."""
        edit_fallback_required: dict = {}
        line_range_reread_required: dict = {}
        syntax_repair_required: dict = {
            "old.py": {"repair_attempted": True, "awaiting_validation": True},
        }
        syntax_validation_required: set[str] = {"old.py"}
        write_attempts_by_path: dict = {}
        worker_file_state: dict = {}
        patch_failed_cycles: dict = {}
        edit_failed_shapes: set[str] = set()

        manager._update_worker_recovery_state(
            name="delete_file",
            args={"path": "old.py"},
            ok=True,
            content='{"ok": true, "deleted": true, "path": "old.py"}',
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
        )

        assert "old.py" not in syntax_validation_required
        assert len(syntax_repair_required) == 0

    def test_write_file_still_schedules_validation(self, manager):
        """write_file on a .py path should still add to syntax_validation_required."""
        edit_fallback_required: dict = {}
        line_range_reread_required: dict = {}
        syntax_repair_required: dict = {}
        syntax_validation_required: set[str] = set()
        write_attempts_by_path: dict = {}
        worker_file_state: dict = {}
        patch_failed_cycles: dict = {}
        edit_failed_shapes: set[str] = set()

        manager._update_worker_recovery_state(
            name="write_file",
            args={"path": "new.py"},
            ok=True,
            content='{"ok": true, "path": "new.py"}',
            edit_failed_shapes=edit_failed_shapes,
            edit_fallback_required=edit_fallback_required,
            line_range_reread_required=line_range_reread_required,
            syntax_repair_required=syntax_repair_required,
            syntax_validation_required=syntax_validation_required,
            write_attempts_by_path=write_attempts_by_path,
            worker_file_state=worker_file_state,
            patch_failed_cycles=patch_failed_cycles,
        )

        assert "new.py" in syntax_validation_required


# ===================================================================
# 18. Redispatch blocker suppression
# ===================================================================

class TestRedispatchBlockerSuppression:
    """_append_dispatch_blocker_message must suppress visible output."""

    def test_no_content_delta_emitted(self, manager, on_event, captured_events, history):
        """No ContentDelta event is fired."""
        from aura.conversation.dispatch import WorkerDispatchResult
        result = WorkerDispatchResult(ok=False, summary="fail")
        manager._append_dispatch_blocker_message(result, "limit", on_event)
        for ev in captured_events:
            assert not isinstance(ev, ContentDelta), "Must not emit ContentDelta"

    def test_no_assistant_message_appended_to_history(self, manager, on_event, captured_events, history):
        """No assistant message is added to history."""
        from aura.conversation.dispatch import WorkerDispatchResult
        before = len(history.messages)
        result = WorkerDispatchResult(ok=False, summary="fail")
        manager._append_dispatch_blocker_message(result, "limit", on_event)
        assert len(history.messages) == before, "Must not append to history"

    def test_done_emitted_with_empty_content(self, manager, on_event, captured_events, history):
        """Done event is emitted with empty content to close stream."""
        from aura.conversation.dispatch import WorkerDispatchResult
        result = WorkerDispatchResult(ok=False, summary="fail")
        manager._append_dispatch_blocker_message(result, "limit", on_event)
        done_events = [ev for ev in captured_events if isinstance(ev, Done)]
        assert len(done_events) == 1, "Exactly one Done must be emitted"
        full = done_events[0].full_message
        assert full.get("content") == "", "Done content must be empty string"

    def test_all_reasons_suppressed(self, manager, on_event, captured_events, history):
        """All four reason codes suppress ContentDelta."""
        from aura.conversation.dispatch import WorkerDispatchResult
        for reason in ("internal", "repeated", "limit", "unknown"):
            captured_events.clear()
            result = WorkerDispatchResult(ok=False, summary="fail")
            if reason == "repeated":
                result = WorkerDispatchResult(ok=False, summary="fail", extras={"dispatch_spec_rejected": True})
            manager._append_dispatch_blocker_message(result, reason, on_event)
            for ev in captured_events:
                assert not isinstance(ev, ContentDelta), f"Reason '{reason}' must not emit ContentDelta"
