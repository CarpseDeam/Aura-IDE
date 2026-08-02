"""The discovery boundary must not damage anything the send loop already does.

A new rejection path and a new internal message are exactly the kind of change
that quietly breaks tool pairing (an assistant tool_call with no matching
result), turns internal steering into a fake user turn, or drops reasoning on
replay. These tests drive the real ``ToolRoundRunner`` and the real API view.

What is asserted here:

* a rejected discovery call still produces a paired tool result;
* the rejection reaches the model as a recoverable payload and a ToolResult
  event, so activity rendering has something to show;
* internal steering stays inside the real user turn;
* reasoning is still replayed and history is never mutated by the view;
* content gating for the single-agent path is unaffected.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from aura.conversation.api_view import _turn_starts, build_api_view
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.pre_edit_loop_guard import (
    DISCOVERY_EXHAUSTED_REASON,
    MAX_DISCOVERY_CALLS_AFTER_FOCUS,
    MAX_DISCOVERY_CALLS_BEFORE_FOCUS,
)
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.registry import ToolRegistry

BEFORE = MAX_DISCOVERY_CALLS_BEFORE_FOCUS
AFTER = MAX_DISCOVERY_CALLS_AFTER_FOCUS


# ── helpers ─────────────────────────────────────────────────────────────────


def assert_tool_pairing_valid(messages: list[dict[str, Any]]) -> None:
    """Every tool message answers a tool_call in the assistant right above it."""
    i = 0
    seen: set[str] = set()
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool":
            raise AssertionError(f"tool message at {i} has no preceding tool_calls")
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        expected = [tc["id"] for tc in msg["tool_calls"]]
        j = i + 1
        answered: list[str] = []
        while j < len(messages) and messages[j].get("role") == "tool":
            call_id = messages[j].get("tool_call_id")
            assert call_id not in seen, f"duplicate tool result {call_id}"
            seen.add(call_id)
            answered.append(call_id)
            j += 1
        assert answered == expected, (
            f"assistant at {i} expected {expected}, got {answered}"
        )
        i = j


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


@pytest.fixture
def runner(tmp_path):
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
    history = History()
    history.set_system("You are Aura's production coding agent.")
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    return (
        ToolRoundRunner(
            history=history,
            tools=tools,
            tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
            planner_refresh=PlannerRefreshState(),
        ),
        history,
        tmp_path,
    )


def exhaust(state: _SendState) -> None:
    guard = state.pre_edit_guard
    for i in range(BEFORE):
        guard.record("read_file", {"path": f"seen_{i}.py"})
    guard.take_internal_messages()
    for i in range(AFTER):
        guard.record("read_file", {"path": f"late_{i}.py"})
    assert guard.discovery_exhausted


def run_round(runner_bundle, state, calls):
    runner, history, _ = runner_bundle
    events: list[Any] = []
    runner.run(
        tool_calls=calls,
        state=state,
        on_event=events.append,
        approval_cb=lambda req: None,
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda cb: None,
    )
    return events


# ── the rejection travels correctly ─────────────────────────────────────────


class TestRejectedDiscoveryStaysWellFormed:

    def test_a_rejected_call_still_gets_a_paired_tool_result(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        exhaust(state)

        calls = [tool_call("call-1", "glob", {"pattern": "**/*.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = [m for m in history.messages if m.get("role") == "tool"]
        assert len(results) == 1
        assert results[0]["tool_call_id"] == "call-1"

    def test_the_result_payload_is_the_recoverable_rejection(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        exhaust(state)

        calls = [tool_call("call-1", "search_codebase", {"query": "retry"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(
            [m for m in history.messages if m.get("role") == "tool"][0]["content"]
        )
        assert payload["reason"] == DISCOVERY_EXHAUSTED_REASON
        assert payload["recoverable"] is True
        assert payload["ok"] is False

    def test_a_tool_result_event_is_emitted_for_activity_rendering(
        self, runner
    ) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        exhaust(state)

        calls = [tool_call("call-1", "glob", {"pattern": "**/*.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        events = run_round(runner, state, calls)

        tool_results = [e for e in events if type(e).__name__ == "ToolResult"]
        assert len(tool_results) == 1
        assert tool_results[0].name == "glob"
        assert tool_results[0].ok is False
        assert tool_results[0].extras["reason"] == DISCOVERY_EXHAUSTED_REASON
        assert tool_results[0].extras["recoverable"] is True

    def test_a_mixed_round_pairs_every_call(self, runner) -> None:
        """One refused, one allowed — both must be answered, in order."""
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)
        exhaust(state)

        calls = [
            tool_call("call-1", "glob", {"pattern": "**/*.py"}),
            tool_call("call-2", "read_file_outline", {"path": "alpha.py"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = [m for m in history.messages if m.get("role") == "tool"]
        assert [r["tool_call_id"] for r in results] == ["call-1", "call-2"]

    def test_narrow_reads_still_execute_after_exhaustion(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        exhaust(state)

        calls = [tool_call("call-1", "read_file_outline", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(
            [m for m in history.messages if m.get("role") == "tool"][0]["content"]
        )
        assert payload.get("reason") != DISCOVERY_EXHAUSTED_REASON

    def test_an_unexhausted_turn_executes_discovery_normally(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(
            [m for m in history.messages if m.get("role") == "tool"][0]["content"]
        )
        assert payload.get("reason") != DISCOVERY_EXHAUSTED_REASON
        assert state.pre_edit_guard.discovery_calls == 1


# ── internal steering stays internal ────────────────────────────────────────


class TestInternalSteeringStaysInsideTheUserTurn:

    def _history_with_focus(self) -> History:
        history = History()
        history.set_system("system")
        history.append_user_text("Fix the retry cap so the job pauses.")

        state = _SendState(mode="single", research_policy=None)
        guard = state.pre_edit_guard
        for i in range(BEFORE):
            calls = [tool_call(f"c{i}", "read_file", {"path": f"m{i}.py"})]
            history.append_assistant({
                "role": "assistant",
                "content": "",
                "reasoning_content": f"considering m{i}\n",
                "tool_calls": calls,
            })
            history.append_tool_result(
                f"c{i}", json.dumps({"ok": True, "path": f"m{i}.py", "content": f"b{i}"})
            )
            guard.record("read_file", {"path": f"m{i}.py"})

        for message in guard.take_internal_messages():
            history.append_internal_user_text(message)
        return history

    def test_the_focus_message_is_marked_internal(self) -> None:
        history = self._history_with_focus()
        internal = [m for m in history.messages if m.get("aura_internal")]

        assert len(internal) == 1
        assert internal[0]["role"] == "user"
        assert "discovery calls" in internal[0]["content"]

    def test_the_focus_message_does_not_start_a_new_turn(self) -> None:
        history = self._history_with_focus()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        starts = _turn_starts(history.messages)
        assert len(starts) == 1, (
            "internal steering created a second, fake user turn"
        )
        assert len(_turn_starts(view.messages)) <= 1

    def test_the_focus_message_still_reaches_the_model_without_the_marker(
        self,
    ) -> None:
        history = self._history_with_focus()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        delivered = [
            m for m in view.messages
            if m.get("role") == "user" and "discovery calls" in str(m.get("content"))
        ]
        assert delivered, "the focus instruction never reached the model"
        assert all("aura_internal" not in m for m in view.messages)

    def test_a_genuine_user_message_still_starts_a_turn(self) -> None:
        history = self._history_with_focus()
        history.append_user_text("actually, do this instead")

        assert len(_turn_starts(history.messages)) == 2


# ── the view remains non-destructive and complete ───────────────────────────


class TestViewRemainsIntact:

    def _history(self) -> History:
        history = History()
        history.set_system("system")
        history.append_user_text("Fix the retry cap.")
        calls = [tool_call("c1", "read_file", {"path": "m.py"})]
        history.append_assistant({
            "role": "assistant",
            "content": "",
            "reasoning_content": "thinking hard about the cap\n",
            "tool_calls": calls,
        })
        history.append_tool_result("c1", json.dumps({"ok": True, "content": "x" * 200}))
        history.append_internal_user_text("Loop guard: 12 discovery calls have run.")
        return history

    def test_building_the_view_does_not_mutate_history(self) -> None:
        history = self._history()
        before = json.dumps(history.messages, sort_keys=True)

        build_api_view("system", history.messages, budget_tokens=1_000)
        build_api_view("system", history.messages, budget_tokens=200_000)

        assert json.dumps(history.messages, sort_keys=True) == before

    def test_reasoning_is_replayed_into_the_view(self) -> None:
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        assert view.stats.reasoning_chars_replayed > 0

    def test_pairing_survives_the_view(self) -> None:
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        assert_tool_pairing_valid([m for m in view.messages if m.get("role") != "system"])

    def test_pairing_survives_a_brutal_budget(self) -> None:
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200)

        assert_tool_pairing_valid([m for m in view.messages if m.get("role") != "system"])


# ── single-mode content gating is unaffected ────────────────────────────────


def test_single_mode_still_gets_a_content_gate_and_a_guard() -> None:
    state = _SendState(mode="single", research_policy=None)

    assert state.content_gate is not None
    assert state.pre_edit_guard is not None


def test_planner_mode_gets_neither() -> None:
    state = _SendState(mode="planner", research_policy=None)

    assert state.content_gate is None
    assert state.pre_edit_guard is None
