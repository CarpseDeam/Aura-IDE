"""Completion truth: a production-action turn reports completed only when one
truthful terminal outcome occurred.

The contract lives in :func:`build_production_receipt`.  A turn that bears a
production action may end ``completed`` only through:

* an applied implementation — at least one authoritative mutation result with
  ``applied is True``, required validation attempted, none failing;
* structured already-satisfied evidence — the requested state already exists,
  recorded explicitly, never inferred from the absence of a write and never
  from assistant prose; or
* a blocker.

A production-action turn that ends with none of those is reported as
``no_authoritative_change`` — never as completed — with a clear reason for the
GUI receipt and logs.  Read-only and genuinely non-production turns keep their
historical completion behaviour.
"""

from __future__ import annotations

import json

import pytest

from aura.bridge.event_relay_write_tracking import _file_mutation_was_applied
from aura.bridge.production_execution import ProductionExecutionSession
from aura.bridge.production_receipt import (
    STATUS_BLOCKED,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_COMPLETED_UNVERIFIED,
    STATUS_NO_AUTHORITATIVE_CHANGE,
    STATUS_PROVIDER_CONTRACT_FAILURE,
    STATUS_VALIDATION_FAILED,
    ProductionRunEvidence,
    build_production_receipt,
)
from aura.client import ToolResult
from aura.conversation.manager import ConversationManager
from aura.conversation.task_router import TaskLane, TaskRoute
from tests.production_loop_harness import (
    Recorder,
    build_manager,
    final_round,
)

CHAT_ROUTE = TaskRoute(
    lane=TaskLane.chat, action="chat", confidence=0.6, reason="no trigger"
)


def _evidence(**overrides) -> ProductionRunEvidence:
    kwargs: dict = {
        "run_id": "prod-x",
        "bears_production_action": True,
    }
    kwargs.update(overrides)
    return ProductionRunEvidence(**kwargs)


def _passed_validation() -> dict:
    return {
        "command": "python -m pytest",
        "exit_code": 0,
        "validation_ok": True,
        "counts_as_validation": True,
    }


def _failed_validation() -> dict:
    return {
        "command": "python -m pytest",
        "exit_code": 1,
        "validation_ok": False,
        "counts_as_validation": True,
        "counts_as_product_failure": True,
    }


def _applied_write() -> dict:
    return {"path": "a.py", "applied": True}


# ── 1-3: applied implementation, failing validation, no proof ───────────────


