"""The production root runs on the extracted ``AgentLoop`` and behaves identically.

``ConversationManager`` used to inline its own ``while True`` round loop. That
loop now lives in :mod:`aura.conversation.agent_loop` so a second agent can run
it later against its own backend. These tests hold both halves of that change
honest:

* the root production path still streams, appends, runs tool rounds, cancels,
  repairs, and fails exactly as before — driven through the real
  ``ConversationManager``/``ToolRegistry``/``ToolRoundRunner`` trio, never a
  stand-in; and
* the extracted loop is genuinely reusable — it takes an injected backend
  stream, reaches for no registry and no Qt, and runs a directly injected
  ``APIAgentBackend.stream`` pointed at a different provider without touching
  the one production hook.
"""
from __future__ import annotations

import inspect
import json
import threading
from pathlib import Path
from typing import Any

import pytest

from aura.backends import APIAgentBackend
from aura.client import ApiError, ContentDelta, Done, Event, ToolCallStart, ToolResult
from aura.conversation import ConversationManager, History
from aura.conversation.agent_loop import AgentLoop, LoopStop
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools import ToolRegistry
from aura.conversation.tools._types import ApprovalDecision
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams

# ── scripted provider ────────────────────────────────────────────────────────


def _call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


class _Scripted:
    """Replays one prepared round per invocation and records every request."""

    def __init__(self, rounds: list, on_call=None) -> None:
        self._rounds = rounds
        self._on_call = on_call
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any):
        index = len(self.calls)
        self.calls.append(kwargs)
        if self._on_call is not None:
            self._on_call(index)
        for event in self._rounds[index]:
            yield event


def _tool_round(calls: list[dict], content: str = "") -> list[Event]:
    events: list[Event] = [
        ToolCallStart(index=i, id=c["id"], name=c["function"]["name"])
        for i, c in enumerate(calls)
    ]
    events.append(
        Done(
            finish_reason="tool_calls",
            full_message={
                "role": "assistant",
                "content": content,
                "tool_calls": calls,
            },
        )
    )
    return events


def _final(text: str = "done") -> list[Event]:
    return [
        ContentDelta(text),
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": text},
        ),
    ]


def _send(
    manager: ConversationManager,
    stream,
    *,
    cancel_event: threading.Event | None = None,
) -> list[Event]:
    """Run one real send with *stream* registered on the production hook."""
    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    events: list[Event] = []
    try:
        manager.send(
            on_event=events.append,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=cancel_event or threading.Event(),
            model="test-model",
            thinking="off",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)
    return events


@pytest.fixture()
def root(tmp_path: Path) -> ConversationManager:
    (tmp_path / "note.txt").write_text("contents\n", encoding="utf-8")
    history = History()
    history.append_user_text("Read note.txt and report.")
    return ConversationManager(history, ToolRegistry(tmp_path))


# ── 1. the root still runs the ordinary turn, now through the loop ───────────


def test_the_root_owns_its_turn_and_delegates_only_the_rounds(root) -> None:
    """History and Skills stay the manager's; the rounds are the loop's."""
    assert isinstance(root._loop, AgentLoop)
    assert root._loop.history is root.history


def test_a_tool_round_still_continues_into_a_second_model_call(root) -> None:
    stream = _Scripted(
        [
            _tool_round([_call("read-1", "read_file", {"path": "note.txt"})]),
            _final("Reported."),
        ]
    )

    events = _send(root, stream)

    assert len(stream.calls) == 2
    assert stream.calls[0]["model"] == stream.calls[1]["model"] == "test-model"
    assert stream.calls[0]["thinking"] == "off"
    results = [e for e in events if isinstance(e, ToolResult)]
    assert [r.tool_call_id for r in results] == ["read-1"]
    assert results[0].ok is True
    assert root.history.messages[-1]["content"] == "Reported."


