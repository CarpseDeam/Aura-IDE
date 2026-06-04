"""Tests for aura.conversation.dispatch — WorkerDispatchRequest/Result."""

from __future__ import annotations

from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
    WorkerOutcomeStatus,
    infer_outcome_status,
    normalize_outcome_status,
)
from aura.bridge.dispatch import (
    _applied_modified_files,
    _build_worker_summary,
    _compute_outcome_status,
    _final_report_claims_failure,
    _final_report_claims_validation,
    _format_spec_as_user_message,
    _unrecovered_validation_failures,
    _validation_results_for_task,
)
from aura.conversation.history import History


# WorkerDispatchRequest

def test_dispatch_request_to_from_dict_roundtrip():
    original = WorkerDispatchRequest(
        goal="Add docstrings",
        files=["module.py", "utils.py"],
        spec="Add Google-style docstrings to all public functions.",
        acceptance="All public functions have docstrings.",
    )
    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)
    assert restored.goal == original.goal
    assert restored.files == original.files
    assert restored.spec == original.spec
    assert restored.acceptance == original.acceptance


def test_dispatch_request_from_dict_missing_fields():
    """Missing fields should default to empty strings/lists."""
    req = WorkerDispatchRequest.from_dict({})
    assert req.goal == ""
    assert req.files == []
    assert req.spec == ""
    assert req.acceptance == ""


def test_dispatch_request_from_dict_files_not_list():
    """If 'files' is not a list, should default to empty list."""
    req = WorkerDispatchRequest.from_dict({"files": "not_a_list"})
    assert req.files == []


def test_dispatch_request_from_dict_files_with_non_strings():
    """Non-string entries in files should be converted to strings."""
    req = WorkerDispatchRequest.from_dict({"files": [123, None, "ok.py"]})
    assert req.files == ["123", "None", "ok.py"]


# WorkerDispatchResult

def test_dispatch_result_success():
    result = WorkerDispatchResult(ok=True, summary="Done.", cancelled=False)
    assert result.ok is True
    assert result.summary == "Done."
    assert result.cancelled is False


def test_dispatch_result_cancelled():
    result = WorkerDispatchResult(ok=False, summary="User cancelled.", cancelled=True)
    assert result.ok is False
    assert result.cancelled is True


def test_dispatch_result_to_tool_payload():
    result = WorkerDispatchResult(ok=True, summary="Success", cancelled=False)
    payload = result.to_tool_payload()
    assert payload == {"ok": True, "cancelled": False, "summary": "Success"}


def test_dispatch_result_to_tool_payload_with_extras():
    result = WorkerDispatchResult(
        ok=True, summary="Done", cancelled=False,
        extras={"diff": "+added line", "commit": "abc1234"},
    )
    payload = result.to_tool_payload()
    assert payload["ok"] is True
    assert payload["extras"]["diff"] == "+added line"
    assert payload["extras"]["commit"] == "abc1234"


def test_dispatch_result_to_tool_payload_empty_extras():
    """Empty extras dict should not appear in payload."""
    result = WorkerDispatchResult(ok=True, summary="Done", cancelled=False, extras={})
    payload = result.to_tool_payload()
    assert "extras" not in payload


def test_dispatch_result_followup_fields_roundtrip():
    result = WorkerDispatchResult(
        ok=False,
        summary="Worker reached the pass limit.",
        needs_followup=True,
        phase_boundary=True,
        followup_reason="worker_tool_call_limit_reached",
        recoverable=True,
        completed=["Read target files"],
        remaining=["Run validation"],
        modified_files=["aura/example.py"],
        validation="Not run",
        suggested_next_spec="Run a validation-only pass.",
    )

    payload = result.to_tool_payload()
    restored = WorkerDispatchResult.from_tool_payload(payload)

    assert payload["needs_followup"] is True
    assert payload["phase_boundary"] is True
    assert restored.recoverable is True
    assert restored.completed == ["Read target files"]
    assert restored.remaining == ["Run validation"]
    assert restored.modified_files == ["aura/example.py"]
    assert restored.validation == "Not run"
    assert restored.suggested_next_spec == "Run a validation-only pass."


