"""Scripted production-loop harness shared by the coding-agent loop tests.

Not a test module.  It builds a real :class:`ConversationManager` over a real
:class:`ToolRegistry` and a real workspace, and drives it with a scripted model
backend, so the tests that use it assert on behaviour of the actual send loop
rather than on a simulation of it.

The scripted backend records every request it receives, which is how a test can
prove the loop uses one stable catalog and the user-selected thinking mode on
every active request — same tool names, same ordering, same schema hash, no
``require_tool_call`` — without inspecting private state.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

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

#: The user's thinking selection for these turns. Deliberately not "off", so a
#: request that accidentally falls back to the old focused/checkpoint thinking
#: override can be told apart from the ambient setting.
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


def read_files_round(call_id: str, paths: list[str]) -> list[Event]:
    """One round reading several distinct files — a batch of observations."""
    return tool_round([(call_id, "read_file", {"path": path}) for path in paths])


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

    # ---- request inspection -------------------------------------------------

    def tool_names(self, index: int) -> list[str]:
        """Exposed tool names, in order, of one recorded request."""
        names: list[str] = []
        for tool in self.calls[index].get("tools") or []:
            fn = tool.get("function") if isinstance(tool, dict) else None
            name = fn.get("name") if isinstance(fn, dict) else None
            if name:
                names.append(str(name))
        return names

    def schema_hash(self, index: int) -> str:
        """Stable identity of one request's exposed tool schemas."""
        tools = self.calls[index].get("tools") or []
        rendered = json.dumps(tools, sort_keys=True, ensure_ascii=False)
        return hashlib.sha1(rendered.encode("utf-8")).hexdigest()[:16]

    def thinking(self, index: int) -> str:
        return str(self.calls[index].get("thinking", ""))

    def sent_require_tool_call(self, index: int) -> bool:
        """Whether ``require_tool_call`` was supplied to this request at all."""
        return "require_tool_call" in self.calls[index]

    def all_requests_stable(self) -> list[str]:
        """Describe any request whose catalog or thinking broke the stable shape.

        Returns a list of human-readable violations; empty means every recorded
        request exposed the same tool names, the same ordering, the same schema
        hash, and no ``require_tool_call``.
        """
        violations: list[str] = []
        reference_hash: str | None = None
        reference_names: list[str] | None = None
        for index, call in enumerate(self.calls):
            names = self.tool_names(index)
            if reference_names is None:
                reference_names = names
            if names != reference_names:
                violations.append(
                    f"request {index} exposed different tool names: "
                    f"{names} != {reference_names}"
                )
            schema = self.schema_hash(index)
            if reference_hash is None:
                reference_hash = schema
            if schema != reference_hash:
                violations.append(
                    f"request {index} schema hash {schema} != {reference_hash}"
                )
            if self.sent_require_tool_call(index):
                violations.append(
                    f"request {index} sent require_tool_call=True"
                )
        return violations

    def every_request_thinking(self) -> str:
        """The one thinking mode every recorded request used, or ``"<mixed>"``."""
        values = {self.thinking(i) for i in range(len(self.calls))}
        if len(values) == 1:
            return next(iter(values))
        return "<mixed>"


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


def make_multi_file_workspace(root: Path, count: int = 6) -> Path:
    """Create a workspace with ``count`` editable source files."""
    root.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (root / f"file_{i:02d}.py").write_text(
            f"VALUE_{i:02d} = {i}\n\nTARGET = 0\n", encoding="utf-8"
        )
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
    thinking: str = SELECTED_THINKING,
) -> None:
    manager.send(
        on_event=recorder,
        approval_cb=approval_cb,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking=thinking,
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


def tool_call_ids(history_messages: list[dict[str, Any]]) -> list[str]:
    """Tool-call ids present in an assistant/tool pairing, in order."""
    ids: list[str] = []
    for msg in history_messages:
        if msg.get("role") == "assistant":
            for tc in (msg.get("tool_calls") or []):
                if isinstance(tc, dict) and tc.get("id"):
                    ids.append(str(tc["id"]))
    return ids
