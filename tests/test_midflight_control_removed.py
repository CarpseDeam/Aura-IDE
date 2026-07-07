"""Regression tests for mid-flight control removal.

Verifies that deleted/disabled subsystems cannot stop, steer, or
terminal-fail a Worker, WorkArtifact, or bridge outcome.

Six scenarios from the dispatch:
1. Repeated failed read/search never stops Worker
2. Many reads and zero writes do not trigger WorkerFlow terminalization
3. Finalization feedback repeats instead of escalating
4. WorkArtifact repeated same failure does not exhaust at three attempts
5. Bridge no-progress cannot become non-recoverable harness error
6. Quality gate does not terminalize after one cleanup attempt
"""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import pytest

from aura.conversation.worker_flow import WorkerFlowHarness


class TestRepeatedFailureNoStop:
    """1. Repeated failed read/search never stops Worker.

    Same tool, same args, same error, repeated at least three times.
    Result reaches model as ordinary failed tool result.
    No stop, phase-boundary, handback, final-report forcing, or terminal payload.
    """

    def _make_flow(self) -> WorkerFlowHarness:
        return WorkerFlowHarness()

    def test_repeated_failed_read_no_block(self):
        """Three+ identical failed reads do not produce a block or steering."""
        flow = self._make_flow()
        for _ in range(5):
            flow.observe_tool_call("read_file", {"path": "missing.py"})
            flow.observe_tool_result("read_file", {"path": "missing.py"}, ok=False, result="File not found")

        # After 5 identical failures: no fatal/blocking outcome, no steering.
        assert flow.fatal_outcome is None
        assert flow.blocking_outcome is None
        assert flow.has_fatal_outcome() is False
        assert flow.has_blocking_outcome() is False

    def test_repeated_failed_search_no_stop(self):
        """Three+ identical failed searches produce no control-flow change."""
        flow = self._make_flow()
        for _ in range(4):
            flow.observe_tool_call("grep_search", {"pattern": "NotFound"})
            flow.observe_tool_result("grep_search", {"pattern": "NotFound"}, ok=False, result="No matches")

        assert flow.has_fatal_outcome() is False
        assert flow.has_blocking_outcome() is False


class TestZeroWriteNotTerminal:
    """2. Many reads and zero writes do not trigger WorkerFlow terminalization.

    Worker performs many reads with no writes. No terminal failure class.
    Still working unless Worker chooses a real handback or emergency cap fires.
    """

    def _make_flow(self) -> WorkerFlowHarness:
        return WorkerFlowHarness()

    def test_many_reads_zero_writes_no_block(self):
        """Many reads without any writes produce no fatal or blocking outcome."""
        flow = self._make_flow()
        for i in range(10):
            flow.observe_tool_call("read_file_range", {"path": f"file_{i}.py", "start_line": 1, "end_line": 10})
            flow.observe_tool_result(
                "read_file_range", {"path": f"file_{i}.py", "start_line": 1, "end_line": 10},
                ok=True,
                result={"content": f"content_{i}", "total_lines": 10},
            )

        assert flow.has_fatal_outcome() is False
        assert flow.has_blocking_outcome() is False
        assert flow.state.write_intents == 0
        assert flow.state.write_actions == 0

    def test_reads_then_write_then_more_reads(self):
        """Writes are counted honestly; reads after writes are not penalized."""
        flow = self._make_flow()
        # 5 reads
        for i in range(5):
            flow.observe_tool_call("read_file", {"path": f"file_{i}.py"})
        # 1 successful write
        flow.observe_tool_call("write_file", {"path": "out.py", "content": "x=1"})
        flow.observe_tool_result("write_file", {"path": "out.py"}, ok=True, result={"applied": True, "path": "out.py"})
        # 5 more reads
        for i in range(5, 10):
            flow.observe_tool_call("read_file", {"path": f"file_{i}.py"})

        assert flow.state.write_intents == 1
        assert flow.state.write_actions == 1
        assert flow.state.changed_paths == {"out.py"}
        assert flow.has_fatal_outcome() is False


