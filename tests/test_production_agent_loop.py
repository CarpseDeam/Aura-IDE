"""Production SINGLE is a conventional coding-agent loop.

The production runtime alternates *nothing*: every active request exposes the
same stable catalog, the user-selected model, and the user-selected thinking
mode, and the loop is exactly

``model request → tool calls → exact tool results → next model request → ...``

until the model returns a final response that has a truthful terminal outcome.
The old decision-checkpoint / focused-action alternation is gone; a write, a
command, a read, or a failed tool result never switches the request into
another workflow mode.

These tests drive the *real* :class:`ConversationManager`, the real
:class:`ToolRegistry`, and a real temporary workspace with a scripted model
backend, and record for every scripted request the exposed tool names in order,
a stable schema hash, the effective thinking mode, whether ``require_tool_call``
was supplied, the tool calls, the tool results, the applied writes, the
validation results, and the final receipt/status.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

from aura.bridge.production_receipt import (
    STATUS_BLOCKED,
    STATUS_COMPLETED,
    STATUS_RUNAWAY_STOPPED,
    ProductionRunEvidence,
    build_production_receipt,
)
from aura.client import ApiError, Done, Event, ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult
from aura.conversation.manager import ConversationManager
from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    IMPLEMENTATION_ROUTE,
    SELECTED_THINKING,
    Recorder,
    ScriptedBackend,
    build_manager,
    final_round,
    make_multi_file_workspace,
    make_workspace,
    read_round,
    run,
    tool_round,
    write_round,
)

PY = sys.executable


def validate(path: str) -> list[Event]:
    return tool_round([("v", "run_terminal_command", {
        "command": f'"{PY}" -m py_compile {path}',
    })])


def check_target_value(path: str, expected: int) -> list[Event]:
    """Run a real check that fails while ``TARGET`` differs from *expected*.

    Reads the file directly rather than importing it: a bytecode cache keyed on
    mtime+size would serve stale bytecode for a same-size rewrite, which is not
    the behaviour under test.
    """
    return tool_round([("v", "run_terminal_command", {
        "command": (
            f'"{PY}" -c "assert \'TARGET = {expected}\' in '
            f"open('{path}', encoding='utf-8').read()\""
        ),
    })])


def write_file(call_id: str, path: str, content: str) -> list[Event]:
    return tool_round([(call_id, "write_file", {"path": path, "content": content})])


def malformed_round(call_id: str, name: str, raw_arguments: str) -> list[Event]:
    """One streamed round whose call carries unparsable arguments."""
    events: list[Event] = [
        ToolCallStart(index=0, id=call_id, name=name),
        ToolCallArgsDelta(index=0, args_chunk=raw_arguments),
        ToolCallEnd(index=0),
    ]
    events.append(Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": raw_arguments},
            }],
        },
    ))
    return events


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


# ── evidence / receipt helpers ──────────────────────────────────────────────


def build_evidence(
    manager: ConversationManager,
    recorder: Recorder,
    *,
    cancelled: bool = False,
) -> ProductionRunEvidence:
    """Project the recorded tool results into structured receipt evidence."""
    writes: list[dict] = []
    not_applied: list[dict] = []
    terminals: list[dict] = []
    validations: list[dict] = []
    for result in recorder.tool_results():
        payload = result.result
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except (TypeError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        if result.name in WRITE_TOOLS:
            record = {
                "path": data.get("path") or data.get("rel_path") or "",
                "rel_path": data.get("rel_path") or data.get("path") or "",
                "is_new_file": bool(data.get("is_new_file")),
            }
            if result.ok and data.get("applied") is True:
                writes.append(record)
            else:
                record["failure_class"] = data.get("failure_class") or "not_applied"
                not_applied.append(record)
        elif result.name in TERMINAL_TOOLS:
            entry = {
                "command": data.get("command") or "",
                "exit_code": data.get("exit_code"),
                "output": data.get("output") or data.get("stdout") or "",
            }
            terminals.append(entry)
            validations.append({
                **entry,
                "validation_ok": data.get("exit_code") == 0,
            })
    return ProductionRunEvidence(
        model="scripted-production-model",
        write_results=writes,
        not_applied_writes=not_applied,
        terminal_results=terminals,
        validation_results=validations,
        final_response=recorder.chat_text,
        cancelled=cancelled,
        blocked_reason=manager.last_turn_blocked_reason,
        already_satisfied=manager.last_turn_already_satisfied,
        bears_production_action=manager.last_turn_bears_production_action,
        runaway_stopped=manager.last_turn_runaway_stopped,
    )


def assert_stable_loop(backend: ScriptedBackend) -> None:
    """Every active request used the one stable catalog and no
    ``require_tool_call``."""
    violations = backend.all_requests_stable()
    assert violations == [], violations
    assert backend.every_request_thinking() == SELECTED_THINKING, (
        "the user-selected thinking mode must be used on every active request"
    )
    hashes = {backend.schema_hash(i) for i in range(len(backend.calls))}
    assert len(hashes) == 1, "the schema hash moved between requests"


# ── 1. one-file task ────────────────────────────────────────────────────────


class TestOneFileTask:
    def test_read_edit_validate_final(self, tmp_path, isolated_streams) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            tool_round([("r0", "read_file", {"path": "file_00.py"})]),
            write_file("w0", "file_00.py", "VALUE_00 = 100\nTARGET = 1\n"),
            validate("file_00.py"),
            final_round("Updated file_00.py; validation passed."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET to 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        assert len(backend.calls) == 4, "read → edit → validate → final"
        results = {r.tool_call_id: r for r in recorder.tool_results()}
        assert results["r0"].ok is True
        assert results["w0"].ok is True
        assert json.loads(results["w0"].result)["applied"] is True
        assert results["v"].ok is True
        assert (workspace / "file_00.py").read_text(encoding="utf-8").startswith(
            "VALUE_00 = 100"
        )
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_COMPLETED


# ── 2. six-file task ────────────────────────────────────────────────────────


class TestSixFileTask:
    def test_first_write_does_not_change_catalog_or_force_finalization(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj", count=6)
        backend = ScriptedBackend([
            # First response: inspect two files and edit only the first.
            tool_round([
                ("r0", "read_file", {"path": "file_00.py"}),
                ("r1", "read_file", {"path": "file_01.py"}),
            ]),
            write_file("w0", "file_00.py", "VALUE_00 = 100\nTARGET = 1\n"),
            # Later responses keep editing the remaining files.
            tool_round([
                ("r2", "read_file", {"path": "file_02.py"}),
            ]),
            write_file("w1", "file_01.py", "VALUE_01 = 101\nTARGET = 1\n"),
            write_file("w2", "file_02.py", "VALUE_02 = 102\nTARGET = 1\n"),
            validate("file_00.py"),
            final_round("Edited three files; validation passed."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(
            workspace, "Set TARGET to 1 in file_00, file_01, and file_02."
        )
        recorder = Recorder()

        run(manager, recorder)

        assert len(backend.calls) == 7
        # The catalog after the first applied write is identical to the one
        # before it — a write never switches the request into another mode.
        assert_stable_loop(backend)
        writes = recorder.results_named("write_file")
        applied = [
            json.loads(w.result)["path"]
            for w in writes if json.loads(w.result).get("applied") is True
        ]
        assert applied == ["file_00.py", "file_01.py", "file_02.py"], applied
        assert all(
            (workspace / p).read_text(encoding="utf-8").startswith("VALUE_")
            for p in ("file_00.py", "file_01.py", "file_02.py")
        )
        # The first write (request 1) did not force finalization: requests 2-6
        # all came after it and were ordinary tool rounds on the same catalog.
        for index in (1, 2, 3, 4, 5, 6):
            assert backend.tool_names(index) == backend.tool_names(0)


# ── 3. failed path ──────────────────────────────────────────────────────────


class TestFailedPath:
    def test_bad_read_then_corrected_path_continues(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            # The wrong path does not exist — an ordinary failed tool result.
            tool_round([("r0", "read_file", {"path": "mod_99.py"})]),
            # The corrected path.
            read_round("r1", 0),
            write_round("w1"),
            final_round("Corrected the path and applied the edit."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md after reading mod_00.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        results = {r.tool_call_id: r for r in recorder.tool_results()}
        assert results["r0"].ok is False, "the bad path must fail"
        assert results["r1"].ok is True, "the corrected path must succeed"
        assert results["w1"].ok is True
        assert (workspace / "notes.md").read_text(encoding="utf-8").startswith(
            "# Notes"
        )


# ── 4. failed mutation ──────────────────────────────────────────────────────


class TestFailedMutation:
    def test_stale_patch_reread_corrected_mutation_applies(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            tool_round([("r0", "read_file", {"path": "file_00.py"})]),
            # A patch that cannot match the current bytes — the tool fails.
            tool_round([("p0", "patch_file", {
                "path": "file_00.py",
                "old_str": "text that is definitely not in the file",
                "new_str": "TARGET = 1",
            })]),
            # The reread is justified by the failure.
            tool_round([("r1", "read_file", {"path": "file_00.py"})]),
            # The corrected mutation applies.
            write_file("w0", "file_00.py", "VALUE_00 = 100\nTARGET = 1\n"),
            validate("file_00.py"),
            final_round("Reread after the stale patch and applied a corrected edit."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET to 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        results = {r.tool_call_id: r for r in recorder.tool_results()}
        assert results["p0"].ok is False, "the stale patch must have failed"
        assert results["r1"].ok is True, "the failure-justified reread must run"
        assert results["w0"].ok is True
        assert json.loads(results["w0"].result)["applied"] is True


# ── 5. failed validation ────────────────────────────────────────────────────


class TestFailedValidation:
    def test_failure_inspection_repair_and_rerun_passes(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        path = "file_00.py"
        failing = check_target_value(path, 1)
        backend = ScriptedBackend([
            # The first edit lands but is wrong (TARGET stays 0).
            write_file("w0", path, "VALUE_00 = 100\nTARGET = 0\n"),
            # The focused validation fails.
            failing,
            # The responsible source is inspected.
            tool_round([("r0", "read_file", {"path": path})]),
            # The repair applies.
            write_file("w1", path, "VALUE_00 = 100\nTARGET = 1\n"),
            # The same validation passes on rerun.
            failing,
            final_round("Validation failed, repaired, and passed on rerun."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Make TARGET equal 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        results = {r.tool_call_id: r for r in recorder.tool_results()}
        assert results["w0"].ok is True
        assert results["r0"].ok is True, "the source inspection must be readable"
        assert results["w1"].ok is True
        terminal_ok = [r.ok for r in recorder.results_named("run_terminal_command")]
        assert terminal_ok == [False, True], (
            "the same validation must fail, then pass on rerun"
        )
        # The failure is visible in the conversation, and the repair is
        # recorded through it.
        stored = json.dumps(manager.history.messages)
        assert "assert" in stored
        assert (workspace / path).read_text(encoding="utf-8").startswith(
            "VALUE_00 = 100"
        )
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_COMPLETED, (
            "a validation that failed and then passed on rerun is a repaired "
            "turn, reported completed"
        )


# ── 6. malformed tool call ──────────────────────────────────────────────────


class TestMalformedToolCall:
    def test_paired_rejection_then_corrected_call_executes(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            malformed_round("m0", "write_file", "{not valid json"),
            write_file("w0", "file_00.py", "VALUE_00 = 100\nTARGET = 1\n"),
            final_round("Corrected the malformed call and applied the edit."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET to 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        results = {r.tool_call_id: r for r in recorder.tool_results()}
        assert results["m0"].ok is False, "the malformed call must be rejected"
        rejected = json.loads(results["m0"].result)
        assert rejected.get("call_rejected") is True, rejected
        assert results["w0"].ok is True, "the corrected call must execute"
        assert json.loads(results["w0"].result)["applied"] is True
        # The rejection is paired in the transcript exactly like any result.
        tool_messages = [
            m for m in manager.history.messages if m.get("role") == "tool"
        ]
        assert [m["tool_call_id"] for m in tool_messages] == ["m0", "w0"]


# ── 7. observation steering ─────────────────────────────────────────────────


class TestObservationSteering:
    def test_four_observation_rounds_emit_exactly_one_steering_message(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            read_round("r0", 0),
            read_round("r1", 1),
            read_round("r2", 2),
            read_round("r3", 3),
            write_round("w1"),
            final_round("Acted."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        # The steering is appended exactly once, after the fourth consecutive
        # observation-only round.
        steerings = [
            m
            for m in manager.history.messages
            if m.get("role") == "user"
            and m.get("aura_internal")
            and "consecutive rounds" in str(m.get("content", ""))
        ]
        assert len(steerings) == 1, (
            f"expected exactly one steering message, got {len(steerings)}"
        )
        # Tools and thinking were not changed to make room for it.
        assert_stable_loop(backend)
        # The write still applied afterwards.
        writes = recorder.results_named("write_file")
        assert writes and json.loads(writes[-1].result).get("applied") is True


# ── 8. observation runaway ──────────────────────────────────────────────────


class TestObservationRunaway:
    def test_eight_observation_rounds_stop_gracefully(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_workspace(tmp_path / "proj")
        rounds = [read_round(f"r{i}", i) for i in range(8)]
        rounds.append(final_round("(should never run)"))
        backend = ScriptedBackend(rounds)
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")
        recorder = Recorder()

        run(manager, recorder)

        # The loop stopped after the eighth observation-only round, before the
        # next request would have been issued.
        assert len(backend.calls) == 8, backend.calls
        assert manager.last_turn_runaway_stopped is True, (
            "eight observation-only rounds must stop the loop gracefully"
        )
        # No completion was claimed, and no provider-contract failure fired.
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_RUNAWAY_STOPPED
        assert not recorder.of_type(ApiError)
        # Every observed result is preserved and paired in the transcript.
        assert len(recorder.results_named("read_file")) == 8
        assert all(r.ok for r in recorder.results_named("read_file"))
        tool_messages = [
            m for m in manager.history.messages if m.get("role") == "tool"
        ]
        assert len(tool_messages) == 8


# ── 9. overall round ceiling ────────────────────────────────────────────────


class TestRoundCeiling:
    def test_ceiling_stops_gracefully_without_api_failure(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            read_round("r0", 0),
            read_round("r1", 1),
            read_round("r2", 2),
            read_round("r3", 3),
            final_round("(should never run)"),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Update notes.md.")
        recorder = Recorder()

        run(manager, recorder, max_tool_rounds=3)

        assert len(backend.calls) == 3
        assert manager.last_turn_runaway_stopped is True
        assert not recorder.of_type(ApiError), (
            "the ceiling is a graceful incomplete stop, not an API failure"
        )
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_RUNAWAY_STOPPED


# ── 10. cancellation ────────────────────────────────────────────────────────


class TestCancellation:
    def test_completed_work_survives_a_mid_stream_cancel(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        cancel = threading.Event()

        class _CancellingBackend:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            def stream(self, **kwargs):
                self.calls.append(kwargs)
                index = len(self.calls)
                if index == 1:
                    # Round 1 completes: a read and an applied write.
                    return iter(
                        tool_round([
                            ("r0", "read_file", {"path": "file_00.py"}),
                            ("w0", "write_file", {
                                "path": "file_00.py",
                                "content": "VALUE_00 = 100\nTARGET = 1\n",
                            }),
                        ])
                    )
                # Round 2 is cancelled mid-stream: nothing from it is kept.
                cancel.set()
                return iter(tool_round([("w1", "write_file", {
                    "path": "file_01.py",
                    "content": "VALUE_01 = 101\nTARGET = 1\n",
                })]))

        backend = _CancellingBackend()
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET in file_00 and file_01.")
        recorder = Recorder()
        manager.send(
            on_event=recorder,
            approval_cb=lambda _r: __import__(
                "aura.conversation.tools._types", fromlist=["ApprovalDecision"]
            ).ApprovalDecision(action="approve"),
            cancel_event=cancel,
            model="scripted-production-model",
            thinking=SELECTED_THINKING,
            hook_name=PRODUCTION_STREAM_HOOK,
            max_tool_rounds=12,
            task_route=IMPLEMENTATION_ROUTE,
        )

        results = {r.tool_call_id: r for r in recorder.tool_results()}
        # The completed read and applied write survive byte-for-byte.
        assert results["r0"].ok is True
        assert results["w0"].ok is True
        assert json.loads(results["w0"].result)["applied"] is True
        assert (workspace / "file_00.py").read_text(encoding="utf-8").startswith(
            "VALUE_00 = 100"
        )
        # The cancelled round's call never entered history as an unpaired call.
        assert not any(
            m.get("role") == "tool" and m.get("tool_call_id") == "w1"
            for m in manager.history.messages
        )

    def test_incomplete_call_pairing_is_repaired(
        self, tmp_path, isolated_streams,
    ) -> None:
        """A cancel that lands after an assistant tool-call block is appended
        (but before its results) repairs the pairing with one fail-closed
        synthetic result per missing call — never rewinding the turn."""
        workspace = make_multi_file_workspace(tmp_path / "proj")
        from aura.conversation.history import History

        history = History()
        history.set_system("You are Aura's production coding agent.")
        history.append_user_text("Set TARGET in file_00 and file_01.")
        # A completed write survives.
        history.append_assistant({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "w0",
                "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({
                    "path": "file_00.py",
                    "content": "VALUE_00 = 100\nTARGET = 1\n",
                })},
            }],
        })
        history.append_tool_result("w0", json.dumps({
            "ok": True, "applied": True, "path": "file_00.py",
        }))
        # An assistant tool-call block whose result never arrived.
        history.append_assistant({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "w1",
                "type": "function",
                "function": {"name": "write_file", "arguments": json.dumps({
                    "path": "file_01.py",
                    "content": "VALUE_01 = 101\nTARGET = 1\n",
                })},
            }],
        })
        manager = ConversationManager(
            history,
            __import__(
                "aura.conversation.tools.registry", fromlist=["ToolRegistry"]
            ).ToolRegistry(workspace_root=workspace, mode="single"),
        )
        cancel = threading.Event()
        cancel.set()
        recorder = Recorder()
        manager.send(
            on_event=recorder,
            approval_cb=lambda _r: __import__(
                "aura.conversation.tools._types", fromlist=["ApprovalDecision"]
            ).ApprovalDecision(action="approve"),
            cancel_event=cancel,
            model="scripted-production-model",
            thinking=SELECTED_THINKING,
            hook_name=PRODUCTION_STREAM_HOOK,
            max_tool_rounds=12,
            task_route=IMPLEMENTATION_ROUTE,
        )

        # The completed write is preserved byte-for-byte.
        assert history.messages[2]["content"].startswith('{"ok": true')
        # The incomplete call is paired with one fail-closed synthetic result.
        w1_tool = next(
            m for m in history.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "w1"
        )
        interrupted = json.loads(w1_tool["content"])
        assert interrupted.get("cancelled") is True
        assert interrupted.get("applied") is not True
        # Pairing is valid: every tool message answers its call.
        calls = {
            tc["id"]
            for m in history.messages
            if m.get("role") == "assistant"
            for tc in (m.get("tool_calls") or [])
        }
        tool_ids = {
            m.get("tool_call_id") for m in history.messages if m.get("role") == "tool"
        }
        assert calls == tool_ids


# ── 11. already satisfied ───────────────────────────────────────────────────


class TestAlreadySatisfied:
    def test_structured_already_satisfied_finishes_truthfully_without_write(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        # The requested state already exists.
        (workspace / "file_00.py").write_text(
            "VALUE_00 = 100\nTARGET = 1\n", encoding="utf-8"
        )
        backend = ScriptedBackend([
            tool_round([("r0", "read_file", {"path": "file_00.py"})]),
            tool_round([("s0", "report_already_satisfied", {
                "evidence": "file_00.py already sets TARGET = 1.",
            })]),
            final_round("Already satisfied."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET to 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        assert manager.last_turn_already_satisfied is True
        assert recorder.results_named("write_file") == [], (
            "already-satisfied must finish without a write"
        )
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_COMPLETED


# ── 12. blocker ─────────────────────────────────────────────────────────────


class TestBlocker:
    def test_structured_blocker_finishes_truthfully(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_multi_file_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            tool_round([("b0", "report_blocker", {
                "blocker": "The CI token is missing.",
                "needed": "A valid token in the environment.",
            })]),
            final_round("Blocked."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace, "Set TARGET to 1 in file_00.py.")
        recorder = Recorder()

        run(manager, recorder)

        assert_stable_loop(backend)
        assert manager.last_turn_blocked_reason == "The CI token is missing."
        assert recorder.results_named("write_file") == []
        receipt = build_production_receipt(build_evidence(manager, recorder))
        assert receipt.status == STATUS_BLOCKED


# ── 13. DeepSeek High ───────────────────────────────────────────────────────


class TestDeepSeekHigh:
    def test_every_active_request_uses_high_with_no_require_tool_call(
        self, tmp_path, isolated_streams,
    ) -> None:
        workspace = make_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            read_round("r0", 0),
            write_round("w1"),
            final_round("Updated notes.md."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        manager = build_manager(workspace)
        recorder = Recorder()

        run(manager, recorder, thinking="high")

        assert backend.every_request_thinking() == "high"
        assert not any(
            backend.sent_require_tool_call(i) for i in range(len(backend.calls))
        )
        # The reasoning-replay requirement is a transport rule; canonical
        # history carries no request-only placeholder.
        from aura.client.deepseek import REASONING_REPLAY_PLACEHOLDER

        assert REASONING_REPLAY_PLACEHOLDER not in json.dumps(
            manager.history.messages
        )
