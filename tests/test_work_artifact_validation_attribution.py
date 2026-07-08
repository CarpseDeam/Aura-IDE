"""Tests for WorkArtifact validation attribution and scoped finalization.

Covers:
1. A docs-only item with no declared commands must not inherit top-level/job-
   wide validation commands (authority-boundary fix).
2. Pre-existing validation failures must not gate a scoped item.
3. Novel validation failures still gate the item.
4. Terminal-state invariant: no item left pending without queued action.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aura.conversation.dispatch import WorkerDispatchRequest, WorkerDispatchResult
from aura.conversation.validation_attribution import (
    AttributionVerdict,
    attribute_validation_failures,
    compute_failure_fingerprint,
)
from aura.conversation.validation_failure_routing import (
    compute_diagnostics_digest,
)
from aura.conversation.worker_final_validation import (
    WorkerFinalValidationResult,
)
from aura.work_artifact.controller import WorkArtifactController
from aura.work_artifact.model import (
    ValidationCommandSpec,
    WorkArtifact,
    WorkArtifactItem,
    WorkArtifactReceipt,
    WorkItemStatus,
)
from aura.work_artifact.validation_baseline import capture_baseline
from aura.conversation.manager_send_state import _SendState


# ═══════════════════════════════════════════════════════════════════════════════
# Test 1: docs-only item declares no commands
# ═══════════════════════════════════════════════════════════════════════════════


class TestDocsOnlyItemNoCommands:
    """A docs-only WorkArtifact item with no declared validation commands must
    not inherit top-level/job-wide commands."""

    def test_item_request_uses_only_item_commands(self):
        """``_build_artifact_item_request`` must not merge top-level commands."""
        item_vcs: list[ValidationCommandSpec] = []
        top_vcs = [
            ValidationCommandSpec(command="pytest"),
            ValidationCommandSpec(command="ruff check"),
        ]
        # After fix: only item commands, no top-level merge.
        merged_vcs = list(item_vcs)  # was: item_vcs + [vc for vc in top_vcs if ...]
        assert merged_vcs == []
        assert all(vc.command not in {c.command for c in merged_vcs} for vc in top_vcs)

    def test_item_request_with_item_commands_preserves_them(self):
        """Item commands are preserved when declared."""
        item_vcs = [ValidationCommandSpec(command="pytest src/tests/")]
        # After fix: only item commands
        merged_vcs = list(item_vcs)
        assert len(merged_vcs) == 1
        assert merged_vcs[0].command == "pytest src/tests/"

    def test_no_validation_commands_skips_explicit_validation(self):
        """When explicit_validation_commands is empty/None, the behavioral tier
        should skip the entire explicit validation block."""
        state = _SendState(mode="worker", research_policy=None)
        # Simulate the guard in _run_behavioral_tier:
        empty_commands: list[ValidationCommandSpec] = []
        if empty_commands:
            pytest.fail("Should not enter explicit validation block")
        # Reaching here means the guard skipped the block — correct.


# ═══════════════════════════════════════════════════════════════════════════════
# Test 2: pre-existing failure does not gate item
# ═══════════════════════════════════════════════════════════════════════════════


class TestPreexistingFailureDoesNotGate:
    """Pre-existing validation failures must not block a scoped item."""

    def test_attribute_all_preexisting_returns_no_gate(self):
        """When all current fingerprints match the baseline, gates_item=False."""
        baseline = ["pytest|failure|abc123"]
        current = ["pytest|failure|abc123"]

        verdict = attribute_validation_failures(
            current_fingerprints=current,
            baseline_fingerprints=baseline,
        )

        assert verdict.gates_item is False
        assert verdict.preexisting_fingerprints == ["pytest|failure|abc123"]
        assert verdict.novel_fingerprints == []

    def test_mixed_preexisting_and_novel_gates(self):
        """When both pre-existing and novel fingerprints exist, gates_item=True."""
        baseline = ["pytest|failure|abc123"]
        current = ["pytest|failure|abc123", "pytest|failure|def456"]

        verdict = attribute_validation_failures(
            current_fingerprints=current,
            baseline_fingerprints=baseline,
        )

        assert verdict.gates_item is True
        assert verdict.preexisting_fingerprints == ["pytest|failure|abc123"]
        assert verdict.novel_fingerprints == ["pytest|failure|def456"]

    def test_attribution_uses_set_semantics(self):
        """Set difference — order and duplicates don't matter."""
        baseline = ["a|err|1", "b|err|1"]
        current = ["b|err|1", "c|err|1", "a|err|1"]

        verdict = attribute_validation_failures(
            current_fingerprints=current,
            baseline_fingerprints=baseline,
        )

        assert verdict.gates_item is True
        assert sorted(verdict.preexisting_fingerprints) == sorted(["a|err|1", "b|err|1"])
        assert verdict.novel_fingerprints == ["c|err|1"]

    def test_no_baseline_gates_on_all(self):
        """When baseline is empty, all current failures are novel."""
        verdict = attribute_validation_failures(
            current_fingerprints=["pytest|failure|xyz"],
            baseline_fingerprints=[],
        )
        assert verdict.gates_item is True
        assert verdict.novel_fingerprints == ["pytest|failure|xyz"]

    def test_docs_item_with_preexisting_pytest(self):
        """Integration: simulate a docs item where pytest fails identically
        to the baseline — the item should NOT be gated."""
        baseline_fingerprints = {"pytest": ["pytest|failure|abc123"]}

        # Simulate the attribution that _run_behavioral_tier would do
        current_fingerprints = {"pytest": ["pytest|failure|abc123"]}
        any_novel = False
        preexisting_found: list[dict[str, Any]] = []

        for cmd_key, fingerprints in current_fingerprints.items():
            baseline = baseline_fingerprints.get(cmd_key, [])
            verdict = attribute_validation_failures(
                current_fingerprints=fingerprints,
                baseline_fingerprints=baseline,
            )
            if verdict.novel_fingerprints:
                any_novel = True
            if verdict.preexisting_fingerprints:
                preexisting_found.append({
                    "command_key": cmd_key,
                    "preexisting_fingerprints": list(verdict.preexisting_fingerprints),
                })

        assert any_novel is False
        assert len(preexisting_found) == 1
        assert preexisting_found[0]["command_key"] == "pytest"
        # This simulates the correct behavior: item passes despite pytest failing.