def test_ordered_results_survive_a_multi_call_batch(root) -> None:
    batch = [
        _call("read-a", "read_file", {"path": "note.txt"}),
        _call("read-b", "read_file", {"path": "note.txt"}),
        _call("read-c", "read_file", {"path": "note.txt"}),
    ]
    events = _send(root, _Scripted([_tool_round(batch), _final()]))

    appended = [
        m["tool_call_id"] for m in root.history.messages if m.get("role") == "tool"
    ]
    assert appended == ["read-a", "read-b", "read-c"]
    assert [
        e.tool_call_id for e in events if isinstance(e, ToolResult)
    ] == ["read-a", "read-b", "read-c"]


def test_every_round_of_a_turn_reuses_the_one_frozen_catalog(root) -> None:
    stream = _Scripted(
        [
            _tool_round([_call("read-1", "read_file", {"path": "note.txt"})]),
            _final(),
        ]
    )

    _send(root, stream)

    assert stream.calls[0]["tools"] is stream.calls[1]["tools"]


def test_each_round_sends_the_accumulated_canonical_history(root) -> None:
    stream = _Scripted(
        [
            _tool_round([_call("read-1", "read_file", {"path": "note.txt"})]),
            _final(),
        ]
    )

    _send(root, stream)

    second = stream.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "tool"]
    assert second[2]["tool_call_id"] == "read-1"
    # A snapshot, not the live list: mutating a request never edits History.
    second[0]["content"] = "tampered"
    assert root.history.messages[0]["content"] != "tampered"


def test_the_production_backend_is_resolved_once_per_round(root) -> None:
    """Re-pointing the one production backend takes effect on the next round.

    The loop holds no captured handler: the root's stream seam asks the
    registry each round, which is what lets the bridge swap providers.
    """
    second = _Scripted([_final("from the replacement")])

    def swap(index: int) -> None:
        if index == 0:
            model_streams.unregister(PRODUCTION_STREAM_HOOK)
            model_streams.register(PRODUCTION_STREAM_HOOK, second)

    first = _Scripted(
        [_tool_round([_call("read-1", "read_file", {"path": "note.txt"})])],
        on_call=swap,
    )

    _send(root, first)

    assert len(first.calls) == 1
    assert len(second.calls) == 1
    assert root.history.messages[-1]["content"] == "from the replacement"


# ── 2. reasoning continuation ────────────────────────────────────────────────


def test_a_reasoning_bearing_assistant_message_reaches_the_next_round(root) -> None:
    """Nothing is compacted or rewritten between rounds.

    The provider's continuation state rides on the assistant message the loop
    appends verbatim; the next round's snapshot must still carry it, which is
    what keeps OpenAI Responses reasoning continuation working.
    """
    calls = [_call("read-1", "read_file", {"path": "note.txt"})]
    first = _tool_round(calls)
    first[-1].full_message["reasoning_content"] = "Checking the file first."
    first[-1].full_message["_aura_provider_reasoning"] = [
        {"id": "rs-1", "encrypted_content": "ENC", "call_id": "read-1"}
    ]

    stream = _Scripted([first, _final()])
    _send(root, stream)

    assistant = stream.calls[1]["messages"][1]
    assert assistant["reasoning_content"] == "Checking the file first."
    assert assistant["_aura_provider_reasoning"] == [
        {"id": "rs-1", "encrypted_content": "ENC", "call_id": "read-1"}
    ]
    assert assistant["tool_calls"][0]["id"] == "read-1"


# ── 3. provider failure ──────────────────────────────────────────────────────


def test_a_provider_failure_stops_the_turn_without_claiming_success(root) -> None:
    stream = _Scripted(
        [
            [ApiError(status_code=500, message="upstream exploded")],
            _final("never reached"),
        ]
    )

    events = _send(root, stream)

    assert len(stream.calls) == 1
    assert [e.message for e in events if isinstance(e, ApiError)] == [
        "upstream exploded"
    ]
    # Nothing was appended: there was no assistant response to keep.
    assert [m["role"] for m in root.history.messages] == ["user"]


def test_a_stream_that_never_completes_appends_nothing(root) -> None:
    events = _send(root, _Scripted([[ContentDelta("half a th")]]))

    assert [m["role"] for m in root.history.messages] == ["user"]
    assert [type(e) for e in events] == [ContentDelta]


# ── 4. cancellation ──────────────────────────────────────────────────────────


