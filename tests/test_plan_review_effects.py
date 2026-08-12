"""Plan Review's runtime mutation gate: authoritative ToolEffect lookup.

Proves the gate lives in ``ToolRegistry.execute`` and reads ``tool_effect``
directly — no second handwritten mutation-name list — so it covers built-in
mutation tools and mutation-classified extensible (MCP) tools alike, while
observation and command/bookkeeping tools stay callable throughout.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aura.conversation.tools.effects import SCHEMA_EFFECT_KEY, ToolEffect
from aura.conversation.tools.registry import ToolRegistry


def _reject_immediately(_request: Any):
    raise AssertionError("approval must not be consulted before a mutation handler runs")


class _FakeMCPClient:
    def call_tool(self, name: str, args: dict) -> dict:
        return {"ok": True, "result": "ok"}


def test_observation_tool_remains_callable_before_plan_approval(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)

    result = registry.execute("read_file", {"path": "a.txt"}, approval_cb=None)
    assert result.payload.get("failure_class") != "plan_review_required"
    assert result.ok is True


def test_command_effect_tool_remains_callable_before_plan_approval(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)
    assert registry.tool_effect("run_diagnostic_command") is ToolEffect.COMMAND

    result = registry.execute(
        "run_diagnostic_command", {"command": "python -c \"print(1)\""}, approval_cb=None
    )
    assert result.payload.get("failure_class") != "plan_review_required"


def test_bookkeeping_review_tool_itself_is_never_gated(tmp_path: Path) -> None:
    """``review_implementation_plan`` is BOOKKEEPING, not MUTATION — it must
    be callable while the gate is active, or the model could never satisfy it."""
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)
    assert registry.tool_effect("review_implementation_plan") is ToolEffect.BOOKKEEPING


def test_mutation_tool_rejected_before_plan_approval_without_running_handler(
    tmp_path: Path, monkeypatch
) -> None:
    called: list[bool] = []
    original = ToolRegistry._handle_write_file

    def spy(self, args, approval_cb, reject_all=False):
        called.append(True)
        return original(self, args, approval_cb, reject_all)

    monkeypatch.setattr(ToolRegistry, "_handle_write_file", spy)

    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)

    result = registry.execute(
        "write_file", {"path": "x.txt", "content": "hi"}, approval_cb=_reject_immediately
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "plan_review_required"
    assert result.payload["required_tool"] == "review_implementation_plan"
    assert called == [], "the mutation handler must never run while blocked"
    assert not (tmp_path / "x.txt").exists()


def test_extensible_mutation_tool_obeys_the_same_effect_based_gate(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)
    registry._mcp_tools.register_tool_def(
        {
            "name": "srv_writer",
            "description": "writes server-side",
            "inputSchema": {"type": "object"},
            SCHEMA_EFFECT_KEY: "mutation",
        },
        _FakeMCPClient(),
    )
    try:
        assert registry.tool_effect("srv_writer") is ToolEffect.MUTATION

        result = registry.execute("srv_writer", {}, approval_cb=_reject_immediately)
        assert result.ok is False
        assert result.payload["failure_class"] == "plan_review_required"
    finally:
        from aura.conversation.tools.registry import TOOL_HANDLERS

        TOOL_HANDLERS.pop("srv_writer", None)


def test_mutation_allowed_once_plan_review_is_not_required(tmp_path: Path) -> None:
    """Sanity check: the gate is Plan-Review-specific, not a general block."""
    registry = ToolRegistry(tmp_path, mode="single")
    assert registry.plan_review.required is False

    from aura.conversation.tools._types import ApprovalDecision

    result = registry.execute(
        "write_file",
        {"path": "x.txt", "content": "hi"},
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
    )
    assert result.ok is True
    assert (tmp_path / "x.txt").exists()