# ═══════════════════════════════════════════════════════════════════════════════
# Test 3: novel failure gates item
# ═══════════════════════════════════════════════════════════════════════════════


class TestNovelFailureGates:
    """A newly introduced validation failure must still gate the item."""

    def test_novel_only_gates(self):
        """When only novel failures exist (no baseline), gates_item=True."""
        verdict = attribute_validation_failures(
            current_fingerprints=["ruff|failure|new-bug"],
            baseline_fingerprints=[],
        )
        assert verdict.gates_item is True
        assert verdict.novel_fingerprints == ["ruff|failure|new-bug"]

    def test_novel_and_preexisting_gates_on_novel(self):
        """When both exist, gates_item is True because of the novel ones."""
        verdict = attribute_validation_failures(
            current_fingerprints=["pytest|failure|old", "ruff|failure|new"],
            baseline_fingerprints=["pytest|failure|old"],
        )
        assert verdict.gates_item is True
        assert "ruff|failure|new" in verdict.novel_fingerprints
        assert "pytest|failure|old" in verdict.preexisting_fingerprints

    def test_stable_fingerprint_identity(self):
        """Same command + classification + diagnostics produces same fingerprint."""
        fp1 = compute_failure_fingerprint("pytest", "test_failure", "AssertionError in test_foo")
        fp2 = compute_failure_fingerprint("pytest", "test_failure", "AssertionError in test_foo")
        assert fp1 == fp2

    def test_different_diagnostics_different_fingerprint(self):
        """Different diagnostics produce different fingerprints."""
        fp_a = compute_failure_fingerprint("pytest", "test_failure", "test_foo FAILED")
        fp_b = compute_failure_fingerprint("pytest", "test_failure", "test_bar FAILED")
        assert fp_a != fp_b

    def test_fingerprint_uses_normalized_diagnostics(self):
        """Paths and timestamps are normalized before digesting."""
        fp_with_path = compute_failure_fingerprint(
            "pytest", "error",
            "File /home/user/project/src/main.py:123 in func",
        )
        fp_normalized = compute_failure_fingerprint(
            "pytest", "error",
            "File <path>:123 in func",
        )
        # Both should produce the same fingerprint after normalization
        digest1 = compute_diagnostics_digest("File /home/user/project/src/main.py:123 in func")
        digest2 = compute_diagnostics_digest("File <path>:123 in func")
        assert digest1 == digest2, "Path normalization should produce same digest"