def test_cancelling_before_the_first_round_reports_and_runs_nothing(root) -> None:
    cancelled = threading.Event()
    cancelled.set()
    stream = _Scripted([_final()])

    events = _send(root, stream, cancel_event=cancelled)

    assert stream.calls == []
    assert [e.message for e in events if isinstance(e, ApiError)] == ["Cancelled."]


def test_cancelling_mid_batch_keeps_every_result_that_already_returned(root) -> None:
    """Cancellation cannot replace authoritative results returned by the batch."""
    cancelled = threading.Event()
    batch = [
        _call("read-a", "read_file", {"path": "note.txt"}),
        _call("read-b", "read_file", {"path": "note.txt"}),
    ]
    stream = _Scripted([_tool_round(batch), _final("never reached")])

    previous = model_streams.get_handler(PRODUCTION_STREAM_HOOK)
    model_streams.unregister(PRODUCTION_STREAM_HOOK)
    model_streams.register(PRODUCTION_STREAM_HOOK, stream)
    events: list[Event] = []

    def on_event(event: Event) -> None:
        events.append(event)
        # Stop the run while results are being published. Both parallel reads
        # have already returned by this point and must remain authoritative.
        if isinstance(event, ToolResult):
            cancelled.set()

    try:
        root.send(
            on_event=on_event,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=cancelled,
            model="test-model",
            thinking="off",
        )
    finally:
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        if previous is not None:
            model_streams.register(PRODUCTION_STREAM_HOOK, previous)

    assert len(stream.calls) == 1  # the model was never called again
    tools = [m for m in root.history.messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["read-a", "read-b"]
    # The completed call keeps its real result verbatim.
    assert json.loads(tools[0]["content"])["ok"] is True
    assert json.loads(tools[1]["content"])["ok"] is True
    assert [e.message for e in events if isinstance(e, ApiError)] == ["Cancelled."]


def test_a_cancelled_partial_answer_is_kept_without_orphan_calls(root) -> None:
    cancelled = threading.Event()

    def cancel_immediately(_index: int) -> None:
        cancelled.set()

    stream = _Scripted(
        [
            [
                Done(
                    finish_reason="tool_calls",
                    full_message={
                        "role": "assistant",
                        "content": "I had started reading",
                        "tool_calls": [
                            _call("read-a", "read_file", {"path": "note.txt"})
                        ],
                    },
                )
            ]
        ],
        on_call=cancel_immediately,
    )

    _send(root, stream, cancel_event=cancelled)

    assistant = root.history.messages[-1]
    assert assistant["content"] == "I had started reading"
    assert "tool_calls" not in assistant


def test_an_empty_cancelled_round_leaves_the_turn_as_it_found_it(root) -> None:
    cancelled = threading.Event()

    def cancel_immediately(_index: int) -> None:
        cancelled.set()

    stream = _Scripted(
        [
            [
                Done(
                    finish_reason="stop",
                    full_message={"role": "assistant", "content": ""},
                )
            ]
        ],
        on_call=cancel_immediately,
    )

    events = _send(root, stream, cancel_event=cancelled)

    assert [m["role"] for m in root.history.messages] == ["user"]
    assert [e.message for e in events if isinstance(e, ApiError)] == ["Cancelled."]


def _loop_over(history: History, tmp_path: Path, stream=None) -> AgentLoop:
    tools = ToolRegistry(tmp_path)
    return AgentLoop(
        history=history,
        stream=stream or (lambda **_kwargs: iter(())),
        tool_round=ToolRoundRunner(
            history=history,
            tools=tools,
            tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        ),
    )


def test_repairing_a_cancelled_turn_twice_changes_nothing(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("go")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_call("read-a", "read_file", {"path": "note.txt"})],
        }
    )
    loop = _loop_over(history, tmp_path)

    loop.repair_cancelled_turn(lambda _event: None)
    once = json.dumps(history.messages)
    loop.repair_cancelled_turn(lambda _event: None)

    assert json.dumps(history.messages) == once


