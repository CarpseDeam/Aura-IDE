"""The loop guard's rejection must not damage anything the send loop does.

The discovery-budget counter is gone; the only structured guard rejection left
is the exact-repeat read. A new rejection path and the aura_internal boundary
are exactly the kind of change that quietly breaks tool pairing (an assistant
tool_call with no matching result) or drops reasoning on replay. These tests
drive the real ``ToolRoundRunner`` and the real API view.

What is asserted here:

* a guard-blocked read still produces a paired tool result;
* the rejection reaches the model as a recoverable payload and a ToolResult
  event, so activity rendering has something to show;
* a mixed round with one guard-blocked call pairs every call, executes nothing,
  and answers the valid sibling with a coherent batch rejection;
* a fresh read is never refused by any count;
* the API view remains non-destructive and keeps tool pairing under a brutal
  budget, with completed-step reasoning shed instead of replayed;
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
from aura.conversation.pre_edit_loop_guard import DUPLICATE_READ_REASON
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.registry import ToolRegistry


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


def guard_blocked_read(state: _SendState) -> None:
    """Record one read so the identical read is the guard-blocked call."""
    state.pre_edit_guard.record("read_file", {"path": "alpha.py"})


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


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [m for m in messages if m.get("role") == "tool"]


# ── the duplicate-read rejection travels correctly ──────────────────────────


class TestDuplicateReadRejectionTravels:

    def test_a_rejected_call_still_gets_a_paired_tool_result(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = tool_results(history.messages)
        assert len(results) == 1
        assert results[0]["tool_call_id"] == "call-1"

    def test_the_result_payload_is_the_recoverable_rejection(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["reason"] == DUPLICATE_READ_REASON
        assert payload["recoverable"] is True
        assert payload["ok"] is False

    def test_a_tool_result_event_is_emitted_for_activity_rendering(
        self, runner,
    ) -> None:
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        events = run_round(runner, state, calls)

        tool_results_events = [e for e in events if type(e).__name__ == "ToolResult"]
        assert len(tool_results_events) == 1
        assert tool_results_events[0].name == "read_file"
        assert tool_results_events[0].ok is False
        assert tool_results_events[0].extras["reason"] == DUPLICATE_READ_REASON
        assert tool_results_events[0].extras["recoverable"] is True

    def test_a_mixed_round_pairs_every_call_and_executes_nothing(
        self, runner,
    ) -> None:
        """One guard-blocked read vetoes the whole batch: the accepted-prefix
        write must not run, and every call still gets exactly one paired result."""
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [
            tool_call("call-1", "read_file", {"path": "alpha.py"}),
            tool_call("call-write", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = tool_results(history.messages)
        assert [r["tool_call_id"] for r in results] == ["call-1", "call-write"]
        assert not (workspace / "new.py").exists(), (
            "the accepted-prefix write executed despite the batch being rejected"
        )
        sibling = json.loads(results[1]["content"])
        assert sibling["batch_rejected"] is True
        assert sibling["rejected_sibling_call_id"] == "call-1"

    def test_a_fresh_narrow_read_executes_normally(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)

        calls = [tool_call("call-1", "read_file_outline", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload.get("reason") != DUPLICATE_READ_REASON
        assert payload.get("ok") is not False

    def test_a_fresh_read_is_never_refused_by_a_count(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload.get("reason") != DUPLICATE_READ_REASON
        # The accepted call was recorded: its exact repeat is now guarded.
        fingerprints = state.pre_edit_guard.seen_reads
        assert any("alpha.py" in fp for fp in fingerprints)


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
        history.append_internal_user_text("completed-step boundary")
        return history

    def test_building_the_view_does_not_mutate_history(self) -> None:
        history = self._history()
        before = json.dumps(history.messages, sort_keys=True)

        build_api_view("system", history.messages, budget_tokens=1_000)
        build_api_view("system", history.messages, budget_tokens=200_000)

        assert json.dumps(history.messages, sort_keys=True) == before

    def test_reasoning_before_the_last_user_message_is_shed(self) -> None:
        """The provider-visible boundary is the last user message, so the
        reasoning that preceded it is dead weight and the stats say exactly
        what was removed."""
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        assert view.stats.reasoning_chars_replayed == 0
        assert view.stats.reasoning_chars_dropped == len("thinking hard about the cap\n")
        assert not any(m.get("reasoning_content") for m in view.messages)

    def test_pairing_survives_the_view(self) -> None:
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200_000)

        assert_tool_pairing_valid([m for m in view.messages if m.get("role") != "system"])

    def test_pairing_survives_a_brutal_budget(self) -> None:
        history = self._history()
        view = build_api_view("system", history.messages, budget_tokens=200)

        assert_tool_pairing_valid([m for m in view.messages if m.get("role") != "system"])

    def test_a_genuine_user_message_still_starts_a_turn(self) -> None:
        history = self._history()
        history.append_user_text("actually, do this instead")

        assert len(_turn_starts(history.messages)) == 2

    def test_completed_step_reasoning_does_not_grow_round_after_round(self) -> None:
        """The production regression: one real request drives a multi-round
        tool loop, and every finished batch used to replay its reasoning on
        every subsequent round. Building the view the way ``send()`` does —
        assistant + paired result per round — must replay only the active
        batch, and that batch must stay provider-valid."""
        history = History()
        history.set_system("system")
        history.append_user_text("Fix the retry cap so the job pauses.")
        for i in range(4):
            history.append_assistant({
                "role": "assistant",
                "content": "",
                "reasoning_content": f"working on round {i}\n",
                "tool_calls": [tool_call(f"r{i}", "read_file", {"path": f"m{i}.py"})],
            })
            history.append_tool_result(
                f"r{i}",
                json.dumps({"ok": True, "path": f"m{i}.py", "content": f"b{i}"}),
            )
            view = build_api_view("system", history.messages, budget_tokens=200_000)

            replayed = [
                m.get("reasoning_content")
                for m in view.messages
                if m.get("role") == "assistant" and m.get("reasoning_content")
            ]
            assert replayed == [f"working on round {i}\n"], (
                f"after round {i} the view replayed {replayed!r}; reasoning "
                "grew with every completed batch"
            )
            # The active chain the provider is about to continue still carries
            # its reasoning — the DeepSeek 400 boundary.
            users = [j for j, m in enumerate(view.messages) if m.get("role") == "user"]
            boundary = users[-1]
            for m in view.messages[boundary + 1:]:
                assert m.get("reasoning_content") or not m.get("tool_calls")
            assert_tool_pairing_valid(
                [m for m in view.messages if m.get("role") != "system"]
            )
        # The transient boundary never reached the stored log.
        assert not any(
            m.get("role") == "user" and "completed-step boundary" in str(m.get("content"))
            for m in history.messages
        )


# ── single-mode content gating is unaffected ────────────────────────────────


def test_single_mode_still_gets_a_content_gate_and_a_guard() -> None:
    state = _SendState(mode="single", research_policy=None)

    assert state.content_gate is not None
    assert state.pre_edit_guard is not None


def test_planner_mode_gets_neither() -> None:
    state = _SendState(mode="planner", research_policy=None)

    assert state.content_gate is None
    assert state.pre_edit_guard is None