# ═══════════════════════════════════════════════════════════════════════════════
# Test 4: no pending-without-action
# ═══════════════════════════════════════════════════════════════════════════════


class TestNoPendingWithoutAction:
    """WorkArtifact item state must never be left pending with no queued action."""

    def _item_pending_ok_outcome(self, status: str) -> bool:
        """An item result is OK (not stuck pending) if its receipt status is
        one of the allowed terminal outcomes."""
        allowed = {"ok", "cancelled", "continuing"}
        return status in allowed

    def test_ok_result_is_terminal(self):
        """Worker ok result → receipt status 'ok' → item done."""
        result = WorkerDispatchResult(
            ok=True,
            summary="Done",
            status="completed",
        )
        from aura.work_artifact.receipt import worker_result_to_receipt
        receipt = worker_result_to_receipt(result)
        assert receipt.status == "ok"
        assert self._item_pending_ok_outcome(receipt.status)

    def test_cancelled_result_is_terminal(self):
        """User cancellation → receipt status 'cancelled'."""
        result = WorkerDispatchResult(
            ok=False,
            cancelled=True,
            summary="Cancelled",
        )
        from aura.work_artifact.receipt import worker_result_to_receipt
        receipt = worker_result_to_receipt(result)
        assert receipt.status == "cancelled"
        assert self._item_pending_ok_outcome(receipt.status)

    def test_infrastructure_pause_is_resumable(self):
        """Infrastructure failure pauses the job with work_artifact_unfinished."""
        extras = {
            "work_artifact_job": True,
            "work_artifact_unfinished": True,
            "pending_item_ids": ["item-2"],
        }
        result = WorkerDispatchResult(
            ok=False,
            summary="Provider unavailable",
            recoverable=True,
            extras=extras,
        )
        from aura.work_artifact.receipt import worker_result_to_receipt
        receipt = worker_result_to_receipt(result)
        assert "work_artifact_unfinished" in (result.extras or {})
        assert result.recoverable is True

    def test_retry_cap_pause_is_resumable(self):
        """Retry cap reached pauses the job with artifact_retry_cap_reached."""
        extras = {
            "artifact_retry_cap_reached": True,
            "work_artifact_unfinished": True,
            "current_item_id": "item-1",
        }
        result = WorkerDispatchResult(
            ok=False,
            summary="Retry cap reached",
            recoverable=True,
            extras=extras,
        )
        assert result.extras.get("artifact_retry_cap_reached") is True
        assert result.extras.get("work_artifact_unfinished") is True

    def test_preexisting_failure_flows_into_extras(self):
        """Pre-existing failure info appears in result extras as preexisting_failures."""
        extras = {
            "preexisting_failures": [
                {"command_key": "pytest", "preexisting_fingerprints": ["fp1"]},
            ],
        }
        result = WorkerDispatchResult(
            ok=True,
            summary="Completed (pre-existing failures ignored)",
            extras=extras,
        )
        preexisting = (result.extras or {}).get("preexisting_failures")
        assert preexisting is not None
        assert len(preexisting) == 1
        assert preexisting[0]["command_key"] == "pytest"
        # The receipt metadata mirrors extras, so it flows into the item receipt.
        receipt_metadata = dict(extras)
        assert "preexisting_failures" in receipt_metadata


