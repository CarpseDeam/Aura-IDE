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
   write are rejected while the original result is still in the request, and
   rereads justified by a failure, a stale-file notice, or pending edit-recovery
   state stay allowed.  After the first applied write the gate is dormant, so
   re-reading to verify an edit is normal work.
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


def write_round(call_id: str = "w1", *, content: str = "# Notes\n\nnew\n") -> list[Event]:
    return tool_round([(call_id, "write_file", {"path": "notes.md", "content": content})])


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

    def all_requests_stable(self) -> list[str]:
        """Any request whose catalog or require_tool_call broke the stable shape."""
        violations: list[str] = []
        reference: list[str] | None = None
        for index, call in enumerate(self.calls):
            names = [
                str(t.get("function", {}).get("name", ""))
                for t in (call.get("tools") or [])
                if isinstance(t.get("function"), dict)
            ]
            if reference is None:
                reference = names
            if names != reference:
                violations.append(f"request {index} exposed {names} != {reference}")
            if "require_tool_call" in call:
                violations.append(f"request {index} sent require_tool_call=True")
        return violations


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
    task_route: TaskRoute | None = IMPLEMENTATION_ROUTE,
) -> None:
    manager.send(
        on_event=recorder,
        approval_cb=approve_all,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="off",
        hook_name=PRODUCTION_STREAM_HOOK,
        max_tool_rounds=max_tool_rounds,
        task_route=task_route,
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

        # A conversational turn owes no edit: no production-action route, so the
        # plain answer closes the turn instead of triggering the
        # prose-not-terminal continuation.
        run(manager, recorder, task_route=None)

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
            write_round("w1"),
            final_round("Applied."),
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
        # The loop kept its stable shape: no narrowed request appeared.
        assert backend.all_requests_stable() == [], backend.all_requests_stable()

    def test_a_different_read_is_not_blocked(
        self, workspace, isolated_streams
    ) -> None:
        backend = ScriptedBackend([
            tool_round([("r1", "read_file", {"path": "notes.md"})]),
            tool_round([("r2", "read_file", {"path": "other.md"})]),
            write_round("w1"),
            final_round("Read both and applied."),
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
            write_round("w1"),
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
            write_round("w1", content="# Notes\n\npatched body\n"),
            final_round("Re-read and applied after the failure."),
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


class TestEmergencyBrakeIsStillTheBackstop:
    def test_the_call_ceiling_is_untouched(self) -> None:
        from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE

        assert MAX_TOOL_CALLS_BY_MODE["single"] == 300
        assert MAX_TOOL_CALLS_BY_MODE["planner"] == 300
        assert MAX_TOOL_CALLS_BY_MODE["worker"] == 300
