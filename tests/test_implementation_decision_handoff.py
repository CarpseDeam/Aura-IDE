"""The explicit handoff from repository discovery to implementation.

The wandering shape this covers is the real one: every discovery round returned
genuinely *new* evidence, so no round ever stalled and no ``A, B, A, B`` cycle
ever formed — and the agent, already able to name the owner, the seams, the
target files, and the change, kept surveying adjacent systems, test
infrastructure, and executable locations instead of editing. Nothing in the
loop was wrong; there was simply no way to say "the decision is made".

``commit_implementation_decision`` is that statement. These tests drive the real
``ConversationManager``, the real ``ToolRegistry``, and the real
``PreEditLoopGuard``, and assert:

* several genuinely-advancing discovery rounds, then one commit, then the very
  next request is the focused action request — with no intervening read,
  search, test-runner investigation, or executable hunt;
* the identical script *without* the commit never goes focused, which is what
  proves the transition came from the decision and not from a stall;
* the decision capsule survives forced API-view compaction byte-identically,
  while the source reads that supported it are free to retire;
* the decision is spent by the act it authorizes;
* an invalid packet is an ordinary failed tool result, not a transition and not
  the end of the turn;
* the tool is exposed only on the ordinary production SINGLE surface;
* a non-implementation route never enters this path;
* the malformed-focused-response repair still works when focused mode was
  entered through a committed decision rather than through a stall.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aura.client.events import Done
from aura.conversation.api_view import RECEIPT_MARKER, build_api_view
from aura.conversation.focused_action import (
    COMMIT_IMPLEMENTATION_DECISION,
    CONTINUE_IMPLEMENTATION_DISCOVERY,
    FocusedActionState,
    should_enter_focused_action,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.catalog import MUTATION_TOOL_NAMES, ToolCatalog
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import model_streams

HOOK = "implementation_decision_test_stream"

IMPLEMENTATION_ROUTE = TaskRoute(
    lane=TaskLane.implementation,
    action="implementation",
    confidence=0.85,
    reason="matched implementation request",
)
CHAT_ROUTE = TaskRoute(
    lane=TaskLane.chat, action="chat", confidence=0.6, reason="no trigger"
)

DECISION_ARGS = {
    "objective": "Make enemies respect faction state when picking a target.",
    "owners": [
        "EnemyCombat is the shared health authority",
        "target_path is the existing target seam",
    ],
    "target_files": ["alpha.py"],
    "change": "Route target selection through the faction owner before combat.",
    "validation": "Re-read alpha.py and check the new seam is wired.",
}


# ── scripted provider ───────────────────────────────────────────────────────


@dataclass
class RecordedRequest:
    thinking: str
    tool_names: tuple[str, ...]
    require_tool_call: bool
    messages: list[dict[str, Any]]


@dataclass
class ScriptedStream:
    rounds: list[dict[str, Any]]
    requests: list[RecordedRequest] = field(default_factory=list)

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
        yield Done(finish_reason="stop", full_message=self.rounds[index])


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

    def called_tools(self) -> list[str]:
        """Tool names actually called, in order, from canonical history."""
        names: list[str] = []
        for msg in self.manager.history.messages:
            for call in msg.get("tool_calls") or []:
                fn = call.get("function") or {}
                names.append(str(fn.get("name", "")))
        return names


def make_harness(
    tmp_path: Path, rounds: list[dict[str, Any]], *, read_only: bool = False
) -> Harness:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Make enemies respect faction state when targeting")
    (tmp_path / "alpha.py").write_text("alpha = 1\n" + "# body\n" * 400, encoding="utf-8")
    (tmp_path / "beta.py").write_text("beta = 1\n" + "# body\n" * 400, encoding="utf-8")
    (tmp_path / "gamma.py").write_text("gamma = 1\n" + "# body\n" * 400, encoding="utf-8")

    tools = ToolRegistry(workspace_root=tmp_path, mode="single", read_only=read_only)
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


def continue_call(call_id: str, question: str) -> dict[str, Any]:
    return assistant(
        tool_call(
            call_id,
            CONTINUE_IMPLEMENTATION_DISCOVERY,
            {
                "unresolved_question": question,
                "needed_evidence": "the definition of the owning symbol",
            },
        )
    )


def request_kind(request: RecordedRequest) -> str:
    """Classify a recorded request by the catalog it exposed."""
    names = set(request.tool_names)
    if not names:
        return "completion"
    if COMMIT_IMPLEMENTATION_DECISION in names and "write_file" not in names:
        return "checkpoint"
    if request.require_tool_call:
        return "focused"
    return "ordinary"


def kinds(harness: Harness) -> list[str]:
    return [request_kind(r) for r in harness.stream.requests]


def advancing_discovery() -> list[dict[str, Any]]:
    """Three observation rounds that each return genuinely new evidence.

    Deliberately *not* a stall and not a cycle: every round opens ground the
    turn did not have, so ``PreEditLoopGuard`` never concludes discovery is
    over. This is the exact sequence that used to run forever — and the reason
    it no longer can is the checkpoint between each pair of rounds, which is
    where the model has to say what it is still trying to find out.
    """
    return [
        assistant(tool_call("d0", "glob", {"pattern": "**/alpha*.py"})),
        continue_call("k0", "which module defines the beta seam?"),
        assistant(tool_call("d1", "glob", {"pattern": "**/beta*.py"})),
        continue_call("k1", "what does alpha.py currently do at that seam?"),
        assistant(tool_call("d2", "read_file", {"path": "alpha.py"})),
    ]


COMMIT_ROUND = assistant(
    tool_call("c1", COMMIT_IMPLEMENTATION_DECISION, DECISION_ARGS)
)
WRITE_ROUND = assistant(
    tool_call("w1", "write_file", {"path": "alpha.py", "content": "alpha = 2\n"})
)
VALIDATE_ROUND = assistant(tool_call("v1", "read_file", {"path": "alpha.py"}))
FINAL_ROUND = assistant(content="Rewired alpha.py through the faction owner.")


# ── 1-8: the real regression ────────────────────────────────────────────────


@pytest.fixture
def committed(tmp_path):
    harness = make_harness(
        tmp_path,
        [*advancing_discovery(), COMMIT_ROUND, WRITE_ROUND, VALIDATE_ROUND, FINAL_ROUND],
    )
    harness.run()
    return harness


def test_the_request_after_a_committed_decision_is_focused(committed):
    assert kinds(committed) == [
        "ordinary",    # observation round
        "checkpoint",  # ← the harness asks, unprompted
        "ordinary",    # the round the named question bought
        "checkpoint",
        "ordinary",
        "checkpoint",  # the commit happens here, at the checkpoint
        "focused",     # ← the very next request is the mutation surface
        "ordinary",    # post-write validation
        "ordinary",    # final
    ]


def test_the_checkpoint_exposes_only_the_decision_controls(committed):
    exposed = set(committed.stream.requests[1].tool_names)
    assert exposed == {
        COMMIT_IMPLEMENTATION_DECISION,
        CONTINUE_IMPLEMENTATION_DISCOVERY,
    }, "no read, search, terminal, editing, or blocker tool at a clean checkpoint"


def test_the_focused_request_exposes_only_the_action_surface(committed):
    action = next(
        r for r in committed.stream.requests if request_kind(r) == "focused"
    )
    assert set(action.tool_names) == set(MUTATION_TOOL_NAMES) | {
        "report_blocker",
        "report_already_satisfied",
    }
    assert action.thinking == "off"


def test_no_discovery_happens_between_the_decision_and_the_write(committed):
    called = committed.called_tools()
    assert called == [
        "glob",
        CONTINUE_IMPLEMENTATION_DISCOVERY,
        "glob",
        CONTINUE_IMPLEMENTATION_DISCOVERY,
        "read_file",
        COMMIT_IMPLEMENTATION_DECISION,
        "write_file",
        "read_file",
    ]
    commit_at = called.index(COMMIT_IMPLEMENTATION_DECISION)
    assert called[commit_at + 1] == "write_file", (
        "nothing may run between the committed decision and the applied write"
    )


def test_the_write_applies_and_focused_validation_follows(committed, tmp_path):
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
    assert committed.approvals == ["alpha.py"]
    # Exactly one focused mutation request in the whole turn.
    assert kinds(committed).count("focused") == 1
    # Validation ran after the mutation, not before it.
    assert committed.called_tools()[-1] == "read_file"


def test_discovery_cannot_chain_without_naming_a_question(tmp_path):
    """The structural fix: the checkpoint arrives whether the model wants it or not.

    Identical advancing rounds, no commit. Before this protocol the guard alone
    never concluded anything and the turn surveyed forever; now the request after
    every observation round is the checkpoint, so the only way to keep looking is
    to name what is still unknown.
    """
    harness = make_harness(
        tmp_path,
        [
            assistant(tool_call("d0", "glob", {"pattern": "**/alpha*.py"})),
            continue_call("k0", "which module owns beta?"),
            assistant(tool_call("d3", "read_file", {"path": "beta.py"})),
            continue_call("k1", "which module owns gamma?"),
            assistant(tool_call("d4", "read_file", {"path": "gamma.py"})),
            COMMIT_ROUND,
            WRITE_ROUND,
            FINAL_ROUND,
        ],
    )
    harness.run()
    assert kinds(harness) == [
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",
        "focused",
        "ordinary",
    ]
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"


# ── 9: the decision capsule survives compaction byte-identically ────────────


def test_the_decision_capsule_survives_forced_compaction(committed):
    messages = committed.manager.history.messages
    commit_result = next(
        m
        for m in messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
    )
    original = commit_result["content"]
    assert json.loads(original)["implementation_decision_committed"] is True

    # A budget far below what the turn's evidence needs: compaction is forced
    # all the way down to dropping whole completed blocks of the current turn,
    # which is exactly the pressure that used to take the decision with it.
    view = build_api_view("system prompt", messages, budget_tokens=400)
    assert view.stats.dropped_blocks > 0, (
        "non-vacuous: the budget must really have forced blocks out"
    )
    capsules = [
        m
        for m in view.messages
        if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
    ]
    assert len(capsules) == 1, "the decision capsule must appear exactly once"
    assert capsules[0]["content"] == original, "byte-identical, never truncated"
    assert "aura compacted" not in capsules[0]["content"]

    # Never folded into a retired-evidence receipt either.
    assert all(
        "implementation_decision_committed" not in (m.get("content") or "")
        for m in view.messages
        if RECEIPT_MARKER in (m.get("content") or "")
    )

    # Stable across rounds, for the provider prefix cache.
    again = build_api_view("system prompt", messages, budget_tokens=400)
    assert (
        next(
            m["content"]
            for m in again.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        )
        == original
    )


def test_source_evidence_is_free_to_retire_under_the_same_pressure(committed):
    """The point of pinning: the reads may go, the decision may not."""
    messages = committed.manager.history.messages
    full = build_api_view("system prompt", messages, budget_tokens=200_000)
    squeezed = build_api_view("system prompt", messages, budget_tokens=400)
    assert squeezed.stats.tokens_after < full.stats.tokens_after
    surviving = {
        m.get("tool_call_id")
        for m in squeezed.messages
        if m.get("role") == "tool"
    }
    assert "c1" in surviving, "the decision capsule stays"
    assert "d2" not in surviving, "the source read that supported it may go"


# ── state lifecycle ─────────────────────────────────────────────────────────


def test_the_decision_is_spent_by_the_act_it_authorizes(tmp_path):
    """A committed decision authorizes one act, not every later act.

    After the focused write applies, the turn returns to the ordinary loop. The
    rounds that follow gather new evidence and never stall, so if the decision
    still authorized a transition there would be a second focused request.
    """
    harness = make_harness(
        tmp_path,
        [
            *advancing_discovery(),
            COMMIT_ROUND,
            WRITE_ROUND,
            assistant(tool_call("d5", "read_file", {"path": "beta.py"})),
            assistant(tool_call("d6", "read_file", {"path": "gamma.py"})),
            FINAL_ROUND,
        ],
    )
    harness.run()
    assert kinds(harness).count("focused") == 1
    assert "checkpoint" not in kinds(harness)[6:], (
        "the pre-write protocol is over once the write applied"
    )


def test_an_invalid_packet_is_an_ordinary_failed_tool_result(tmp_path):
    """Invalid arguments do not transition and do not end the turn."""
    harness = make_harness(
        tmp_path,
        [
            *advancing_discovery(),
            assistant(
                tool_call(
                    "c9",
                    COMMIT_IMPLEMENTATION_DECISION,
                    {
                        "objective": "  ",
                        "owners": [],
                        "target_files": [],
                        "change": "",
                    },
                )
            ),
            assistant(tool_call("d7", "read_file", {"path": "beta.py"})),
            COMMIT_ROUND,
            WRITE_ROUND,
            FINAL_ROUND,
        ],
    )
    harness.run()

    payload = json.loads(
        next(
            m
            for m in harness.manager.history.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c9"
        )["content"]
    )
    assert payload["ok"] is False
    assert payload["failure_class"] == "invalid_arguments"
    assert kinds(harness) == [
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",  # the invalid packet: a failed tool result, no transition
        "ordinary",    # the failure buys an ordinary recovery round
        "checkpoint",  # and the checkpoint returns
        "focused",     # only the *valid* packet transitions
        "ordinary",
    ]
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"


# ── exposure: only the ordinary production SINGLE surface ───────────────────


def _names(defs) -> set[str]:
    return {str(d.get("function", {}).get("name", "")) for d in defs}


def test_exposed_only_on_the_production_single_surface():
    catalog = ToolCatalog()
    assert COMMIT_IMPLEMENTATION_DECISION in _names(
        catalog.build_tool_defs(mode="single", read_only=False)
    )
    assert COMMIT_IMPLEMENTATION_DECISION not in _names(
        catalog.build_tool_defs(mode="single", read_only=True)
    )
    assert COMMIT_IMPLEMENTATION_DECISION not in _names(
        catalog.build_tool_defs(mode="planner", read_only=False)
    )
    assert COMMIT_IMPLEMENTATION_DECISION not in _names(
        catalog.build_focused_action_tool_defs()
    )


def test_it_is_classified_as_bookkeeping_and_mutates_nothing(tmp_path):
    from aura.conversation.tools.effects import ToolEffect

    assert (
        ToolCatalog().effect_for(COMMIT_IMPLEMENTATION_DECISION)
        is ToolEffect.BOOKKEEPING
    )
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    before = sorted(p.name for p in tmp_path.iterdir())

    def refuse(_request):  # pragma: no cover - must never be called
        raise AssertionError("bookkeeping must not ask for approval")

    result = tools.execute(COMMIT_IMPLEMENTATION_DECISION, DECISION_ARGS, refuse)
    assert result.ok is True
    assert result.payload["mutation"] is False
    assert result.payload["applied"] is False
    assert result.payload["decision"]["target_files"] == ["alpha.py"]
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_a_non_implementation_turn_never_enters_this_path():
    guard = PreEditLoopGuard()
    state = FocusedActionState()
    state.commit_decision("abc123")
    assert (
        should_enter_focused_action(
            mode="single",
            route=CHAT_ROUTE,
            guard=guard,
            task_completion_context=False,
            state=state,
        )
        is False
    )
    assert (
        should_enter_focused_action(
            mode="single",
            route=IMPLEMENTATION_ROUTE,
            guard=guard,
            task_completion_context=False,
            state=state,
        )
        is True
    ), "the same committed decision on an implementation route does transition"


# ── 13: malformed-response repair still holds on this route ─────────────────


def test_malformed_focused_response_is_repaired_after_a_committed_decision(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            *advancing_discovery(),
            COMMIT_ROUND,
            # Focused request 1: prose only — nothing usable in it.
            assistant(content="I will now edit alpha.py."),
            # Focused request 2 (reissued with one correction): the act.
            WRITE_ROUND,
            FINAL_ROUND,
        ],
    )
    harness.run()

    assert kinds(harness) == [
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",
        "ordinary",
        "checkpoint",
        "focused",
        "focused",
        "ordinary",
    ], "the identical focused request was reissued, not a discovery round"
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
    assert harness.manager.last_turn_provider_contract_failure is False
    correction = harness.stream.requests[7].messages[-1]
    assert correction["role"] == "user"
    assert "Nothing in your previous response was executed" in correction["content"]
    assert all(
        m.get("content") != correction["content"]
        for m in harness.manager.history.messages
    ), "the correction is request-local and never enters canonical history"