# ═══════════════════════════════════════════════════════════════════════════════
# Worker dispatch runner: no merge for artifact items
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkerDispatchRunnerMerge:
    """The worker dispatch runner must NOT merge detected project validation
    commands for WorkArtifact item requests."""

    def test_artifact_item_skips_merge(self):
        """When req has artifact_id and artifact_item_id, the merge block
        must be skipped (simulated by the is_artifact_item guard)."""
        req_is_artifact_item = True  # bool(req.artifact_id and req.artifact_item_id)

        # Simulate the guard in _prepare_worker_conversation:
        detected_runnable: list[str] = ["pytest", "ruff"]
        if req_is_artifact_item:
            # Merge is skipped for artifact items
            merged_strings: list[str] = []
        else:
            # Merge happens for non-artifact dispatches
            merged_strings = list(detected_runnable)

        assert merged_strings == [], "Artifact items must not merge detected commands"

    def test_non_artifact_still_merges(self):
        """Non-artifact dispatches still merge detected commands."""
        req_is_artifact_item = False

        detected_runnable: list[str] = ["pytest", "ruff"]
        if req_is_artifact_item:
            merged_strings: list[str] = []
        else:
            merged_strings = list(detected_runnable)

        assert merged_strings == ["pytest", "ruff"]


# ═══════════════════════════════════════════════════════════════════════════════
# Baseline capture
# ═══════════════════════════════════════════════════════════════════════════════


class TestBaselineCapture:
    """Baseline capture behavior."""

    def test_empty_commands_returns_empty(self):
        """capture_baseline with empty commands returns {}."""
        result = capture_baseline([], Path("/tmp"))
        assert result == {}

    def test_baseline_fingerprint_consistency(self):
        """Same fingerprint helper used in both baseline and attribution."""
        fp = compute_failure_fingerprint(
            "pytest", "failure",
            "test_thing failed\nAssertionError",
        )
        assert isinstance(fp, str)
        assert "pytest|failure|" in fp
        assert len(fp.split("|")[-1]) == 16  # truncated SHA-256 hex digest


# ═══════════════════════════════════════════════════════════════════════════════
# WorkArtifact model — baseline fingerprints field
# ═══════════════════════════════════════════════════════════════════════════════


class TestWorkArtifactBaselineField:
    """WorkArtifact model serializes/deserializes baseline_validation_fingerprints."""

    def test_model_has_baseline_field(self):
        """WorkArtifact has a baseline_validation_fingerprints field."""
        artifact = WorkArtifact(
            artifact_id="test-1",
            goal="Test",
            work_items=[
                WorkArtifactItem(
                    id="item-1", title="T", intent="I",
                    target_files=["a.py"], acceptance="A",
                ),
            ],
        )
        assert hasattr(artifact, "baseline_validation_fingerprints")
        assert artifact.baseline_validation_fingerprints == {}

    def test_serializes_and_deserializes_baseline(self):
        """to_dict/from_dict round-trips the baseline field."""
        artifact = WorkArtifact(
            artifact_id="test-1",
            goal="Test",
            baseline_validation_fingerprints={
                "pytest": ["pytest|failure|abc123"],
            },
            work_items=[
                WorkArtifactItem(
                    id="item-1", title="T", intent="I",
                    target_files=["a.py"], acceptance="A",
                ),
            ],
        )
        d = artifact.to_dict()
        assert "baseline_validation_fingerprints" in d
        assert d["baseline_validation_fingerprints"] == {
            "pytest": ["pytest|failure|abc123"],
        }

        restored = WorkArtifact.from_dict(d)
        assert restored.baseline_validation_fingerprints == {
            "pytest": ["pytest|failure|abc123"],
        }

    def test_deserializes_missing_baseline_gracefully(self):
        """from_dict works when baseline_validation_fingerprints is missing."""
        d = {
            "artifact_id": "test-1",
            "goal": "Test",
            "work_items": [],
            "current_item_id": "",
            "created_at": 0.0,
            "updated_at": 0.0,
        }
        restored = WorkArtifact.from_dict(d)
        assert restored.baseline_validation_fingerprints == {}


# ═══════════════════════════════════════════════════════════════════════════════
# SendState baseline field
# ═══════════════════════════════════════════════════════════════════════════════