class TestAppliedImplementation:
    def test_write_with_passing_validation_succeeds(self) -> None:
        receipt = build_production_receipt(_evidence(
            write_results=[_applied_write()],
            validation_results=[_passed_validation()],
        ))
        assert receipt.ok is True
        assert receipt.status == STATUS_COMPLETED
        assert receipt.needs_followup is False

    def test_write_with_failing_validation_does_not_succeed(self) -> None:
        receipt = build_production_receipt(_evidence(
            write_results=[_applied_write()],
            validation_results=[_failed_validation()],
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_VALIDATION_FAILED
        assert receipt.needs_followup is True

    def test_write_without_validation_is_still_unverified(self) -> None:
        receipt = build_production_receipt(_evidence(
            write_results=[_applied_write()],
        ))
        assert receipt.status == STATUS_COMPLETED_UNVERIFIED
        assert receipt.ok is True

    def test_no_write_no_proof_does_not_succeed(self) -> None:
        receipt = build_production_receipt(_evidence(
            final_response="All done!",
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.needs_followup is True
        assert "no applied write" in receipt.text


# ── 4-5: structured already-satisfied vs assistant prose ────────────────────


class TestAlreadySatisfied:
    def test_explicit_already_satisfied_evidence_succeeds(self) -> None:
        receipt = build_production_receipt(_evidence(
            already_satisfied=True,
            final_response="alpha.py already contains the fix.",
        ))
        assert receipt.ok is True
        assert receipt.status == STATUS_COMPLETED
        assert receipt.metadata["already_satisfied"] is True
        assert "Already satisfied" in receipt.text

    def test_already_satisfied_never_inferred_from_no_write(self) -> None:
        """No write, no structured flag — the flag is never assumed."""
        receipt = build_production_receipt(_evidence(
            final_response="No changes were needed.",
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE

    def test_assistant_prose_claiming_success_without_evidence_fails(self) -> None:
        receipt = build_production_receipt(_evidence(
            final_response="Everything already works, nothing to change.",
        ))
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.ok is False


# ── 6-8: blockers, cancellation, provider-contract precedence ───────────────


class TestPrecedence:
    def test_blocker_remains_blocked(self) -> None:
        receipt = build_production_receipt(_evidence(
            blocked_reason="pytest is not installed",
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_BLOCKED
        assert receipt.needs_followup is True

    def test_cancellation_remains_cancelled(self) -> None:
        receipt = build_production_receipt(_evidence(
            cancelled=True,
            write_results=[_applied_write()],
            validation_results=[_passed_validation()],
        ))
        assert receipt.ok is False
        assert receipt.status == STATUS_CANCELLED

    def test_provider_contract_failure_retains_precedence(self) -> None:
        receipt = build_production_receipt(_evidence(
            provider_contract_failure=True,
            write_results=[_applied_write()],
            validation_results=[_passed_validation()],
        ))
        assert receipt.status == STATUS_PROVIDER_CONTRACT_FAILURE
        assert receipt.ok is False

    def test_failed_validation_outranks_already_satisfied(self) -> None:
        receipt = build_production_receipt(_evidence(
            already_satisfied=True,
            validation_results=[_failed_validation()],
        ))
        assert receipt.status == STATUS_VALIDATION_FAILED


# ── 9: read-only turns keep their completion behaviour ──────────────────────


class TestReadOnlyAndNonProduction:
    def test_read_only_turn_can_complete_without_mutation(self) -> None:
        """A read-only turn bears no production action, so a mutation-free
        answer completes exactly as it always did."""
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            bears_production_action=False,
            final_response="Here is what I found.",
        ))
        assert receipt.ok is True
        assert receipt.status == STATUS_COMPLETED

    def test_non_production_receipt_unchanged(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            bears_production_action=False,
            final_response="Aura lives in aura/settings.py.",
        ))
        assert receipt.ok is True
        assert receipt.status == STATUS_COMPLETED
        assert receipt.metadata["already_satisfied"] is False
        assert receipt.metadata["bears_production_action"] is False

    def test_non_production_writes_stay_unverified(self) -> None:
        receipt = build_production_receipt(ProductionRunEvidence(
            run_id="prod-x",
            bears_production_action=False,
            write_results=[_applied_write()],
        ))
        assert receipt.status == STATUS_COMPLETED_UNVERIFIED


# ── 10-11: failed writes and synthetic cancellations never count ────────────


class TestNoFalseExecutionCredit:
    def test_a_failed_write_does_not_count_as_applied(self) -> None:
        """A write tool result that explicitly says not-applied is not a write."""
        assert _file_mutation_was_applied(
            "write_file",
            ok=True,
            parsed={"ok": True, "applied": False, "path": "a.py",
                    "write_outcome": "not_applied_edit_mechanics_blocked"},
            extras={},
        ) is False

    def test_a_rejected_write_does_not_make_a_no_write_turn_completed(self) -> None:
        receipt = build_production_receipt(_evidence(
            not_applied_writes=[{"path": "a.py", "failure_class": "rejected"}],
        ))
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.ok is False
        assert receipt.metadata["not_applied_writes"] == ["a.py"]

    def test_synthetic_cancellation_result_is_not_an_applied_write(self) -> None:
        payload = {
            "ok": False,
            "cancelled": True,
            "recoverable": False,
            "failure_class": "cancelled",
            "execution_status": "interrupted_before_authoritative_result",
            "tool": "write_file",
            "message": (
                "Cancelled before Aura received an authoritative result. Do not "
                "infer that the operation completed or that a mutation applied."
            ),
        }
        assert _file_mutation_was_applied(
            "write_file", ok=False, parsed=payload, extras={}
        ) is False
        assert "applied" not in payload, "synthetic results never set applied"

    def test_synthetic_cancellation_does_not_produce_a_completed_receipt(self) -> None:
        receipt = build_production_receipt(_evidence(
            failed_tool_results=[{
                "name": "write_file",
                "ok": False,
                "cancelled": True,
                "failure_class": "cancelled",
            }],
        ))
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.ok is False


# ── session ledger: the receipt inputs are fed by the execution ledger ──────


@pytest.fixture
def approval_proxy():
    class _Proxy:
        def consume_last_event(self):
            return None
    return _Proxy()


@pytest.fixture
def session(qapp, approval_proxy) -> ProductionExecutionSession:
    return ProductionExecutionSession(approval_proxy=approval_proxy)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    return app


class TestSessionLedger:
    def test_failed_write_lands_in_not_applied_not_write_results(
        self, session
    ) -> None:
        session.begin(model="m")
        session.handle_event(ToolResult(
            tool_call_id="w1",
            name="write_file",
            ok=False,
            result=json.dumps({
                "ok": False,
                "applied": False,
                "path": "a.py",
                "write_outcome": "not_applied_edit_mechanics_blocked",
            }),
            extras={},
        ))
        evidence = session.evidence()
        assert evidence.write_results == []
        assert [w["path"] for w in evidence.not_applied_writes] == ["a.py"]
        receipt = session.finish(bears_production_action=True)
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.ok is False

    def test_synthetic_cancellation_result_counts_as_neither_success_nor_write(
        self, session
    ) -> None:
        session.begin(model="m")
        session.handle_event(ToolResult(
            tool_call_id="w1",
            name="write_file",
            ok=False,
            result=json.dumps({
                "ok": False,
                "cancelled": True,
                "recoverable": False,
                "failure_class": "cancelled",
                "execution_status": "interrupted_before_authoritative_result",
                "tool": "write_file",
            }),
            extras={},
        ))
        evidence = session.evidence()
        assert evidence.write_results == []
        assert evidence.not_applied_writes == []
        receipt = session.finish(bears_production_action=True)
        assert receipt.status == STATUS_NO_AUTHORITATIVE_CHANGE
        assert receipt.ok is False


# ── the manager records whether the turn bore a production action ───────────


@pytest.fixture
def isolated_streams(monkeypatch) -> "object":
    import aura.conversation.manager as manager_module
    from aura.model_streams import ModelStreamRegistry

    registry = ModelStreamRegistry()
    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


def _run_turn(manager: ConversationManager, route, isolated_streams, script) -> None:
    import threading

    from aura.model_streams import PRODUCTION_STREAM_HOOK
    from tests.production_loop_harness import ScriptedBackend

    backend = ScriptedBackend(script)
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
    manager.send(
        on_event=Recorder(),
        approval_cb=lambda _r: None,
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="high",
        hook_name=PRODUCTION_STREAM_HOOK,
        task_route=route,
    )


class TestManagerProductionActionFlag:
    def test_implementation_turn_bears_a_production_action(
        self, tmp_path, isolated_streams
    ) -> None:
        from tests.production_loop_harness import IMPLEMENTATION_ROUTE, make_workspace

        workspace = make_workspace(tmp_path / "ws")
        manager = build_manager(workspace, "Update notes.md.")
        _run_turn(manager, IMPLEMENTATION_ROUTE, isolated_streams, [final_round("Nothing to change.")])
        assert manager.last_turn_bears_production_action is True

    def test_read_only_registry_turn_bears_no_production_action(
        self, tmp_path, isolated_streams
    ) -> None:
        from tests.production_loop_harness import IMPLEMENTATION_ROUTE, make_workspace

        workspace = make_workspace(tmp_path / "ws")
        manager = build_manager(workspace, "Update notes.md.", read_only=True)
        _run_turn(manager, IMPLEMENTATION_ROUTE, isolated_streams, [final_round("Ok.")])
        assert manager.last_turn_bears_production_action is False

    def test_chat_turn_bears_no_production_action(
        self, tmp_path, isolated_streams
    ) -> None:
        from tests.production_loop_harness import make_workspace

        workspace = make_workspace(tmp_path / "ws")
        manager = build_manager(workspace, "Where do settings live?")
        _run_turn(manager, CHAT_ROUTE, isolated_streams, [final_round("aura/settings.py.")])
        assert manager.last_turn_bears_production_action is False
