"""Scripted production-loop harness shared by the pre-write loop tests.

Not a test module.  It builds a real :class:`ConversationManager` over a real
:class:`ToolRegistry` and a real workspace, and drives it with a scripted model
backend, so the tests that use it assert on behaviour of the actual send loop
rather than on a simulation of it.

The scripted backend records every request it receives, which is how a test can
tell an ordinary reasoning request (no ``require_tool_call``) from the focused
action request (``require_tool_call=True``) without inspecting private state.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from aura.client import (
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK

IMPLEMENTATION_ROUTE = TaskRoute(
    lane=TaskLane.implementation,
    action="implementation",
    confidence=0.85,
    reason="scripted implementation turn",
)

RESEARCH_ROUTE = TaskRoute(
    lane=TaskLane.research,
    action="research",
    confidence=0.85,
    reason="scripted research turn",
)

HYBRID_ROUTE = TaskRoute(
    lane=TaskLane.research,
    action="research_then_worker",
    confidence=0.85,
    reason="scripted hybrid coding turn",
)

#: The user's selection for these turns. Deliberately not "off", so the focused
#: request's thinking-off can be told apart from the ambient setting.
SELECTED_THINKING = "high"


def tool_round(calls: list[tuple[str, str, dict]], *, text: str = "") -> list[Event]:
    """One streamed round that ends in tool calls."""
    events: list[Event] = []
    if text:
        events.append(ContentDelta(text=text))
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
            "content": text,
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
    """One round reading a distinct workspace file — genuinely new evidence."""
    return tool_round([(call_id, "read_file", {"path": f"mod_{index:02d}.py"})])


def write_round(call_id: str = "w1", *, body: str = "acted") -> list[Event]:
    return tool_round([(call_id, "write_file", {
        "path": "notes.md", "content": f"# Notes\n\n{body}\n",
    })])


class ScriptedBackend:
    """A model backend that replays a fixed list of rounds, recording requests."""

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

    def request_shapes(self) -> list[bool]:
        """``True`` for each focused action request, ``False`` for ordinary."""
        return [bool(c.get("require_tool_call")) for c in self.calls]


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def of_type(self, kind: type) -> list[Event]:
        return [e for e in self.events if isinstance(e, kind)]

    def tool_results(self) -> list[ToolResult]:
        return [e for e in self.events if isinstance(e, ToolResult)]

    def results_named(self, name: str) -> list[ToolResult]:
        return [r for r in self.tool_results() if r.name == name]

    @property
    def chat_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ContentDelta))

    @property
    def reasoning_text(self) -> str:
        return "".join(e.text for e in self.events if isinstance(e, ReasoningDelta))


def make_workspace(root: Path, *, modules: int = 50) -> Path:
    """Create the scripted workspace: many distinct modules plus a notes file."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(modules):
        (root / f"mod_{i:02d}.py").write_text(
            f"# module {i}\n\nvalue = {i}\n", encoding="utf-8"
        )
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    return root


def approve_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def reject_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="reject")


def build_manager(
    workspace: Path, user_text: str = "Update notes.md.", *, read_only: bool = False
) -> ConversationManager:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text(user_text)
    registry = ToolRegistry(
        workspace_root=workspace, mode="single", read_only=read_only
    )
    return ConversationManager(history, registry)


def run(
    manager: ConversationManager,
    recorder: Recorder,
    *,
    route: TaskRoute | None = IMPLEMENTATION_ROUTE,
    approval_cb=approve_all,
    max_tool_rounds: int = 20,
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


def system_texts(call: dict) -> list[str]:
    return [
        str(m.get("content", ""))
        for m in call.get("messages") or []
        if m.get("role") == "system"
    ]
