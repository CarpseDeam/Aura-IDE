"""The production SINGLE decision protocol, reproduced from the real failure.

The run this covers is the one that shipped: a turn that read genuinely useful
things, never stalled, never cycled, never committed a decision — and therefore
never edited.  Committing was *optional*, so a model that simply did not call it
kept chaining reads until something unrelated ran out.  When the focused request
finally did fire, DeepSeek answered it with a sentence of prose beside the tool
call, and the harness treated that envelope as a provider-contract failure: the
coding task dead-stopped with all its evidence intact and no edit made.  A third
defect sat underneath both: compaction retired a read into a receipt while the
loop guard went on rejecting the reread as "you already have this".

Everything here drives the real :class:`ConversationManager`, the real
:class:`ToolRegistry`, the real :class:`PreEditLoopGuard`, and the real API-view
compaction over a real workspace.  The provider is replayed with the shapes
DeepSeek actually produced — prose beside a call, several calls, a malformed
batch — not with the perfectly-formed sequence the protocol would prefer.
"""

from __future__ import annotations

import json
import threading

import pytest

from aura.client import ApiError, ContentDelta, Done, Event, ToolResult
from aura.conversation.api_view import (
    RECEIPT_MARKER,
    build_api_view,
)
from aura.conversation.focused_action import (
    COMMIT_IMPLEMENTATION_DECISION,
    CONTINUE_IMPLEMENTATION_DISCOVERY,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import (
    DUPLICATE_READ_REASON,
    PreEditLoopGuard,
    read_fingerprint,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    KIND_CHECKPOINT,
    KIND_COMPLETION,
    KIND_FOCUSED,
    KIND_ORDINARY,
    Recorder,
    ScriptedBackend,
    approve_all,
    build_manager,
    final_round,
    make_workspace,
    reject_all,
    run,
    tool_round,
)

ORD = KIND_ORDINARY
CHK = KIND_CHECKPOINT
ACT = KIND_FOCUSED
FIN = KIND_COMPLETION


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def workspace(tmp_path):
    return make_workspace(tmp_path / "proj")


# ── the replayed provider shapes ────────────────────────────────────────────


def read(call_id: str, path: str, *, text: str = "") -> list[Event]:
    return tool_round([(call_id, "read_file", {"path": path})], text=text)


def keep_looking(call_id: str, question: str, evidence: str) -> list[Event]:
    return tool_round([(call_id, CONTINUE_IMPLEMENTATION_DISCOVERY, {
        "unresolved_question": question,
        "needed_evidence": evidence,
    })])


def commit(call_id: str = "c1", *, targets: list[str] | None = None) -> list[Event]:
    return tool_round([(call_id, COMMIT_IMPLEMENTATION_DECISION, {
        "objective": "Record the outcome in notes.md.",
        "owners": ["notes.md is the authoritative note surface"],
        "target_files": targets or ["notes.md"],
        "change": "Replace the body with the requested text.",
    })])


def prose_plus_write(call_id: str = "w1", *, body: str = "acted") -> list[Event]:
    """DeepSeek's actual shape: a sentence of narration beside the call."""
    return tool_round(
        [(call_id, "write_file", {
            "path": "notes.md", "content": f"# Notes\n\n{body}\n",
        })],
        text="I'll update notes.md now.",
    )


def two_writes() -> list[Event]:
    """Two valid mutation calls: an ordinary batch, not a contract violation."""
    return tool_round([
        ("w1", "write_file", {"path": "notes.md", "content": "# Notes\n\nacted\n"}),
        ("w2", "write_file", {"path": "mod_00.py", "content": "value = 99\n"}),
    ])


def prose_only() -> list[Event]:
    """A focused response with no tool call at all."""
    return [
        ContentDelta(text="Here is what I would do: first I would open..."),
        Done(finish_reason="stop", full_message={
            "role": "assistant",
            "content": "Here is what I would do: first I would open...",
        }),
    ]


def mixed_batch() -> list[Event]:
    """One valid mutation beside a tool this request does not expose."""
    return tool_round([
        ("w1", "write_file", {"path": "notes.md", "content": "# Notes\n\nacted\n"}),
        ("x9", "read_file", {"path": "notes.md"}),
    ])


def kinds(backend: ScriptedBackend) -> list[str]:
    return backend.request_kinds()


def called_tools(manager: ConversationManager) -> list[str]:
    names: list[str] = []
    for msg in manager.history.messages:
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            names.append(str(fn.get("name", "")))
    return names


#: Anything that would mean the turn went back to looking around instead of
#: editing. Item 10 forbids every one of these between the committed decision
#: and the first mutation.
OBSERVATION_TOOLS = frozenset({
    "read_file", "read_files", "read_file_range", "read_file_outline",
    "list_directory", "glob", "grep_search", "find_usages", "search_codebase",
    "code_intel_outline", "code_intel_references", "code_intel_dependents",
    "run_terminal_command", "run_and_watch", "run_diagnostic_command",
    "web_search", "get_workspace_snapshot", "git_status", "git_diff",
})


# ── 1-10: the wandering sequence now terminates in an edit ──────────────────


class TestTheRealWanderingSequenceTerminates:
    """One continuous turn: useful discovery, two checkpoints, an edit."""

    @pytest.fixture
    def turn(self, workspace, isolated_streams):
        backend = ScriptedBackend([
            # 1. a genuinely useful observation round
            read("r0", "notes.md", text="Let me look at the notes file."),
            # 3. the checkpoint answer: keep looking, and say why
            keep_looking(
                "k0",
                "which module writes the value notes.md records?",
                "the definition of the writer in mod_00.py",
            ),
            # 4. another observation round
            read("r1", "mod_00.py"),
            # 6. the decision
            commit(),
            # 8. DeepSeek's real shape: prose beside the mutation
            prose_plus_write(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)
        return backend, recorder, manager, workspace

    def test_1_the_observation_round_really_gathered_evidence(self, turn) -> None:
        _backend, recorder, _manager, _ws = turn
        reads = recorder.results_named("read_file")
        assert len(reads) == 2 and all(r.ok for r in reads), (
            f"non-vacuous: both reads really ran — {recorder.tool_results()}"
        )
        assert "notes.md" in str(reads[0].result)

    def test_2_to_7_the_alternation_is_owned_by_the_send_loop(self, turn) -> None:
        backend, _recorder, _manager, _ws = turn
        assert kinds(backend) == [
            ORD,   # 1. the observation round
            CHK,   # 2. the next request is automatically the checkpoint
            ORD,   # 3-4. the round the named question bought
            CHK,   # 5. and the checkpoint again, automatically
            ACT,   # 6-7. the commit hands the next request the mutation surface
            ORD,
        ], kinds(backend)

    def test_3_the_model_had_to_name_what_it_did_not_know(self, turn) -> None:
        _backend, _recorder, manager, _ws = turn
        payload = json.loads(next(
            m["content"] for m in manager.history.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "k0"
        ))
        assert payload["ok"] is True
        assert payload["implementation_discovery_continued"] is True
        assert payload["unresolved"]["unresolved_question"] == (
            "which module writes the value notes.md records?"
        )
        assert payload["unresolved"]["needed_evidence"]
        assert payload["mutation"] is False and payload["applied"] is False

    def test_5_the_checkpoint_exposes_no_investigation_tools(self, turn) -> None:
        backend, _recorder, _manager, _ws = turn
        for call in backend.checkpoint_calls():
            names = {
                str(t.get("function", {}).get("name", ""))
                for t in call["tools"]
            }
            assert names == {
                COMMIT_IMPLEMENTATION_DECISION,
                CONTINUE_IMPLEMENTATION_DISCOVERY,
            }, names
            assert not (names & OBSERVATION_TOOLS)

    def test_7_the_request_after_the_decision_exposes_mutations_only(
        self, turn,
    ) -> None:
        backend, _recorder, _manager, _ws = turn
        action = backend.action_calls()[0]
        names = {
            str(t.get("function", {}).get("name", "")) for t in action["tools"]
        }
        assert "write_file" in names
        assert {"report_blocker", "report_already_satisfied"} <= names
        assert not (names & OBSERVATION_TOOLS)
        assert not (names & {
            COMMIT_IMPLEMENTATION_DECISION, CONTINUE_IMPLEMENTATION_DISCOVERY
        })

    def test_8_and_9_prose_beside_the_call_executes_the_call(self, turn) -> None:
        _backend, recorder, _manager, ws = turn
        writes = recorder.results_named("write_file")
        assert len(writes) == 1 and writes[0].ok, (
            f"the usable mutation really ran — {recorder.tool_results()}"
        )
        assert (ws / "notes.md").read_text(encoding="utf-8") == "# Notes\n\nacted\n"

    def test_8_the_discarded_prose_never_reaches_chat_or_history(self, turn) -> None:
        _backend, recorder, manager, _ws = turn
        assert "I'll update notes.md now." not in recorder.chat_text
        assert not any(
            "I'll update notes.md now." in str(m.get("content"))
            for m in manager.history.messages
        )

    def test_10_nothing_looks_around_between_the_decision_and_the_edit(
        self, turn,
    ) -> None:
        _backend, _recorder, manager, _ws = turn
        called = called_tools(manager)
        commit_at = called.index(COMMIT_IMPLEMENTATION_DECISION)
        write_at = called.index("write_file")
        between = called[commit_at + 1:write_at]
        assert between == [], (
            f"discovery reopened between the decision and the edit: {between}"
        )
        assert not (set(called[commit_at:write_at]) & OBSERVATION_TOOLS)

    def test_the_turn_never_reports_a_provider_contract_failure(self, turn) -> None:
        _backend, _recorder, manager, _ws = turn
        assert manager.last_turn_provider_contract_failure is False


def test_9_several_valid_mutation_calls_run_as_one_batch(
    workspace, isolated_streams,
) -> None:
    """A batch is a batch. The whole-batch preflight decides, not a call count."""
    backend = ScriptedBackend([
        read("r0", "notes.md"),
        commit(targets=["notes.md", "mod_00.py"]),
        two_writes(),
        final_round("Updated both."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    recorder = Recorder()
    run(build_manager(workspace), recorder)

    writes = recorder.results_named("write_file")
    assert len(writes) == 2 and all(w.ok for w in writes), (
        f"both mutations really applied — {recorder.tool_results()}"
    )
    assert (workspace / "notes.md").read_text(encoding="utf-8") == "# Notes\n\nacted\n"
    assert (workspace / "mod_00.py").read_text(encoding="utf-8") == "value = 99\n"
    assert kinds(backend) == [ORD, CHK, ACT, ORD]


# ── 11-12: a malformed response repairs the protocol, not the task ──────────


class TestMalformedResponsesNeverEndTheTask:

    def test_11_prose_only_then_a_corrected_response_still_edits(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            prose_only(),          # unusable
            prose_plus_write(),    # corrected
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        assert kinds(backend) == [ORD, CHK, ACT, ACT, ORD]
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_11_the_gathered_evidence_is_byte_identical_across_the_repair(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            prose_only(),
            prose_plus_write(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        run(build_manager(workspace), Recorder())

        violating, corrected = backend.action_calls()
        assert corrected["messages"][:-1] == violating["messages"], (
            "the reissued request re-derived or dropped gathered evidence"
        )
        correction = corrected["messages"][-1]
        assert correction["role"] == "user"
        assert "Nothing in your previous response was executed" in correction["content"]
        assert "unchanged and still applies" in correction["content"]

    def test_11_a_mixed_batch_executes_nothing_and_pairs_every_call(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            mixed_batch(),         # one valid mutation beside an unexposed read
            prose_plus_write("w3"),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        # Nothing from the mixed batch ran: the file holds the *corrected*
        # write's content, and the rejected read never executed.
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )
        rejected = {
            m["tool_call_id"]: json.loads(m["content"])
            for m in manager.history.messages
            if m.get("role") == "tool" and m.get("tool_call_id") in {"w1", "x9"}
        }
        assert set(rejected) == {"w1", "x9"}, (
            "every call in the rejected batch owes a paired result"
        )
        assert all(p["ok"] is False for p in rejected.values())
        assert all(p["applied"] is False for p in rejected.values())
        assert rejected["x9"]["call_rejected"] is True
        assert rejected["w1"]["batch_rejected"] is True, (
            "the valid sibling is told it was valid and still did not run"
        )
        # The unexposed read was answered, never executed: its result is the
        # rejection payload, not a file body.
        assert "content" not in rejected["x9"]

    def test_12_a_repeated_shape_falls_back_instead_of_failing_the_task(
        self, workspace, isolated_streams,
    ) -> None:
        """The identical unusable shape twice is not a dead coding task."""
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            prose_only(),
            prose_only(),          # the same shape, after being corrected
            prose_plus_write(),    # answered in the ordinary request instead
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        assert kinds(backend) == [ORD, CHK, ACT, ACT, ORD, ORD], kinds(backend)
        assert manager.last_turn_provider_contract_failure is False
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )
        # The fallback request carries the whole production catalog, and every
        # byte of evidence the turn had.
        fallback = backend.calls[4]
        names = {
            str(t.get("function", {}).get("name", "")) for t in fallback["tools"]
        }
        assert {"read_file", "glob", "write_file"} <= names
        assert any(
            m.get("tool_call_id") == "r0" for m in fallback["messages"]
        ), "the fallback lost the turn's gathered evidence"
        assert any(
            m.get("tool_call_id") == "c1" for m in fallback["messages"]
        ), "the fallback lost the committed decision capsule"

    def test_12_no_provider_contract_failure_status_for_any_shape(
        self, workspace, isolated_streams,
    ) -> None:
        from aura.bridge.production_receipt import STATUS_PROVIDER_CONTRACT_FAILURE

        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            mixed_batch(),
            mixed_batch(),
            prose_plus_write("w4"),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        assert manager.last_turn_provider_contract_failure is False
        assert STATUS_PROVIDER_CONTRACT_FAILURE not in recorder.chat_text
        assert "Provider contract failure" not in recorder.chat_text
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )


# ── 13-14: duplicate-read truth follows the outbound view ───────────────────


class TestDuplicateReadTruthFollowsTheApiView:

    def _history_with_read(self, *, content: str) -> History:
        history = History()
        history.set_system("You are Aura's production coding agent.")
        history.append_user_text("Update notes.md.")
        history.append_assistant({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "r0",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"path": "notes.md"}),
                },
            }],
        })
        history.append_tool_result("r0", json.dumps({
            "ok": True, "path": "notes.md", "content": content,
        }))
        return history

    def test_14_an_unretired_exact_duplicate_is_still_rejected(self) -> None:
        history = self._history_with_read(content="short body")
        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": "notes.md"}, "r0")

        view = build_api_view("system", history.messages, budget_tokens=200_000)
        assert view.residency.is_resident("r0"), (
            "non-vacuous: the result really did survive this budget intact"
        )
        guard.note_api_view_residency(view.residency.resident_call_ids)

        assert guard.is_rereadable(
            read_fingerprint("read_file", {"path": "notes.md"})
        ) is False
        rejection = guard.check("read_file", {"path": "notes.md"})
        assert rejection is not None
        assert rejection["reason"] == DUPLICATE_READ_REASON

    def test_13_a_compacted_read_becomes_rereadable(self) -> None:
        """The rejection's own justification is what decides it.

        Under a budget that forces the read out of the outbound view, "you
        already have this" is false — so the guard stops saying it.
        """
        history = self._history_with_read(content="body line\n" * 4_000)
        # Enough other completed evidence that the read is beyond the recent
        # allowance and genuinely retires.
        for i in range(1, 6):
            history.append_assistant({
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": f"r{i}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": json.dumps({"path": f"mod_{i:02d}.py"}),
                    },
                }],
            })
            history.append_tool_result(f"r{i}", json.dumps({
                "ok": True, "path": f"mod_{i:02d}.py", "content": "x\n" * 4_000,
            }))

        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": "notes.md"}, "r0")

        view = build_api_view("system", history.messages, budget_tokens=2_000)
        assert not view.residency.is_resident("r0"), (
            "non-vacuous: the budget really did retire the original result"
        )
        guard.note_api_view_residency(view.residency.resident_call_ids)

        assert guard.is_rereadable(
            read_fingerprint("read_file", {"path": "notes.md"})
        ) is True
        assert guard.check("read_file", {"path": "notes.md"}) is None, (
            "a read the model can no longer see is not a duplicate"
        )
        assert guard.rereads_allowed_after_compaction == 1

    def test_residency_is_read_off_the_view_not_predicted(self) -> None:
        """A result that is merely *truncated* is not resident either."""
        history = self._history_with_read(content="body\n" * 20_000)
        view = build_api_view("system", history.messages, budget_tokens=600)
        resident = view.residency.is_resident("r0")
        outbound = [
            m for m in view.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "r0"
        ]
        canonical = next(
            m for m in history.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "r0"
        )
        if outbound:
            assert resident is (outbound[0]["content"] == canonical["content"])
        else:
            assert resident is False

    def test_without_any_view_the_duplicate_rule_is_unchanged(self) -> None:
        """Fail-closed: no residency evidence means the old behaviour stands."""
        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": "notes.md"}, "r0")
        assert guard.check("read_file", {"path": "notes.md"}) is not None

    def test_a_read_recorded_without_a_call_id_stays_rejectable(self) -> None:
        guard = PreEditLoopGuard()
        guard.record("read_file", {"path": "notes.md"})
        guard.note_api_view_residency(frozenset())
        assert guard.check("read_file", {"path": "notes.md"}) is not None


