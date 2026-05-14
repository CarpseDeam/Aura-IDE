"""Tests for simple count-based tool limits."""

from __future__ import annotations

import json

from aura.conversation.tool_limits import (
    MAX_DISPATCH_CALLS_PER_PLANNER_TURN,
    MAX_TERMINAL_CALLS_PER_WORKER_PASS,
    MAX_TOOL_CALLS_BY_MODE,
    MAX_WRITE_CALLS_PER_WORKER_PASS,
    ToolLimitState,
    limit_reached_payload,
)


def test_worker_allows_calls_up_to_total_limit():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TOOL_CALLS_BY_MODE["worker"]):
        allowed, info = state.check("read_file")
        assert allowed is True
        assert info == {}
        state.record("read_file")

    assert state.total_calls == MAX_TOOL_CALLS_BY_MODE["worker"]


def test_worker_rejects_after_total_limit_as_phase_boundary():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TOOL_CALLS_BY_MODE["worker"]):
        state.record("read_file")

    allowed, info = state.check("read_file")

    assert allowed is False
    assert info["ok"] is False
    assert info["limit_reached"] is True
    assert info["recoverable"] is True
    assert info["phase_boundary"] is True
    assert info["reason"] == "worker_tool_call_limit_reached"


def test_worker_terminal_cap_rejects_terminal_command():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_TERMINAL_CALLS_PER_WORKER_PASS):
        allowed, _info = state.check("run_terminal_command")
        assert allowed is True
        state.record("run_terminal_command")

    allowed, info = state.check("run_terminal_command")

    assert allowed is False
    assert info["reason"] == "worker_terminal_call_limit_reached"
    assert info["limit_reached"] is True


def test_worker_write_cap_rejects_write_tool():
    state = ToolLimitState(mode="worker")
    for _ in range(MAX_WRITE_CALLS_PER_WORKER_PASS):
        allowed, _info = state.check("edit_file")
        assert allowed is True
        state.record("edit_file")

    allowed, info = state.check("write_file")

    assert allowed is False
    assert info["reason"] == "worker_write_call_limit_reached"
    assert info["limit_reached"] is True


def test_planner_dispatch_cap_is_per_model_round():
    state = ToolLimitState(mode="planner")
    for _ in range(MAX_DISPATCH_CALLS_PER_PLANNER_TURN):
        allowed, _info = state.check("dispatch_to_worker")
        assert allowed is True
        state.record("dispatch_to_worker")

    allowed, info = state.check("dispatch_to_worker")
    assert allowed is False
    assert info["reason"] == "planner_dispatch_call_limit_reached"

    state.begin_model_round()
    allowed, info = state.check("dispatch_to_worker")
    assert allowed is True
    assert info == {}


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
