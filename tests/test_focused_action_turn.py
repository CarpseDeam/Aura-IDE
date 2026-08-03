"""The focused action turn: one request that serializes a decision into one act.

Once ``PreEditLoopGuard`` has concluded that a production ``implementation``
turn has stopped producing new evidence and nothing has been written, the send
loop stops opening ordinary reasoning streams and issues exactly one
action-serialization request: thinking off, mutation tools plus
``report_blocker``, and a provider-neutral requirement that the answer be a
tool call.

These tests drive the real ``ConversationManager``, the real ``ToolRegistry``,
and the real ``PreEditLoopGuard`` — the focused flag is reached by actually
stalling the loop, not by poking state. What is asserted:

* which request is focused, and that there is exactly one of them;
* the thinking mode on every request, before, during, and after;
* the exact tool surface the focused request exposes;
* each provider's own required-tool mapping;
* that a write, a rejected write, a blocker, and a contract-violating provider
  each leave focused action by their own path, none of them through a retry
  allowance or an attempt count;
* that zero, multiple, unknown, and mixed tool-call responses execute nothing
  and are discarded whole — prose, reasoning, calls, and the provider's own
  ``Done`` — while the *decision* survives: the same focused request goes out
  again with one request-local correction, over byte-identical evidence;
* that only a repeat of the identical structural violation, after that explicit
  correction, ends the turn as a provider-contract failure;
* that a read-only registry never enters focused mutation mode;
* that a successful blocker finalizes against a request with no tool catalog;
* that the blocked reason reaches the completion-receipt path.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aura.client.events import ApiError, ContentDelta, Done, ToolResult
from aura.conversation.checkpoint_protocol import (
    ACTION_EXECUTE,
    ACTION_REISSUE,
    KIND_CONFLICTING_CONTROL,
    KIND_INVALID_CALL_IN_BATCH,
    KIND_NO_USABLE_CALL,
    checkpoint_correction,
    normalize_checkpoint_response,
)
from aura.conversation.focused_action import (
    ACTION_FAILED_MESSAGE,
    COMMIT_IMPLEMENTATION_DECISION,
    CONTINUE_IMPLEMENTATION_DISCOVERY,
    FOCUSED_CONTROL_TOOLS,
    FocusedActionState,
    should_enter_focused_action,
    tool_call_names,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
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


def assistant(*calls: dict[str, Any], content: str = "", reasoning: str = "") -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = reasoning
    if calls:
        message["tool_calls"] = list(calls)
    return message


def discovery_round() -> list[dict[str, Any]]:
    """The rounds that hand the send loop the focused action protocol.

    The first glob surfaces the harness workspace's only file as new evidence;
    the checkpoint between them names an unresolved question and buys one more
    observation round; the second glob returns the same file list, so that round
    produced no new evidence and the guard concludes discovery is over.
    """
    return [
        assistant(tool_call("d0", "glob", {"pattern": "**/alpha*.py"})),
        assistant(
            tool_call(
                "k0",
                CONTINUE_IMPLEMENTATION_DISCOVERY,
                {
                    "unresolved_question": "does anything else define alpha?",
                    "needed_evidence": "the full python file list",
                },
            )
        ),
        assistant(tool_call("d1", "glob", {"pattern": "**/*.py"})),
    ]


#: Index of the first focused action request in a script built on
#: ``discovery_round()``: three discovery/checkpoint rounds precede it.
FOCUSED_AT = 3


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


def kinds(harness: "Harness") -> list[str]:
    return [request_kind(r) for r in harness.stream.requests]


#: The request shapes ``discovery_round()`` produces.
DISCOVERY_KINDS = ["ordinary", "checkpoint", "ordinary"]


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


def make_harness(tmp_path: Path, rounds: list[dict[str, Any]], *, read_only: bool = False) -> Harness:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Fix the loader in alpha.py")
    (tmp_path / "alpha.py").write_text("alpha = 1\n", encoding="utf-8")

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


WRITE_ROUND = assistant(
    tool_call("w1", "write_file", {"path": "alpha.py", "content": "alpha = 2\n"})
)
FINAL_ROUND = assistant(content="Changed alpha.py: alpha is now 2.")


# ── 1-5, 11: activation, thinking modes, tool surface ───────────────────────


@pytest.fixture
def focused_write(tmp_path):
    harness = make_harness(
        tmp_path, [*discovery_round(), WRITE_ROUND, FINAL_ROUND]
    )
    harness.run()
    return harness


def test_focused_implementation_enters_exactly_one_action_request(focused_write):
    assert kinds(focused_write) == [*DISCOVERY_KINDS, "focused", "ordinary"]


def test_discovery_uses_the_user_selected_thinking_mode(focused_write):
    assert focused_write.stream.requests[0].thinking == "high"


def test_focused_action_request_uses_thinking_off(focused_write):
    assert focused_write.stream.requests[FOCUSED_AT].thinking == "off"


def test_selected_thinking_mode_is_restored_after_the_action(focused_write):
    assert focused_write.stream.requests[FOCUSED_AT + 1].thinking == "high"


def test_only_mutation_tools_and_the_control_exits_are_exposed(focused_write):
    exposed = set(focused_write.stream.requests[FOCUSED_AT].tool_names)
    assert exposed == set(MUTATION_TOOL_NAMES) | {
        "report_blocker",
        "report_already_satisfied",
    }
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
    for index in (0, 2, 4):
        assert "report_blocker" not in focused_write.stream.requests[index].tool_names


def test_ordinary_requests_keep_the_full_production_catalog(focused_write):
    ordinary = set(focused_write.stream.requests[0].tool_names)
    assert {"read_file", "glob", "write_file", "run_terminal_command"} <= ordinary


def test_a_successful_write_exits_focused_action_state(focused_write, tmp_path):
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
    # Only one focused request, and the round after it is an ordinary one.
    assert kinds(focused_write).count("focused") == 1
    assert request_kind(focused_write.stream.requests[FOCUSED_AT + 1]) == "ordinary"


# ── 10: no local cap on action arguments ────────────────────────────────────


def test_large_write_arguments_are_not_truncated(tmp_path):
    body = "".join(f"LINE_{i:06d} = {i}\n" for i in range(20_000))
    assert len(body) > 300_000
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(
                tool_call("w1", "write_file", {"path": "big.py", "content": body})
            ),
            FINAL_ROUND,
        ],
    )
    harness.run()
    assert (tmp_path / "big.py").read_text(encoding="utf-8") == body


# ── 12: a rejected write returns to the loop; only evidence ends the turn ───


def _send_rejecting(harness) -> None:
    harness.manager.send(
        on_event=harness.events.append,
        approval_cb=harness.reject,
        cancel_event=harness.cancel,
        model="test-model",
        thinking="high",
        hook_name=HOOK,
        task_route=IMPLEMENTATION_ROUTE,
    )


def test_a_rejected_write_returns_to_the_ordinary_loop(tmp_path):
    """A rejection is a result to read, not the end of the implementation.

    The rejected write hands its result back to the ordinary loop. Here the
    round that follows repeats a glob whose evidence the turn already has, so
    it neither advances the turn nor changes the diagnosis — and *that*, not the
    rejection, is what ends the turn truthfully.
    """
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            WRITE_ROUND,
            # Back in the ordinary loop, repeating evidence already seen.
            assistant(tool_call("d2", "glob", {"pattern": "**/*.py"})),
            assistant(tool_call("d3", "glob", {"pattern": "**/alpha*.py"})),
        ],
    )
    _send_rejecting(harness)

    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert kinds(harness) == [*DISCOVERY_KINDS, "focused", "ordinary"], (
        "the act returned to an ordinary round, which learned nothing"
    )
    assert harness.approvals == ["alpha.py"]
    assert ACTION_FAILED_MESSAGE in "".join(
        e.text for e in harness.events if isinstance(e, ContentDelta)
    )


def test_a_repeated_identical_rejection_ends_truthfully(tmp_path):
    """New evidence hands the act back; a failure already seen does not."""
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            WRITE_ROUND,
            # The ordinary round gathers genuinely new evidence, so the act is
            # handed back for a corrected decision...
            assistant(tool_call("d2", "glob", {"pattern": "**/*.md"})),
            # ...but the decision is not corrected, and the identical rejection
            # teaches the turn nothing it had not already seen.
            WRITE_ROUND,
            assistant(tool_call("d4", "glob", {"pattern": "**/*.txt"})),
        ],
    )
    _send_rejecting(harness)

    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert kinds(harness) == [
        *DISCOVERY_KINDS, "focused", "ordinary", "focused",
    ], "new evidence re-armed the act; the repeat of a known failure ended it"
    assert harness.approvals == ["alpha.py", "alpha.py"], (
        "non-vacuous: the write tool really ran twice and was really rejected"
    )
    last_tool_result = json.loads(
        [m for m in harness.manager.history.messages if m.get("role") == "tool"][-1][
            "content"
        ]
    )
    assert last_tool_result["ok"] is False
    assert last_tool_result.get("applied") is not True
    assert ACTION_FAILED_MESSAGE in "".join(
        e.text for e in harness.events if isinstance(e, ContentDelta)
    )


# ── 13: report_blocker mutates nothing and yields one factual final ─────────


def test_report_blocker_performs_no_mutation_and_ends_with_one_final(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
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
    # Evidence, checkpoint, stall, the action, and one factual final.
    assert kinds(harness) == [*DISCOVERY_KINDS, "focused", "completion"]
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


def test_blocker_final_uses_no_tool_catalog(tmp_path):
    """A successful blocker ends the attempt; the factual final is produced by
    a request that exposes no tool catalog, so it cannot mutate or re-open
    planning."""
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(
                tool_call("b1", "report_blocker", {"blocker": "generated at build time"})
            ),
            assistant(content="I could not edit alpha.py: it is generated."),
        ],
    )
    harness.run()

    assert harness.stream.requests[-1].tool_names == ()


# ── 13b: report_already_satisfied records explicit evidence, no mutation ────


def test_already_satisfied_ends_the_turn_without_a_write(tmp_path):
    """The focused action request exposes the already-satisfied exit; a
    structured report ends the attempt with no mutation and exactly one
    factual final."""
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(
                tool_call(
                    "s1",
                    "report_already_satisfied",
                    {"evidence": "alpha.py already sets alpha = 2"},
                )
            ),
            assistant(content="alpha.py already sets alpha = 2."),
        ],
    )
    before = sorted(p.name for p in tmp_path.iterdir())
    harness.run()

    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert harness.approvals == []
    # Evidence, checkpoint, stall, the action, and one factual final.
    assert kinds(harness) == [*DISCOVERY_KINDS, "focused", "completion"]
    # The focused request offered the already-satisfied control tool.
    assert "report_already_satisfied" in (
        harness.stream.requests[FOCUSED_AT].tool_names
    )

    assert harness.manager.last_turn_already_satisfied is True
    assert harness.manager.last_turn_bears_production_action is True

    results = [
        json.loads(m["content"])
        for m in harness.manager.history.messages
        if m.get("role") == "tool" and "already_satisfied_reported" in str(m.get("content"))
    ]
    assert len(results) == 1
    payload = results[0]
    assert payload["ok"] is True
    assert payload["mutation"] is False
    assert payload["applied"] is False
    assert payload["evidence"] == "alpha.py already sets alpha = 2"


def test_already_satisfied_final_uses_no_tool_catalog(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(
                tool_call("s1", "report_already_satisfied", {"evidence": "already set"})
            ),
            assistant(content="Already present; nothing to change."),
        ],
    )
    harness.run()
    assert harness.stream.requests[-1].tool_names == ()


def test_schema_invalid_already_satisfied_report_does_not_complete(tmp_path):
    """A report_already_satisfied call missing its required evidence argument is
    rejected by preflight like any invalid act: it does not complete the turn
    as already-satisfied."""
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(tool_call("s1", "report_already_satisfied", {})),
            assistant(content="The report was rejected; I will re-check."),
        ],
    )
    harness.run()
    assert harness.manager.last_turn_already_satisfied is False
    assert kinds(harness) == [*DISCOVERY_KINDS, "focused", "ordinary"]


def test_blocked_reason_reaches_the_completion_receipt_path(tmp_path):
    harness = make_harness(
        tmp_path,
        [
            *discovery_round(),
            assistant(
                tool_call("b1", "report_blocker", {"blocker": "generated at build time"})
            ),
            assistant(content="I could not edit alpha.py: it is generated."),
        ],
    )
    harness.run()

    assert harness.manager.last_turn_blocked_reason == "generated at build time"
    assert harness.manager.last_turn_provider_contract_failure is False


# ── 14: the focused provider contract — exactly one exposed action call ─────


VIOLATING_PROSE = "Here is what I would do: first I would open..."


def prose_only_round() -> dict[str, Any]:
    return assistant(
        content=VIOLATING_PROSE, reasoning="weighing the options\n"
    )


def zero_call_round() -> dict[str, Any]:
    message = assistant(content=VIOLATING_PROSE)
    message["tool_calls"] = []
    return message


def batched_write_round() -> dict[str, Any]:
    """Two valid, ordinary mutation calls: a batch, not a violation."""
    return assistant(
        tool_call("x1", "write_file", {"path": "alpha.py", "content": "a = 2\n"}),
        tool_call("x2", "write_file", {"path": "beta.py", "content": "b = 3\n"}),
    )


def conflicting_control_round() -> dict[str, Any]:
    """Two control calls that cannot both be true."""
    return assistant(
        tool_call("x1", "report_blocker", {"blocker": "generated"}),
        tool_call("x2", "report_already_satisfied", {"evidence": "already set"}),
    )


def unknown_call_round() -> dict[str, Any]:
    return assistant(tool_call("x1", "mystery_tool", {"path": "alpha.py"}))


def mixed_call_round() -> dict[str, Any]:
    return assistant(
        tool_call("x1", "write_file", {"path": "alpha.py", "content": "a = 2\n"}),
        tool_call("x2", "mystery_tool", {"path": "alpha.py"}),
    )


def no_message_round() -> None:
    """Sentinel for a normally completed stream carrying no ``full_message``."""
    return None


#: Every response shape the focused request cannot use, as a factory so each run
#: gets a fresh message (the content gate normalises tool-call messages in
#: place). Several valid mutation calls are deliberately *not* here: those are an
#: ordinary batch and execute — see ``TestSeveralValidCallsAreABatch``.
VIOLATION_FACTORIES = {
    "prose_only": prose_only_round,
    "zero_calls": zero_call_round,
    "conflicting_control": conflicting_control_round,
    "unknown_tool": unknown_call_round,
    "mixed_batch": mixed_call_round,
    "no_full_message": no_message_round,
}


#: Response shapes whose calls can each receive a paired rejection result. Those
#: are appended so the model can read exactly why nothing ran; the shapes that
#: carry no pairable call are discarded whole, because a synthetic result with
#: no call to pair to would corrupt the transcript.
PAIRABLE_SHAPES = {"conflicting_control", "unknown_tool", "mixed_batch"}


class TestFocusedContractViolationsAreRepaired:
    """An unusable focused response executes nothing, and the decision survives.

    The send loop reissues the *same* focused request with one request-local
    correction instead of dead-stopping a turn whose repository evidence is
    entirely intact. Nothing here is a retry allowance, and nothing here is
    terminal: see :class:`TestRepeatedShapeFallsBackToTheOrdinaryRequest`.
    """

    def _run(self, tmp_path: Path, round_: dict[str, Any] | None) -> Harness:
        """Violation, then a corrected single write, then the final answer."""
        harness = make_harness(
            tmp_path, [*discovery_round(), round_, WRITE_ROUND, FINAL_ROUND]
        )
        harness.run()
        return harness

    def _assert_recovered_and_wrote(
        self, harness: Harness, *, pairable: bool = False
    ) -> None:
        # 1-7: nothing executed from the violating response, and the corrected
        # focused request applied the write.
        assert (harness.root / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
        assert harness.approvals == ["alpha.py"], (
            "exactly one act ran: the corrected one"
        )

        requests = harness.stream.requests
        assert kinds(harness) == [
            *DISCOVERY_KINDS, "focused", "focused", "ordinary",
        ], "the violation was corrected inside focused mode, not by resending"

        # 3, 14: no terminal failure text, no failure flag, no receipt status.
        contents = [e.text for e in harness.events if isinstance(e, ContentDelta)]
        assert not any("Provider contract failure" in c for c in contents)
        assert harness.manager.last_turn_provider_contract_failure is False
        assert harness.manager.last_turn_blocked_reason == ""

        # 9, 10: the violating Done is never projected; the corrected one is,
        # exactly once. Four rounds reach the UI, not five.
        dones = [e for e in harness.events if isinstance(e, Done)]
        assert len(dones) == 5, (
            "the provider's Done for the violating focused round was forwarded"
        )
        projected_call_ids = [
            call["id"]
            for done in dones
            for call in (done.full_message.get("tool_calls") or [])
        ]
        assert projected_call_ids == ["d0", "k0", "d1", "w1"]

        # 8: no violating prose or reasoning reached history. The calls
        # themselves are kept only when each of them could be paired to a
        # truthful rejection result — never executed, and never left unpaired.
        assistants = [
            m for m in harness.manager.history.messages if m.get("role") == "assistant"
        ]
        stored_tool_call_ids = {
            call["id"]
            for m in assistants
            for call in (m.get("tool_calls") or [])
        }
        if pairable:
            assert stored_tool_call_ids == {"d0", "k0", "d1", "w1", "x1", "x2"} or (
                stored_tool_call_ids == {"d0", "k0", "d1", "w1", "x1"}
            ), stored_tool_call_ids
            rejected = [
                json.loads(m["content"])
                for m in harness.manager.history.messages
                if m.get("role") == "tool"
                and m.get("tool_call_id", "").startswith("x")
            ]
            assert rejected, "the rejected calls received paired results"
            assert all(r["ok"] is False for r in rejected)
            assert all(r["applied"] is False for r in rejected)
        else:
            assert stored_tool_call_ids == {"d0", "k0", "d1", "w1"}
        assert not any(VIOLATING_PROSE in (m.get("content") or "") for m in assistants)
        assert not any(m.get("reasoning_content") for m in assistants)

        # 12: the corrective request is still the focused request.
        violating, corrected = requests[FOCUSED_AT], requests[FOCUSED_AT + 1]
        assert corrected.thinking == "off" == violating.thinking
        assert corrected.tool_names == violating.tool_names
        assert corrected.require_tool_call is True

        # 11: the evidence gathered before the violation is byte-identical. The
        # correction is the only thing added to the outbound copy, beyond the
        # rejection block when the calls could be paired.
        prefix = len(violating.messages)
        assert corrected.messages[:prefix] == violating.messages, (
            "the corrective request re-derived or dropped gathered evidence"
        )
        correction = corrected.messages[-1]
        assert correction["role"] == "user"
        assert "editing tools" in correction["content"]
        assert (
            "Nothing in your previous response was executed"
            in correction["content"]
        )

        # 13: the frozen system prompt did not move.
        systems = {
            r.messages[0]["content"]
            for r in requests
            if r.messages and r.messages[0].get("role") == "system"
        }
        assert len(systems) == 1

        # The correction is request-local: it is in no other request and in no
        # stored message.
        assert not any(
            "Nothing in your previous response was executed"
            in str(m.get("content"))
            for m in harness.manager.history.messages
        )
        assert not any(
            "Nothing in your previous response was executed"
            in str(m.get("content"))
            for r in requests
            if r is not corrected
            for m in r.messages
        )

    @pytest.mark.parametrize("shape", sorted(VIOLATION_FACTORIES))
    def test_a_violation_is_discarded_corrected_and_then_writes(
        self, tmp_path, shape,
    ) -> None:
        """Cases 1-7: every unusable shape recovers into the same edit."""
        harness = self._run(tmp_path, VIOLATION_FACTORIES[shape]())
        self._assert_recovered_and_wrote(
            harness, pairable=shape in PAIRABLE_SHAPES
        )

    @pytest.mark.parametrize(
        "malformed",
        [
            "not-a-dict",
            None,
            42,
            {"id": "x2"},                                   # no function
            {"id": "x2", "function": "write_file"},         # function not a dict
            {"id": "x2", "function": {}},                   # no name
            {"id": "x2", "function": {"name": ""}},         # empty name
            {"id": "x2", "function": {"name": None}},       # name not a string
        ],
        ids=lambda v: str(v)[:24],
    )
    def test_one_valid_call_plus_a_malformed_raw_entry_executes_nothing(
        self, tmp_path, malformed,
    ) -> None:
        """Case 5: the raw ``tool_calls`` collection is what is validated.

        Filtering the names first would silently drop the unreadable entry and
        present this response as a single clean call — executing a mutation
        while the provider in fact asked for two acts, one of them unreadable.
        Recovery changed terminality, not this.
        """
        message = assistant(
            tool_call("x1", "write_file", {"path": "alpha.py", "content": "a = 2\n"}),
        )
        message["tool_calls"].append(malformed)
        harness = self._run(tmp_path, message)
        self._assert_recovered_and_wrote(harness)

    @pytest.mark.parametrize(
        "tool_calls",
        ["write_file", {"function": {"name": "write_file"}}, 7],
        ids=["string", "dict", "int"],
    )
    def test_a_non_list_tool_calls_field_recovers_safely(
        self, tmp_path, tool_calls,
    ) -> None:
        """Case 6: a raw ``tool_calls`` field that is not a list."""
        message = assistant(content=VIOLATING_PROSE)
        message["tool_calls"] = tool_calls
        harness = self._run(tmp_path, message)
        self._assert_recovered_and_wrote(harness)

    def test_a_recovered_violation_may_end_through_a_blocker(self, tmp_path) -> None:
        """Case 15: correction does not force a write — a truthful structured
        blocker is still a legitimate ending."""
        harness = make_harness(
            tmp_path,
            [
                *discovery_round(),
                prose_only_round(),
                assistant(
                    tool_call(
                        "b1", "report_blocker", {"blocker": "generated at build time"}
                    )
                ),
                assistant(content="I could not edit alpha.py: it is generated."),
            ],
        )
        harness.run()

        assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
        assert harness.manager.last_turn_blocked_reason == "generated at build time"
        assert harness.manager.last_turn_provider_contract_failure is False
        assert harness.stream.requests[-1].tool_names == ()

    def test_a_recovered_violation_may_end_through_already_satisfied(
        self, tmp_path,
    ) -> None:
        """Case 16: structured already-satisfied evidence, after a correction."""
        harness = make_harness(
            tmp_path,
            [
                *discovery_round(),
                prose_only_round(),
                assistant(
                    tool_call(
                        "s1",
                        "report_already_satisfied",
                        {"evidence": "alpha.py already sets alpha = 1"},
                    )
                ),
                assistant(content="Already present; nothing to change."),
            ],
        )
        harness.run()

        assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
        assert harness.manager.last_turn_already_satisfied is True
        assert harness.manager.last_turn_provider_contract_failure is False

    def test_two_different_violation_shapes_are_distinct_evidence(
        self, tmp_path,
    ) -> None:
        """Case 17: a *different* violation is new evidence, not a second
        attempt — there is no numeric allowance being spent here."""
        harness = make_harness(
            tmp_path,
            [
                *discovery_round(),
                prose_only_round(),
                conflicting_control_round(),
                unknown_call_round(),
                WRITE_ROUND,
                FINAL_ROUND,
            ],
        )
        harness.run()

        assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
        assert kinds(harness) == [
            *DISCOVERY_KINDS, "focused", "focused", "focused", "focused", "ordinary",
        ]
        assert harness.manager.last_turn_provider_contract_failure is False
        # Each correction named its own shape.
        corrections = [
            r.messages[-1]["content"]
            for r in harness.stream.requests[FOCUSED_AT + 1:FOCUSED_AT + 4]
        ]
        assert "no usable tool call" in corrections[0]
        assert "contradict each other" in corrections[1]
        assert "does not expose" in corrections[2]

    def test_the_real_failure_shape_no_longer_requires_a_resend(
        self, tmp_path,
    ) -> None:
        """Case 22: the observed defect, end to end.

        Extensive discovery, one prose-only focused response, then — with no
        user action at all — a corrective focused request, the applied write,
        and the ordinary validation/final response.
        """
        harness = make_harness(
            tmp_path,
            [
                assistant(tool_call("d0", "glob", {"pattern": "**/alpha*.py"})),
                assistant(
                    tool_call(
                        "k0",
                        CONTINUE_IMPLEMENTATION_DISCOVERY,
                        {
                            "unresolved_question": "what does alpha.py set?",
                            "needed_evidence": "the body of alpha.py",
                        },
                    )
                ),
                assistant(tool_call("d1", "read_file", {"path": "alpha.py"})),
                assistant(
                    tool_call("c1", COMMIT_IMPLEMENTATION_DECISION, {
                        "objective": "Set alpha to 2.",
                        "owners": ["alpha.py owns the value"],
                        "target_files": ["alpha.py"],
                        "change": "alpha = 2",
                    })
                ),
                prose_only_round(),
                WRITE_ROUND,
                FINAL_ROUND,
            ],
        )
        harness.run()

        assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
        assert kinds(harness) == [
            "ordinary", "checkpoint", "ordinary", "checkpoint",
            "focused", "focused", "ordinary",
        ]
        # One send call did the whole thing: the user never resent the task.
        assert harness.manager.last_turn_provider_contract_failure is False
        assert not any(
            "Provider contract failure" in e.text
            for e in harness.events if isinstance(e, ContentDelta)
        )
        finals = [
            m for m in harness.manager.history.messages if m.get("role") == "assistant"
        ]
        assert finals[-1]["content"] == FINAL_ROUND["content"]


class TestRepeatedShapeFallsBackToTheOrdinaryRequest:
    """The identical unusable shape twice is not a failed coding task.

    It is evidence that *this request shape* is not working with this provider.
    So the send loop stops asking for it and asks the ordinary production
    request instead — the shape the provider has already answered many times in
    the same turn — with every byte of gathered evidence, the decision state,
    and the unresolved question intact. No status, no terminal message, and no
    discarded work.
    """

    def _run_repeated(self, tmp_path: Path, factory) -> Harness:
        harness = make_harness(
            tmp_path,
            [*discovery_round(), factory(), factory(), WRITE_ROUND, FINAL_ROUND],
        )
        harness.run()
        return harness

    @pytest.mark.parametrize("shape", sorted(VIOLATION_FACTORIES))
    def test_the_same_shape_twice_falls_back_and_still_edits(
        self, tmp_path, shape,
    ) -> None:
        harness = self._run_repeated(tmp_path, VIOLATION_FACTORIES[shape])

        # The corrective focused request went out once; the repeat of the same
        # shape handed the next request back to the ordinary catalog, where the
        # write landed.
        assert kinds(harness) == [
            *DISCOVERY_KINDS, "focused", "focused", "ordinary", "ordinary",
        ], kinds(harness)
        assert (harness.root / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
        assert harness.approvals == ["alpha.py"]

        # 12: never a provider-contract failure, in any of its forms.
        contents = [e.text for e in harness.events if isinstance(e, ContentDelta)]
        assert not any("Provider contract failure" in c for c in contents)
        assert harness.manager.last_turn_provider_contract_failure is False
        assert harness.manager.last_turn_blocked_reason == ""

        # The fallback request is the full production catalog, so the model can
        # answer in the shape it has been answering in all turn.
        fallback = harness.stream.requests[FOCUSED_AT + 2]
        assert {"read_file", "glob", "write_file"} <= set(fallback.tool_names)
        assert fallback.require_tool_call is False

        # 11: the evidence gathered before the first unusable response is still
        # there, byte-identical.
        violating = harness.stream.requests[FOCUSED_AT]
        evidence = [
            m for m in violating.messages
            if m.get("role") == "tool" and m.get("tool_call_id") in {"d0", "k0", "d1"}
        ]
        assert evidence, "non-vacuous: the turn really had gathered evidence"
        for message in evidence:
            assert message in fallback.messages, (
                "the fallback re-derived or dropped gathered evidence"
            )

        # No violating prose or reasoning reached history either way.
        assistants = [
            m for m in harness.manager.history.messages if m.get("role") == "assistant"
        ]
        assert not any(VIOLATING_PROSE in (m.get("content") or "") for m in assistants)
        assert not any(m.get("reasoning_content") for m in assistants)

    def test_the_fallback_still_reaches_a_truthful_blocker(self, tmp_path) -> None:
        """Falling back does not force an edit: the honest endings still work."""
        harness = make_harness(
            tmp_path,
            [
                *discovery_round(),
                prose_only_round(),
                prose_only_round(),
                assistant(
                    tool_call(
                        "b1", "report_blocker", {"blocker": "generated at build time"}
                    )
                ),
                assistant(content="I could not edit alpha.py: it is generated."),
            ],
        )
        harness.run()

        assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
        assert harness.manager.last_turn_provider_contract_failure is False



def test_cancellation_during_the_corrective_request_is_unchanged(tmp_path):
    """Case 19: cancellation keeps its existing semantics inside recovery."""
    harness = make_harness(
        tmp_path, [*discovery_round(), prose_only_round(), WRITE_ROUND]
    )
    harness.stream.cancel_on_request = FOCUSED_AT + 1  # the corrective request
    harness.run()

    errors = [e for e in harness.events if isinstance(e, ApiError)]
    assert errors and errors[-1].message == "Cancelled."
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    assert len(harness.stream.requests) == FOCUSED_AT + 2
    assert harness.manager.last_turn_provider_contract_failure is False


def test_an_ordinary_turn_issues_exactly_the_same_requests_as_before(tmp_path):
    """Case 20: no violation, no extra request, no extra context.

    The correction exists only in the outbound copy of the request that
    corrects something, so a clean turn's requests are byte-identical to what
    they always were.
    """
    harness = make_harness(tmp_path, [*discovery_round(), WRITE_ROUND, FINAL_ROUND])
    harness.run()

    assert kinds(harness) == [*DISCOVERY_KINDS, "focused", "ordinary"]
    for request in harness.stream.requests:
        assert not any(
            "Nothing in your previous response was executed" in str(m.get("content"))
            for m in request.messages
        )
    # The last message of every request is what the history produced — the
    # focused request added nothing of its own.
    assert harness.stream.requests[FOCUSED_AT].messages[-1]["role"] == "tool"


# ── the normalization itself ────────────────────────────────────────────────


EXPOSED = ("write_file", "patch_file", "report_blocker")


def normalize(message, exposed=None, control=None):
    return normalize_checkpoint_response(
        message,
        exposed_tools=exposed if exposed is not None else EXPOSED,
        control_tools=control if control is not None else ("report_blocker",),
    )


@pytest.mark.parametrize(
    "message, kind",
    [
        (None, KIND_NO_USABLE_CALL),
        ("not a message", KIND_NO_USABLE_CALL),
        ({}, KIND_NO_USABLE_CALL),
        ({"tool_calls": None}, KIND_NO_USABLE_CALL),
        ({"tool_calls": []}, KIND_NO_USABLE_CALL),
        ({"tool_calls": "write_file"}, KIND_NO_USABLE_CALL),
        ({"tool_calls": 7}, KIND_NO_USABLE_CALL),
        ({"tool_calls": [None]}, KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [{"id": "a"}]}, KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [{"id": "a", "function": ["write_file"]}]},
         KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [{"id": "a", "function": {}}]}, KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [{"id": "a", "function": {"name": ""}}]},
         KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [tool_call("a", "read_file", {})]},
         KIND_INVALID_CALL_IN_BATCH),
        ({"tool_calls": [tool_call("a", "Write_File", {})]},
         KIND_INVALID_CALL_IN_BATCH),
        (
            {"tool_calls": [
                tool_call("a", "report_blocker", {}),
                tool_call("b", "write_file", {}),
            ]},
            KIND_CONFLICTING_CONTROL,
        ),
    ],
)
def test_normalization_classifies_every_unusable_shape(message, kind) -> None:
    result = normalize(message)
    assert result.action == ACTION_REISSUE
    assert result.ok is False
    assert result.kind == kind
    assert result.fingerprint.startswith(kind)


@pytest.mark.parametrize(
    "message, expected_names",
    [
        ({"tool_calls": [tool_call("a", "write_file", {})]}, ["write_file"]),
        ({"tool_calls": [tool_call("a", "report_blocker", {})]}, ["report_blocker"]),
        (
            # Prose alongside one valid call: the prose is the caller's to drop,
            # the call runs.
            {
                "content": "I will now edit alpha.py.",
                "reasoning_content": "thinking...",
                "tool_calls": [tool_call("a", "write_file", {})],
            },
            ["write_file"],
        ),
        (
            # Several ordinary mutation calls: a normal batch, not a violation.
            {"tool_calls": [
                tool_call("a", "write_file", {}),
                tool_call("b", "patch_file", {}),
            ]},
            ["write_file", "patch_file"],
        ),
    ],
)
def test_normalization_executes_what_is_usable(message, expected_names) -> None:
    result = normalize(message)
    assert result.action == ACTION_EXECUTE
    assert result.ok is True
    assert result.kind == ""
    assert result.fingerprint == ""
    assert [c["function"]["name"] for c in result.calls] == expected_names
    assert result.rejections == ()


def test_every_call_in_a_rejected_batch_receives_a_paired_result() -> None:
    """Nothing executes, and nothing is left unpaired in the transcript."""
    result = normalize({"tool_calls": [
        tool_call("good", "write_file", {}),
        tool_call("bad", "mystery_tool", {}),
    ]})
    assert result.action == ACTION_REISSUE
    assert [r.tool_call_id for r in result.rejections] == ["good", "bad"]
    assert all(r.payload["ok"] is False for r in result.rejections)
    assert all(r.payload["applied"] is False for r in result.rejections)
    # The valid sibling is told the truth: it was valid, and it still did not run.
    assert result.rejections[0].payload["batch_rejected"] is True
    assert result.rejections[1].payload["call_rejected"] is True


def test_a_batch_that_cannot_be_paired_is_discarded_whole() -> None:
    """An entry with no usable id could not receive a result, so none is faked."""
    result = normalize({"tool_calls": [
        tool_call("good", "write_file", {}),
        {"function": {"name": "write_file"}},  # no id
    ]})
    assert result.action == ACTION_REISSUE
    assert result.rejections == ()


def test_the_fingerprint_is_structural_and_ignores_noisy_output() -> None:
    """Two responses differing only in prose, reasoning, and call ids are the
    same shape — otherwise every repeat would look novel and no correction would
    ever be recognised as one already tried."""
    first = normalize({
        "content": "let me think about this",
        "reasoning_content": "step one...",
        "tool_calls": [
            tool_call("call_aaa", "report_blocker", {}),
            tool_call("call_bbb", "write_file", {}),
        ],
    })
    second = normalize({
        "content": "actually, a different plan entirely",
        "reasoning_content": "step two...",
        "tool_calls": [
            tool_call("call_zzz", "report_blocker", {}),
            tool_call("call_yyy", "write_file", {}),
        ],
    })
    assert first.fingerprint == second.fingerprint

    # A different *shape* is a different fingerprint.
    assert normalize({"tool_calls": []}).fingerprint != first.fingerprint
    # So is the same kind naming a different tool.
    assert (
        normalize({"tool_calls": [tool_call("a", "read_file", {})]}).fingerprint
        != normalize({"tool_calls": [tool_call("a", "glob", {})]}).fingerprint
    )


def test_the_correction_names_the_reason_the_tools_and_the_shape() -> None:
    result = normalize({"tool_calls": []})
    text = checkpoint_correction(result, EXPOSED, allow_batch=False)
    assert "Nothing in your previous response was executed" in text
    assert result.reason in text
    for name in EXPOSED:
        assert name in text
    assert "exactly one tool call" in text
    assert "Prose cannot accompany the call" in text
    assert "unchanged and still applies" in text


def test_the_focused_correction_allows_a_batch() -> None:
    text = checkpoint_correction(
        normalize({"tool_calls": []}), EXPOSED, allow_batch=True
    )
    assert "several calls of the same editing tools" in text


def test_an_edit_attempted_at_the_checkpoint_is_pointed_at_the_commit() -> None:
    """Reaching for an editing tool at the checkpoint means the decision is
    made; the correction says so instead of repeating the rule."""
    text = checkpoint_correction(
        normalize(
            {"tool_calls": [tool_call("a", "write_file", {})]},
            exposed=(COMMIT_IMPLEMENTATION_DECISION,),
        ),
        (COMMIT_IMPLEMENTATION_DECISION, CONTINUE_IMPLEMENTATION_DISCOVERY),
        allow_batch=False,
        edit_attempted=True,
    )
    assert "you already know what to change" in text
    assert COMMIT_IMPLEMENTATION_DECISION in text


def test_protocol_recovery_state_clears_for_a_new_decision() -> None:
    state = FocusedActionState()
    state.pending_correction = "fix your formatting"
    state.violation_fingerprints.add("no_usable_call|0|")
    state.last_violation = "no_usable_call"
    state.clear_protocol_recovery()
    assert state.pending_correction == ""
    assert state.violation_fingerprints == set()
    assert state.last_violation == ""


def test_tool_call_names_never_raises_on_a_malformed_collection() -> None:
    """Reporting must survive what normalization rejects."""
    assert tool_call_names({"tool_calls": 7}) == []
    assert tool_call_names({"tool_calls": ["x", None, 3]}) == []
    assert tool_call_names({"tool_calls": [tool_call("a", "write_file", {}), 3]}) == [
        "write_file"
    ]


class TestFocusedDoneIsDeferredUntilValidated:
    """The provider's ``Done`` for a focused round is held until the response
    has been validated: forwarded once if the contract held, never projected if
    it did not."""

    def test_a_valid_focused_response_forwards_one_original_done(
        self, focused_write,
    ) -> None:
        dones = [e for e in focused_write.events if isinstance(e, Done)]
        write_dones = [
            d for d in dones
            if any(
                call.get("id") == "w1"
                for call in (d.full_message.get("tool_calls") or [])
            )
        ]
        assert len(write_dones) == 1, (
            "a valid focused response forwards the provider's own Done exactly "
            "once — deferring it must not drop or duplicate it"
        )
        # One Done per round, still: the discovery rounds and the checkpoint
        # between them, the focused write, and the final answer.
        assert len(dones) == 5
        assert (focused_write.root / "alpha.py").read_text(encoding="utf-8") == (
            "alpha = 2\n"
        )

    def test_the_deferred_done_precedes_the_tool_result(
        self, focused_write,
    ) -> None:
        """Order is unchanged from ordinary rounds: the round's terminal event
        first, then the single tool it authorized."""
        kinds = [
            ("done", e.full_message.get("tool_calls"))
            if isinstance(e, Done)
            else ("result", getattr(e, "name", ""))
            for e in focused_write.events
            if isinstance(e, (Done, ToolResult))
        ]
        done_index = next(
            i for i, (kind, payload) in enumerate(kinds)
            if kind == "done"
            and any(c.get("id") == "w1" for c in (payload or []))
        )
        result_index = next(
            i for i, (kind, payload) in enumerate(kinds)
            if kind == "result" and payload == "write_file"
        )
        assert done_index < result_index


# ── read-only mode exposes no focused mutation request ──────────────────────


def test_read_only_mode_exposes_no_focused_mutation_request(tmp_path):
    harness = make_harness(
        tmp_path,
        [*discovery_round(), FINAL_ROUND],
        read_only=True,
    )
    harness.run()

    # No narrowed request at all: a read-only registry owes no implementation,
    # so neither the checkpoint nor the focused mutation surface is entered.
    assert not any(r.require_tool_call for r in harness.stream.requests)
    assert set(kinds(harness)) == {"ordinary"}
    assert {r.thinking for r in harness.stream.requests} == {"high"}


# ── 15: every other kind of turn is untouched ───────────────────────────────


@pytest.mark.parametrize(
    "route",
    [
        # ``None`` is deliberately not here: a production send with no route now
        # resolves one from the real user message, and this harness's message
        # ("Fix the loader in alpha.py") is a coding request. The predicate is
        # still pure over a ``None`` route — see ``test_activation_predicate``.
        CHAT_ROUTE,
        TaskRoute(TaskLane.research, "web_research", 0.9, "research"),
        TaskRoute(TaskLane.validation, "validation", 0.9, "validation"),
        TaskRoute(TaskLane.built_in_action, "git_status", 1.0, "built-in"),
    ],
)
def test_non_implementation_turns_never_enter_focused_action(tmp_path, route):
    harness = make_harness(
        tmp_path,
        [*discovery_round(), WRITE_ROUND, FINAL_ROUND],
    )
    harness.run(route=route)
    assert not any(r.require_tool_call for r in harness.stream.requests)
    assert set(kinds(harness)) == {"ordinary"}
    assert {r.thinking for r in harness.stream.requests} == {"high"}
    assert "report_blocker" not in harness.stream.requests[2].tool_names


def test_a_direct_mutation_is_never_forced_through_discovery(tmp_path):
    """A turn that can already edit does so on its first request.

    The checkpoint sits between observation rounds; it is not a toll on turns
    that never needed to look. The very first production request carries the
    full catalog, mutation tools included.
    """
    harness = make_harness(tmp_path, [WRITE_ROUND, FINAL_ROUND])
    harness.run()
    assert kinds(harness) == ["ordinary", "ordinary"]
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 2\n"
    assert "write_file" in harness.stream.requests[0].tool_names


# ── 16: cancellation is unchanged ───────────────────────────────────────────


def test_user_cancellation_during_the_focused_request_is_unchanged(tmp_path):
    harness = make_harness(tmp_path, [*discovery_round(), WRITE_ROUND])
    harness.stream.cancel_on_request = FOCUSED_AT
    harness.run()

    errors = [e for e in harness.events if isinstance(e, ApiError)]
    assert errors and errors[-1].message == "Cancelled."
    assert (tmp_path / "alpha.py").read_text(encoding="utf-8") == "alpha = 1\n"
    # No further request was made and the cancel event was never set by Aura's
    # own machinery — the test set it, standing in for the user.
    assert len(harness.stream.requests) == FOCUSED_AT + 1


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
    ["write", "blocker", "protocol_fallback", "recovered_contract_violation"],
)
def test_history_tool_pairing_is_preserved(tmp_path, script_name):
    scripts = {
        "write": [*discovery_round(), WRITE_ROUND, FINAL_ROUND],
        "blocker": [
            *discovery_round(),
            assistant(tool_call("b1", "report_blocker", {"blocker": "generated"})),
            assistant(content="Blocked."),
        ],
        # The corrected request repeats the identical shape, which hands the
        # next request back to the ordinary catalog rather than ending the turn.
        "protocol_fallback": [
            *discovery_round(),
            assistant(content="prose only"),
            assistant(content="prose only"),
            WRITE_ROUND,
            FINAL_ROUND,
        ],
        "recovered_contract_violation": [
            *discovery_round(),
            assistant(content="prose only"),
            WRITE_ROUND,
            FINAL_ROUND,
        ],
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
