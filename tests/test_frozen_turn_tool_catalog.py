"""Phase 3C: the model-facing tool catalog is frozen once per ``send()``.

``ConversationManager.send`` used to call ``ToolRegistry.tool_defs()`` inside
its ``while True`` round loop, so a dynamic-tool rescan, a newly connected MCP
server, or any other mid-turn catalog change could silently alter the request
surface between rounds of the *same* user turn. These tests drive the real
``ConversationManager``/``ToolRegistry``/``ToolRoundRunner`` trio (never a
fake) against a scripted multi-round provider stream and prove the catalog is
resolved exactly once per send, reused verbatim for every request and
preflight in that send, and only re-resolved on the next send.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from aura.client import Done, Event, ToolCallStart, ToolResult
from aura.conversation import ConversationManager, History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tools import ToolRegistry
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.schemas import WORKSPACE_SNAPSHOT_TOOL_DEF
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _ScriptedStream:
    """Replays one fixed round per call: a tool-call batch, or plain content.

    ``rounds`` is a list where each entry is either a list of tool-call dicts
    (the assistant calls tools this round) or ``None`` (the assistant answers
    with plain content and the turn ends). An optional ``on_call`` hook runs
    synchronously at the top of each invocation, before any event is yielded,
    so a test can mutate workspace/registry state mid-send at a precise round
    boundary.
    """

    def __init__(self, rounds: list[list[dict] | None], on_call=None) -> None:
        self._rounds = rounds
        self._on_call = on_call
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        idx = len(self.calls)
        self.calls.append(kwargs)
        if self._on_call is not None:
            self._on_call(idx)
        round_calls = self._rounds[idx]
        if round_calls is None:
            yield Done(
                finish_reason="stop",
                full_message={"role": "assistant", "content": "done"},
            )
            return
        for i, tc in enumerate(round_calls):
            yield ToolCallStart(index=i, id=tc["id"], name=tc["function"]["name"])
        yield Done(
            finish_reason="tool_calls",
            full_message={
                "role": "assistant",
                "content": "",
                "tool_calls": round_calls,
            },
        )


def _run_send(
    manager: ConversationManager, stream: _ScriptedStream
) -> list[Event]:
    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    events: list[Event] = []
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
    return events


def _tool_results(events: list[Event]) -> dict[str, ToolResult]:
    return {e.tool_call_id: e for e in events if isinstance(e, ToolResult)}


def _names(defs: list[dict]) -> set[str]:
    return {d["function"]["name"] for d in defs}


def _make_manager(tmp_path: Path) -> tuple[ConversationManager, ToolRegistry, History]:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    history = History()
    history.append_user_text("Inspect and report on the request.")
    tools = ToolRegistry(tmp_path)
    manager = ConversationManager(history, tools)
    return manager, tools, history


def test_tool_defs_resolved_exactly_once_per_send(tmp_path: Path, monkeypatch) -> None:
    manager, tools, _history = _make_manager(tmp_path)

    original_tool_defs = ToolRegistry.tool_defs
    call_count = 0

    def _counting_tool_defs(self):
        nonlocal call_count
        call_count += 1
        return original_tool_defs(self)

    monkeypatch.setattr(ToolRegistry, "tool_defs", _counting_tool_defs)

    rounds = [
        [_tool_call("call-1", "read_file", {"path": "a.py"})],
        [_tool_call("call-2", "read_file", {"path": "a.py"})],
        None,
    ]
    stream = _ScriptedStream(rounds)
    events = _run_send(manager, stream)

    assert len(stream.calls) == 3
    assert call_count == 1
    results = _tool_results(events)
    assert results["call-1"].ok is True
    assert results["call-2"].ok is True


def test_every_request_and_preflight_receives_the_same_frozen_catalog(
    tmp_path: Path, monkeypatch
) -> None:
    manager, tools, _history = _make_manager(tmp_path)

    captured_preflight_defs: list[list[dict] | None] = []
    original_run = ToolRoundRunner.run

    def _spy_run(self, **kwargs):
        captured_preflight_defs.append(kwargs.get("tool_defs"))
        return original_run(self, **kwargs)

    monkeypatch.setattr(ToolRoundRunner, "run", _spy_run)

    rounds = [
        [_tool_call("call-1", "read_file", {"path": "a.py"})],
        [_tool_call("call-2", "read_file", {"path": "a.py"})],
        None,
    ]
    stream = _ScriptedStream(rounds)
    _run_send(manager, stream)

    assert len(stream.calls) == 3
    request_catalogs = [c["tools"] for c in stream.calls]
    # Every provider request in the send carries the exact same object.
    assert request_catalogs[0] is request_catalogs[1] is request_catalogs[2]
    # Every tool-round preflight in the send received that same object too.
    assert len(captured_preflight_defs) == 2
    assert captured_preflight_defs[0] is request_catalogs[0]
    assert captured_preflight_defs[1] is request_catalogs[0]


def test_dynamic_tool_added_after_the_first_round_does_not_reach_later_requests(
    tmp_path: Path,
) -> None:
    """A ``.aura/tools/`` script dropped mid-send must not widen the catalog
    of any request already in flight for this turn — only the next send sees
    it, exactly as a workspace rescan mid-turn must not."""
    manager, tools, _history = _make_manager(tmp_path)
    tools_dir = tmp_path / ".aura" / "tools"

    def _drop_dynamic_tool_after_round_0(idx: int) -> None:
        if idx != 0:
            return
        tools_dir.mkdir(parents=True, exist_ok=True)
        (tools_dir / "surprise.py").write_text(
            'def surprise_tool(x: str):\n    """Doc."""\n    return {}\n',
            encoding="utf-8",
        )

    rounds = [
        [_tool_call("call-1", "read_file", {"path": "a.py"})],
        [_tool_call("call-2", "surprise_tool", {"x": "y"})],
        None,
    ]
    stream = _ScriptedStream(rounds, on_call=_drop_dynamic_tool_after_round_0)
    events = _run_send(manager, stream)

    assert len(stream.calls) == 3
    for call in stream.calls:
        assert "surprise_tool" not in _names(call["tools"])

    results = _tool_results(events)
    assert results["call-2"].ok is False
    assert results["call-2"].extras.get("failure_class") == "tool_call_not_exposed"

    # The file really did land, and a *fresh* catalog resolution (the next
    # send) does see it — only the send already in flight was shielded.
    assert "surprise_tool" in _names(tools.tool_defs())


def test_a_subsequent_send_resolves_a_fresh_catalog_and_sees_the_change(
    tmp_path: Path,
) -> None:
    manager, tools, history = _make_manager(tmp_path)

    # First send: plain answer, no tool calls, catalog has no surprise_tool.
    first_stream = _ScriptedStream([None])
    first_events = _run_send(manager, first_stream)
    assert not _tool_results(first_events)
    assert "surprise_tool" not in _names(first_stream.calls[0]["tools"])

    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True, exist_ok=True)
    (tools_dir / "surprise.py").write_text(
        'def surprise_tool(x: str):\n    """Doc."""\n    return {}\n',
        encoding="utf-8",
    )

    history.append_user_text("Now call the new capability.")
    second_stream = _ScriptedStream(
        [[_tool_call("call-1", "surprise_tool", {"x": "y"})], None]
    )
    second_events = _run_send(manager, second_stream)

    assert "surprise_tool" in _names(second_stream.calls[0]["tools"])
    results = _tool_results(second_events)
    assert results["call-1"].extras.get("failure_class") != "tool_call_not_exposed"
    assert results["call-1"].ok is True


def test_optional_tool_present_at_turn_start_remains_callable_on_a_later_round(
    tmp_path: Path, monkeypatch
) -> None:
    """``get_workspace_snapshot`` is a genuine registered handler the
    production catalog never advertises by default. Simulate it having been
    available (e.g. via a connected capability) at the moment this turn's
    catalog was resolved, and prove a later round can still call it."""
    manager, tools, _history = _make_manager(tmp_path)

    original_tool_defs = ToolRegistry.tool_defs
    call_count = 0

    def _extended_tool_defs(self):
        nonlocal call_count
        call_count += 1
        return original_tool_defs(self) + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]

    monkeypatch.setattr(ToolRegistry, "tool_defs", _extended_tool_defs)

    rounds = [
        [_tool_call("call-1", "read_file", {"path": "a.py"})],
        [_tool_call("call-2", "get_workspace_snapshot", {})],
        None,
    ]
    stream = _ScriptedStream(rounds)
    events = _run_send(manager, stream)

    assert call_count == 1
    results = _tool_results(events)
    assert results["call-2"].extras.get("failure_class") != "tool_call_not_exposed"
    assert results["call-2"].ok is True


def test_optional_tool_absent_at_turn_start_does_not_become_callable_midway(
    tmp_path: Path, monkeypatch
) -> None:
    """A capability that connects *during* a send (after the frozen catalog
    was already resolved) must not make its tool callable in that same send,
    even though a fresh catalog resolution would now include it."""
    manager, tools, _history = _make_manager(tmp_path)

    connected = {"value": False}
    original_tool_defs = ToolRegistry.tool_defs

    def _conditionally_extended_tool_defs(self):
        defs = original_tool_defs(self)
        if connected["value"]:
            defs = defs + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]
        return defs

    monkeypatch.setattr(ToolRegistry, "tool_defs", _conditionally_extended_tool_defs)

    def _connect_after_round_0(idx: int) -> None:
        if idx == 0:
            connected["value"] = True

    rounds = [
        [_tool_call("call-1", "read_file", {"path": "a.py"})],
        [_tool_call("call-2", "get_workspace_snapshot", {})],
        None,
    ]
    stream = _ScriptedStream(rounds, on_call=_connect_after_round_0)
    events = _run_send(manager, stream)

    results = _tool_results(events)
    assert results["call-2"].ok is False
    assert results["call-2"].extras.get("failure_class") == "tool_call_not_exposed"

    # The flip really did take effect for a fresh resolution — proving the
    # exclusion above came from the frozen snapshot, not from the tool being
    # permanently unavailable.
    assert "get_workspace_snapshot" in _names(tools.tool_defs())
