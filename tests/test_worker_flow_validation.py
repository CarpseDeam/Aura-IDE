"""Focused tests for WorkerFlow validation satisfaction — Phase 1.

Verifies that ``WorkerFlowHarness.observe_tool_result`` uses
``validation_payload_passed`` for validation tools, not raw ``ok``.
"""
from __future__ import annotations

from aura.conversation.worker_flow import WorkerFlowHarness


class TestWorkerFlowValidationSatisfaction:
    """WorkerFlow must not mark validation satisfied from raw terminal
    ok alone — it must use the validation truth helper."""

    def _make_flow(self) -> WorkerFlowHarness:
        flow = WorkerFlowHarness()
        flow.state.validation_required_before_final = True
        return flow

    # ── Pass scenarios ───────────────────────────────────────────────────

    def test_passed_with_command_outcome_classification(self):
        """A validation tool result with ``command_outcome_classification``
        = "passed" and ``counts_as_validation`` = True marks satisfied."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "run_terminal_command",
            ok=True,
            result={
                "ok": True,
                "exit_code": 0,
                "command": "pytest",
                "command_outcome_classification": "passed",
                "command_success": True,
                "counts_as_validation": True,
                "counts_as_product_failure": False,
            },
        )
        assert not flow.requires_validation_before_final()

    def test_passed_with_validation_classification(self):
        """A validation tool result with ``validation_classification``
        = "passed" marks satisfied (event-relay path)."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "run_terminal_command",
            ok=True,
            result={
                "ok": True,
                "exit_code": 0,
                "command": "pytest",
                "validation_classification": "passed",
                "classification": "passed",
                "counts_as_validation": True,
                "counts_as_product_failure": False,
            },
        )
        assert not flow.requires_validation_before_final()

    # ── Raw ok=True alone is never enough ────────────────────────────────

    def test_raw_ok_alone_does_not_satisfy(self):
        """Raw terminal ``ok=True`` with ``exit_code=0`` but NO validation
        metadata must NOT mark validation satisfied."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "run_terminal_command",
            ok=True,
            result={
                "ok": True,
                "exit_code": 0,
                "command": "pytest",
                "output": "passed",
            },
        )
        assert flow.requires_validation_before_final()

    def test_exit_code_1_with_ok_true_does_not_satisfy(self):
        """``exit_code=1`` with ``ok=True`` (terminal execution succeeded
        but the command failed) must NOT mark validation satisfied."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "run_terminal_command",
            ok=True,
            result={
                "ok": True,
                "exit_code": 1,
                "command": "pytest",
                "validation_classification": "product_validation_failed",
                "classification": "product_validation_failed",
                "counts_as_validation": True,
                "counts_as_product_failure": True,
            },
        )
        assert flow.requires_validation_before_final()

    def test_non_validation_tool_not_affected(self):
        """Non-validation tools (write_file) should still work normally,
        unaffected by the change."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "write_file",
            ok=True,
            args={"path": "a.py"},
            result={"ok": True, "path": "a.py", "applied": True},
        )
        assert flow.requires_validation_before_final()  # writes set this

    # ── run_and_watch also uses truth helper ─────────────────────────────

    def test_run_and_watch_uses_truth_helper(self):
        """run_and_watch is in VALIDATION_TOOLS and must also use
        ``validation_payload_passed``."""
        flow = self._make_flow()
        flow.observe_tool_result(
            "run_and_watch",
            ok=True,
            result={
                "ok": True,
                "exit_code": 0,
                "command": "python -m aura --selfcheck",
                "command_outcome_classification": "passed",
                "command_success": True,
                "counts_as_validation": True,
                "counts_as_product_failure": False,
            },
        )
        assert not flow.requires_validation_before_final()
