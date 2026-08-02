"""Projection proofs for direct production execution.

The production session must drive Aura's existing polished workspace: run
identity, reasoning, live TODO, tool cards, writes and diffs, terminal output,
external process output, validation evidence, exactly one completion receipt,
cancellation, and a clean return to idle.
"""

from __future__ import annotations

import json

import pytest

from aura.bridge.production_execution import (
    ProductionExecutionSession,
    new_production_run_id,
)
from aura.bridge.production_receipt import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_COMPLETED_UNVERIFIED,
    STATUS_PROVIDER_CONTRACT_FAILURE,
    STATUS_VALIDATION_FAILED,
    ProductionRunEvidence,
    build_production_receipt,
    summarize_validation,
)
from aura.client import (
    AgentProcessFinished,
    AgentProcessOutput,
    AgentProcessStarted,
    ContentDelta,
    Done,
    ReasoningDelta,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.worker_todo import UPDATE_WORKER_TODO_TOOL

pytest.importorskip("PySide6.QtWidgets")


class _ApprovalProxyStub:
    """Records approvals; mimics _ApprovalProxy's consume_last_event contract."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def queue_event(self, rel_path: str, old: str, new: str, is_new_file: bool) -> None:
        self.events.append({
            "rel_path": rel_path,
            "old_content": old,
            "new_content": new,
            "is_new_file": is_new_file,
        })

    def consume_last_event(self):
        return self.events.pop(0) if self.events else None


class _SignalRecorder:
    """Collects every projected signal emission for assertions."""

    def __init__(self, session: ProductionExecutionSession) -> None:
        self.started: list[str] = []
        self.finished: list[tuple] = []
        self.cancelled: list[str] = []
        self.reasoning: list[tuple[str, str]] = []
        self.content: list[tuple[str, str]] = []
        self.tool_starts: list[tuple[str, str, str]] = []
        self.tool_args: list[tuple[str, str, str]] = []
        self.tool_ends: list[tuple[str, str]] = []
        self.tool_results: list[tuple] = []
        self.diffs: list[tuple] = []
        self.terminal: list[tuple[str, str, str]] = []
        self.process_started: list[tuple] = []
        self.process_output: list[tuple] = []
        self.process_finished: list[tuple] = []
        self.activity: list[tuple[str, list]] = []
        self.todo: list[tuple[str, list]] = []

        session.workerStarted.connect(lambda r: self.started.append(r))
        session.workerFinished.connect(lambda *a: self.finished.append(a))
        session.workerCancelled.connect(lambda r: self.cancelled.append(r))
        session.workerReasoningDelta.connect(lambda *a: self.reasoning.append(a))
        session.workerContentDelta.connect(lambda *a: self.content.append(a))
        session.workerToolCallStart.connect(lambda *a: self.tool_starts.append(a))
        session.workerToolCallArgs.connect(lambda *a: self.tool_args.append(a))
        session.workerToolCallEnd.connect(lambda *a: self.tool_ends.append(a))
        session.workerToolResult.connect(lambda *a: self.tool_results.append(a))
        session.workerDiffDecided.connect(lambda *a: self.diffs.append(a))
        session.workerTerminalOutput.connect(lambda *a: self.terminal.append(a))
        session.workerAgentProcessStarted.connect(lambda *a: self.process_started.append(a))
        session.workerAgentProcessOutput.connect(lambda *a: self.process_output.append(a))
        session.workerAgentProcessFinished.connect(lambda *a: self.process_finished.append(a))
        session.workerActivityUpdated.connect(lambda *a: self.activity.append(a))
        session.workerTodoUpdated.connect(lambda *a: self.todo.append(a))


@pytest.fixture
def approval_proxy() -> _ApprovalProxyStub:
    return _ApprovalProxyStub()


@pytest.fixture
def session(qapp, approval_proxy) -> ProductionExecutionSession:
    return ProductionExecutionSession(approval_proxy=approval_proxy)


@pytest.fixture
def recorder(session) -> _SignalRecorder:
    return _SignalRecorder(session)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


def _terminal_result(tool_id: str, command: str, exit_code: int) -> ToolResult:
    payload = {
        "ok": exit_code == 0,
        "command": command,
        "exit_code": exit_code,
        "output": "boom traceback" if exit_code else "all good",
        "counts_as_validation": True,
        "validation_classification": "passed" if exit_code == 0 else "failed",
        "counts_as_product_failure": exit_code != 0,
    }
    return ToolResult(
        tool_call_id=tool_id,
        name="run_terminal_command",
        ok=exit_code == 0,
        result=json.dumps(payload),
        extras={},
    )


def _write_result(tool_id: str, path: str, is_new_file: bool = False) -> ToolResult:
    payload = {
        "ok": True,
        "applied": True,
        "path": path,
        "rel_path": path,
        "is_new_file": is_new_file,
    }
    return ToolResult(
        tool_call_id=tool_id,
        name="write_file",
        ok=True,
        result=json.dumps(payload),
        extras={"approval": "approve", "rel_path": path},
    )


# ── 6: direct execution enters the workspace lifecycle ──────────────────────


class TestExecutionLifecycle:
    def test_begin_emits_started_with_stable_run_id(self, session, recorder) -> None:
        run_id = session.begin(model="prod-model")
        assert run_id.startswith("prod-")
        assert recorder.started == [run_id]
        assert session.run_id == run_id
        assert session.is_active()

    def test_run_ids_are_unique(self) -> None:
        assert new_production_run_id() != new_production_run_id()

    def test_every_event_correlates_to_the_same_run_id(self, session, recorder) -> None:
        """One stable execution identity across reasoning, tools, terminal, TODO."""
        run_id = session.begin(model="m")
        session.handle_event(ReasoningDelta(text="thinking"))
        session.handle_event(ToolCallStart(index=0, id="t1", name="read_file"))
        session.handle_event(ToolCallArgsDelta(index=0, args_chunk='{"path"'))
        session.handle_event(ToolCallEnd(index=0))
        session.handle_event(ToolResult(
            tool_call_id="t1", name="read_file", ok=True,
            result=json.dumps({"ok": True, "path": "a.py"}), extras={},
        ))
        session.handle_event(TerminalOutput(tool_call_id="t2", text="output"))

        assert {r[0] for r in recorder.reasoning} == {run_id}
        assert {r[0] for r in recorder.tool_starts} == {run_id}
        assert {r[0] for r in recorder.tool_args} == {run_id}
        assert {r[0] for r in recorder.tool_ends} == {run_id}
        assert {r[0] for r in recorder.tool_results} == {run_id}
        assert {r[0] for r in recorder.terminal} == {run_id}


# ── 7: reasoning and tool events project ────────────────────────────────────


class TestStreamProjection:
    def test_reasoning_projects_but_prose_stays_chat_owned(
        self, session, recorder
    ) -> None:
        """Reasoning is execution activity; the answer belongs to the chat.

        The session must not re-emit assistant prose as Worker activity — that
        is what put answers in the ephemeral Worker Log instead of the chat.
        """
        session.begin()
        session.handle_event(ReasoningDelta(text="weighing options"))
        session.handle_event(ContentDelta(text="here is the plan"))
        assert [r[1] for r in recorder.reasoning] == ["weighing options"]
        assert recorder.content == []

    def test_tool_calls_project_with_args_and_result(self, session, recorder) -> None:
        session.begin()
        session.handle_event(ToolCallStart(index=0, id="t1", name="grep_search"))
        session.handle_event(ToolCallArgsDelta(index=0, args_chunk='{"pattern":"x"}'))
        session.handle_event(ToolCallEnd(index=0))
        session.handle_event(ToolResult(
            tool_call_id="t1", name="grep_search", ok=True,
            result=json.dumps({"ok": True, "matches": 3}), extras={},
        ))
        assert recorder.tool_starts[0][1:] == ("t1", "grep_search")
        assert recorder.tool_args[0][1:] == ("t1", '{"pattern":"x"}')
        assert recorder.tool_ends[0][1] == "t1"
        assert recorder.tool_results[0][1:4] == ("t1", "grep_search", True)

    def test_activity_heartbeat_projects(self, session, recorder) -> None:
        session.begin()
        session.handle_event(ToolCallStart(index=0, id="t1", name="read_file"))
        assert recorder.activity, "tool start must produce an activity snapshot"
        kinds = {e["kind"] for e in recorder.activity[-1][1]}
        assert any("tool_started" in k for k in kinds)


# ── 8: live TODO with exactly one active item ───────────────────────────────


class TestTodoProjection:
    def _todo_result(self, items: list[dict]) -> ToolResult:
        return ToolResult(
            tool_call_id="todo-1",
            name=UPDATE_WORKER_TODO_TOOL,
            ok=True,
            result=json.dumps({"ok": True, "items": items}),
            extras={"worker_todo": True},
        )

    def test_todo_snapshot_reaches_the_gui_with_one_active_item(
        self, session, recorder
    ) -> None:
        run_id = session.begin()
        session.handle_event(self._todo_result([
            {"id": "inspect", "text": "Inspect ownership", "status": "done"},
            {"id": "edit", "text": "Apply the edit", "status": "active"},
            {"id": "validate", "text": "Run validation", "status": "pending"},
        ]))
        assert recorder.todo, "TODO snapshot must project to the GUI"
        emitted_run_id, items = recorder.todo[-1]
        assert emitted_run_id == run_id
        assert [i["status"] for i in items] == ["done", "active", "pending"]
        assert sum(1 for i in items if i["status"] == "active") == 1

    def test_todo_advances_across_updates(self, session, recorder) -> None:
        session.begin()
        session.handle_event(self._todo_result([
            {"id": "a", "text": "A", "status": "active"},
            {"id": "b", "text": "B", "status": "pending"},
            {"id": "c", "text": "C", "status": "pending"},
        ]))
        session.handle_event(self._todo_result([
            {"id": "a", "text": "A", "status": "done"},
            {"id": "b", "text": "B", "status": "active"},
            {"id": "c", "text": "C", "status": "pending"},
        ]))
        assert len(recorder.todo) == 2
        assert recorder.todo[-1][1][1]["status"] == "active"

    def test_invalid_todo_snapshot_does_not_project_or_break_the_run(
        self, session, recorder
    ) -> None:
        session.begin()
        session.handle_event(self._todo_result([
            {"id": "a", "text": "A", "status": "active"},
            {"id": "b", "text": "B", "status": "active"},
            {"id": "c", "text": "C", "status": "pending"},
        ]))
        assert recorder.todo == []
        assert session.is_active()


# ── 9: write approval and diff events ───────────────────────────────────────


class TestWriteAndDiffProjection:
    def test_approved_write_projects_a_diff(
        self, session, recorder, approval_proxy
    ) -> None:
        session.begin()
        approval_proxy.queue_event("app/main.py", "old", "new", False)
        session.handle_event(_write_result("w1", "app/main.py"))

        assert recorder.diffs, "approved write must project a diff"
        run_id, tool_id, decision, rel_path, old, new, is_new = recorder.diffs[0]
        assert (tool_id, decision, rel_path, old, new, is_new) == (
            "w1", "approve", "app/main.py", "old", "new", False
        )

    def test_applied_write_lands_in_the_execution_ledger(self, session) -> None:
        session.begin()
        session.handle_event(_write_result("w1", "app/main.py", is_new_file=True))
        paths = [w.get("path") for w in session.relay.write_results]
        assert "app/main.py" in paths


# ── 10 & 11: terminal and external process output ───────────────────────────


class TestTerminalAndProcessProjection:
    def test_terminal_output_projects(self, session, recorder) -> None:
        session.begin()
        session.handle_event(TerminalOutput(tool_call_id="t9", text="pytest: 3 passed"))
        assert recorder.terminal[0][1:] == ("t9", "pytest: 3 passed")

    def test_external_process_output_projects_with_exit_status(
        self, session, recorder
    ) -> None:
        session.begin()
        session.handle_event(AgentProcessStarted(
            process_id="p1", label="dev server", command="npm run dev",
        ))
        session.handle_event(AgentProcessOutput(process_id="p1", text="listening"))
        session.handle_event(AgentProcessFinished(process_id="p1", exit_code=0))

        assert recorder.process_started[0][1:] == ("p1", "dev server", "npm run dev")
        assert recorder.process_output[0][1:] == ("p1", "listening")
        assert recorder.process_finished[0][1:] == ("p1", 0)
        assert session.evidence().process_results == [
            {"process_id": "p1", "label": "dev server", "command": "npm run dev", "exit_code": 0}
        ]


# ── 12 & 13: failure → repair → rerun stays one turn, one receipt ───────────


class TestFailureRepairRerun:
    def _run_failure_repair_rerun(self, session, approval_proxy):
        session.begin(model="prod-model")
        session.handle_event(ReasoningDelta(text="inspecting"))
        approval_proxy.queue_event("pkg/mod.py", "old", "broken", False)
        session.handle_event(_write_result("w1", "pkg/mod.py"))
        session.handle_event(_terminal_result("c1", "python -m pytest", exit_code=1))
        approval_proxy.queue_event("pkg/mod.py", "broken", "fixed", False)
        session.handle_event(_write_result("w2", "pkg/mod.py"))
        session.handle_event(_terminal_result("c2", "python -m pytest", exit_code=0))
        session.handle_event(Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "Fixed the import and reran pytest."},
        ))

    def test_failure_edit_and_successful_rerun_stay_one_turn(
        self, session, recorder, approval_proxy
    ) -> None:
        """Proof 12: both validation events belong to the same production run."""
        self._run_failure_repair_rerun(session, approval_proxy)
        run_id = session.run_id
        receipt = session.finish()

        assert receipt is not None
        # Same run id throughout — never a second execution turn.
        assert recorder.started == [run_id]
        assert {d[0] for d in recorder.diffs} == {run_id}
        evidence = session.evidence()
        assert len(evidence.validation_results) == 2
        assert [v["exit_code"] for v in evidence.validation_results] == [1, 0]

    def test_receipt_reports_the_repair_and_both_events_remain_visible(
        self, session, approval_proxy
    ) -> None:
        self._run_failure_repair_rerun(session, approval_proxy)
        receipt = session.finish()

        assert receipt.ok is True
        assert receipt.status == STATUS_COMPLETED
        assert "after repair" in receipt.text
        assert "failed, repaired, passed on rerun" in receipt.text
        assert receipt.metadata["validation_repaired"] is True
        entry = receipt.metadata["validation"][0]
        assert entry["attempts"] == 2
        assert entry["passed"] is True
        assert entry["repaired"] is True

    def test_exactly_one_completion_receipt(
        self, session, recorder, approval_proxy
    ) -> None:
        """Proof 13: finish() is idempotent — one receipt per turn."""
        self._run_failure_repair_rerun(session, approval_proxy)
        first = session.finish()
        second = session.finish()

        assert first is not None
        assert second is None
        assert len(recorder.finished) == 1

    def test_receipt_cites_real_run_data(self, session, approval_proxy) -> None:
        self._run_failure_repair_rerun(session, approval_proxy)
        receipt = session.finish()
        metadata = receipt.metadata

        assert metadata["modified_files"] == ["pkg/mod.py"]
        assert metadata["commands_run"] == ["python -m pytest", "python -m pytest"]
        assert metadata["final_response"] == "Fixed the import and reran pytest."
        assert metadata["model"] == "prod-model"
        assert metadata["extras"]["direct_production_execution"] is True
        assert "pkg/mod.py" in receipt.text


# ── 14: return to idle ──────────────────────────────────────────────────────


class TestReturnToIdle:
    def test_session_becomes_idle_after_finish(self, session, approval_proxy) -> None:
        session.begin()
        session.handle_event(Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "done"},
        ))
        session.finish()
        assert session.is_active() is False

    def test_finished_signal_carries_receipt_text_and_status(
        self, session, recorder
    ) -> None:
        run_id = session.begin()
        session.handle_event(Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": "no changes needed"},
        ))
        session.finish()
        emitted_run_id, ok, summary, needs_followup, status = recorder.finished[0]
        assert emitted_run_id == run_id
        assert ok is True
        assert status == STATUS_COMPLETED
        assert needs_followup is False
        # The receipt reports execution facts; the prose is chat-owned.
        assert "no changes needed" not in summary
        assert "Production Report" in summary

    def test_clear_resets_projected_state(self, session) -> None:
        session.begin()
        session.handle_event(_write_result("w1", "a.py"))
        session.clear()
        assert session.run_id == ""
        assert session.relay.write_results == []


# ── 15: cancellation ────────────────────────────────────────────────────────


class TestCancellation:
    def test_cancel_emits_cancelled_and_no_receipt(self, session, recorder) -> None:
        run_id = session.begin()
        session.handle_event(ReasoningDelta(text="working"))
        session.cancel()

        assert recorder.cancelled == [run_id]
        assert recorder.finished == []
        assert session.is_active() is False

    def test_note_cancelled_then_finish_reports_cancellation(
        self, session, recorder
    ) -> None:
        run_id = session.begin()
        session.note_cancelled()
        receipt = session.finish()

        assert receipt is None
        assert recorder.cancelled == [run_id]
        assert recorder.finished == []

    def test_cancelled_evidence_builds_a_cancelled_receipt(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x", cancelled=True,
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_CANCELLED
        assert "Stopped by user" in receipt.text


# ── receipt truthfulness ────────────────────────────────────────────────────


class TestReceiptTruthfulness:
    def test_writes_without_validation_are_reported_unverified(self) -> None:
        """A finished stream is not proof."""
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            write_results=[{"path": "a.py", "applied": True}],
            final_response="All done!",
        ))
        assert receipt.status == STATUS_COMPLETED_UNVERIFIED
        assert "not verified" in receipt.text
        assert "unverified" in receipt.text

    def test_failing_validation_is_not_success(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            write_results=[{"path": "a.py", "applied": True}],
            validation_results=[{
                "command": "python -m pytest",
                "exit_code": 1,
                "validation_ok": False,
                "output": "1 failed",
            }],
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_VALIDATION_FAILED
        assert receipt.needs_followup is True

    def test_api_error_is_reported_as_harness_error(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x", api_errors=["502: upstream unavailable"],
        ))
        assert receipt.ok is False
        assert "502: upstream unavailable" in receipt.text

    def test_blocked_reason_surfaces(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x", blocked_reason="pytest is not installed",
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_BLOCKED
        assert receipt.needs_followup is True
        assert "pytest is not installed" in receipt.text
        assert receipt.metadata["blocked_reason"] == "pytest is not installed"

    def test_provider_contract_failure_is_its_own_terminal_status(self) -> None:
        """A focused request the provider answered without the required tool
        call is a provider-contract failure, never a completed turn."""
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x", provider_contract_failure=True,
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_PROVIDER_CONTRACT_FAILURE
        assert receipt.needs_followup is True
        assert "Provider Contract Failure" in receipt.text
        assert "exactly one tool call" in receipt.text
        assert receipt.metadata["provider_contract_failure"] is True

    def test_a_contract_failure_never_reports_completed(self) -> None:
        """Even alongside writes/validation, the failure is the truth."""
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            provider_contract_failure=True,
            write_results=[{"path": "a.py", "applied": True}],
            validation_results=[{
                "command": "python -m pytest", "exit_code": 0, "validation_ok": True,
            }],
        ))
        assert receipt.status == STATUS_PROVIDER_CONTRACT_FAILURE

    def test_rejected_writes_are_reported(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            not_applied_writes=[{"path": "b.py", "failure_class": "rejected"}],
        ))
        assert "Writes not applied" in receipt.text
        assert "b.py" in receipt.text
        assert receipt.metadata["not_applied_writes"] == ["b.py"]

    def test_process_exit_status_is_reported(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            process_results=[{
                "process_id": "p1", "label": "build", "command": "npm run build",
                "exit_code": 2,
            }],
        ))
        assert "exit 2" in receipt.text

    def test_summarize_validation_groups_by_command(self) -> None:
        outcomes = summarize_validation([
            {"command": "python  -m  pytest", "exit_code": 1, "validation_ok": False},
            {"command": "python -m pytest", "exit_code": 0, "validation_ok": True},
            {"command": "ruff check .", "exit_code": 0, "validation_ok": True},
        ])
        assert [o.command for o in outcomes] == ["python -m pytest", "ruff check ."]
        assert outcomes[0].attempts == 2
        assert outcomes[0].repaired is True
        assert outcomes[1].repaired is False


# ── 20: production projection never dispatches ──────────────────────────────


class TestProjectionHasNoDispatchAuthority:
    def test_session_exposes_no_dispatch_surface(self, session) -> None:
        for forbidden in (
            "request_dispatch",
            "user_dispatched",
            "showSpecCard",
            "dispatch_to_worker",
            "_run_worker",
        ):
            assert not hasattr(session, forbidden)

    def test_session_does_not_hold_a_dispatch_proxy(self, session) -> None:
        assert not any(
            "dispatch" in name.lower() for name in vars(session)
        ), "production session must not depend on _DispatchProxy state"
