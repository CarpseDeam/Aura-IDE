"""Plan Review's gate over the terminal-special-cased tool round path.

``run_terminal_command`` and ``run_and_watch`` bypass ``ToolRegistry.execute()``
— ``ToolRoundRunner`` dispatches them straight to ``ToolRunner`` because they
stream their own output as they run. Before this fix, Plan Review's runtime
guarantee ("a MUTATION/COMMAND call is refused before it reaches any handler")
was only true for calls that went through ``ToolRegistry.execute()``, so a
model could run arbitrary workspace-mutating shell commands before a plan was
ever approved.

These tests exercise ``ToolRoundRunner.run()`` directly — the one seam both
registry-routed and terminal-special-cased calls share — and prove the
required semantics while Plan Review is required and not yet approved:

1. run_terminal_command does not execute before approval
2. run_and_watch does not execute before approval
3. ordinary MUTATION remains blocked
4. OBSERVATION remains allowed
5. BOOKKEEPING remains allowed
6. review_implementation_plan remains callable
7. COMMAND and MUTATION become callable normally after approval
8. Plan OFF preserves current terminal behavior
9. a COMMAND-classified extensible tool is blocked pre-review
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from aura.client import ToolResult
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.plan_review import ApprovedPlan, PlanReviewDecision
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.effects import SCHEMA_EFFECT_KEY
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry


def tool_call(call_id: str, name: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def make_runner(tmp_path: Path) -> tuple[ToolRoundRunner, ToolRegistry, History]:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    tools = ToolRegistry(workspace_root=tmp_path)
    tool_runner = ToolRunner(history=history, workspace_root=tmp_path)
    runner = ToolRoundRunner(history=history, tools=tools, tool_runner=tool_runner)
    return runner, tools, history


def run_batch(runner: ToolRoundRunner, calls: list[dict[str, Any]]) -> list[Any]:
    events: list[Any] = []
    runner.run(
        tool_calls=calls,
        on_event=events.append,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda cb: None,
    )
    return events


def result_payload(events: list[Any], call_id: str) -> dict[str, Any]:
    for ev in events:
        if isinstance(ev, ToolResult) and ev.tool_call_id == call_id:
            return json.loads(ev.result)
    raise AssertionError(f"no ToolResult for {call_id}")


# ── 1 & 2: terminal-special-cased tools never reach ToolRunner while blocked ─


def test_run_terminal_command_does_not_execute_before_approval(tmp_path, monkeypatch) -> None:
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)

    def _fail(*_a, **_kw):
        raise AssertionError("run_terminal_command handler must not run while blocked")

    monkeypatch.setattr(ToolRunner, "handle_terminal_command", _fail)

    events = run_batch(runner, [tool_call("c1", "run_terminal_command", {"command": "echo hi"})])

    payload = result_payload(events, "c1")
    assert payload["ok"] is False
    assert payload["failure_class"] == "plan_review_required"
    assert payload["required_tool"] == "review_implementation_plan"


def test_run_and_watch_does_not_execute_before_approval(tmp_path, monkeypatch) -> None:
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)

    def _fail(*_a, **_kw):
        raise AssertionError("run_and_watch handler must not run while blocked")

    monkeypatch.setattr(ToolRunner, "handle_run_and_watch", _fail)

    events = run_batch(runner, [tool_call("c1", "run_and_watch", {"window_seconds": 5})])

    payload = result_payload(events, "c1")
    assert payload["ok"] is False
    assert payload["failure_class"] == "plan_review_required"
    assert payload["required_tool"] == "review_implementation_plan"


# ── 3, 4, 5: the other effect classes, through the same seam ────────────────


def test_ordinary_mutation_remains_blocked_through_the_tool_round(tmp_path, monkeypatch) -> None:
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)

    called: list[bool] = []
    original = ToolRegistry._handle_write_file

    def spy(self, args, approval_cb, reject_all=False):
        called.append(True)
        return original(self, args, approval_cb, reject_all)

    monkeypatch.setattr(ToolRegistry, "_handle_write_file", spy)

    events = run_batch(
        runner, [tool_call("c1", "write_file", {"path": "x.txt", "content": "hi"})]
    )

    payload = result_payload(events, "c1")
    assert payload["ok"] is False
    assert payload["failure_class"] == "plan_review_required"
    assert called == [], "the mutation handler must never run while blocked"
    assert not (tmp_path / "x.txt").exists()


def test_observation_remains_allowed_through_the_tool_round(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)

    events = run_batch(runner, [tool_call("c1", "read_file", {"path": "a.txt"})])

    payload = result_payload(events, "c1")
    assert payload.get("failure_class") != "plan_review_required"
    assert payload["ok"] is True


def test_bookkeeping_remains_allowed_through_the_tool_round(tmp_path) -> None:
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)

    events = run_batch(
        runner, [tool_call("c1", "report_blocker", {"blocker": "waiting on input"})]
    )

    payload = result_payload(events, "c1")
    assert payload["ok"] is True
    assert payload["blocker_reported"] is True


# ── 6: the gate's own escape hatch ───────────────────────────────────────────


def test_review_implementation_plan_remains_callable_while_blocked(tmp_path) -> None:
    class _Proxy:
        def request_review(self, goal, files, spec, acceptance, summary):
            return PlanReviewDecision(
                approved=True,
                plan=ApprovedPlan(
                    goal=goal, files=tuple(files), spec=spec,
                    acceptance=acceptance, summary=summary,
                ),
            )

    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)
    tools.set_plan_review_proxy(_Proxy())

    events = run_batch(
        runner,
        [tool_call("c1", "review_implementation_plan", {
            "goal": "g", "spec": "s", "acceptance": "a",
        })],
    )

    payload = result_payload(events, "c1")
    assert payload["ok"] is True
    assert payload["approved"] is True
    assert tools.plan_review.approved is True


# ── 7: once approved, everything resumes normally ────────────────────────────


def test_command_and_mutation_become_callable_after_approval(tmp_path, monkeypatch) -> None:
    runner, tools, history = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)
    tools.plan_review.approve(ApprovedPlan(goal="g", files=(), spec="s", acceptance="a"))

    terminal_calls: list[str] = []

    def fake_terminal(self, tool_call_id, args, on_event, cancel_event, explicit_validation_commands=None):
        terminal_calls.append(tool_call_id)
        payload = json.dumps({"ok": True, "exit_code": 0, "output": "hi", "command": args.get("command", "")})
        self._history.append_tool_result(tool_call_id, payload)
        on_event(ToolResult(tool_call_id=tool_call_id, name="run_terminal_command", ok=True, result=payload))
        return {"_terminal_payload": {"ok": True}}

    monkeypatch.setattr(ToolRunner, "handle_terminal_command", fake_terminal)

    events = run_batch(
        runner,
        [
            tool_call("c1", "run_terminal_command", {"command": "echo hi"}),
            tool_call("c2", "write_file", {"path": "x.txt", "content": "hi"}),
        ],
    )

    assert terminal_calls == ["c1"], "run_terminal_command must execute once approved"
    terminal_payload = result_payload(events, "c1")
    assert terminal_payload["ok"] is True

    write_payload = result_payload(events, "c2")
    assert write_payload["ok"] is True
    assert (tmp_path / "x.txt").read_text(encoding="utf-8") == "hi"


# ── 8: Plan OFF is byte-for-byte the previous terminal behavior ─────────────


def test_plan_off_preserves_current_terminal_behavior(tmp_path, monkeypatch) -> None:
    runner, tools, _ = make_runner(tmp_path)
    # Plan Review left at its default (off) — no begin_turn(required=True).
    assert tools.plan_review.required is False

    terminal_calls: list[str] = []

    def fake_terminal(self, tool_call_id, args, on_event, cancel_event, explicit_validation_commands=None):
        terminal_calls.append(tool_call_id)
        payload = json.dumps({"ok": True, "exit_code": 0, "output": "hi", "command": args.get("command", "")})
        self._history.append_tool_result(tool_call_id, payload)
        on_event(ToolResult(tool_call_id=tool_call_id, name="run_terminal_command", ok=True, result=payload))
        return {"_terminal_payload": {"ok": True}}

    monkeypatch.setattr(ToolRunner, "handle_terminal_command", fake_terminal)

    events = run_batch(runner, [tool_call("c1", "run_terminal_command", {"command": "echo hi"})])

    assert terminal_calls == ["c1"]
    assert result_payload(events, "c1")["ok"] is True


# ── 9: extensible COMMAND-classified tools obey the same gate ───────────────


class _FakeMCPClient:
    def call_tool(self, name: str, args: dict) -> dict:
        return {"ok": True, "result": "ok"}


def test_extensible_command_tool_blocked_before_review(tmp_path) -> None:
    runner, tools, _ = make_runner(tmp_path)
    tools.plan_review.begin_turn(required=True)
    tools._mcp_tools.register_tool_def(
        {
            "name": "srv_runner",
            "description": "runs something server-side",
            "inputSchema": {"type": "object"},
            SCHEMA_EFFECT_KEY: "command",
        },
        _FakeMCPClient(),
    )
    try:
        events = run_batch(runner, [tool_call("c1", "srv_runner", {})])
        payload = result_payload(events, "c1")
        assert payload["ok"] is False
        assert payload["failure_class"] == "plan_review_required"
    finally:
        TOOL_HANDLERS.pop("srv_runner", None)
