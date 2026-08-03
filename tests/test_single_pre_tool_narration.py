"""The SINGLE production runtime must not replay its own pre-tool narration.

These drive the *real* ``ConversationManager`` loop with a scripted production
backend and a real ``ToolRegistry`` on a temp workspace.  Feeding an ideal
transcript into ``ProductionExecutionSession`` proves nothing about circular
planning: the loop only closes when streamed prose reaches chat and is stored
back into history, and only the manager controls that.

Two contracts are under test:

1. **Buffering / history.**  A tool-calling round's prose is neither projected
   nor persisted; its ``tool_calls`` survive intact and execute.  A no-tool
   round streams and persists normally.
2. **Pre-edit loop guard.**  Exact repeated reads before the first applied
   write are rejected, and the first round that produces no new evidence and
   no progress is the focused action transition — the send loop answers the
   next request with the one-required-tool-call action request instead of
   another reasoning stream.  Unique evidence never stalls *the guard*,
   truncated reads and their continuations count as evidence, successful
   commands are progress (failed ones are not), and rereads justified by a
   failure, a stale-file notice, or pending edit-recovery state stay allowed.
3. **Implementation discovery stage.**  The guard's rule alone cannot end a
   turn whose every result is technically novel, so an implementation turn also
   gets exactly two ordinary discovery hops before the first applied write.
   Whatever the guard concludes, the third request is the focused action
   request.  See ``tests/test_implementation_discovery_stage.py`` for the full
   stage contract.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import (
    DUPLICATE_READ_REASON,
    PreEditLoopGuard,
)
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry

#: These are implementation turns, so a stalled discovery round hands the send
#: loop the focused action request rather than another reasoning stream.
IMPLEMENTATION_ROUTE = TaskRoute(
    lane=TaskLane.implementation,
    action="implementation",
    confidence=0.85,
    reason="scripted implementation turn",
)

# The verbose pre-tool essay the production model streams before every tool
# call — the text that used to be replayed into the next round's prompt.
PRE_TOOL_ESSAY = (
    "Let me think about this. Option A is to return a paused job from "
    "`_run_item`; Option B is to raise and catch upstream. Acceptance "
    "criteria: the job pauses, the receipt names the cap, existing callers "
    "keep working. I will now read the runner to confirm the seam, then apply "
    "the change. Here is the patch I intend to write:\n"
    "```python\nif attempts >= CAP:\n    return _paused(job)\n```"
)

FINAL_ANSWER = "Changed `notes.md`: added the project overview. Verified by reading it back."


# ── scripted production backend ─────────────────────────────────────────────


def tool_round(
    calls: list[tuple[str, str, dict]],
    *,
    text: str = "",
    reasoning: str = "",
) -> list[Event]:
    """One streamed round that ends in tool calls."""
    events: list[Event] = []
    if reasoning:
        events.append(ReasoningDelta(text=reasoning))
    if text:
        # Streamed in two chunks, exactly as a provider would.
        half = len(text) // 2
        events.append(ContentDelta(text=text[:half]))
        events.append(ContentDelta(text=text[half:]))
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
        full_message={
            "role": "assistant",
            "content": text,
            "reasoning_content": reasoning or None,
            "tool_calls": tool_calls,
        },
    ))
    return events


def final_round(text: str) -> list[Event]:
    """One streamed round that ends the turn with prose."""
    return [
        ContentDelta(text=text),
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": text},
        ),
    ]


class ScriptedBackend:
    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(final_round("(script exhausted)"))

    def messages_for_round(self, index: int) -> list[dict]:
        return self.calls[index]["messages"]


class Recorder:
    """Everything the manager projected, split by owner."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    @property
    def chat_text(self) -> str:
        return "".join(
            e.text for e in self.events if isinstance(e, ContentDelta)
        )

    @property
    def reasoning_text(self) -> str:
        return "".join(
            e.text for e in self.events if isinstance(e, ReasoningDelta)
        )

    def of_type(self, kind: type) -> list[Event]:
        return [e for e in self.events if isinstance(e, kind)]

    @property
    def tool_names_started(self) -> list[str]:
        return [e.name for e in self.of_type(ToolCallStart)]

    @property
    def tool_results(self) -> list[ToolResult]:
        return self.of_type(ToolResult)

    @property
    def done_messages(self) -> list[dict]:
        return [e.full_message for e in self.of_type(Done) if e.full_message]


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "notes.md").write_text("# Notes\n\nold body\n", encoding="utf-8")
    (root / "other.md").write_text("# Other\n", encoding="utf-8")
    return root