def test_an_unpairable_block_is_dropped_and_the_turn_survives(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("go")
    history.append_assistant({"role": "assistant", "content": "first step done"})
    history.append_assistant(
        {"role": "assistant", "content": "", "tool_calls": [{"no": "id"}]}
    )

    _loop_over(history, tmp_path).repair_cancelled_turn(lambda _event: None)

    assert [m["role"] for m in history.messages] == ["user", "assistant"]
    assert history.messages[-1]["content"] == "first step done"


# ── 5. the loop is reusable on its own terms ─────────────────────────────────


class _FakeProviderClient:
    """A provider client whose ``stream`` replays prepared rounds."""

    def __init__(self, rounds: list) -> None:
        self._rounds = rounds
        self.calls: list[dict[str, Any]] = []

    def stream(self, **kwargs: Any):
        index = len(self.calls)
        self.calls.append(kwargs)
        return iter(self._rounds[index])


def test_the_loop_runs_a_directly_injected_backend_from_another_provider(
    tmp_path: Path,
) -> None:
    """No hook, no registry: the backend's ``stream`` is handed straight in.

    This is the seam a child agent needs — its own History, its own tool
    round, and its own provider — while the one production hook keeps whatever
    the root registered.
    """
    (tmp_path / "note.txt").write_text("contents\n", encoding="utf-8")
    hook_before = model_streams.get_handler(PRODUCTION_STREAM_HOOK)

    backend = APIAgentBackend(provider="anthropic")
    client = _FakeProviderClient(
        [
            _tool_round([_call("read-1", "read_file", {"path": "note.txt"})]),
            _final("child answered"),
        ]
    )
    backend._client = client

    history = History()
    history.append_user_text("Read note.txt.")
    tools = ToolRegistry(tmp_path)
    loop = AgentLoop(
        history=history,
        stream=backend.stream,
        tool_round=ToolRoundRunner(
            history=history,
            tools=tools,
            tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        ),
    )

    events: list[Event] = []
    outcome = loop.run(
        on_event=events.append,
        approval_cb=lambda _request: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        model="child-model",
        thinking="high",
        tool_defs=tools.tool_defs(),
    )

    assert outcome.stop is LoopStop.COMPLETED
    assert len(client.calls) == 2
    assert client.calls[0]["model"] == "child-model"
    assert client.calls[0]["thinking"] == "high"
    assert [e.tool_call_id for e in events if isinstance(e, ToolResult)] == ["read-1"]
    assert history.messages[-1]["content"] == "child answered"
    # The root's production hook was never registered, swapped, or consulted.
    assert model_streams.get_handler(PRODUCTION_STREAM_HOOK) is hook_before


def test_the_loop_reports_how_it_stopped(tmp_path: Path) -> None:
    history = History()
    history.append_user_text("go")

    def outcome_for(rounds: list) -> LoopStop:
        loop = _loop_over(History(), tmp_path, stream=_Scripted(rounds))
        return loop.run(
            on_event=lambda _event: None,
            approval_cb=lambda _request: ApprovalDecision(action="approve"),
            cancel_event=threading.Event(),
            model="m",
            thinking="off",
            tool_defs=[],
        ).stop

    assert outcome_for([_final()]) is LoopStop.COMPLETED
    assert outcome_for([[ApiError(status_code=500, message="boom")]]) is LoopStop.API_ERROR
    assert outcome_for([[ContentDelta("no done")]]) is LoopStop.NO_RESPONSE

    cancelled = threading.Event()
    cancelled.set()
    loop = _loop_over(history, tmp_path)
    assert loop.run(
        on_event=lambda _event: None,
        approval_cb=lambda _request: ApprovalDecision(action="approve"),
        cancel_event=cancelled,
        model="m",
        thinking="off",
        tool_defs=[],
    ).stop is LoopStop.CANCELLED


def test_the_loop_reaches_for_no_registry_and_no_qt() -> None:
    """Its dependencies arrive through its constructor or not at all."""
    import aura.conversation.agent_loop as agent_loop

    source = inspect.getsource(agent_loop)

    assert "model_streams" not in source
    assert "PRODUCTION_STREAM_HOOK" not in source
    assert "PySide6" not in source
    assert "QObject" not in source


def test_the_root_manager_no_longer_carries_its_own_round_loop() -> None:
    import aura.conversation.manager as manager

    assert "while True:" not in inspect.getsource(manager.ConversationManager.send)