class TestFinalizationFeedbackRepeats:
    """3. Finalization feedback repeats instead of escalating.

    Same unmet validation/proof condition appears twice.
    Same feedback is appended twice.
    No second-attempt terminal failure.
    """

    def test_validation_required_always_feedback(self, monkeypatch):
        """When validation is required, feedback is appended every time.

        This test verifies the finalization gate code path at a behavioural
        level — in an integration test the gate would call
        ``history.append_user_text`` each time.
        """
        from aura.conversation.worker_finalization_gate import (
            handle_worker_candidate_finalization,
        )

        state = MagicMock()
        state.worker_needs_final_report = False
        state.worker_flow = MagicMock()
        state.worker_flow.requires_validation_before_final.return_value = True
        state.worker_flow.validation_required_text.return_value = "Run validation now."
        state.worker_flow.changed_file_classification.return_value.docs_only = False
        state.worker_explicit_validation_passed = False
        state.import_verification_required = set()
        state.syntax_repair_required = {}
        state.edit_fallback_required = {}
        state.line_range_reread_required = {}
        state.patch_invalid_syntax_required = {}
        state.reject_all_for_turn = False
        state.candidate_final_message = None

        history = MagicMock()
        on_event = MagicMock()
        finish_fn = MagicMock()

        # First call — should append feedback and return "continue"
        result1 = handle_worker_candidate_finalization(
            state=state,
            full_message={},
            history=history,
            workspace_root="/tmp",
            on_event=on_event,
            finish_worker_recoverable_followup=finish_fn,
        )
        assert result1 == "continue"
        assert history.append_user_text.call_count >= 1

        # Reset mocks to simulate a second round
        history.reset_mock()
        state.candidate_final_message = None

        # Second call — same condition, same feedback, no terminal
        result2 = handle_worker_candidate_finalization(
            state=state,
            full_message={},
            history=history,
            workspace_root="/tmp",
            on_event=on_event,
            finish_worker_recoverable_followup=finish_fn,
        )
        assert result2 == "continue"
        # Feedback was appended again (not blocked by a nudge flag)
        assert history.append_user_text.call_count >= 1
        # finish_worker_recoverable_followup was NOT called (no terminal)
        assert finish_fn.call_count == 0

    def test_final_report_proof_always_feedback(self, monkeypatch):
        """When proof is missing, feedback is repeated, never terminal."""
        from aura.conversation.worker_finalization_gate import (
            handle_worker_candidate_finalization,
        )
        from aura.conversation.worker_final_report_guard import (
            worker_final_report_missing_proof,
        )

        state = MagicMock()
        state.worker_needs_final_report = False
        state.worker_flow = MagicMock()
        state.worker_flow.requires_validation_before_final.return_value = False
        state.worker_explicit_validation_passed = False
        state.import_verification_required = set()
        state.syntax_repair_required = {}
        state.edit_fallback_required = {}
        state.line_range_reread_required = {}
        state.patch_invalid_syntax_required = {}
        state.reject_all_for_turn = False

        # Make the candidate message look like it needs proof
        candidate = {"role": "assistant", "content": '{"status": "done", "summary": "Did the thing"}'}

        # Mock worker_final_report_missing_proof to always return True
        original_fn = worker_final_report_missing_proof

        call_count = [0]

        def always_missing_proof(*args, **kwargs):
            call_count[0] += 1
            return True

        monkeypatch.setattr(
            "aura.conversation.worker_finalization_gate.worker_final_report_missing_proof",
            always_missing_proof,
        )

        history = MagicMock()
        on_event = MagicMock()
        finish_fn = MagicMock()

        # First call — append feedback + continue
        result1 = handle_worker_candidate_finalization(
            state=state,
            full_message=candidate,
            history=history,
            workspace_root="/tmp",
            on_event=on_event,
            finish_worker_recoverable_followup=finish_fn,
        )
        assert result1 == "continue"
        assert history.append_user_text.call_count >= 1

        history.reset_mock()

        # Second call — same feedback, no terminal
        result2 = handle_worker_candidate_finalization(
            state=state,
            full_message=candidate,
            history=history,
            workspace_root="/tmp",
            on_event=on_event,
            finish_worker_recoverable_followup=finish_fn,
        )
        assert result2 == "continue"
        assert history.append_user_text.call_count >= 1
        assert finish_fn.call_count == 0