def approve_all(_request) -> ApprovalDecision:
    return ApprovalDecision(action="approve")


def build_manager(workspace: Path, user_text: str) -> ConversationManager:
    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text(user_text)
    registry = ToolRegistry(workspace_root=workspace, mode="single")
    return ConversationManager(history, registry)


def run(
    manager: ConversationManager,
    recorder: Recorder,
    *,
    max_tool_rounds: int = 12,
) -> None:
    manager.send(
        on_event=recorder,
        approval_cb=approve_all,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="off",
        hook_name=PRODUCTION_STREAM_HOOK,
        max_tool_rounds=max_tool_rounds,
        task_route=IMPLEMENTATION_ROUTE,
    )


def assistant_messages(manager: ConversationManager) -> list[dict]:
    return [
        m for m in manager.history.messages if m.get("role") == "assistant"
    ]


# ── 1: buffering and history contract ───────────────────────────────────────


class TestPreToolNarrationIsSuppressed:
    """A verbose tool-calling round must leave no prose behind."""

    def _script(self) -> list[list[Event]]:
        return [
            tool_round(
                [("w1", "write_file", {
                    "path": "notes.md",
                    "content": "# Notes\n\nProject overview.\n",
                })],
                text=PRE_TOOL_ESSAY,
                reasoning="Weighing the two seams.\n",
            ),
            final_round(FINAL_ANSWER),
        ]

    def test_pre_tool_prose_never_reaches_chat(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        assert "Option A" not in recorder.chat_text
        assert "```python" not in recorder.chat_text
        assert PRE_TOOL_ESSAY not in recorder.chat_text
        # The final answer is the only prose chat ever saw.
        assert recorder.chat_text == FINAL_ANSWER

    def test_pre_tool_prose_is_not_stored_in_history(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")

        run(manager, recorder := Recorder())
        assert recorder  # the recorder is exercised by the other proofs

        stored = json.dumps(manager.history.messages)
        assert "Option A" not in stored
        assert "Acceptance criteria" not in stored
        assert "if attempts >= CAP" not in stored

    def test_the_next_round_prompt_does_not_replay_the_essay(
        self, workspace, isolated_streams
    ) -> None:
        """The loop closes through the prompt — prove it is open."""
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")

        run(manager, Recorder())

        assert len(backend.calls) >= 2, "the tool round must have continued"
        replayed = json.dumps(backend.messages_for_round(1))
        assert "Option A" not in replayed
        assert "Acceptance criteria" not in replayed

    def test_tool_calls_survive_intact_and_execute(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        tool_call_message = next(
            m for m in assistant_messages(manager) if m.get("tool_calls")
        )
        calls = tool_call_message["tool_calls"]
        assert [c["id"] for c in calls] == ["w1"]
        assert calls[0]["function"]["name"] == "write_file"
        assert json.loads(calls[0]["function"]["arguments"])["path"] == "notes.md"

        # It really ran.
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nProject overview.\n"
        )
        assert [r.name for r in recorder.tool_results] == ["write_file"]
        assert recorder.tool_results[0].ok is True

    def test_stored_tool_call_message_is_provider_valid(
        self, workspace, isolated_streams
    ) -> None:
        """content normalised to "", tool_calls and reasoning_content kept."""
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")

        run(manager, Recorder())

        tool_call_message = next(
            m for m in assistant_messages(manager) if m.get("tool_calls")
        )
        assert tool_call_message["content"] == ""
        assert isinstance(tool_call_message["content"], str), (
            "content must be a string, not None — OpenAI-style backends reject None"
        )
        assert tool_call_message["reasoning_content"] == "Weighing the two seams.\n", (
            "DeepSeek thinking mode requires reasoning_content to be replayed"
        )

        # The API view keeps the same shape.
        api_message = next(
            m for m in manager.history.for_api()
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert api_message["content"] == ""
        assert api_message["reasoning_content"] == "Weighing the two seams.\n"
        assert api_message["tool_calls"][0]["id"] == "w1"

    def test_reasoning_tool_and_activity_projection_stay_intact(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        assert recorder.reasoning_text == "Weighing the two seams.\n"
        assert recorder.tool_names_started == ["write_file"]
        assert recorder.of_type(ToolCallArgsDelta), "tool args must still stream"
        assert recorder.of_type(ToolCallEnd), "tool end must still project"
        assert len(recorder.tool_results) == 1
        # Both rounds still emit Done so the workspace can close its cards.
        assert len(recorder.done_messages) == 2

    def test_projected_done_carries_no_pre_tool_prose(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        tool_done = recorder.done_messages[0]
        assert tool_done["tool_calls"], "the tool round's Done is first"
        assert tool_done["content"] == ""
        assert recorder.done_messages[1]["content"] == FINAL_ANSWER

    def test_final_no_tool_response_is_shown_and_persisted(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend(self._script())
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Add a project overview to notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        assert recorder.chat_text.count(FINAL_ANSWER) == 1
        final = assistant_messages(manager)[-1]
        assert final["content"] == FINAL_ANSWER
        assert not final.get("tool_calls")


class TestConversationalTurnIsUnaffected:
    def test_plain_answer_streams_and_persists_normally(
        self, workspace, isolated_streams
    ) -> None:
        answer = "It is a tidy Qt app with a clean bridge boundary."
        backend = ScriptedBackend([final_round(answer)])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "What do you think of this project?")
        recorder = Recorder()

        run(manager, recorder)

        assert recorder.chat_text == answer
        assert assistant_messages(manager)[-1]["content"] == answer

    def test_prose_before_an_api_error_is_still_shown(
        self, workspace, isolated_streams
    ) -> None:
        """A dead stream must not swallow what it already generated."""
        partial = "Reading the runner to find the cap..."

        class _FailingBackend:
            def stream(self, **_kwargs):
                return iter([
                    ContentDelta(text=partial),
                    ApiError(status_code=502, message="upstream unavailable"),
                ])

        isolated_streams.register(PRODUCTION_STREAM_HOOK, _FailingBackend().stream)
        manager = build_manager(workspace, "Fix the cap.")
        recorder = Recorder()

        run(manager, recorder)

        assert recorder.chat_text == partial
        assert recorder.of_type(ApiError)
        # Nothing incomplete was persisted.
        assert assistant_messages(manager) == []


# ── 2: pre-edit loop guard ──────────────────────────────────────────────────


class TestRepeatedReadsAreBlocked:
    def test_exact_repeat_read_before_the_first_write_is_rejected(
        self, workspace, isolated_streams
    ) -> None:
        read_call = ("r1", "read_file", {"path": "notes.md"})
        backend = ScriptedBackend([
            tool_round([read_call]),
            tool_round([("r2", "read_file", {"path": "notes.md"})]),
            final_round("Stopped re-reading."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        results = {r.tool_call_id: r for r in recorder.tool_results}
        assert results["r1"].ok is True, "the first read must run"
        assert results["r2"].ok is False, "the exact repeat must be rejected"
        payload = json.loads(results["r2"].result)
        assert payload["reason"] == DUPLICATE_READ_REASON
        assert payload["loop_guard"] is True
        # The rejection is a normal tool result the model can recover from.
        assert any(
            m.get("role") == "tool" and m.get("tool_call_id") == "r2"
            for m in manager.history.messages
        )

    def test_a_different_read_is_not_blocked(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend([
            tool_round([("r1", "read_file", {"path": "notes.md"})]),
            tool_round([("r2", "read_file", {"path": "other.md"})]),
            final_round("Read both."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Compare the two notes.")
        recorder = Recorder()

        run(manager, recorder)

        assert all(r.ok for r in recorder.tool_results)

    def test_the_guard_goes_dormant_after_the_first_applied_write(
        self, workspace, isolated_streams
    ) -> None:
        """Re-reading to verify your own edit is normal work, not a loop."""
        backend = ScriptedBackend([
            tool_round([("r1", "read_file", {"path": "notes.md"})]),
            tool_round([("w1", "write_file", {
                "path": "notes.md", "content": "# Notes\n\nnew\n",
            })]),
            tool_round([("r2", "read_file", {"path": "notes.md"})]),
            final_round("Verified the write."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Rewrite notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        results = {r.tool_call_id: r for r in recorder.tool_results}
        assert results["w1"].ok is True
        assert results["r2"].ok is True, (
            "post-write verification reads must stay allowed"
        )


class TestLegitimateRereadsSurvive:
    def test_a_reread_after_a_failed_tool_call_is_allowed(
        self, workspace, isolated_streams
    ) -> None:
        """A failing edit is exactly when the model *should* look again."""
        backend = ScriptedBackend([
            tool_round([("r1", "read_file", {"path": "notes.md"})]),
            # A patch that cannot match — the tool fails.
            tool_round([("p1", "patch_file", {
                "path": "notes.md",
                "old_str": "text that is definitely not in the file",
                "new_str": "replacement",
            })]),
            tool_round([("r2", "read_file", {"path": "notes.md"})]),
            final_round("Re-read after the failure."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Patch notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        results = {r.tool_call_id: r for r in recorder.tool_results}
        assert results["p1"].ok is False, "the patch must actually have failed"
        assert results["r2"].ok is True, (
            "a reread justified by a tool failure must not be blocked"
        )

    def test_a_stale_file_notice_reopens_only_the_named_paths(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.record("read_file", {"path": "other.md"})
        guard.end_round()

        assert guard.check("read_file", {"path": "notes.md"}) is not None
        guard.note_stale_paths(["notes.md"])
        assert guard.check("read_file", {"path": "notes.md"}) is None, (
            "the invalidated path must be readable again"
        )
        assert guard.check("read_file", {"path": "other.md"}) is not None, (
            "unrelated reads stay guarded"
        )

    def test_pending_edit_recovery_state_allows_a_reread(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        assert guard.check("read_file", {"path": "notes.md"}) is not None
        assert guard.check(
            "read_file", {"path": "notes.md"}, recovery_pending=True
        ) is None

    def test_failure_grace_lasts_one_round_only(self) -> None:
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "notes.md"})
        guard.observe_result("patch_file", ok=False)
        guard.end_round()

        # The round right after the failure may reread.
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is None
        guard.record("read_file", {"path": "notes.md"})
        guard.end_round()

        # The one after that is guarded again.
        guard.begin_round()
        assert guard.check("read_file", {"path": "notes.md"}) is not None


class TestStalledDiscoveryFiresTheFocusedRequest:
    """The first round with no new evidence and no progress is the protocol
    transition: the send loop answers the next request with the focused action
    request instead of another reasoning stream."""

    def test_unique_reads_reach_focused_action_after_two_hops(
        self, workspace, isolated_streams
    ) -> None:
        """Novel evidence no longer buys unbounded ordinary discovery.

        The guard's own stall rule cannot fire here — every read returns a
        different file — which is exactly the production loop that ran forever.
        The discovery stage ends it: two executed hops, then action-only.
        """
        for name in ("alpha.py", "beta.py", "gamma.py", "delta.py"):
            (workspace / name).write_text(
                f"# {name}\n\ncontent unique to {name}\n", encoding="utf-8"
            )
        rounds: list[list[Event]] = [
            tool_round([(f"r{i}", "read_file", {"path": target})])
            for i, target in enumerate(
                ("notes.md", "other.md", "alpha.py", "beta.py", "gamma.py")
            )
        ]
        rounds.append(final_round("Enough evidence; implementing."))
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")

        run(manager, Recorder())

        assert not backend.calls[0].get("require_tool_call"), (
            "the survey request is an ordinary request"
        )
        assert not backend.calls[1].get("require_tool_call"), (
            "the final-evidence request is still an ordinary request"
        )
        assert backend.calls[2].get("require_tool_call") is True, (
            "the third request must be action-only, however novel the evidence"
        )

    def test_equivalent_evidence_rounds_fire_the_focused_action_request(
        self, workspace, isolated_streams
    ) -> None:
        """Re-reading the same bytes under a new argument is not new evidence,
        so the round after the stall is the action-serialization request."""
        rounds: list[list[Event]] = [
            tool_round([("r0", "read_file", {"path": "notes.md"})]),
            tool_round([("r1", "read_file", {"path": "notes.md", "_n": 1})]),
            tool_round([("w1", "write_file", {
                "path": "notes.md", "content": "# Notes\n\nnew\n",
            })]),
            final_round("Applied the change."),
        ]
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")

        run(manager, Recorder())

        focused = backend.calls[2]
        assert focused.get("require_tool_call") is True, (
            "the request after the stalled round must require exactly one tool call"
        )
        exposed = {
            str(t.get("function", {}).get("name", "")) for t in focused.get("tools") or []
        }
        assert "write_file" in exposed
        assert "report_blocker" in exposed
        assert "read_file" not in exposed
        # The one authorized act really landed.
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nnew\n"
        )

    def test_truncated_reads_allow_focused_continuation(
        self, workspace, isolated_streams
    ) -> None:
        """A truncated read plus range continuations is discovery, not a stall."""
        from aura.config import MAX_READ_BYTES

        lines = [
            f"line {i:05d} " + ("x" * 120)
            for i in range((MAX_READ_BYTES // 100) + 200)
        ]
        (workspace / "big.md").write_text("\n".join(lines), encoding="utf-8")
        rounds: list[list[Event]] = [
            tool_round([("t0", "read_file", {"path": "big.md"})]),
            tool_round([("t1", "read_file_range", {
                "path": "big.md", "start_line": 2100, "end_line": 2200,
            })]),
            tool_round([("t2", "read_file_range", {
                "path": "big.md", "start_line": 2201, "end_line": 2250,
            })]),
            final_round("Continuing from the truncated read."),
        ]
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace, "Update big.md.")

        run(manager, recorder)

        # The continuation reads are still ordinary discovery: neither is
        # refused as a repeat, and both actually execute. The guard has no
        # quarrel with them — a truncated read's continuation is real evidence.
        rejected = [
            r for r in recorder.of_type(ToolResult)
            if DUPLICATE_READ_REASON in str(r.result)
        ]
        assert not rejected, "a truncated read's continuation is never a repeat"
        assert not backend.calls[1].get("require_tool_call"), (
            "the continuation round is the final ordinary request, not action-only"
        )
        # What ends discovery is the stage, not the guard's stall rule.
        assert backend.calls[2].get("require_tool_call") is True

    def test_identical_results_under_changed_arguments_stall(self) -> None:
        """Cosmetic argument changes cannot launder the same evidence as new."""
        guard = PreEditLoopGuard()
        payload = {
            "ok": True,
            "path": "notes.md",
            "content": "# Notes\n\nold body\n",
            "truncated": False,
            "content_hash": "a" * 64,
            "file_size": 18,
        }
        for index in range(2):
            guard.begin_round()
            guard.record("read_file", {"path": "notes.md", "_n": index})
            guard.observe_result("read_file", True, payload)
            guard.end_round()
        assert guard.focused is True

    def test_new_search_results_never_stall(self) -> None:
        """A genuinely new search result is evidence, not a stall."""
        guard = PreEditLoopGuard()
        for pattern in ("loop", "guard", "steering"):
            guard.begin_round()
            guard.record("grep_search", {"pattern": pattern})
            guard.observe_result("grep_search", True, {
                "ok": True,
                "matches": [{
                    "path": f"src/{pattern}.py",
                    "line_number": 1,
                    "content": f"def {pattern}():",
                }],
                "engine": "ripgrep",
                "searched_files": 3,
                "skipped_files": 0,
                "summary": f"found {pattern}",
            })
            guard.end_round()
        assert guard.focused is False

    def test_equivalent_search_results_stall(self) -> None:
        """The same matches under a different pattern are the same evidence."""
        guard = PreEditLoopGuard()
        matches = [
            {"path": "src/a.py", "line_number": 3, "content": "x = 1"},
        ]
        for index in range(2):
            guard.begin_round()
            guard.record("grep_search", {"pattern": "x" if index == 0 else "X"})
            guard.observe_result("grep_search", True, {
                "ok": True,
                "matches": matches,
                "engine": "ripgrep",
                "searched_files": 7,
                "skipped_files": 0,
                "summary": "same matches again",
            })
            guard.end_round()
        assert guard.focused is True

    def test_a_truncated_read_counts_as_new_evidence_then_repeats_stall(self) -> None:
        """A truncated payload is new evidence; only its repetition stalls."""
        guard = PreEditLoopGuard()
        payload = {
            "ok": True,
            "path": "big.md",
            "content": "first 200KB of the file...",
            "truncated": True,
            "content_hash": "b" * 64,
            "file_size": 300000,
        }
        guard.begin_round()
        guard.record("read_file", {"path": "big.md"})
        guard.observe_result("read_file", True, payload)
        guard.end_round()
        assert guard.focused is False, "the truncated read is new evidence"

        guard.begin_round()
        guard.record("read_file", {"path": "big.md", "_n": 1})
        guard.observe_result("read_file", True, payload)
        guard.end_round()
        assert guard.focused is True, "an identical repeat of it is a stall"

    def test_no_focused_request_when_the_turn_makes_progress(
        self, workspace, isolated_streams
    ) -> None:
        """A turn that applied a write never opens a focused request, and the
        guard stays dormant for reads after the write."""
        rounds: list[list[Event]] = [
            tool_round([("r0", "read_file", {"path": "notes.md"})]),
            tool_round([("w0", "write_file", {
                "path": "notes.md", "content": "# Notes\n\nnew\n",
            })]),
            tool_round([("r1", "read_file", {"path": "other.md"})]),
            tool_round([("r2", "read_file", {"path": "notes.md", "n": 2})]),
            tool_round([("r3", "read_file", {"path": "other.md", "n": 3})]),
            final_round("Finished."),
        ]
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Rewrite notes.md.")

        run(manager, Recorder())

        assert not [
            c.get("require_tool_call") for c in backend.calls
            if c.get("require_tool_call")
        ], "a turn that edited must never open a focused request"

    def test_successful_terminal_commands_count_as_progress(self) -> None:
        """Progress is the command's *result*, not the decision to run one."""
        guard = PreEditLoopGuard()
        for _ in range(2):
            guard.begin_round()
            guard.record("read_file", {"path": "a"})
            guard.record("run_terminal_command", {"command": "pytest -q"})
            guard.observe_result(
                "run_terminal_command", True, json.dumps({"exit_code": 0})
            )
            guard.end_round()
        assert guard.focused is False

    def test_failing_terminal_commands_open_recovery_not_focus(self) -> None:
        """The bug: intent was recorded as progress before the command ran.

        The failing round opens recovery rather than forcing a mutation. The
        round after it is the one granted recovery round — and if it recovers
        nothing, it is the transition, so recovery cannot latch focus off.
        """
        guard = PreEditLoopGuard()
        guard.begin_round()
        guard.record("read_file", {"path": "a"})
        guard.record("run_terminal_command", {"command": "pytest -q"})
        guard.observe_result(
            "run_terminal_command",
            False,
            json.dumps({"exit_code": 1, "command": "pytest -q"}),
        )
        guard.end_round()

        assert guard.focused is False
        assert guard._failure_active is True

    def test_the_todo_tool_does_not_launder_a_stalled_round(self) -> None:
        """Bookkeeping is not evidence — it must not reset the stall."""
        guard = PreEditLoopGuard()
        payload = {
            "ok": True,
            "path": "notes.md",
            "content": "# Notes\n\nold body\n",
            "truncated": False,
            "content_hash": "a" * 64,
            "file_size": 18,
        }
        for index in range(2):
            guard.begin_round()
            guard.record("update_worker_todo", {"items": []})
            guard.record("read_file", {"path": "notes.md", "_n": index})
            guard.observe_result("read_file", True, payload)
            guard.end_round()
        assert guard.focused is True


class TestEmergencyBrakeIsStillTheBackstop:
    def test_the_300_call_brake_is_untouched(self) -> None:
        from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE

        assert MAX_TOOL_CALLS_BY_MODE["single"] == 300
        assert MAX_TOOL_CALLS_BY_MODE["planner"] == 300
        assert MAX_TOOL_CALLS_BY_MODE["worker"] == 300
