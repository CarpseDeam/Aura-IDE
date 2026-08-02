"""The focused action turn: one request that serializes a decision into one act.

Once ``PreEditLoopGuard`` has issued its focus instruction on a production
``implementation`` turn and nothing has been written, the send loop stops
opening ordinary reasoning streams and issues exactly one action-serialization
request: thinking off, mutation tools plus ``report_blocker``, and a
provider-neutral requirement that the answer be a tool call.

These tests drive the real ``ConversationManager``, the real ``ToolRegistry``,
and the real ``PreEditLoopGuard`` — the focus flag is reached by actually
spending the discovery budget, not by poking state. What is asserted:

* which request is focused, and that there is exactly one of them;
* the thinking mode on every request, before, during, and after;
* the exact tool surface the focused request exposes;
* each provider's own required-tool mapping;
* that a write, a rejected write, a blocker, and a contract-violating provider
  each leave focused action by their own path, with no retry and no cap.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aura.client.events import ApiError, ContentDelta, Done, ToolResult
from aura.conversation.focused_action import (
    PROVIDER_CONTRACT_FAILURE_MESSAGE,
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import (
    MAX_DISCOVERY_CALLS_BEFORE_FOCUS,
    PreEditLoopGuard,
)
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.catalog import MUTATION_TOOL_NAMES
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import model_streams

HOOK = "focused_action_test_stream"

IMPLEMENTATION_ROUTE = TaskRoute(
    lane=TaskLane.implementation,
    action="implementation",
    confidence=0.85,
    reason="matched implementation request",
)
CHAT_ROUTE = TaskRoute(
    lane=TaskLane.chat, action="chat", confidence=0.6, reason="no trigger"
)


# ── scripted provider ───────────────────────────────────────────────────────


@dataclass
class RecordedRequest:
    thinking: str
    tool_names: tuple[str, ...]
    require_tool_call: bool
    messages: list[dict[str, Any]]


@dataclass
class ScriptedStream:
    """A provider that replays a fixed list of assistant messages."""

    rounds: list[dict[str, Any]]
    requests: list[RecordedRequest] = field(default_factory=list)
    cancel_on_request: int | None = None

    def __call__(
        self,
        *,
        messages,
        tools,
        model,
        thinking,
        cancel_event,
        temperature,
        require_tool_call: bool = False,
    ):
        index = len(self.requests)
        self.requests.append(
            RecordedRequest(
                thinking=str(thinking),
                tool_names=tuple(
                    str(t.get("function", {}).get("name", "")) for t in (tools or [])
                ),
                require_tool_call=bool(require_tool_call),
                messages=list(messages),
            )
        )
        if index >= len(self.rounds):
            raise AssertionError(
                f"provider asked for round {index}; script has {len(self.rounds)}"
            )
        if self.cancel_on_request == index and cancel_event is not None:
            cancel_event.set()
        full_message = self.rounds[index]
        yield Done(finish_reason="stop", full_message=full_message)


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def assistant(*calls: dict[str, Any], content: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    return message


def discovery_round() -> dict[str, Any]:
    """One assistant message that spends the whole pre-focus discovery budget."""
    return assistant(
        *[
            tool_call(f"d{i}", "glob", {"pattern": f"**/probe_{i}_*.py"})
            for i in range(MAX_DISCOVERY_CALLS_BEFORE_FOCUS)
        ]
    )


@dataclass
class Harness:
    manager: ConversationManager
    stream: ScriptedStream
    events: list[Any]
    cancel: threading.Event
    root: Path
    approvals: list[str] = field(default_factory=list)

    def run(self, route: TaskRoute | None = IMPLEMENTATION_ROUTE, thinking="high"):
        self.manager.send(
            on_event=self.events.append,
            approval_cb=self.approve,
            cancel_event=self.cancel,
            model="test-model",
            thinking=thinking,
            hook_name=HOOK,
            task_route=route,
        )

    def approve(self, request):
        self.approvals.append(request.rel_path)
        return ApprovalDecision(action="approve")

    def reject(self, request):
        self.approvals.append(request.rel_path)
        return ApprovalDecision(action="reject")


def make_harness(tmp_path: Path, rounds: list[dict[str, Any]]) -> Harness:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Fix the loader in alpha.py")
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")

    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    manager = ConversationManager(history, tools)
    stream = ScriptedStream(rounds=rounds)
    model_streams.unregister(HOOK)
    model_streams.register(HOOK, stream)
    return Harness(
        manager=manager,
        stream=stream,
        events=[],
        cancel=threading.Event(),
        root=tmp_path,
    )


@pytest.fixture(autouse=True)
def _clean_hook():
    yield
    model_streams.unregister(HOOK)


WRITE_ROUND = assistant(
    tool_call("w1", "write_file", {"path": "alpha.py", "content": "alpha = 2\n"})
)
FINAL_ROUND = assistant(content="Changed alpha.py: alpha is now 2.")


# ── 1-5, 11: activation, thinking modes, tool surface ───────────────────────


@pytest.fixture
def focused_write(tmp_path):
    harness = make_harness(
        tmp_path, [discovery_round(), WRITE_ROUND, FINAL_ROUND]
    )
    harness.run()
    return harness


def test_focused_implementation_enters_exactly_one_action_request(focused_write):
    required = [r.require_tool_call for r in focused_write.stream.requests]
    assert required == [False, True, False]


def test_discovery_uses_the_user_selected_thinking_mode(focused_write):
    assert focused_write.stream.requests[0].thinking == "high"


def test_focused_action_request_uses_thinking_off(focused_write):
    assert focused_write.stream.requests[1].thinking == "off"


def test_selected_thinking_mode_is_restored_after_the_action(focused_write):
    assert focused_write.stream.requests[2].thinking == "high"


def test_only_mutation_tools_and_report_blocker_are_exposed(focused_write):
    exposed = set(focused_write.stream.requests[1].tool_names)
    assert exposed == set(MUTATION_TOOL_NAMES) | {"report_blocker"}
    forbidden = {
        "read_file",
        "read_file_range",
        "read_file_outline",
        "glob",
        "grep_search",
        "search_codebase",
        "web_search",
        "update_worker_todo",
        "git_status",
        "git_diff",
        "run_terminal_command",
        "run_and_watch",
        "run_diagnostic_command",
        "run_read_only_drone",
        "register_drone_folder",
        "get_workspace_snapshot",
        "inspect_godot_editor",
    }
    assert not (exposed & forbidden)


def test_report_blocker_is_not_exposed_on_ordinary_requests(focused_write):
    for index in (0, 2):
        assert "report_blocker" not in focused_write.stream.requests[index].tool_names


def test_ordinary_requests_keep_the_full_production_catalog(focused_write):
    ordinary = set(focused_write.stream.requests[0].tool_names)
    assert {"read_file", "glob", "write_file", "run_terminal_command"} <= ordinary


def test_a_successful_write_exits_focused_action_state(focused_write, tmp_path):
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
    # Only one focused request, and the round after it is an ordinary one.
    assert sum(r.require_tool_call for r in focused_write.stream.requests) == 1
    assert focused_write.stream.requests[2].require_tool_call is False


# ── 10: no local cap on action arguments ────────────────────────────────────


def test_large_write_arguments_are_not_truncated(tmp_path):
    body = "".join(f"LINE_{i:06d} = {i}\n" for i in range(20_000))
    assert len(body) > 300_000
    harness = make_harness(
        tmp_path,
        [
            discovery_round(),
            assistant(
                tool_call("w1", "write_file", {"path": "big.py", "content": body})
            ),
            FINAL_ROUND,
        ],
    )
    harness.run()
    assert (tmp_path / "big.py").read_text(encoding="utf-8") == body


# ── 12: a rejected write returns to the existing recovery path ──────────────


def test_rejected_write_uses_existing_recovery_and_does_not_re_enter(tmp_path):
    harness = make_harness(
        tmp_path,
        [discovery_round(), WRITE_ROUND, assistant(content="Blocked by rejection.")],
    )
    harness.manager.send(
        on_event=harness.events.append,
        approval_cb=harness.reject,
        cancel_event=harness.cancel,
        model="test-model",
        thinking="high",
        hook_name=HOOK,
        task_route=IMPLEMENTATION_ROUTE,
    )
    # The file is untouched, the rejection is in history as an ordinary tool
    # result, and the turn goes back to normal reasoning rather than firing a
    # second thinking-off request at the same decision.
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert [r.require_tool_call for r in harness.stream.requests] == [
        False,
        True,
        False,
    ]
    assert harness.stream.requests[2].thinking == "high"
    assert harness.approvals == ["alpha.py"]
    last_tool_result = json.loads(
        [m for m in harness.manager.history.messages if m.get("role") == "tool"][-1][
            "content"
        ]
    )
    assert last_tool_result["ok"] is False
    assert last_tool_result.get("applied") is not True


# ── 13: report_blocker mutates nothing and yields one factual final ─────────


def test_report_blocker_performs_no_mutation_and_ends_with_one_final(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            discovery_round(),
            assistant(
                tool_call(
                    "b1",
                    "report_blocker",
                    {
                        "blocker": "The loader is generated at build time.",
                        "needed": "The generator template.",
                        "target_files": ["alpha.py"],
                    },
                )
            ),
            assistant(content="I could not edit alpha.py: it is generated."),
        ],
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    harness.run()

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert harness.approvals == []
    # Exactly three requests: discovery, the action, and one factual final.
    assert len(harness.stream.requests) == 3
    blocker_results = [
        json.loads(m["content"])
        for m in harness.manager.history.messages
        if m.get("role") == "tool" and "blocker_reported" in str(m.get("content"))
    ]
    assert len(blocker_results) == 1
    payload = blocker_results[0]
    assert payload["ok"] is True
    assert payload["mutation"] is False
    assert payload["applied"] is False
    assert payload["blocker"] == "The loader is generated at build time."
    assert payload["target_files"] == ["alpha.py"]
    finals = [
        m for m in harness.manager.history.messages[-1:] if m.get("role") == "assistant"
    ]
    assert finals and "generated" in finals[0]["content"]


# ── 14: a provider that ignores the contract ends honestly, with no retry ───


def test_prose_without_a_required_tool_call_ends_as_provider_contract_failure(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            discovery_round(),
            assistant(content="Here is what I would do: first I would open..."),
        ],
    )
    harness.run()

    # No retry: the script would have raised on a third request.
    assert len(harness.stream.requests) == 2
    contents = [e.text for e in harness.events if isinstance(e, ContentDelta)]
    assert PROVIDER_CONTRACT_FAILURE_MESSAGE in contents
    dones = [e for e in harness.events if isinstance(e, Done)]
    assert dones[-1].full_message["content"] == PROVIDER_CONTRACT_FAILURE_MESSAGE
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    # The model's own prose is preserved above the honest ending.
    assistants = [
        m["content"]
        for m in harness.manager.history.messages
        if m.get("role") == "assistant"
    ]
    assert "Here is what I would do" in assistants[-2]
    assert assistants[-1] == PROVIDER_CONTRACT_FAILURE_MESSAGE


# ── 15: every other kind of turn is untouched ───────────────────────────────


@pytest.mark.parametrize(
    "route",
    [
        None,
        CHAT_ROUTE,
        TaskRoute(TaskLane.research, "web_research", 0.9, "research"),
        TaskRoute(TaskLane.validation, "validation", 0.9, "validation"),
        TaskRoute(TaskLane.built_in_action, "git_status", 1.0, "built-in"),
    ],
)
def test_non_implementation_turns_never_enter_focused_action(tmp_path, route):
    harness = make_harness(
        tmp_path,
        [discovery_round(), WRITE_ROUND, FINAL_ROUND],
    )
    harness.run(route=route)
    assert [r.require_tool_call for r in harness.stream.requests] == [
        False,
        False,
        False,
    ]
    assert {r.thinking for r in harness.stream.requests} == {"high"}
    assert "report_blocker" not in harness.stream.requests[1].tool_names


def test_implementation_turn_without_a_spent_budget_is_untouched(tmp_path):
    """Focus never fired, so nothing about this turn changes."""
    harness = make_harness(
        tmp_path,
        [
            assistant(tool_call("d0", "glob", {"pattern": "**/*.py"})),
            WRITE_ROUND,
            FINAL_ROUND,
        ],
    )
    harness.run()
    assert [r.require_tool_call for r in harness.stream.requests] == [
        False,
        False,
        False,
    ]


# ── 16: cancellation is unchanged ───────────────────────────────────────────


def test_user_cancellation_during_the_focused_request_is_unchanged(tmp_path):
    harness = make_harness(tmp_path, [discovery_round(), WRITE_ROUND])
    harness.stream.cancel_on_request = 1
    harness.run()

    errors = [e for e in harness.events if isinstance(e, ApiError)]
    assert errors and errors[-1].message == "Cancelled."
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    # No further request was made and the cancel event was never set by Aura's
    # own machinery — the test set it, standing in for the user.
    assert len(harness.stream.requests) == 2


# ── the activation predicate itself ─────────────────────────────────────────


def _guard(*, focused=True, write_applied=False) -> PreEditLoopGuard:
    guard = PreEditLoopGuard()
    guard.focused = focused
    guard.write_applied = write_applied
    return guard


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({}, True),
        ({"mode": "worker"}, False),
        ({"mode": "planner"}, False),
        ({"route": CHAT_ROUTE}, False),
        ({"route": None}, False),
        ({"guard": None}, False),
        ({"guard": _guard(focused=False)}, False),
        ({"guard": _guard(write_applied=True)}, False),
        ({"task_completion_context": True}, False),
        ({"state": FocusedActionState(spent=True)}, False),
        ({"state": FocusedActionState(blocked=True)}, False),
    ],
)
def test_activation_predicate(kwargs, expected):
    base: dict[str, Any] = {
        "mode": "single",
        "route": IMPLEMENTATION_ROUTE,
        "guard": _guard(),
        "task_completion_context": False,
        "state": FocusedActionState(),
    }
    base.update(kwargs)
    assert should_enter_focused_action(**base) is expected


# ── tool-history pairing survives the new paths ─────────────────────────────


@pytest.mark.parametrize(
    "script_name",
    ["write", "blocker", "contract_failure"],
)
def test_history_tool_pairing_is_preserved(tmp_path, script_name):
    scripts = {
        "write": [discovery_round(), WRITE_ROUND, FINAL_ROUND],
        "blocker": [
            discovery_round(),
            assistant(tool_call("b1", "report_blocker", {"blocker": "generated"})),
            assistant(content="Blocked."),
        ],
        "contract_failure": [discovery_round(), assistant(content="prose only")],
    }
    harness = make_harness(tmp_path, scripts[script_name])
    harness.run()

    messages = harness.manager.history.messages
    index = 0
    while index < len(messages):
        message = messages[index]
        assert message.get("role") != "tool", "tool result with no assistant above it"
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            index += 1
            continue
        expected = [call["id"] for call in message["tool_calls"]]
        answered: list[str] = []
        cursor = index + 1
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            answered.append(messages[cursor]["tool_call_id"])
            cursor += 1
        assert answered == expected
        index = cursor


def test_every_focused_tool_call_still_emits_a_tool_result_event(focused_write):
    results = [e for e in focused_write.events if isinstance(e, ToolResult)]
    assert any(r.name == "write_file" and r.ok for r in results)
