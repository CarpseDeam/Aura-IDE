"""The discovery ceiling, for every turn that actually ends in an edit.

The implementation discovery stage bounds a production coding turn to two
ordinary discovery hops before its first applied write.  It asked the wrong
question about *which* turns it governs: it compared the route's lane against
``TaskLane.implementation`` and nothing else.

A coding request that also needs current external facts does not route to that
lane.  It routes to::

    TaskLane.research, action="research_then_worker"

— the research lane, because the research has to happen first, not because the
turn stops there.  It still ends in an edit.  Reading the lane alone exempted
every one of those turns from the ceiling entirely, and an exempt turn has no
bound on ordinary discovery at all: that is the runaway.

:func:`~aura.conversation.task_router.route_bears_production_action` is the one
shared predicate both authorities now ask, so
``implementation_staging_applies`` and ``should_enter_focused_action`` cannot
disagree about which turns are bounded.

Three regressions live here, each driven through the real
``ConversationManager`` loop, the real ``ToolRegistry``, and the real
``PreEditLoopGuard``:

1. a hybrid ``research_then_worker`` route gets two ordinary hops, then acts;
2. a send given *no* route resolves one from the real user message, so a caller
   cannot switch the ceiling off by omitting an argument;
3. a focused write that failed or was rejected ends the turn truthfully instead
   of falling back into unrestricted pre-write discovery — the one path that
   reached the ordinary loop with the stage already spent.

Every test proves the tools really executed.  A ceiling that held because
nothing ran would be no ceiling at all.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.client import (
    ContentDelta,
    Done,
    Event,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.conversation.focused_action import (
    ACTION_FAILED_MESSAGE,
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.manager_send_state import implementation_staging_applies
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import (
    TaskLane,
    TaskRoute,
    classify_user_request,
    route_bears_production_action,
)
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

#: A real coding request that also needs current external facts. Deliberately
#: not a hand-built route: the point of these tests is that the *classifier's
#: own* answer for this kind of request used to escape the ceiling.
HYBRID_REQUEST = (
    "Update notes.md to document the current Godot 4.5 signal syntax, "
    "checking the latest docs online"
)

#: A plain coding request, used where the route is omitted entirely.
CODING_REQUEST = "Update notes.md."

SELECTED_THINKING = "high"


# ── scripted production backend ─────────────────────────────────────────────


def tool_round(calls: list[tuple[str, str, dict]]) -> list[Event]:
    events: list[Event] = []
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        },
    ))
    return events


def final_round(text: str) -> list[Event]:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


def read_round(call_id: str, index: int) -> list[Event]:
    return tool_round([(call_id, "read_file", {"path": f"mod_{index:02d}.py"})])


def write_round(call_id: str = "w1") -> list[Event]:
    return tool_round([(call_id, "write_file", {
        "path": "notes.md", "content": "# Notes\n\nacted\n",
    })])


class ScriptedBackend:
    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        # A script that runs out means the loop asked for more requests than the
        # test expected. Say so here rather than letting the turn quietly end.
        return iter(final_round("(script exhausted)"))

    def ordinary_calls(self) -> list[dict]:
        return [c for c in self.calls if not c.get("require_tool_call")]

    def focused_calls(self) -> list[dict]:
        return [c for c in self.calls if c.get("require_tool_call")]


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def tool_results(self) -> list[ToolResult]:
        return [e for e in self.events if isinstance(e, ToolResult)]

    @property
    def chat_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ContentDelta))


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    for i in range(10):
        (root / f"mod_{i:02d}.py").write_text(
            f"# module {i}\n\nvalue = {i}\n", encoding="utf-8"
        )
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    return root


def approve_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def reject_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="reject")


def build_manager(workspace: Path, user_text: str) -> ConversationManager:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text(user_text)
    return ConversationManager(
        history, ToolRegistry(workspace_root=workspace, mode="single")
    )


def run(
    manager: ConversationManager,
    recorder: Recorder,
    *,
    route: TaskRoute | None,
    approval_cb=approve_all,
    max_tool_rounds: int = 12,
) -> None:
    manager.send(
        on_event=recorder,
        approval_cb=approval_cb,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking=SELECTED_THINKING,
        hook_name=PRODUCTION_STREAM_HOOK,
        max_tool_rounds=max_tool_rounds,
        task_route=route,
    )


# ── 1: the hybrid route is bounded like any other coding turn ───────────────


def test_hybrid_research_then_worker_route_gets_two_hops_then_acts(
    workspace, isolated_streams
) -> None:
    """The confirmed defect, as a regression test.

    The classifier's own answer for a coding request that needs current facts
    is the research lane with ``research_then_worker``. That route used to skip
    the stage entirely and buy unlimited ordinary discovery.
    """
    route = classify_user_request(HYBRID_REQUEST)
    assert route.lane is TaskLane.research, "precondition: not the implementation lane"
    assert route.action == "research_then_worker", (
        "precondition: this is the hybrid action, not answer-only research"
    )

    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round(),
        final_round("Applied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(build_manager(workspace, HYBRID_REQUEST), recorder, route=route)

    reads = [r for r in recorder.tool_results() if r.name == "read_file"]
    assert len(reads) == 2 and all(r.ok for r in reads), (
        "non-vacuous: both ordinary hops really ran their reads, successfully — "
        f"got {[(r.name, r.ok) for r in recorder.tool_results()]}"
    )
    assert [bool(c.get("require_tool_call")) for c in backend.calls] == [
        False, False, True, False,
    ], (
        "exactly two ordinary discovery requests on a hybrid coding turn, then "
        "the focused action request, then the ordinary final response"
    )


def test_answer_only_research_is_still_unbounded(workspace, isolated_streams) -> None:
    """The boundary of the fix: an answer-only turn has no act to serialize."""
    route = TaskRoute(TaskLane.research, "web_research", 0.9, "answer-only research")
    assert not route_bears_production_action(route)

    backend = ScriptedBackend([
        read_round("r0", 0), read_round("r1", 1),
        read_round("r2", 2), read_round("r3", 3),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    run(build_manager(workspace, "What is the current Godot release?"),
        Recorder(), route=route)

    assert not backend.focused_calls()


def test_both_authorities_read_the_one_shared_predicate() -> None:
    """The condition exists once, so the two gates cannot drift apart."""
    hybrid = TaskRoute(TaskLane.research, "research_then_worker", 0.9, "hybrid")
    guard = PreEditLoopGuard()
    guard.focused = True

    assert route_bears_production_action(hybrid)
    assert implementation_staging_applies(
        mode="single", route=hybrid, guard=guard, read_only=False
    )
    assert should_enter_focused_action(
        mode="single",
        route=hybrid,
        guard=guard,
        task_completion_context=False,
        state=FocusedActionState(),
    )


# ── 2: a missing route is resolved, not treated as "no ceiling" ─────────────


def test_resolved_route_reaches_focused_action(workspace, isolated_streams) -> None:
    """A caller that passes no route cannot silently disable the ceiling.

    The route is resolved from the latest real user message — the same
    deterministic classification the send layer performs — so the turn is bound
    exactly as it would have been had the caller passed one.
    """
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round(),
        final_round("Applied."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(build_manager(workspace, CODING_REQUEST), recorder, route=None)

    reads = [r for r in recorder.tool_results() if r.name == "read_file"]
    assert len(reads) == 2 and all(r.ok for r in reads), (
        "non-vacuous: both ordinary hops really ran their reads, successfully"
    )
    assert [bool(c.get("require_tool_call")) for c in backend.calls] == [
        False, False, True, False,
    ], "two ordinary hops, then the focused act, then the final response"
    writes = [r for r in recorder.tool_results() if r.name == "write_file"]
    assert writes and writes[0].ok, "the focused act really executed"
    assert (workspace / "notes.md").read_text(encoding="utf-8") == (
        "# Notes\n\nacted\n"
    )


# ── 3: a failed focused write cannot reopen ordinary discovery ──────────────


def test_rejected_focused_write_cannot_reopen_ordinary_discovery(
    workspace, isolated_streams
) -> None:
    """The last escape from the ceiling, closed.

    Both ordinary hops are spent, the focused request is spent, and its one act
    changed nothing. Returning to the ordinary loop from here is unbounded by
    construction: the stage cannot advance past ``EXHAUSTED`` and the focused
    request cannot fire twice. The turn ends as failed instead.
    """
    backend = ScriptedBackend([
        read_round("r0", 0),
        read_round("r1", 1),
        write_round(),
        # Anything the loop asks for beyond this point is the reopened
        # discovery loop this test exists to forbid.
        read_round("r3", 3),
        read_round("r4", 4),
        read_round("r5", 5),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()

    run(
        build_manager(workspace, CODING_REQUEST),
        recorder,
        route=None,
        approval_cb=reject_all,
    )

    writes = [r for r in recorder.tool_results() if r.name == "write_file"]
    assert writes, "non-vacuous: the focused write tool really ran"
    assert not writes[0].ok, "the write was rejected, so nothing was applied"
    assert (workspace / "notes.md").read_text(encoding="utf-8") == (
        "# Notes\n\nold body\n"
    )

    assert len(backend.calls) == 3, (
        "no request after the focused one: "
        f"{[bool(c.get('require_tool_call')) for c in backend.calls]}"
    )
    assert len(backend.ordinary_calls()) == 2
    assert ACTION_FAILED_MESSAGE in recorder.chat_text, (
        "the turn ends truthfully as failed rather than silently"
    )
    assert recorder.events and isinstance(recorder.events[-1], Done), (
        "the turn is terminated, not left hanging"
    )