def test_dispatch_result_from_legacy_payload():
    restored = WorkerDispatchResult.from_tool_payload(
        {"ok": True, "cancelled": False, "summary": "Done"}
    )

    assert restored.ok is True
    assert restored.summary == "Done"
    assert restored.needs_followup is False
    assert restored.completed == []


def test_normalize_outcome_status_accepts_enum_and_valid_strings():
    assert normalize_outcome_status(WorkerOutcomeStatus.validation_failed) == "validation_failed"
    assert normalize_outcome_status(" validation_failed ") == "validation_failed"
    assert normalize_outcome_status("not_a_status") is None
    assert normalize_outcome_status(None) is None


def test_dispatch_result_from_tool_payload_drops_unknown_status():
    restored = WorkerDispatchResult.from_tool_payload(
        {"ok": False, "summary": "legacy", "status": "not_a_status"}
    )

    assert restored.status is None


def test_infer_outcome_status_legacy_non_ok_defaults_to_followup():
    result = WorkerDispatchResult(ok=False, summary="Something failed.")

    assert infer_outcome_status(result) == WorkerOutcomeStatus.needs_followup.value


def test_infer_outcome_status_uses_explicit_harness_signals():
    result = WorkerDispatchResult(
        ok=False,
        summary="Harness error - RuntimeError: boom",
        extras={"worker_internal_error": True},
    )

    assert infer_outcome_status(result) == WorkerOutcomeStatus.harness_error.value


# Structured fields roundtrips


def test_dispatch_request_structured_fields_roundtrip():
    original = WorkerDispatchRequest(
        goal="Refactor auth",
        files=["auth.py"],
        spec="Extract auth logic into its own module.",
        acceptance="All tests pass.",
        summary="Planner summary",
        allowed_responsibilities=["refactoring"],
        forbidden_responsibilities=["new features"],
        required_outputs=["auth.py"],
        validation_commands=["pytest tests/ -x"],
        risk_notes=["auth is critical"],
        non_goals=["rewrite from scratch"],
        expected_dataclass_fields={"MyClass": ["a", "b"]},
    )
    data = original.to_dict()
    restored = WorkerDispatchRequest.from_dict(data)
    assert restored.goal == original.goal
    assert restored.files == original.files
    assert restored.spec == original.spec
    assert restored.acceptance == original.acceptance
    assert restored.summary == original.summary
    assert restored.allowed_responsibilities == original.allowed_responsibilities
    assert restored.forbidden_responsibilities == original.forbidden_responsibilities
    assert restored.required_outputs == original.required_outputs
    assert restored.validation_commands == original.validation_commands
    assert restored.risk_notes == original.risk_notes
    assert restored.non_goals == original.non_goals
    assert restored.expected_dataclass_fields == {"MyClass": ["a", "b"]}


def test_dispatch_request_structured_fields_default_to_empty():
    """Missing structured fields should default to empty lists/dicts."""
    req = WorkerDispatchRequest.from_dict(
        {"goal": "x", "files": [], "spec": "", "acceptance": ""}
    )
    assert req.allowed_responsibilities == []
    assert req.forbidden_responsibilities == []
    assert req.required_outputs == []
    assert req.validation_commands == []
    assert req.risk_notes == []
    assert req.non_goals == []
    assert req.expected_dataclass_fields == {}


def test_dispatch_request_structured_fields_not_list():
    """Non-list structured fields should default to empty lists/dicts."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "allowed_responsibilities": "not_a_list",
            "forbidden_responsibilities": None,
            "required_outputs": 123,
            "validation_commands": "pytest",
            "risk_notes": {},
            "non_goals": 0,
        }
    )
    assert req.allowed_responsibilities == []
    assert req.forbidden_responsibilities == []
    assert req.required_outputs == []
    assert req.validation_commands == []
    assert req.risk_notes == []
    assert req.non_goals == []


def test_dispatch_request_expected_dataclass_fields_dict():
    """expected_dataclass_fields as dict roundtrips correctly."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": {"MyModel": ["id", "name"]},
        }
    )
    assert req.expected_dataclass_fields == {"MyModel": ["id", "name"]}


