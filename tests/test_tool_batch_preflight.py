"""Multi-call batch preflight and rejection pairing for the tool round.

Before any call in an assistant round executes, the whole proposed batch is
parsed, classified against the registry's authoritative tool-effect metadata,
and checked by the limit and loop-guard gates *before any call executes*.  If
any call is invalid, the entire batch is rejected coherently: no accepted
prefix runs, and every rejected tool call receives exactly one paired tool
result — the invalid call gets its own rejection payload, every valid sibling
gets a batch-rejection payload explaining that nothing ran.

The loop guard's only structured rejection is the exact-repeat read before the
first applied write; there is no discovery spend budget any more.

What is asserted here:

* a batch with one guard-blocked read is rejected as a whole;
* an accepted prefix (a mutation) never executes when a sibling vetoes the batch;
* every rejected call — invalid and valid sibling alike — receives exactly one
  paired tool result, in call order, through both history and events;
* parse-error and limit-blocked batches are rejected the same way;
* ``is_narrow_read`` distinguishes bounded ``read_file`` windows from unbounded
  reads and historical replay tools;
* the guard's effect classifier covers non-built-in observation tools (Git,
  Godot, dynamic, MCP, drone, workspace, web) via the registry wiring.
"""

from __future__ import annotations

import json
import threading
from typing import Any

import pytest

from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundOutcome, ToolRoundRunner
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.pre_edit_loop_guard import (
    DUPLICATE_READ_REASON,
    PreEditLoopGuard,
    is_narrow_read,
)
from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry


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


