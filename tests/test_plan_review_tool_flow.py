"""End-to-end Plan Review tool flow.

Covers: approval returns the human-edited plan, mutation unblocks after
approval, cancellation leaves mutation blocked, a new turn requires a new
review, Plan OFF preserves default production behavior, Approve stays
independent of Plan, and the whole thing runs through exactly one
ConversationManager / History / model — never a second conversation.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from aura.client import ContentDelta, Done, Event, ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.plan_review import ApprovedPlan, PlanReviewDecision, PlanReviewState
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

# ``tests/production_loop_harness.py`` imports a module that no longer exists
# in this repo (``aura.conversation.task_router``) — pre-existing, unrelated
# breakage this task does not chase. These are self-contained equivalents of
# the handful of helpers this file needs.


def tool_round(calls: list[tuple[str, str, dict]]) -> list[Event]:
    events: list[Event] = []
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
        full_message={"role": "assistant", "content": "", "tool_calls": tool_calls},
    ))
    return events


def final_round(text: str) -> list[Event]:
    return [
        ContentDelta(text=text),
        Done(finish_reason="stop", full_message={"role": "assistant", "content": text}),
    ]


def make_workspace(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    return root


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
        return iter(final_round("(script exhausted)"))


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def results_named(self, name: str) -> list[ToolResult]:
        return [
            e for e in self.events
            if isinstance(e, ToolResult) and e.name == name
        ]


class _ScriptedProxy:
    """A fake ``PlanReviewProxy`` that returns one canned decision."""

    def __init__(self, decision: PlanReviewDecision) -> None:
        self.decision = decision
        self.calls: list[tuple] = []

    def request_review(self, goal, files, spec, acceptance, summary):
        self.calls.append((goal, files, spec, acceptance, summary))
        return self.decision


# ── registry-level: approve / cancel ────────────────────────────────────────


def test_approved_review_returns_the_user_edited_plan_and_unblocks_mutation(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)

    edited_plan = ApprovedPlan(
        goal="edited goal", files=("a.py",), spec="edited spec",
        acceptance="edited acceptance", summary="edited summary",
    )
    registry.set_plan_review_proxy(
        _ScriptedProxy(PlanReviewDecision(approved=True, plan=edited_plan, user_edited=True))
    )

    result = registry.execute(
        "review_implementation_plan",
        {"goal": "original goal", "spec": "original spec", "acceptance": "original acceptance"},
        approval_cb=None,
    )

    assert result.ok is True
    assert result.payload["approved"] is True
    assert result.payload["user_edited"] is True
    assert result.payload["goal"] == "edited goal"
    assert result.payload["files"] == ["a.py"]
    assert result.payload["spec"] == "edited spec"
    assert registry.plan_review.approved is True

    write_result = registry.execute(
        "write_file",
        {"path": "a.py", "content": "print(1)\n"},
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
    )
    assert write_result.ok is True
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "print(1)\n"


def test_cancelled_review_leaves_mutation_blocked(tmp_path: Path) -> None:
    registry = ToolRegistry(tmp_path, mode="single")
    registry.plan_review.begin_turn(required=True)
    registry.set_plan_review_proxy(_ScriptedProxy(PlanReviewDecision(approved=False)))

    result = registry.execute(
        "review_implementation_plan",
        {"goal": "g", "spec": "s", "acceptance": "a"},
        approval_cb=None,
    )
    assert result.ok is False
    assert result.payload["approved"] is False
    assert result.payload["failure_class"] == "plan_review_cancelled"
    assert registry.plan_review.approved is False

    def _fail(_req):
        raise AssertionError("must not reach approval after a cancelled review")

    write_result = registry.execute(
        "write_file", {"path": "a.py", "content": "x"}, approval_cb=_fail
    )
    assert write_result.ok is False
    assert write_result.payload["failure_class"] == "plan_review_required"


def test_next_turn_requires_a_new_review() -> None:
    state = PlanReviewState()
    state.begin_turn(required=True)
    state.approve(ApprovedPlan(goal="g", files=(), spec="s", acceptance="a"))
    assert state.approved is True

    state.begin_turn(required=True)
    assert state.approved is False
    assert state.approved_plan is None
    assert state.blocks(ToolEffect.MUTATION) is True


# ── full-loop integration: one manager, one history, one model ─────────────


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


def test_same_model_resumes_with_approved_plan_and_diff_approval_still_applies(
    tmp_path: Path, isolated_streams: ModelStreamRegistry
) -> None:
    workspace = make_workspace(tmp_path / "ws")
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Update notes.md.")
    registry = ToolRegistry(workspace_root=workspace, mode="single")
    registry.plan_review.begin_turn(required=True)
    edited_plan = ApprovedPlan(
        goal="edited goal", files=("notes.md",), spec="edited spec",
        acceptance="edited acceptance", summary="",
    )
    proxy = _ScriptedProxy(PlanReviewDecision(approved=True, plan=edited_plan, user_edited=True))
    registry.set_plan_review_proxy(proxy)
    manager = ConversationManager(history, registry)

    approvals: list[str] = []

    def approval_cb(request):
        approvals.append(request.tool_name)
        return ApprovalDecision(action="approve")

    backend = ScriptedBackend([
        tool_round([("r1", "review_implementation_plan", {
            "goal": "draft goal", "spec": "draft spec", "acceptance": "draft acceptance",
        })]),
        tool_round([("w1", "write_file", {"path": "notes.md", "content": "# Notes\n\nupdated\n"})]),
        final_round("Done."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    recorder = Recorder()
    manager.send(
        on_event=recorder,
        approval_cb=approval_cb,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="high",
    )

    # Exactly one manager over one history drove all three model requests —
    # no second conversation, no dispatch runtime.
    assert manager.history is history
    assert len(backend.calls) == 3

    review_result = recorder.results_named("review_implementation_plan")[0]
    review_payload = json.loads(review_result.result)
    assert review_payload["ok"] is True
    assert review_payload["approved"] is True
    assert review_payload["goal"] == "edited goal", "the tool result must carry the human-approved values"

    assert registry.plan_review.approved is True

    write_result = recorder.results_named("write_file")[0]
    write_payload = json.loads(write_result.result)
    assert write_payload["ok"] is True
    assert "updated" in (workspace / "notes.md").read_text(encoding="utf-8")

    # File-diff approval behavior is untouched and independent of Plan Review.
    assert "write_file" in approvals


def test_plan_review_off_preserves_default_production_mutation_behavior(
    tmp_path: Path, isolated_streams: ModelStreamRegistry
) -> None:
    workspace = make_workspace(tmp_path / "ws2")
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Update notes.md.")
    registry = ToolRegistry(workspace_root=workspace, mode="single")
    # Plan Review left at its default (off) — no begin_turn(required=True).
    manager = ConversationManager(history, registry)

    backend = ScriptedBackend([
        tool_round([("w1", "write_file", {"path": "notes.md", "content": "# Notes\n\ndirect\n"})]),
        final_round("Done."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    recorder = Recorder()
    manager.send(
        on_event=recorder,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="off",
    )

    exposed_names = {
        (tool.get("function") or {}).get("name")
        for tool in backend.calls[0].get("tools") or []
    }
    assert "review_implementation_plan" not in exposed_names

    write_result = recorder.results_named("write_file")[0]
    assert json.loads(write_result.result)["ok"] is True
    assert "direct" in (workspace / "notes.md").read_text(encoding="utf-8")