def test_dispatch_request_expected_dataclass_fields_list_old_format():
    """expected_dataclass_fields as list (old format) degrades to {}."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": ["a", "b"],
        }
    )
    assert req.expected_dataclass_fields == {}


def test_dispatch_request_expected_dataclass_fields_none():
    """expected_dataclass_fields as None degrades to {}."""
    req = WorkerDispatchRequest.from_dict(
        {
            "goal": "x",
            "expected_dataclass_fields": None,
        }
    )
    assert req.expected_dataclass_fields == {}


def test_rg_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'rg "show_response" app/tray.py',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_rg_pipeline_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'rg "show_response" app/tray.py | rg "No recent"',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_rg_pipeline_no_match_validation_preview_shape_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output_preview": "",
            "command": 'rg "show_response" app/tray.py | rg "No recent"',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_grep_no_match_validation_is_not_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "",
            "command": 'grep -R "show_response" app/',
        }
    ]

    assert _unrecovered_validation_failures(results) == []


def test_py_compile_exit_1_validation_remains_unrecovered_failure():
    results = [
        {
            "ok": False,
            "exit_code": 1,
            "output": "SyntaxError: invalid syntax",
            "command": "python -m py_compile app/tray.py",
        }
    ]

    assert _unrecovered_validation_failures(results) == results


def test_rg_real_error_validation_remains_unrecovered_failure():
    exit_2 = {
        "ok": False,
        "exit_code": 2,
        "output": "",
        "command": 'rg "unterminated" app/tray.py',
    }
    error_output = {
        "ok": False,
        "exit_code": 1,
        "output": "regex parse error: unclosed group",
        "command": 'rg "(" app/tray.py',
    }

    assert _unrecovered_validation_failures([exit_2]) == [exit_2]
    assert _unrecovered_validation_failures([error_output]) == [error_output]


def test_arbitrary_exit_1_validation_remains_unrecovered_failure():
    result = {
        "ok": False,
        "exit_code": 1,
        "output": "",
        "command": 'python -c "raise SystemExit(1)"',
    }

    assert _unrecovered_validation_failures([result]) == [result]


def test_worker_prompt_guides_search_validation_semantics():
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Check it.",
    )

    prompt = _format_spec_as_user_message(req)

    assert "Use grep_search for searching" in prompt
    assert "make intended no-match exit 0" in prompt
    assert "pytest" not in prompt


def test_lone_rg_terminal_result_does_not_count_as_task_validation():
    terminal_results = [
        {
            "ok": True,
            "exit_code": 0,
            "output": "match",
            "output_preview": "match",
            "command": 'rg "needle" app/tray.py',
        }
    ]

    assert _validation_results_for_task([], terminal_results, []) == []


def test_explicit_validation_command_counts_from_terminal_results():
    terminal_results = [
        {
            "ok": True,
            "exit_code": 0,
            "output": "match",
            "output_preview": "match",
            "command": 'rg "needle" app/tray.py && exit 1 || exit 0',
        }
    ]

    assert _validation_results_for_task(
        [],
        terminal_results,
        ['rg "needle" app/tray.py && exit 1 || exit 0'],
    ) == terminal_results


def test_final_report_partial_suite_with_py_compile_is_not_harness_failure():
    assert _final_report_claims_failure("Could not run full test suite, but py_compile passed")
    assert _final_report_claims_validation("Could not run full test suite, but py_compile passed")

    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [{"tool": "edit_file", "path": "a.py"}],
        [],
        {},
        ["Worker final report mentioned possible blocker, failed validation, or incomplete verification."],
    )

    assert not summary.startswith("Harness error")
    assert summary.startswith("Worker completed with caveats")


def test_no_blocker_remains_does_not_trip_failure_sniffing():
    assert not _final_report_claims_failure("No blocker remains. py_compile passed.")


def test_not_tested_does_not_count_as_validation_success():
    assert not _final_report_claims_validation("Changed the file. Not tested.")


def test_raw_rg_passed_does_not_count_as_validation_success():
    assert not _final_report_claims_validation('rg "old" app/tray.py passed')


def test_structured_worker_failure_summary_is_not_harness_error():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [],
        ["blocked by missing dependency (worker_blocked)."],
        {},
        [],
    )

    assert summary.startswith("Worker needs follow-up")
    assert not summary.startswith("Harness error")


# normalize_worker_task

from aura.conversation.dispatch import normalize_worker_task, WorkerTaskSpec


def test_normalize_worker_task_validation_commands_explicit():
    """Explicit validation_commands on the request should be forwarded directly."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="pytest tests/",
        validation_commands=["ruff check .", "py_compile x.py"],
    )
    spec = normalize_worker_task(req)
    assert spec.validation_commands == ["ruff check .", "py_compile x.py"]