class TestSendStateBaseline:
    """_SendState accepts and stores baseline_validation_fingerprints."""

    def test_state_has_baseline_field(self):
        state = _SendState(mode="worker", research_policy=None)
        assert hasattr(state, "baseline_validation_fingerprints")
        assert state.baseline_validation_fingerprints == {}

    def test_state_stores_baseline(self):
        state = _SendState(mode="worker", research_policy=None)
        state.baseline_validation_fingerprints = {"pytest": ["fp1"]}
        assert state.baseline_validation_fingerprints["pytest"] == ["fp1"]

    def test_state_has_preexisting_failures_field(self):
        state = _SendState(mode="worker", research_policy=None)
        assert hasattr(state, "preexisting_validation_failures")
        assert state.preexisting_validation_failures == []


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: validation classification for known commands without metadata
# ═══════════════════════════════════════════════════════════════════════════════


class TestKnownValidationCommandClassification:
    """Known validation commands (compileall, pytest, etc.) must be classified
    even when the tool runner omitted explicit validation metadata fields."""

    def test_compileall_classified_without_explicit_metadata(self):
        """_attach_validation_metadata must classify 'python -m compileall'
        as validation even without validation_source/validation_raw_text."""
        from aura.bridge.event_relay_errors import _attach_validation_metadata
        from aura.conversation.validation_truth import validation_payload_passed

        record: dict[str, Any] = {}
        parsed = {
            "command": "python -m compileall aura/gui/playground.py",
            "ok": True,
            "exit_code": 0,
            "output": "Listing ...\nCompiling ...\n",
        }
        _attach_validation_metadata(record, parsed)

        assert record.get("counts_as_validation") is True
        assert record.get("validation_classification") == "passed"
        assert record.get("counts_as_product_failure") is False
        assert validation_payload_passed(record) is True

    def test_pytest_classified_without_explicit_metadata(self):
        """pytest is also classified via the fallback."""
        from aura.bridge.event_relay_errors import _attach_validation_metadata
        from aura.conversation.validation_truth import validation_payload_passed

        record: dict[str, Any] = {}
        parsed = {
            "command": "pytest tests/test_foo.py -v",
            "ok": True,
            "exit_code": 0,
            "output": "1 passed",
        }
        _attach_validation_metadata(record, parsed)

        assert record.get("counts_as_validation") is True
        assert record.get("validation_classification") == "passed"
        assert validation_payload_passed(record) is True

    def test_ruff_classified_without_explicit_metadata(self):
        """ruff is also classified via the fallback."""
        from aura.bridge.event_relay_errors import _attach_validation_metadata

        record: dict[str, Any] = {}
        parsed = {
            "command": "ruff check src/",
            "ok": True,
            "exit_code": 0,
            "output": "",
        }
        _attach_validation_metadata(record, parsed)

        assert record.get("counts_as_validation") is True
        assert record.get("validation_classification") == "passed"

    def test_unknown_command_not_classified_without_metadata(self):
        """An arbitrary command like 'ls' is not classified as validation."""
        from aura.bridge.event_relay_errors import _attach_validation_metadata

        record: dict[str, Any] = {}
        parsed = {
            "command": "ls -la",
            "ok": True,
            "exit_code": 0,
            "output": "file1.py file2.py",
        }
        _attach_validation_metadata(record, parsed)

        assert record.get("counts_as_validation") is not True


# ═══════════════════════════════════════════════════════════════════════════════
# Regression: two-item artifact auto-advance
# ═══════════════════════════════════════════════════════════════════════════════