def malformed_call(call_id: str, name: str) -> dict[str, Any]:
    """A tool call whose arguments are not valid JSON."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": "{not json"},
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


# ── whole-batch rejection ───────────────────────────────────────────────────


class TestWholeBatchRejection:

    def test_a_guard_blocked_batch_is_rejected_as_a_whole(self, runner) -> None:
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        # A write that would otherwise be accepted sits first in the batch;
        # the guard-blocked repeat read after it must veto the whole batch.
        calls = [
            tool_call("call-write", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
            tool_call("call-read", "read_file", {"path": "alpha.py"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = tool_results(history.messages)
        assert [r["tool_call_id"] for r in results] == ["call-write", "call-read"]
        assert not (workspace / "new.py").exists(), (
            "the accepted-prefix write executed despite the batch being rejected"
        )

    def test_every_rejected_call_receives_exactly_one_paired_result(
        self, runner,
    ) -> None:
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        # Mutations are the valid siblings here: they pass the guard gate (only
        # observation calls are gated), so they must be answered as batch
        # rejections rather than executed.
        calls = [
            tool_call("call-w1", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
            tool_call("call-read", "read_file", {"path": "alpha.py"}),
            tool_call("call-w2", "write_file", {
                "path": "other.py", "content": "y = 2\n",
            }),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        events = run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        assert [r["tool_call_id"] for r in tool_results(history.messages)] == [
            "call-w1", "call-read", "call-w2",
        ]
        emitted = [e for e in events if type(e).__name__ == "ToolResult"]
        assert [e.tool_call_id for e in emitted] == ["call-w1", "call-read", "call-w2"]
        assert not (workspace / "new.py").exists()
        assert not (workspace / "other.py").exists()

    def test_valid_siblings_get_a_coherent_batch_rejection_payload(
        self, runner,
    ) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [
            tool_call("call-write", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
            tool_call("call-read", "read_file", {"path": "alpha.py"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        sibling = json.loads(tool_results(history.messages)[0]["content"])
        assert sibling["ok"] is False
        assert sibling["recoverable"] is True
        assert sibling["batch_rejected"] is True
        assert sibling["reason"] == "tool_batch_rejected_before_execution"
        assert sibling["rejected_sibling_call_id"] == "call-read"
        assert sibling["rejected_sibling_tool"] == "read_file"
        assert "No call in this batch executed" in sibling["message"]

    def test_the_invalid_call_keeps_its_own_rejection_reason(self, runner) -> None:
        _, history, _ = runner
        state = _SendState(mode="single", research_policy=None)
        guard_blocked_read(state)

        calls = [tool_call("call-1", "read_file", {"path": "alpha.py"})]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        payload = json.loads(tool_results(history.messages)[0]["content"])
        assert payload["reason"] == DUPLICATE_READ_REASON
        assert payload["recoverable"] is True

    def test_a_parse_error_batch_is_rejected_coherently(self, runner) -> None:
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)

        calls = [
            malformed_call("call-bad", "read_file"),
            tool_call("call-write", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        run_round(runner, state, calls)

        assert_tool_pairing_valid(history.messages)
        results = tool_results(history.messages)
        assert [r["tool_call_id"] for r in results] == ["call-bad", "call-write"]

        bad = json.loads(results[0]["content"])
        assert bad["ok"] is False
        assert "failed to parse tool arguments" in bad["error"]

        sibling = json.loads(results[1]["content"])
        assert sibling["batch_rejected"] is True
        assert sibling["rejected_sibling_call_id"] == "call-bad"
        assert not (workspace / "new.py").exists()

    def test_a_limit_blocked_batch_is_rejected_coherently(self, runner) -> None:
        _, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)
        # Exhaust the emergency total-call backstop so the first preflight
        # check fails without executing anything.
        state.limits.total_calls = MAX_TOOL_CALLS_BY_MODE["single"]

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

        limited = json.loads(results[0]["content"])
        assert limited["limit_reached"] is True
        assert limited["reason"] == "single_emergency_tool_call_limit_reached"

        sibling = json.loads(results[1]["content"])
        assert sibling["batch_rejected"] is True
        assert sibling["rejected_sibling_call_id"] == "call-1"
        assert not (workspace / "new.py").exists()


# ── cancellation ─────────────────────────────────────────────────────────────


class TestCancellation:

    def test_a_cancelled_round_executes_no_call_and_fabricates_no_result(
        self, runner,
    ) -> None:
        """After a valid preflight, an already-set cancel event must stop the
        round before any call runs, with no fabricated tool results."""
        runner_obj, history, workspace = runner
        state = _SendState(mode="single", research_policy=None)

        calls = [
            tool_call("call-w", "write_file", {
                "path": "new.py", "content": "x = 1\n",
            }),
            tool_call("call-r", "read_file", {"path": "alpha.py"}),
        ]
        history.append_assistant({"role": "assistant", "content": "", "tool_calls": calls})
        cancel_event = threading.Event()
        cancel_event.set()
        cleaned_up: list[bool] = []
        events: list[Any] = []

        outcome = runner_obj.run(
            tool_calls=calls,
            state=state,
            on_event=events.append,
            approval_cb=lambda req: None,
            cancel_event=cancel_event,
            dispatch_cb=None,
            cleanup_cancelled=lambda cb: cleaned_up.append(True),
        )

        assert outcome == ToolRoundOutcome(action="return")
        assert cleaned_up == [True], "cleanup_cancelled was not invoked"
        assert tool_results(history.messages) == [], (
            "a cancelled round must not fabricate results for unexecuted calls"
        )
        assert not (workspace / "new.py").exists()


# ── narrow-read classification ──────────────────────────────────────────────


class TestIsNarrowRead:

    def test_a_bounded_read_file_window_is_narrow(self) -> None:
        assert is_narrow_read("read_file", {"path": "a.py", "offset": 10, "limit": 40})

    def test_an_unbounded_read_file_is_broad(self) -> None:
        assert not is_narrow_read("read_file", {"path": "a.py"})
        assert not is_narrow_read("read_file", {"path": "a.py", "offset": 10})
        assert not is_narrow_read("read_file", {"path": "a.py", "limit": 40})

    def test_zero_and_non_int_bounds_are_not_narrow(self) -> None:
        assert not is_narrow_read(
            "read_file", {"path": "a.py", "offset": 0, "limit": 40}
        )
        assert not is_narrow_read(
            "read_file", {"path": "a.py", "offset": True, "limit": 40}
        )
        assert not is_narrow_read(
            "read_file", {"path": "a.py", "offset": 10, "limit": 0}
        )

    def test_historical_replay_tools_stay_narrow(self) -> None:
        assert is_narrow_read("read_file_range", {"path": "a.py"})
        assert is_narrow_read("read_file_outline", {"path": "a.py"})

    def test_other_observation_tools_are_broad(self) -> None:
        assert not is_narrow_read("glob", {"pattern": "**/*.py"})
        assert not is_narrow_read("grep_search", {"pattern": "x"})
        assert not is_narrow_read("list_directory", {"path": "."})


# ── registry-driven guard classification ───────────────────────────────────


class _FakeMCPClient:
    def call_tool(self, name: str, args: dict) -> dict:
        return {"ok": True, "result": "ok"}


class TestGuardEffectClassification:

    def test_observation_tools_from_every_channel_produce_evidence(
        self, tmp_path,
    ) -> None:
        """Git, Godot, web, workspace, drone, dynamic, and MCP observation
        calls are all counted as evidence — the guard never falls back to a
        built-in name set."""
        (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        # Registering an MCP tool also touches the module-global TOOL_HANDLERS
        # (back-compat path); clean it up so the catalog enumeration test in
        # test_tool_effects.py never sees this name.
        registry._mcp_tools.register_tool_def(
            {
                "name": "server_observer",
                "description": "reads server side",
                "inputSchema": {"type": "object"},
            },
            _FakeMCPClient(),
        )
        TOOL_HANDLERS.pop("server_observer", None)

        state = _SendState(
            mode="single",
            research_policy=None,
            tool_effect=registry.tool_effect,
        )
        guard = state.pre_edit_guard

        observation_names = [
            "git_status",               # Git
            "inspect_godot_assets",     # Godot
            "web_search",               # web
            "get_workspace_snapshot",   # workspace
            "launch_read_only_drone",   # drone
            "some_dynamic_observer",    # dynamic default -> observation
            "server_observer",          # MCP default -> observation
        ]
        guard.begin_round()
        for i, name in enumerate(observation_names):
            guard.record(name, {"path": f"m{i}.py"})
        for i, name in enumerate(observation_names):
            guard.observe_result(
                name, True, json.dumps({"path": f"m{i}.py", "content": f"b{i}"})
            )
        guard.end_round()

        assert len(guard.seen_evidence) == len(observation_names), (
            "observation results through non-built-in channels were not "
            "treated as evidence"
        )
        assert guard.focused is False, (
            "a round full of new evidence through every channel must not stall"
        )

    def test_mutation_and_command_classifications_are_not_observation(
        self, tmp_path,
    ) -> None:
        (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        guard = PreEditLoopGuard(effect_lookup=registry.tool_effect)

        assert guard._is_mutation("write_file")
        assert guard._is_command("run_terminal_command")
        assert not guard._is_observation("write_file")
        assert not guard._is_observation("run_terminal_command")
        assert guard._is_observation("read_file")

    def test_a_wired_guard_does_not_track_mutations_as_reads(
        self, tmp_path,
    ) -> None:
        (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")
        registry = ToolRegistry(workspace_root=tmp_path, mode="single")
        state = _SendState(
            mode="single",
            research_policy=None,
            tool_effect=registry.tool_effect,
        )
        guard = state.pre_edit_guard

        guard.record("write_file", {"path": "alpha.py", "content": "x"})
        guard.record("read_file", {"path": "alpha.py"})

        # Only the observation left a read fingerprint; the mutation is not
        # part of duplicate-read tracking or the stall reckoning.
        assert len(guard.seen_reads) == 1
        assert guard.check("read_file", {"path": "alpha.py"})["reason"] == (
            DUPLICATE_READ_REASON
        )
        assert guard.check("write_file", {"path": "alpha.py"}) is None