def test_normalize_worker_task_validation_commands_fallback():
    """Empty validation_commands but acceptance has a backtick-quoted command."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Run `pytest tests/test_x.py -v` to check.",
        validation_commands=[],
    )
    spec = normalize_worker_task(req)
    assert "pytest tests/test_x.py -v" in spec.validation_commands


def test_normalize_worker_task_non_goals_explicit():
    """Explicit non_goals should be forwarded, not parsed from spec."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="## Non-Goals\n- Parse spec\n- Rewrite",
        acceptance="",
        non_goals=["explicit", "only"],
    )
    spec = normalize_worker_task(req)
    assert spec.non_goals == ["explicit", "only"]


def test_normalize_worker_task_structured_fields():
    """allowed_responsibilities, forbidden_responsibilities, required_outputs, risk_notes, expected_dataclass_fields forwarded."""
    req = WorkerDispatchRequest(
        goal="Do things",
        files=["x.py"],
        spec="Do it.",
        acceptance="Check it.",
        allowed_responsibilities=["editing"],
        forbidden_responsibilities=["new files"],
        required_outputs=["x.py"],
        risk_notes=["breaks easily"],
        expected_dataclass_fields={"MyClass": ["a"]},
    )
    spec = normalize_worker_task(req)
    assert spec.allowed_responsibilities == ["editing"]
    assert spec.forbidden_responsibilities == ["new files"]
    assert spec.required_outputs == ["x.py"]
    assert spec.risk_notes == ["breaks easily"]
    assert spec.contract is not None
    assert spec.contract.expected_dataclass_fields == {"MyClass": ["a"]}


# New focused tests for dispatch and approval proxy timeout and cancellation

def test_dispatch_proxy_timeout():
    from unittest.mock import Mock
    from aura.bridge.dispatch import _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    # Create approval proxy mock
    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    # We want to patch DISPATCH_TIMEOUT or call it under thread or short timeout so it doesn't block for 300s in test.
    # Let's temporarily override DISPATCH_TIMEOUT in the module!
    import aura.bridge.dispatch
    orig_timeout = aura.bridge.dispatch.DISPATCH_TIMEOUT
    aura.bridge.dispatch.DISPATCH_TIMEOUT = 0.05  # 50ms for fast test execution!
    try:
        res = proxy.request_dispatch("test_call_id", req)
        assert res.ok is False
        assert res.recoverable is True
        assert "Plan expired" in res.summary
        assert "UI signal" not in res.summary
        assert res.extras.get("dispatch_not_started") is True
        assert res.extras.get("dispatch_approval_timeout") is True
    finally:
        aura.bridge.dispatch.DISPATCH_TIMEOUT = orig_timeout


def test_dispatch_proxy_stale_dispatch():
    from unittest.mock import Mock
    from aura.bridge.dispatch import _DispatchProxy

    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=Mock(),
    )

    # If we call user_dispatched with a tool_call_id that is not in pending:
    res = proxy.user_dispatched("stale_call_id", "goal", [], "spec", "acceptance", "")
    assert res is False

    # Same for user_cancelled:
    res = proxy.user_cancelled("stale_call_id")
    assert res is False