class TestArtifactRepeatedFailureNoExhaust:
    """4. WorkArtifact repeated same failure does not exhaust at three attempts.

    Same failure signature, same modified files set, at least four attempts.
    No recovery_exhausted, no WorkArtifact job recovery exhausted.
    """

    def test_artifact_retry_loop_no_stall_limit(self):
        """The artifact item retry loop no longer has _ARTIFACT_ITEM_STALL_LIMIT."""
        import aura.bridge.dispatch as dispatch_mod

        assert not hasattr(dispatch_mod._DispatchProxy, "_ARTIFACT_ITEM_STALL_LIMIT")

    def test_artifact_aggregate_no_recovery_exhausted(self):
        """_aggregate_artifact_results no longer emits recovery_exhausted."""
        from aura.bridge.dispatch import _DispatchProxy
        from aura.conversation.worker_outcome import WorkerOutcomeStatus
        from aura.conversation.dispatch import (
            WorkerDispatchResult,
            WorkerDispatchRequest,
        )

        proxy = _DispatchProxy.__new__(_DispatchProxy)
        proxy._artifact_controller = MagicMock()

        req = MagicMock(spec=WorkerDispatchRequest)
        item_results = [
            ("item_1", WorkerDispatchResult(ok=False, summary="failed")),
        ]
        result = proxy._aggregate_artifact_results(
            tool_call_id="test",
            approved_req=req,
            item_results=item_results,
            recovered_item_ids=[],
            failed_attempts={},
            total_items=1,
        )
        # Must NOT say "recovery exhausted"
        assert "recovery exhausted" not in result.summary.lower()
        extras = result.extras if isinstance(result.extras, dict) else {}
        assert extras.get("recovery_exhausted") is not True


class TestBridgeNoProgressNotNonRecoverable:
    """5. Bridge no-progress cannot become non-recoverable harness error.

    No touched files, no concrete blocker.
    Assert not recoverable=False / needs_followup=False / harness_error
    solely from no-progress.
    """

    def test_no_progress_is_continuation(self):
        """has_no_progress_failure in classifier produces recoverable=True."""
        from aura.bridge.worker_outcome_classifier import _classify_worker_completion

        relay = MagicMock()
        relay.touched_files = set()
        relay.write_results = []
        relay.failed_tool_results = []
        relay.api_errors = []
        relay.phase_boundary_info = None

        completion = {
            "final_report": "",
            "continuation": {},
            "write_failures": [],
            "source_inspection_blockers": [],
            "terminal_policy_blockers": [],
            "environment_setup_blockers": [],
            "failed_validation": [],
            "validation_not_run": True,
            "validation_command_issues": [],
            "diagnostic_environment_caveats": [],
            "acceptance_unverified": False,
            "is_implementation": True,
            "has_writes": False,
            "validation_results": [],
            "not_applied_writes": [],
            "unrecovered_not_applied_writes": [],
            "internal_recovery_steers": [],
            "behavioral_validation": {"skipped": [], "could_not_run": []},
        }
        messages = {
            "result_errors": [],
            "result_caveats": [],
            "structured_failure": {},
            "recoverable_write_failures": [],
            "failed_write_tools": [],
            "quality_findings": [],
        }

        outcome = _classify_worker_completion(
            relay=relay,
            completion=completion,
            messages=messages,
            internal_error=None,
        )
        assert outcome["recoverable"] is True, (
            f"No-progress should be recoverable, got recoverable={outcome['recoverable']}"
        )
        assert outcome["needs_followup"] is True, (
            f"No-progress should need followup, got needs_followup={outcome['needs_followup']}"
        )
        assert outcome["status"] != "harness_error", (
            f"No-progress should not be harness_error, got {outcome['status']}"
        )