# ── 15: the decision and its target source survive compaction ───────────────


class TestTheDecisionAndItsSourceSurviveCompaction:

    @pytest.fixture
    def pre_write_history(self, workspace, isolated_streams):
        """A turn paused after the commit, before any mutation applied."""
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            keep_looking("k0", "what does mod_00 hold?", "the module body"),
            read("r1", "mod_00.py"),
            commit(targets=["notes.md"]),
            prose_only(),
            prose_only(),
            final_round("Stopping here."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())
        return manager

    def test_15_the_decision_capsule_is_byte_identical_under_pressure(
        self, pre_write_history,
    ) -> None:
        messages = pre_write_history.history.messages
        original = next(
            m["content"] for m in messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        )
        assert json.loads(original)["implementation_decision_committed"] is True

        view = build_api_view("system prompt", messages, budget_tokens=400)
        capsules = [
            m for m in view.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "c1"
        ]
        assert len(capsules) == 1
        assert capsules[0]["content"] == original, "byte-identical, never truncated"
        assert all(
            "implementation_decision_committed" not in (m.get("content") or "")
            for m in view.messages
            if RECEIPT_MARKER in (m.get("content") or "")
        )

    def test_15_the_target_file_source_is_pinned_with_the_decision(
        self, pre_write_history,
    ) -> None:
        """The edit request must carry the source it is going to modify.

        ``r0`` reads ``notes.md``, which the decision names as its target file;
        ``r1`` reads an unrelated module and is free to retire.
        """
        messages = pre_write_history.history.messages
        original = next(
            m["content"] for m in messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "r0"
        )
        view = build_api_view("system prompt", messages, budget_tokens=400)
        surviving = {
            m.get("tool_call_id"): m.get("content")
            for m in view.messages if m.get("role") == "tool"
        }
        assert "r0" in surviving, "the decision's target source was retired"
        assert surviving["r0"] == original, "and it must be byte-identical"
        assert view.residency.is_resident("r0")

    def test_15_the_pin_releases_after_the_first_applied_mutation(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            keep_looking("k0", "what does mod_00 hold?", "the module body"),
            read("r1", "mod_00.py"),
            commit(targets=["notes.md"]),
            prose_plus_write(),
            *[read(f"v{i}", f"mod_{i + 1:02d}.py") for i in range(6)],
            final_round("Validated."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())

        messages = manager.history.messages
        applied = [
            m for m in messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "w1"
        ]
        assert applied and json.loads(applied[0]["content"])["applied"] is True, (
            "non-vacuous: the mutation really applied"
        )
        view = build_api_view("system prompt", messages, budget_tokens=400)
        surviving = {
            m.get("tool_call_id") for m in view.messages if m.get("role") == "tool"
        }
        assert "r0" not in surviving, (
            "after the edit landed, the pre-write source rejoins the ordinary "
            "compaction ladder"
        )

    def test_15_the_pin_is_stable_across_rounds(self, pre_write_history) -> None:
        messages = pre_write_history.history.messages
        first = build_api_view("system prompt", messages, budget_tokens=400)
        again = build_api_view("system prompt", messages, budget_tokens=400)
        assert [m.get("content") for m in first.messages] == [
            m.get("content") for m in again.messages
        ], "the pinned prefix must not move between rounds"


# ── 16: everything else the turn still owes ─────────────────────────────────


class TestSurroundingBehaviourIsUnchanged:

    def test_a_direct_write_is_never_forced_through_the_checkpoint(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            prose_plus_write(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        run(build_manager(workspace), recorder)

        assert kinds(backend) == [ORD, ORD], (
            "a turn that could already edit owes no discovery and no checkpoint"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )

    def test_read_only_mode_never_enters_either_narrowed_request(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            read("r1", "mod_00.py"),
            final_round("Here is what I found."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        run(build_manager(workspace, read_only=True), Recorder())

        assert set(kinds(backend)) == {ORD}
        assert not any(c.get("require_tool_call") for c in backend.calls)

    def test_cancellation_at_the_checkpoint_keeps_the_transcript_valid(
        self, workspace, isolated_streams,
    ) -> None:
        cancel = threading.Event()

        class CancellingBackend(ScriptedBackend):
            def stream(self, **kwargs):
                if kwargs.get("require_tool_call"):
                    cancel.set()
                return super().stream(**kwargs)

        backend = CancellingBackend([
            read("r0", "notes.md"),
            keep_looking("k0", "q", "e"),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        manager.send(
            on_event=recorder,
            approval_cb=approve_all,
            cancel_event=cancel,
            model="scripted-production-model",
            thinking="high",
            hook_name=PRODUCTION_STREAM_HOOK,
            max_tool_rounds=20,
            task_route=None,
        )

        errors = [e for e in recorder.events if isinstance(e, ApiError)]
        assert errors and errors[-1].message == "Cancelled."
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        ), "nothing was written"
        _assert_tool_pairing(manager)

    def test_a_blocker_at_the_checkpoint_is_offered_only_after_a_failure(
        self, workspace, isolated_streams,
    ) -> None:
        """``report_blocker`` needs evidence that something external broke."""
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            keep_looking("k0", "q", "e"),
            tool_round([("d1", "run_diagnostic_command", {"command": "definitely-not-a-command"})]),
            read("r1", "mod_00.py"),
            commit(),
            prose_plus_write(),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())

        checkpoints = backend.checkpoint_calls()
        first_names = {
            str(t.get("function", {}).get("name", "")) for t in checkpoints[0]["tools"]
        }
        assert "report_blocker" not in first_names, (
            "with nothing broken, the honest answers are commit and continue"
        )
        last_names = {
            str(t.get("function", {}).get("name", "")) for t in checkpoints[-1]["tools"]
        }
        assert "report_blocker" in last_names, (
            "once a tool really failed this turn, the blocker exit is offered"
        )

    def test_a_failed_write_returns_to_the_loop_and_the_turn_edits(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            # A write whose path escapes the workspace: it fails on its own terms.
            tool_round([("bad", "write_file", {
                "path": "../outside.md", "content": "nope",
            })]),
            read("r1", "mod_00.py"),
            # The corrected decision, then the corrected act.
            commit("c2"),
            prose_plus_write("good"),
            final_round("Applied after correcting the path."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        writes = recorder.results_named("write_file")
        assert [w.ok for w in writes] == [False, True], (
            f"the failed act was evidence, not the end — {recorder.tool_results()}"
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nacted\n"
        )
        assert manager.last_turn_provider_contract_failure is False
        _assert_tool_pairing(manager)

    def test_a_rejected_write_is_not_a_completed_turn(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            prose_plus_write(),
            read("r1", "mod_00.py"),
            prose_plus_write("w2", body="again"),
            final_round("Could not apply."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder, approval_cb=reject_all)

        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        ), "nothing was written"
        assert manager.last_turn_provider_contract_failure is False
        assert manager.last_turn_blocked_reason == ""
        assert manager.last_turn_already_satisfied is False

    def test_a_truthful_blocker_still_ends_the_turn_blocked(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            tool_round([("b1", "report_blocker", {
                "blocker": "notes.md is generated at build time.",
            })]),
            final_round("Blocked: notes.md is generated."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())

        assert manager.last_turn_blocked_reason == (
            "notes.md is generated at build time."
        )
        assert (workspace / "notes.md").read_text(encoding="utf-8") == (
            "# Notes\n\nold body\n"
        )
        assert kinds(backend)[-1] == FIN, "the final answer exposes no tools"

    def test_already_satisfied_still_ends_the_turn_truthfully(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            tool_round([("s1", "report_already_satisfied", {
                "evidence": "notes.md already holds the requested body.",
            })]),
            final_round("Already present."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())

        assert manager.last_turn_already_satisfied is True
        assert manager.last_turn_blocked_reason == ""
        assert kinds(backend)[-1] == FIN

    def test_post_write_validation_runs_after_the_edit_not_before(
        self, workspace, isolated_streams,
    ) -> None:
        backend = ScriptedBackend([
            read("r0", "notes.md"),
            commit(),
            prose_plus_write(),
            tool_round([("v1", "read_file", {"path": "notes.md"})]),
            final_round("Verified."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()
        manager = build_manager(workspace)
        run(manager, recorder)

        called = called_tools(manager)
        assert called.index("write_file") < called.index("read_file", 1), (
            f"validation must follow the edit — {called}"
        )
        assert kinds(backend) == [ORD, CHK, ACT, ORD, ORD]

    @pytest.mark.parametrize(
        "script_name",
        ["clean", "repaired", "fallback", "batch", "blocked"],
    )
    def test_tool_call_pairing_survives_every_path(
        self, workspace, isolated_streams, script_name,
    ) -> None:
        scripts = {
            "clean": [read("r0", "notes.md"), commit(), prose_plus_write(),
                      final_round("Done.")],
            "repaired": [read("r0", "notes.md"), commit(), mixed_batch(),
                         prose_plus_write("w5"), final_round("Done.")],
            "fallback": [read("r0", "notes.md"), commit(), prose_only(),
                         prose_only(), prose_plus_write(), final_round("Done.")],
            "batch": [read("r0", "notes.md"),
                      commit(targets=["notes.md", "mod_00.py"]),
                      two_writes(), final_round("Done.")],
            "blocked": [read("r0", "notes.md"), commit(),
                        tool_round([("b1", "report_blocker", {"blocker": "x"})]),
                        final_round("Blocked.")],
        }
        backend = ScriptedBackend(scripts[script_name])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        run(manager, Recorder())
        _assert_tool_pairing(manager)


def _assert_tool_pairing(manager: ConversationManager) -> None:
    """Every assistant tool-call block is followed by exactly its results.

    A transcript that fails this poisons every later request to the provider, so
    it is checked on each path the protocol can take.
    """
    messages = manager.history.messages
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
        assert answered == expected, f"unpaired block at {index}: {answered} != {expected}"
        index = cursor