def test_dispatch_proxy_cancel_all_unblocks_and_cancels_active_dialog():
    from unittest.mock import Mock
    import threading
    import time
    from aura.bridge.dispatch import _DispatchProxy
    from aura.conversation.dispatch import WorkerDispatchRequest

    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    # Let's trigger a dispatch wait in a background thread
    results = []
    def runner():
        res = proxy.request_dispatch("test_call_id", req)
        results.append(res)

    t = threading.Thread(target=runner)
    t.start()

    # Wait a tiny bit to ensure the thread is waiting
    time.sleep(0.05)

    # Now cancel all pending
    proxy.cancel_all_pending()
    t.join(timeout=1.0)

    # Verify that cancel_active_dialog was called on the approval proxy
    approval.cancel_active_dialog.assert_called_once()

    # Verify that request_dispatch returned a cancelled result
    assert len(results) == 1
    assert results[0].ok is False
    assert results[0].cancelled is True
    assert results[0].summary == "Cancelled"
    assert results[0].extras.get("dispatch_not_started") is True
    assert results[0].extras.get("dispatch_cancelled") is True


def test_approval_proxy_active_dialog_cancellation():
    from unittest.mock import Mock, patch
    from aura.bridge.approval_proxy import _ApprovalProxy
    from aura.conversation.tools import ApprovalRequest

    proxy = _ApprovalProxy(parent_widget=Mock())
    assert proxy._active_dialog is None

    # Let's mock DiffApprovalDialog
    with patch("aura.bridge.approval_proxy.DiffApprovalDialog") as mock_dlg_cls:
        mock_dlg = Mock()
        mock_dlg_cls.return_value = mock_dlg

        # When dlg.exec() is called, we will simulate calling cancel_active_dialog
        def fake_exec():
            # While exec is running, the active_dialog must be set
            assert proxy._active_dialog is mock_dlg
            proxy.cancel_active_dialog()
            return 0

        mock_dlg.exec.side_effect = fake_exec
        mock_dlg.decision.return_value = Mock(action="reject")

        # Now trigger the open_dialog Slot directly (which is what BlockingQueuedConnection invokes)
        proxy._last_request = ApprovalRequest(
            tool_name="write_file",
            rel_path="a.py",
            old_content="old",
            new_content="new",
            is_new_file=False,
        )
        proxy._open_dialog()

        # After execution completes, active_dialog should be None again
        assert proxy._active_dialog is None


def test_dispatch_proxy_cancelled_before_start():
    from unittest.mock import Mock
    import threading
    import time
    from aura.bridge.dispatch import _DispatchProxy, DISPATCH_TIMEOUT
    from aura.conversation.dispatch import WorkerDispatchRequest

    approval = Mock()
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=Mock(),
        approval_proxy=approval,
    )

    req = WorkerDispatchRequest(
        goal="Test goal",
        files=["test.py"],
        spec="test spec",
        acceptance="test acceptance",
    )

    results = []

    def runner():
        res = proxy.request_dispatch("test_call_id", req)
        results.append(res)

    t = threading.Thread(target=runner)
    t.start()

    # Wait briefly for the thread to start waiting on decision_event
    time.sleep(0.05)

    # Simulate user cancelling before timeout
    proxy.user_cancelled("test_call_id")
    t.join(timeout=DISPATCH_TIMEOUT + 1)

    assert len(results) == 1
    res = results[0]
    assert res.ok is False
    assert res.cancelled is True
    assert res.summary == "Cancelled"
    assert res.extras.get("dispatch_not_started") is True
    assert res.extras.get("dispatch_cancelled") is True


# WorkerDispatchResult — completion semantics tests