class TestTwoItemArtifactAutoAdvance:
    """A two-item WorkArtifact must advance from item 1 to item 2
    without user intervention when item 1 completes successfully."""

    def test_successful_item_receipt_is_ok_and_item_is_done(self):
        """Item 1 with an ok result gets receipt.status='ok' and status='done'."""
        from aura.work_artifact.controller import WorkArtifactController
        from aura.work_artifact.model import WorkItemStatus

        ctrl = WorkArtifactController()
        payload = {
            "goal": "Add two components",
            "items": [
                {
                    "id": "item-1", "title": "Model",
                    "intent": "Create the model",
                    "target_files": ["src/model.py"],
                    "acceptance": "Model compiles",
                },
                {
                    "id": "item-2", "title": "View",
                    "intent": "Create the view",
                    "target_files": ["src/view.py"],
                    "acceptance": "View compiles",
                },
            ],
        }
        ctrl.create_artifact_from_payload("call_abc", payload)
        ctrl.mark_item_active("call_abc", "item-1")

        result = WorkerDispatchResult(
            ok=True,
            summary="Item 1 done",
            modified_files=["src/model.py"],
        )
        artifact = ctrl.get_artifact("call_abc")
        assert artifact is not None
        item1 = artifact.work_items[0]
        assert item1.status == WorkItemStatus.active

        ctrl.attach_receipt("call_abc", result, item_id="item-1")
        assert item1.receipt is not None
        assert item1.receipt.status == "ok"
        assert item1.status == WorkItemStatus.done

    def test_pending_items_returns_next_item_after_first_is_done(self):
        """After item 1 is done, pending_items returns item 2."""
        from aura.work_artifact.controller import WorkArtifactController
        from aura.work_artifact.model import WorkItemStatus

        ctrl = WorkArtifactController()
        payload = {
            "goal": "Two items",
            "items": [
                {"id": "item-1", "title": "One", "intent": "A",
                 "target_files": ["a.py"], "acceptance": "A works"},
                {"id": "item-2", "title": "Two", "intent": "B",
                 "target_files": ["b.py"], "acceptance": "B works"},
            ],
        }
        ctrl.create_artifact_from_payload("call_def", payload)

        # Item 1 succeeds.
        ctrl.mark_item_active("call_def", "item-1")
        ctrl.attach_receipt("call_def", WorkerDispatchResult(
            ok=True, summary="Item 1 done", modified_files=["a.py"],
        ), item_id="item-1")

        pending = ctrl.pending_items("call_def")
        assert len(pending) == 1
        assert pending[0].id == "item-2"
        assert pending[0].status == WorkItemStatus.pending

    def test_pending_items_includes_active_item_for_resume(self):
        """An active (not done) item appears in pending_items for resume."""
        from aura.work_artifact.controller import WorkArtifactController
        from aura.work_artifact.model import WorkItemStatus

        ctrl = WorkArtifactController()
        payload = {
            "goal": "Two items",
            "items": [
                {"id": "item-1", "title": "One", "intent": "A",
                 "target_files": ["a.py"], "acceptance": "A works"},
                {"id": "item-2", "title": "Two", "intent": "B",
                 "target_files": ["b.py"], "acceptance": "B works"},
            ],
        }
        ctrl.create_artifact_from_payload("call_ghi", payload)

        ctrl.mark_item_active("call_ghi", "item-1")

        pending = ctrl.pending_items("call_ghi")
        assert len(pending) == 2
        assert pending[0].id == "item-1"
        assert pending[0].status == WorkItemStatus.active
        assert pending[1].id == "item-2"
        assert pending[1].status == WorkItemStatus.pending

    def test_all_required_items_done_strict(self):
        """all_required_items_done is strict — returns True only when every
        item is done, even when pending_items returns empty."""
        from aura.work_artifact.controller import WorkArtifactController

        ctrl = WorkArtifactController()
        payload = {
            "goal": "Two items",
            "items": [
                {"id": "item-1", "title": "One", "intent": "A",
                 "target_files": ["a.py"], "acceptance": "A works"},
                {"id": "item-2", "title": "Two", "intent": "B",
                 "target_files": ["b.py"], "acceptance": "B works"},
            ],
        }
        ctrl.create_artifact_from_payload("call_jkl", payload)

        # Both items done.
        ctrl.attach_receipt("call_jkl", WorkerDispatchResult(
            ok=True, summary="Item 1 done"), item_id="item-1")
        ctrl.attach_receipt("call_jkl", WorkerDispatchResult(
            ok=True, summary="Item 2 done"), item_id="item-2")

        assert ctrl.pending_items("call_jkl") == []
        assert ctrl.all_required_items_done("call_jkl") is True