class TestQualityNoTerminalAfterOneCleanup:
    """6. Quality gate does not terminalize after one cleanup attempt.

    Cleanup findings repeat, feedback repeats.
    No terminal finish unless concrete impossible/conflicting evidence exists.
    """

    def test_quality_hard_block_is_feedback_not_terminal(
        self, monkeypatch, tmp_path,
    ):
        """hard_block returns 'cleanup', appends feedback, never 'finished'."""
        from aura.conversation.worker_quality_gate import (
            handle_worker_quality_gate,
        )
        from aura.conversation.quality.models import (
            WorkerQualityDecision,
        )

        # Create .git so the gate proceeds past the early-exit check.
        (tmp_path / ".git").mkdir()

        decision = WorkerQualityDecision(
            ok=False,
            hard_block=True,
            needs_cleanup=True,
            instruction="Hard block: structural issues found.",
            findings=[],
        )
        monkeypatch.setattr(
            "aura.conversation.worker_quality_gate.evaluate_worker_quality",
            lambda *args, **kwargs: decision,
        )

        state = MagicMock()
        state.worker_quality_enabled = True
        state.worker_app_writes = {"test.py"}
        state.last_quality_ok_fingerprint = None
        state.dispatched_target_files = []
        state.last_quality_findings = []

        history = MagicMock()
        on_event = MagicMock()

        result = handle_worker_quality_gate(
            state=state,
            workspace_root=str(tmp_path),
            history=history,
            on_event=on_event,
        )

        assert result == "cleanup", (
            f"hard_block should return 'cleanup', got {result!r}"
        )
        assert result != "finished", (
            "hard_block must not return 'finished'"
        )
        assert history.append_user_text.call_count >= 1, (
            "append_user_text should have been called with the instruction"
        )


class TestRuntimeShapeRegression:
    """7. Runtime shape — WorkerFlowHarness and callers no longer have
    filter_tool_defs or should_block_tool.
    """

    def test_worker_flow_has_no_filter_tool_defs(self):
        """WorkerFlowHarness has no filter_tool_defs method."""
        flow = WorkerFlowHarness()
        assert not hasattr(flow, "filter_tool_defs"), (
            "WorkerFlowHarness should not have filter_tool_defs"
        )

    def test_worker_flow_has_no_should_block_tool(self):
        """WorkerFlowHarness has no should_block_tool method."""
        flow = WorkerFlowHarness()
        assert not hasattr(flow, "should_block_tool"), (
            "WorkerFlowHarness should not have should_block_tool"
        )

    def test_manager_py_no_filter_tool_defs(self):
        """manager.py should not reference filter_tool_defs."""
        import aura.conversation.manager as mgr
        import inspect
        source = inspect.getsource(mgr)
        assert "filter_tool_defs" not in source, (
            "manager.py must not reference filter_tool_defs"
        )

    def test_manager_tool_round_no_should_block_tool(self):
        """manager_tool_round.py should not reference should_block_tool."""
        import aura.conversation.manager_tool_round as mtr
        import inspect
        source = inspect.getsource(mtr)
        assert "should_block_tool" not in source, (
            "manager_tool_round.py must not reference should_block_tool"
        )

    def test_dispatch_no_recovery_exhausted(self):
        """dispatch.py should not reference recovery_exhausted."""
        import aura.bridge.dispatch as dispatch_mod
        source = inspect.getsource(dispatch_mod)
        assert "recovery_exhausted" not in source, (
            "dispatch.py must not reference recovery_exhausted"
        )

    def test_dispatch_has_retry_cap(self):
        """dispatch.py should have _ARTIFACT_ITEM_RETRY_CAP."""
        import aura.bridge.dispatch as dispatch_mod
        assert hasattr(dispatch_mod, "_ARTIFACT_ITEM_RETRY_CAP"), (
            "dispatch.py must define _ARTIFACT_ITEM_RETRY_CAP"
        )
