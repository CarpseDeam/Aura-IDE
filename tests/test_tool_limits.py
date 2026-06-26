"""Tests for emergency tool-call guardrails."""

from __future__ import annotations

import json

from aura.conversation.tool_limits import (
    MAX_TOOL_CALLS_BY_MODE,
    ToolLimitState,
    limit_reached_payload,
)


def test_worker_allows_calls_up_to_emergency_limit():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TOOL_CALLS_BY_MODE["worker"]):
        allowed, info = state.check("read_file")
        assert allowed is True
        assert info == {}
        state.record("read_file")

    assert state.total_calls == MAX_TOOL_CALLS_BY_MODE["worker"]


def test_worker_emergency_limit_is_recoverable_phase_boundary():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TOOL_CALLS_BY_MODE["worker"]):
        state.record("read_file")

    allowed, info = state.check("read_file")

    assert allowed is False
    assert info["ok"] is False
    assert info["limit_reached"] is True
    assert info["recoverable"] is True
    assert info["phase_boundary"] is True
    assert info["reason"] == "worker_emergency_tool_call_limit_reached"


def test_planner_context_reads_are_not_category_capped():
    state = ToolLimitState(mode="planner")

    for _ in range(50):
        allowed, info = state.check("grep_search")
        assert allowed is True
        assert info == {}
        state.record("grep_search")

    assert state.planner_context_calls == 50


def test_planner_dispatch_is_not_category_capped_per_round():
    state = ToolLimitState(mode="planner")

    for _ in range(5):
        allowed, info = state.check("dispatch_to_worker")
        assert allowed is True
        assert info == {}
        state.record("dispatch_to_worker")

    assert state.round_dispatch_calls == 5
    state.begin_model_round()
    assert state.round_dispatch_calls == 0
    assert state.dispatch_calls == 5


def test_worker_terminal_and_write_tools_are_not_category_capped():
    state = ToolLimitState(mode="worker")

    for name in ["run_terminal_command", "write_file"]:
        for _ in range(40):
            allowed, info = state.check(name)
            assert allowed is True
            assert info == {}
            state.record(name)

    assert state.terminal_calls == 40
    assert state.write_calls == 40


def test_limit_payload_is_json_with_recoverable_fields():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TOOL_CALLS_BY_MODE["worker"]):
        state.record("read_file")

    _allowed, info = state.check("read_file")
    parsed = json.loads(limit_reached_payload(info))

    assert parsed["ok"] is False
    assert parsed["limit_reached"] is True
    assert parsed["recoverable"] is True
    assert parsed["phase_boundary"] is True
