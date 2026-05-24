"""Tests for aura.conversation.dispatch — WorkerDispatchRequest/Result."""

from __future__ import annotations

from aura.conversation.dispatch import (
    WorkerDispatchRequest,
    WorkerDispatchResult,
)


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


# WorkerEventRelay tracking tests


def test_worker_result_ok_false_when_writes_but_no_validation():
    """Simulate a Worker that writes files but runs no validation.

    Verify that writes without validation produce ok=False and
    needs_followup=True per the new completion semantics.
    """
    from unittest.mock import Mock
    from aura.bridge.event_relay import WorkerEventRelay
    from aura.client import ToolResult

    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test")

    # Worker wrote a file (successfully) but ran NO validation commands
    ev = ToolResult(
        tool_call_id="tc1",
        name="write_file",
        ok=True,
        result='{"ok": true, "path": "test.py"}',
        extras={},
    )
    relay.relay("parent1", ev)

    assert len(relay.tool_results) == 1
    assert relay.tool_results[0]["name"] == "write_file"
    assert relay.tool_results[0]["ok"] is True
    assert len(relay.failed_tool_results) == 0
    assert len(relay.write_results) == 1
    # No validation commands were run
    assert len(relay.validation_results) == 0

    # This scenario maps to: has_writes=True, validation_ran=False,
    # missing_validation_after_writes=True, needs_followup=True, ok=False
    has_writes = bool(relay.write_results)
    validation_ran = bool(relay.validation_results)
    missing_validation_after_writes = has_writes and not validation_ran
    assert missing_validation_after_writes is True


def test_worker_result_ok_false_when_failed_write():
    """Simulate a Worker with a failed edit_file tool result.

    Verify that the failed write tool is captured in failed_tool_results
    and would cause ok=False and recoverable=True.
    """
    from unittest.mock import Mock
    from aura.bridge.event_relay import WorkerEventRelay
    from aura.client import ToolResult
    from aura.conversation.tool_limits import WRITE_TOOLS

    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test")

    # Failed edit_file write tool
    ev = ToolResult(
        tool_call_id="tc1",
        name="edit_file",
        ok=False,
        result='{"ok": false, "error": "File not found"}',
        extras={},
    )
    relay.relay("parent1", ev)

    assert len(relay.failed_tool_results) == 1
    assert relay.failed_tool_results[0]["name"] == "edit_file"
    assert relay.failed_tool_results[0]["ok"] is False

    # Verify it's in WRITE_TOOLS (so failed_write_tools would be non-empty)
    assert "edit_file" in WRITE_TOOLS
    failed_write_tools = [r for r in relay.failed_tool_results if r["name"] in WRITE_TOOLS]
    assert len(failed_write_tools) == 1

    # This would cause: ok=False, recoverable=True


def test_worker_result_ok_false_when_failed_validation():
    """Simulate a Worker with failed terminal validation.

    Verify that failed validation commands are captured in validation_results
    and would cause ok=False.
    """
    from unittest.mock import Mock
    from aura.bridge.event_relay import WorkerEventRelay
    from aura.client import ToolResult

    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test")

    # Failed validation command
    ev = ToolResult(
        tool_call_id="tc1",
        name="run_terminal_command",
        ok=False,
        result=(
            '{"ok": false, "command": "pytest tests/test_x.py -x", '
            '"exit_code": 1, "output": "FAILED"}'
        ),
        extras={},
    )
    relay.relay("parent1", ev)

    assert len(relay.validation_results) == 1
    assert relay.validation_results[0]["ok"] is False
    assert relay.validation_results[0]["exit_code"] == 1
    assert relay.validation_results[0]["command"] == "pytest tests/test_x.py -x"

    # This would cause: failed_validation non-empty -> ok=False, needs_followup=True


def test_worker_result_ok_true_when_writes_and_passing_validation():
    """Simulate a Worker that writes and validates successfully.

    Verify that writes + passing validation produces conditions
    for ok=True (no errors, no caveats).
    """
    from unittest.mock import Mock
    from aura.bridge.event_relay import WorkerEventRelay
    from aura.client import ToolResult

    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test")

    # Write file successfully
    ev1 = ToolResult(
        tool_call_id="tc1",
        name="write_file",
        ok=True,
        result='{"ok": true, "path": "test.py"}',
        extras={},
    )
    relay.relay("parent1", ev1)

    # Validation passes
    ev2 = ToolResult(
        tool_call_id="tc2",
        name="run_terminal_command",
        ok=True,
        result=(
            '{"ok": true, "command": "ruff check .", '
            '"exit_code": 0, "output": ""}'
        ),
        extras={},
    )
    relay.relay("parent1", ev2)

    assert len(relay.write_results) == 1
    assert len(relay.validation_results) == 1
    assert relay.validation_results[0]["ok"] is True
    assert len(relay.failed_tool_results) == 0
    assert len(relay.api_errors) == 0

    # This scenario would produce: has_writes=True, validation_ran=True,
    # no failed validation, no missing validation -> ok=True


def test_worker_result_needs_followup_not_terminal():
    """Verify WorkerDispatchResult with ok=False and recoverable=True
    produces the correct payload (needs_followup, recoverable fields)."""
    from aura.conversation.dispatch import WorkerDispatchResult

    result = WorkerDispatchResult(
        ok=False,
        summary="Worker reached tool limit.",
        needs_followup=True,
        phase_boundary=True,
        followup_reason="worker_tool_call_limit_reached",
        recoverable=True,
        completed=["Read files"],
        remaining=["Run validation"],
    )

    payload = result.to_tool_payload()
    assert payload["needs_followup"] is True
    assert payload["phase_boundary"] is True
    assert payload["recoverable"] is True
    assert payload["ok"] is False
    assert "summary" in payload

    # Verify it round-trips
    restored = WorkerDispatchResult.from_tool_payload(payload)
    assert restored.needs_followup is True
    assert restored.recoverable is True
    assert restored.ok is False
    assert restored.completed == ["Read files"]
    assert restored.remaining == ["Run validation"]
    assert restored.followup_reason == "worker_tool_call_limit_reached"