def test_worker_result_ok_false_when_writes_but_no_validation():
    """A Worker that writes files but runs no validation must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Modified files but no validation",
        needs_followup=True, recoverable=True,
        modified_files=["test.py"],
        extras={"caveats": ["Worker modified files but ran no validation command."]},
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    payload = result.to_tool_payload()
    assert payload["ok"] is False
    assert payload.get("needs_followup") is True
    assert payload.get("recoverable") is True


def test_worker_result_ok_false_when_failed_write():
    """A Worker with a failed edit_file must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Write tool failed",
        extras={"errors": ["Worker write tool 'edit_file' reported failure."]},
    )
    assert not result.ok
    payload = result.to_tool_payload()
    assert payload["ok"] is False


def test_worker_result_ok_false_when_failed_validation():
    """A Worker with failed terminal validation must NOT be ok."""
    result = WorkerDispatchResult(
        ok=False, summary="Validation failed",
        needs_followup=True, recoverable=True,
        extras={"errors": ["Validation command failed (exit code 1): python -m pytest"]},
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    payload = result.to_tool_payload()
    assert payload["ok"] is False


def test_worker_result_ok_true_when_writes_and_passing_validation():
    """A Worker that writes AND validates successfully must BE ok."""
    result = WorkerDispatchResult(
        ok=True, summary="Wrote files and validated",
        needs_followup=False,
        modified_files=["test.py"],
    )
    assert result.ok
    payload = result.to_tool_payload()
    assert payload["ok"] is True


def test_modified_files_are_derived_from_applied_write_receipts_only():
    writes = [
        {"path": "changed.py", "applied": True, "write_outcome": "applied"},
        {"path": "changed.py", "applied": True, "write_outcome": "applied"},
        {"path": "claimed.py", "applied": False, "write_outcome": "not_applied_craft_rejected"},
        {"path": ".aura/tmp/_check_acceptance.py", "applied": True, "write_outcome": "applied"},
    ]

    assert _applied_modified_files(writes) == ["changed.py"]


def test_worker_summary_dedupes_duplicate_modified_file_rows_and_count():
    summary = _build_worker_summary(
        WorkerDispatchRequest(goal="Fix", files=["a.py"], spec="spec", acceptance=""),
        History(),
        [
            {"tool": "apply_edit_transaction", "path": "a.py", "applied": True, "is_new_file": False},
            {"tool": "apply_edit_transaction", "path": "a.py", "applied": True, "is_new_file": False},
            {"tool": "write_file", "path": "b.py", "applied": True, "is_new_file": True},
        ],
        [],
        {},
        [],
        validation_results=[{"command": "python -m py_compile a.py b.py", "ok": True, "exit_code": 0}],
    )

    assert "Files changed   : 2 (1 edited, 1 new)" in summary
    assert summary.count("a.py   (edit)") == 1
    assert summary.count("b.py   (new)") == 1


def test_worker_result_needs_followup_not_terminal():
    """WorkerDispatchResult with ok=False and recoverable=True should not be terminal."""
    result = WorkerDispatchResult(
        ok=False, summary="Needs another pass",
        needs_followup=True, recoverable=True,
    )
    assert not result.ok
    assert result.needs_followup
    assert result.recoverable
    # Test roundtrip
    payload = result.to_tool_payload()
    restored = WorkerDispatchResult.from_tool_payload(payload)
    assert not restored.ok
    assert restored.needs_followup
    assert restored.recoverable


def test_patch_quality_unresolved_summary_is_not_worker_failed():
    req = WorkerDispatchRequest(goal="Fix code", files=["a.py"], spec="spec", acceptance="")
    summary = _build_worker_summary(
        req,
        History(),
        [],
        ["Patch quality needs repair: Define missing (patch_quality_unresolved)."],
        {"status": "patch_quality_unresolved", "reason": "Patch quality needs repair: Define missing"},
        [],
    )

    assert summary.startswith("Patch quality needs repair:")
    assert "Worker failed" not in summary


def _status_for(**overrides):
    args = {
        "ok": False,
        "needs_followup": True,
        "recoverable": True,
        "has_internal_failure": False,
        "has_validation_failure": False,
        "has_recoverable_edit_blocker": False,
        "has_quality_bounce_blocker": False,
        "has_source_inspection_blocker": False,
        "has_no_work": False,
        "is_implementation": True,
        "has_unverified_acceptance": False,
        "has_hard_failure": True,
        "has_applied_writes": False,
        "result_errors": ["failed"],
        "result_caveats": [],
        "continuation": {},
        "structured_failure": {},
        "write_failures": [],
    }
    args.update(overrides)
    return _compute_outcome_status(**args)


def test_compute_outcome_status_distinguishes_rejection_and_bounce_classes():
    assert _status_for(
        structured_failure={"failure_class": "approval_rejected"}
    ) == WorkerOutcomeStatus.approval_rejected.value
    assert _status_for(
        write_failures=[{"failure_class": "compiler_rejected"}]
    ) == WorkerOutcomeStatus.craft_rejected.value
    assert _status_for(
        write_failures=[{"reject": True}]
    ) == WorkerOutcomeStatus.craft_rejected.value
    assert _status_for(
        structured_failure={"failure_class": "patch_quality_unresolved"},
        continuation={"status": "patch_quality_unresolved"},
    ) == WorkerOutcomeStatus.craft_bounced.value


def test_compute_outcome_status_distinguishes_validation_mechanics_and_harness():
    assert _status_for(
        has_recoverable_edit_blocker=True
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value
    assert _status_for(
        write_failures=[{"failure_class": "edit_transaction_symbol_not_found"}]
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value
    assert _status_for(
        has_validation_failure=True
    ) == WorkerOutcomeStatus.validation_failed.value
    assert _status_for(
        has_source_inspection_blocker=True,
        result_errors=["Terminal source inspection was blocked."],
    ) == WorkerOutcomeStatus.needs_followup.value
    assert _status_for(
        has_internal_failure=True
    ) == WorkerOutcomeStatus.harness_error.value


def test_compute_outcome_status_completed_with_caveats_requires_ok():
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        result_caveats=["minor caveat"],
    ) == WorkerOutcomeStatus.completed_with_caveats.value


def test_recovered_write_failure_does_not_cause_edit_mechanics_blocked():
    """A worker that recovered from a failed edit and applied writes should NOT be edit_mechanics_blocked."""
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        write_failures=[{"failure_class": "edit_transaction_not_applicable"}],
        has_applied_writes=True,
    ) != WorkerOutcomeStatus.edit_mechanics_blocked.value
    # With writes applied and no hard errors, should be completed
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        write_failures=[{"failure_class": "edit_transaction_not_applicable"}],
        has_applied_writes=True,
    ) == WorkerOutcomeStatus.completed.value


def test_unrecovered_write_failure_after_prior_write_blocks_completion():
    assert _status_for(
        ok=False,
        needs_followup=True,
        recoverable=True,
        has_hard_failure=False,
        result_errors=[],
        has_recoverable_edit_blocker=True,
        has_applied_writes=True,
        write_failures=[{"failure_class": "edit_mechanics_old_str_not_found"}],
    ) == WorkerOutcomeStatus.edit_mechanics_blocked.value


def test_validation_not_run_after_files_changed_is_needs_followup():
    assert _status_for(
        ok=False,
        needs_followup=True,
        recoverable=True,
        has_hard_failure=False,
        result_errors=[],
        has_applied_writes=True,
        has_unverified_acceptance=True,
        result_caveats=["Files changed but validation did not run."],
    ) == WorkerOutcomeStatus.needs_followup.value


def test_environment_caveat_is_not_edit_mechanics_failure():
    assert _status_for(
        ok=True,
        needs_followup=False,
        recoverable=False,
        has_hard_failure=False,
        result_errors=[],
        has_applied_writes=True,
        result_caveats=["Pre-existing environment issue on a.py: yaml import missing"],
        write_failures=[{"failure_class": "pre_existing_environment_issue"}],
    ) == WorkerOutcomeStatus.completed_with_caveats.value
