from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, PropertyMock, Mock, patch

from aura.bridge.dispatch import (
    _DispatchProxy,
    _cleanup_new_root_check_files,
    _format_worker_write_failure,
    _is_recoverable_worker_write_failure,
    _root_check_files,
    _unrecovered_validation_failures,
)
from aura.bridge.event_relay import WorkerEventRelay
from aura.client.events import Done, ToolResult
from aura.conversation.dispatch import WorkerDispatchRequest
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools.fs_edit_structured import propose_edit_symbol
from aura.conversation.tools.fs_write import propose_edit, propose_line_range_edit
from aura.conversation.tools.registry import ToolRegistry
from aura.hooks import hooks
from aura.sandbox import SandboxResult


def _tool_call(tool_id: str, name: str, args: dict) -> dict:
    return {
        "id": tool_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _done(content: str = "", tool_calls: list[dict] | None = None) -> Done:
    return Done(
        finish_reason="tool_calls" if tool_calls else "stop",
        full_message={
            "role": "assistant",
            "content": content,
            "reasoning_content": None,
            **({"tool_calls": tool_calls} if tool_calls else {}),
        },
    )


def _register_worker_hook(handler) -> None:
    hooks.unregister("generate_worker_code")
    hooks.register("generate_worker_code", handler)


def test_edit_file_old_str_not_found_has_structured_recovery_info(tmp_workspace):
    target = tmp_workspace / "target.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")

    result = propose_edit(tmp_workspace, target, "gamma = 3", "gamma = 4")

    assert result["ok"] is False
    assert result["failure_class"] == "edit_mechanics_old_str_not_found"
    assert result["path"] == "target.py"
    assert result["suggested_next_tool"] == "edit_line_range"
    assert "old_str not found" in result["error"]
    assert "nearest_candidates" in result


def test_edit_symbol_missing_symbol_has_structured_recovery_info(sample_py_file, tmp_workspace):
    result = propose_edit_symbol(
        tmp_workspace,
        sample_py_file,
        "function",
        "missing_symbol",
        "def missing_symbol():\n    return None\n",
    )

    assert result["ok"] is False
    assert result["failure_class"] == "edit_mechanics_symbol_not_found"
    assert result["symbol_name"] == "missing_symbol"
    assert result["suggested_next_tool"] == "edit_line_range"
    assert result["available_symbols"]["functions"]
    assert result["suggested_fallback"] == "read_file_outline/read_file then edit_line_range"


def test_edit_line_range_can_patch_after_edit_file_fails(tmp_workspace):
    target = tmp_workspace / "target.py"
    target.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")

    failed = propose_edit(tmp_workspace, target, "missing = 0", "missing = 1")
    patched = propose_line_range_edit(tmp_workspace, target, 2, 3, "two = 22\n")

    assert failed["failure_class"] == "edit_mechanics_old_str_not_found"
    assert patched["ok"] is True
    assert patched["start_line"] == 2
    assert patched["end_line"] == 3
    assert "two = 22" in patched["new_content"]


def test_worker_event_relay_preserves_failed_tool_error_payload():
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test")
    payload = {
        "ok": False,
        "path": "a.py",
        "error": "old_str not found in file",
        "failure_class": "edit_mechanics_old_str_not_found",
        "suggested_next_tool": "edit_line_range",
        "internal_recovery_steer": True,
        "best_fuzzy_ratio": 0.25,
        "nearest_candidates": [{"start_line": 3, "end_line": 5, "text": "x"}],
    }

    relay.relay(
        "dispatch-1",
        ToolResult("tool-1", "edit_file", False, json.dumps(payload)),
    )

    failed = relay.failed_tool_results[-1]
    assert failed["name"] == "edit_file"
    assert failed["error"] == "old_str not found in file"
    assert failed["failure_class"] == "edit_mechanics_old_str_not_found"
    assert failed["suggested_next_tool"] == "edit_line_range"
    assert failed["nearest_candidates"][0]["start_line"] == 3
    assert failed["internal_recovery_steer"] is True


def test_repeated_identical_edit_attempt_is_blocked_and_redirected(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    tools.execute.return_value = ToolExecResult(
        ok=False,
        payload={
            "ok": False,
            "path": "a.py",
            "error": "old_str not found in file",
            "failure_class": "edit_mechanics_old_str_not_found",
        },
    )
    manager = ConversationManager(history, tools)
    events = []
    tc1 = _tool_call("e1", "edit_file", {"path": "a.py", "old_str": "x", "new_str": "y"})
    tc2 = _tool_call("e2", "edit_file", {"path": "a.py", "old_str": "x", "new_str": "z"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc1])]),
            iter([_done(tool_calls=[tc2])]),
            iter([_done("Done.")]),
            iter([_done("Still done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
            hook_name="generate_worker_code",
        )
    finally:
        hooks.unregister("generate_worker_code")

    edit_results = [ev for ev in events if isinstance(ev, ToolResult) and ev.name == "edit_file"]
    assert tools.execute.call_count == 1
    assert len(edit_results) == 2
    blocked = json.loads(edit_results[-1].result)
    assert "Repeated failed edit tactic" in blocked["error"]
    assert blocked["suggested_next_tool"] == "apply_edit_transaction"
    assert "patch_file" not in blocked.get("suggested_next_action", "")
    assert blocked["internal_recovery_steer"] is True
    assert any(
        msg.get("role") == "user"
        and "Previous edit failed recoverably" in str(msg.get("content"))
        for msg in history.messages
    )
    final_payload = json.loads(history.messages[-1]["content"])
    assert final_payload["failure_class"] == "worker_recovery_exhausted"
    assert final_payload["details"]["path"] == "a.py"
    assert final_payload["details"]["tool"] == "edit_file"
    assert final_payload["details"]["failure_class"] == "edit_mechanics_old_str_not_found"


def test_repeated_recovery_blocks_remain_internal_steering(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    tools.execute.return_value = ToolExecResult(
        ok=False,
        payload={
            "ok": False,
            "path": "a.py",
            "error": "old_str not found in file",
            "failure_class": "edit_mechanics_old_str_not_found",
        },
    )
    manager = ConversationManager(history, tools)
    events = []
    args = {"path": "a.py", "old_str": "x", "new_str": "y"}
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[_tool_call("e1", "edit_file", args)])]),
            iter([_done(tool_calls=[_tool_call("e2", "edit_file", args)])]),
            iter([_done(tool_calls=[_tool_call("e3", "edit_file", args)])]),
            iter([_done("Recovery reported.")]),
            iter([_done("Still stopped.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
            hook_name="generate_worker_code",
        )
    finally:
        hooks.unregister("generate_worker_code")

    edit_results = [ev for ev in events if isinstance(ev, ToolResult) and ev.name == "edit_file"]
    assert tools.execute.call_count == 1
    payload = json.loads(edit_results[-1].result)
    assert payload["internal_recovery_steer"] is True
    assert payload["repeated_blocks"] >= 2
    assert "phase_boundary" not in payload


def test_py_compile_failure_blocks_unrelated_tool_until_syntax_repair(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    manager = ConversationManager(history, tools)
    events = []
    tc_compile = _tool_call("t1", "run_terminal_command", {"command": "python -m py_compile a.py", "timeout": 30})
    tc_grep = _tool_call("g1", "grep_search", {"pattern": "anything"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc_compile])]),
            iter([_done(tool_calls=[tc_grep])]),
            iter([_done("Stopped.")]),
            iter([_done("Still stopped.")]),
        ]
    )

    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=False,
        stdout="SyntaxError: invalid syntax in a.py",
        stderr="",
        exit_code=1,
    )

    try:
        _register_worker_hook(hook)
        with patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox), patch("aura.conversation.tool_runner.load_settings") as load_settings:
            load_settings.return_value.sandbox_mode = "host"
            manager.send(
                on_event=events.append,
                approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
                cancel_event=threading.Event(),
                model="test-model",
                thinking="off",
                hook_name="generate_worker_code",
            )
    finally:
        hooks.unregister("generate_worker_code")

    grep_results = [ev for ev in events if isinstance(ev, ToolResult) and ev.name == "grep_search"]
    assert tools.execute.call_count == 0
    assert grep_results
    payload = json.loads(grep_results[-1].result)
    assert payload["failure_class"] == "syntax_invalid"
    assert "pass py_compile before any unrelated tool call" in payload["error"]


def test_quality_bounce_adds_repair_guidance_without_blocking_tools(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    tools.execute.side_effect = [
        ToolExecResult(
        ok=True,
        payload={
            "ok": True,
            "applied": False,
            "quality_bounce": True,
            "path": "a.py",
            "tool_name": "edit_file",
            "repair_instructions": "Line 2: [undefined-name] Name 'missing' is used but never defined.",
            "craft_issues": [
                {
                    "line": 2,
                    "column": 11,
                    "code": "undefined-name",
                    "message": "Name 'missing' is used but never defined.",
                    "suggestion": "Define or import the name before using it.",
                }
            ],
            "suggested_next_action": "Repair the proposed patch and retry this file.",
        },
    ),
        ToolExecResult(ok=True, payload={"ok": True, "path": "a.py", "content": "value = 1\n"}),
        ToolExecResult(ok=True, payload={"ok": True, "path": "a.py", "applied": "write_file", "is_new_file": False}),
    ]
    manager = ConversationManager(history, tools)
    events = []
    args = {"path": "a.py", "old_str": "value = 1", "new_str": "value = missing"}
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[_tool_call("e1", "edit_file", args)])]),
            iter([_done(tool_calls=[_tool_call("r1", "read_file", {"path": "a.py"})])]),
            iter([_done(tool_calls=[_tool_call("w1", "write_file", {"path": "a.py", "content": "value = 2\n"})])]),
            iter([_done("Done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        with patch.object(ConversationManager, "_run_focused_py_compile", return_value=(True, "a.py: ok")):
            manager.send(
                on_event=events.append,
                approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
                cancel_event=threading.Event(),
                model="test-model",
                thinking="off",
                hook_name="generate_worker_code",
            )
    finally:
        hooks.unregister("generate_worker_code")

    edit_results = [ev for ev in events if isinstance(ev, ToolResult) and ev.name == "edit_file"]
    read_results = [ev for ev in events if isinstance(ev, ToolResult) and ev.name == "read_file"]
    assert tools.execute.call_count == 3
    assert len(edit_results) == 1
    bounced = json.loads(edit_results[-1].result)
    assert bounced["quality_bounce"] is True
    assert bounced["applied"] is False
    assert read_results
    assert any(
        msg.get("role") == "user"
        and "Craft reviewed the proposed patch and returned repair notes" in str(msg.get("content"))
        for msg in history.messages
    )
    assert not any(
        isinstance(ev, ToolResult)
        and json.loads(ev.result).get("failure_class") == "compiler_rejected"
        for ev in events
        if isinstance(ev, ToolResult) and ev.result.startswith("{")
    )


def test_compiler_bounce_failed_repair_finishes_with_single_reason(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    bounce_payload = {
        "ok": True,
        "applied": False,
        "quality_bounce": True,
        "path": "a.py",
        "tool_name": "edit_file",
        "repair_instructions": "Line 2: [undefined-name] Name 'missing' is used but never defined.",
        "craft_issues": [{"line": 2, "code": "undefined-name", "message": "Name 'missing' is used but never defined."}],
        "suggested_next_action": "Repair the proposed patch and retry this file.",
    }
    tools.execute.side_effect = [
        ToolExecResult(ok=True, payload=dict(bounce_payload)),
        ToolExecResult(ok=True, payload=dict(bounce_payload)),
    ]
    manager = ConversationManager(history, tools)
    events = []
    hook = MagicMock(
        side_effect=[
            iter([
                _done(tool_calls=[
                    _tool_call("e1", "edit_file", {"path": "a.py", "old_str": "x", "new_str": "missing"})
                ])
            ]),
            iter([
                _done(tool_calls=[
                    _tool_call("w1", "write_file", {"path": "a.py", "content": "value = missing\n"})
                ])
            ]),
            iter([_done("Done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        manager.send(
            on_event=events.append,
            approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
            cancel_event=threading.Event(),
            model="test-model",
            thinking="off",
            hook_name="generate_worker_code",
        )
    finally:
        hooks.unregister("generate_worker_code")

    assert tools.execute.call_count == 2
    final_payload = json.loads(history.messages[-1]["content"])
    assert final_payload == {
        "ok": False,
        "failure_class": "patch_quality_unresolved",
        "error": "Patch quality needs repair: Line 2: [undefined-name] Name 'missing' is used but never defined.",
    }


def test_worker_cannot_finish_after_python_write_until_py_compile(tmp_path):
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    tools.execute.return_value = ToolExecResult(
        ok=True,
        payload={
            "ok": True,
            "path": "a.py",
            "applied": "write_file",
            "is_new_file": False,
        },
    )
    manager = ConversationManager(history, tools)
    events = []
    tc_write = _tool_call("w1", "write_file", {"path": "a.py", "content": "value = 1\n"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc_write])]),
            iter([_done("Done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        with patch.object(ConversationManager, '_run_focused_py_compile', return_value=(True, "a.py: ok")) as compile_mock:
            manager.send(
                on_event=events.append,
                approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
                cancel_event=threading.Event(),
                model="test-model",
                thinking="off",
                hook_name="generate_worker_code",
            )
    finally:
        hooks.unregister("generate_worker_code")

    compile_mock.assert_called_once_with(["a.py"])
    validation_events = [
        ev for ev in events
        if isinstance(ev, ToolResult)
        and ev.name == "run_terminal_command"
        and ev.tool_call_id == "auto_py_compile"
    ]
    assert len(validation_events) == 1
    payload = json.loads(validation_events[0].result)
    assert payload["ok"] is True
    assert payload["command"] == "python -m py_compile a.py"
    assert payload["auto_validation"] is True
    content = history.messages[-1]["content"]
    assert "failure_class" not in content


def test_recoverable_edit_mechanics_exhaustion_is_single_final_reason(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_text("value = 1\n", encoding="utf-8")
    req = WorkerDispatchRequest(
        goal="Update value",
        files=["a.py"],
        spec="Update value in a.py.",
        acceptance="",
        summary="Update a.py",
    )
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=lambda mode: ToolRegistry(tmp_workspace, mode=mode),
        approval_proxy=Mock(request_approval=MagicMock(return_value=ApprovalDecision("approve")), consume_last_event=MagicMock(return_value=None)),
        workspace_root=tmp_workspace,
    )
    proxy.set_auto_commit_enabled(False)
    tc = _tool_call("e1", "edit_file", {"path": "a.py", "old_str": "missing", "new_str": "value = 2"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc])]),
            iter([_done("Done.")]),
            iter([_done("Still done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        result = proxy._run_worker("dispatch-1", req, SimpleNamespace(cancel_event=None))
    finally:
        hooks.unregister("generate_worker_code")

    assert result.ok is False
    assert result.needs_followup is True
    assert result.recoverable is False
    assert result.extras["failed_write_tools"] == []
    assert result.extras["errors"] == [
        "Worker stopped before recovering from a recoverable edit mechanics failure. "
        "(worker_recovery_exhausted). Path: a.py. Tool: edit_file. "
        "Reason: edit_mechanics_old_str_not_found."
    ]
    assert result.status == "edit_mechanics_blocked"
    assert "Edit mechanics blocked" in result.summary
    assert "old_str not found" not in result.summary


def test_auto_py_compile_validation_is_counted_by_dispatch(tmp_workspace):
    req = WorkerDispatchRequest(
        goal="Create a Python module",
        files=["a.py"],
        spec="Create a.py with a simple value.",
        acceptance="",
        summary="Create a.py",
    )
    proxy = _DispatchProxy(
        parent_widget=Mock(),
        registry_factory=lambda mode: ToolRegistry(tmp_workspace, mode=mode),
        approval_proxy=Mock(
            request_approval=MagicMock(return_value=ApprovalDecision("approve")),
            consume_last_event=MagicMock(return_value=None),
        ),
        workspace_root=tmp_workspace,
    )
    proxy.set_auto_commit_enabled(False)
    tc = _tool_call("w1", "write_file", {"path": "a.py", "content": "value = 1\n"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc])]),
            iter([_done("Done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        with patch.object(
            ConversationManager,
            "_run_focused_py_compile",
            return_value=(True, "a.py: ok"),
        ):
            result = proxy._run_worker("dispatch-1", req, SimpleNamespace(cancel_event=None))
    finally:
        hooks.unregister("generate_worker_code")

    assert result.ok is True
    assert result.needs_followup is False
    assert result.recoverable is False
    assert "Worker modified files but ran no validation command." not in result.extras["caveats"]
    assert result.extras["validation_results"] == [
        {
            "command": "python -m py_compile a.py",
            "ok": True,
            "exit_code": 0,
            "output": "a.py: ok",
            "output_preview": "a.py: ok",
            "auto_validation": True,
        }
    ]


def test_root_check_scratch_files_are_cleaned(tmp_workspace):
    before = _root_check_files(tmp_workspace)
    scratch = tmp_workspace / "_check_acceptance.py"
    scratch.write_text("print('temporary')\n", encoding="utf-8")

    cleaned = _cleanup_new_root_check_files(tmp_workspace, before)

    assert cleaned == ["_check_acceptance.py"]
    assert not scratch.exists()


def test_root_check_scratch_file_write_is_rejected_before_approval(tmp_workspace):
    registry = ToolRegistry(tmp_workspace, mode="worker")
    approve_cb = MagicMock(return_value=ApprovalDecision("approve"))

    result = registry.execute(
        "write_file",
        {"path": "_check_acceptance.py", "content": "print('temporary')\n"},
        approve_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "validation_scratch_banned"
    assert result.payload["applied"] is False
    assert result.payload["write_outcome"].startswith("not_applied_")
    assert result.payload["suggested_next_tool"] == "run_terminal_command"
    assert approve_cb.call_count == 0
    assert not (tmp_workspace / "_check_acceptance.py").exists()


def test_write_file_invalid_python_blocks_before_approval_when_craft_disabled(tmp_workspace, monkeypatch):
    registry = ToolRegistry(tmp_workspace, mode="worker")
    approve_cb = MagicMock(return_value=ApprovalDecision("approve"))
    monkeypatch.setenv("AURA_CRAFT", "0")

    result = registry.execute(
        "write_file",
        {"path": "broken.py", "content": "def broken(:\n    pass\n"},
        approve_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "syntax_invalid"
    assert result.payload["applied"] is False
    assert result.payload["write_outcome"] == "not_applied_craft_rejected"
    assert approve_cb.call_count == 0
    assert not (tmp_workspace / "broken.py").exists()


def test_edit_file_invalid_python_blocks_before_approval_when_craft_disabled(tmp_workspace, monkeypatch):
    target = tmp_workspace / "target.py"
    original = "def ok():\n    return 1\n"
    target.write_text(original, encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")
    approve_cb = MagicMock(return_value=ApprovalDecision("approve"))
    monkeypatch.setenv("AURA_CRAFT", "0")

    result = registry.execute(
        "edit_file",
        {"path": "target.py", "old_str": "def ok():\n    return 1", "new_str": "def ok(:\n    return 2"},
        approve_cb,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "syntax_invalid"
    assert result.payload["applied"] is False
    assert result.payload["write_outcome"] == "not_applied_craft_rejected"
    assert approve_cb.call_count == 0
    assert target.read_text(encoding="utf-8") == original


def test_apply_edit_transaction_arg_error_has_not_applied_truth(tmp_workspace):
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "apply_edit_transaction",
        {"path": "target.py", "operations": "not-a-list"},
        MagicMock(return_value=ApprovalDecision("approve")),
        False,
    )

    assert result.ok is False
    assert result.payload["applied"] is False
    assert result.payload["write_outcome"].startswith("not_applied_")


def test_edit_line_range_applied_result_includes_line_metadata(tmp_workspace):
    target = tmp_workspace / "target.py"
    target.write_text("one = 1\ntwo = 2\nthree = 3\n", encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")
    approve_cb = MagicMock(return_value=ApprovalDecision("approve"))

    result = registry.execute(
        "edit_line_range",
        {"path": "target.py", "start_line": 2, "end_line": 3, "new_str": "two = 22\n"},
        approve_cb,
        False,
    )

    assert result.ok is True
    assert result.payload["path"] == "target.py"
    assert result.payload["applied"] is True
    assert result.payload["applied_tool"] == "edit_line_range"
    assert result.payload["start_line"] == 2
    assert result.payload["end_line"] == 3
    assert "backup" in result.payload


def test_failed_write_summary_uses_exact_reason_not_generic():
    result = {
        "name": "write_file",
        "path": "a.py",
        "error": "compiler rejected generated Python",
        "failure_class": "compiler_rejected",
    }

    summary = _format_worker_write_failure(result)

    assert "compiler rejected generated Python" in summary
    assert "reported failure" not in summary
    assert not _is_recoverable_worker_write_failure(result)
    assert _is_recoverable_worker_write_failure(
        {"name": "edit_file", "failure_class": "edit_mechanics_old_str_not_found"}
    )


def test_recovered_py_compile_failure_is_not_final_validation_failure():
    results = [
        {"command": "python -m py_compile aura/example.py", "ok": False, "exit_code": 1},
        {"command": "python -m py_compile aura/example.py", "ok": True, "exit_code": 0},
    ]

    assert _unrecovered_validation_failures(results) == []

    unrecovered = _unrecovered_validation_failures([
        {"command": "python -m py_compile aura/example.py", "ok": False, "exit_code": 1},
    ])
    assert len(unrecovered) == 1

def test_auto_py_compile_success_when_worker_stops(tmp_path):
    """Test A: Auto py_compile success — worker writes .py file, stops without
    py_compile, manager auto-runs focused py_compile which passes."""
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    tools.execute.return_value = ToolExecResult(
        ok=True,
        payload={
            "ok": True,
            "path": "graph_main_window.py",
            "applied": "write_file",
            "is_new_file": False,
        },
    )
    manager = ConversationManager(history, tools)
    events = []
    tc_write = _tool_call("w1", "write_file", {"path": "graph_main_window.py", "content": "x = 1\n"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc_write])]),
            iter([_done("Done.")]),
        ]
    )

    try:
        _register_worker_hook(hook)
        with patch.object(ConversationManager, '_run_focused_py_compile', return_value=(True, "graph_main_window.py: ok")) as compile_mock:
            manager.send(
                on_event=events.append,
                approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
                cancel_event=threading.Event(),
                model="test-model",
                thinking="off",
                hook_name="generate_worker_code",
            )
    finally:
        hooks.unregister("generate_worker_code")

    compile_mock.assert_called_once_with(["graph_main_window.py"])
    validation_events = [
        ev for ev in events
        if isinstance(ev, ToolResult)
        and ev.name == "run_terminal_command"
        and ev.tool_call_id == "auto_py_compile"
    ]
    assert len(validation_events) == 1
    payload = json.loads(validation_events[0].result)
    assert payload["ok"] is True
    assert payload["command"] == "python -m py_compile graph_main_window.py"
    assert payload["auto_validation"] is True
    content = history.messages[-1]["content"]
    assert "failure_class" not in content
    assert "error" not in content


def test_auto_py_compile_failure_triggers_repair_and_recovers(tmp_path):
    """Test B: Auto py_compile fails, repair instruction sent, Worker fixes,
    auto-py_compile passes on retry."""
    history = History()
    tools = MagicMock(spec=ToolRegistry)
    tools.tool_defs.return_value = []
    type(tools).mode = PropertyMock(return_value="worker")
    type(tools).workspace_root = PropertyMock(return_value=tmp_path)
    # Two writes: original, then repair
    tools.execute.side_effect = [
        ToolExecResult(
            ok=True,
            payload={"ok": True, "path": "bad_syntax.py", "applied": "write_file", "is_new_file": False},
        ),
        ToolExecResult(
            ok=True,
            payload={"ok": True, "path": "bad_syntax.py", "applied": "write_file", "is_new_file": False},
        ),
    ]
    manager = ConversationManager(history, tools)
    events = []
    tc_write = _tool_call("w1", "write_file", {"path": "bad_syntax.py", "content": "x = 1\n"})
    tc_repair = _tool_call("w2", "write_file", {"path": "bad_syntax.py", "content": "x = 1\n"})
    hook = MagicMock(
        side_effect=[
            iter([_done(tool_calls=[tc_write])]),   # 1: write original file
            iter([_done("Done.")]),                  # 2: stop -> auto-py_compile FAIL -> repair instruction
            iter([_done(tool_calls=[tc_repair])]),   # 3: write repaired file
            iter([_done("Done.")]),                  # 4: stop -> auto-py_compile PASS -> return
        ]
    )

    auto_compile_calls = 0

    def _fake_compile(paths):
        nonlocal auto_compile_calls
        auto_compile_calls += 1
        if auto_compile_calls == 1:
            return (False, "bad_syntax.py: SyntaxError: invalid syntax at line 1")
        return (True, "bad_syntax.py: ok")

    try:
        _register_worker_hook(hook)
        with patch.object(ConversationManager, '_run_focused_py_compile', side_effect=_fake_compile):
            manager.send(
                on_event=events.append,
                approval_cb=MagicMock(return_value=ApprovalDecision("approve")),
                cancel_event=threading.Event(),
                model="test-model",
                thinking="off",
                hook_name="generate_worker_code",
            )
    finally:
        hooks.unregister("generate_worker_code")

    # Auto-py_compile should have been called twice
    assert auto_compile_calls == 2
    validation_events = [
        ev for ev in events
        if isinstance(ev, ToolResult)
        and ev.name == "run_terminal_command"
        and ev.tool_call_id == "auto_py_compile"
    ]
    assert len(validation_events) == 2
    assert json.loads(validation_events[0].result)["ok"] is False
    assert json.loads(validation_events[1].result)["ok"] is True
    # The focused py_compile diagnostic message should be in history
    assert any(
        msg.get("role") == "user"
        and "Focused py_compile failed" in str(msg.get("content"))
        for msg in history.messages
    )
    # Final completion — no failure_class
    content = history.messages[-1]["content"]
    assert "failure_class" not in content
    assert "error" not in content


def test_auto_py_compile_scratch_paths_filtered():
    """Test C: Scratch validation paths (.aura/tmp/...) are filtered from
    syntax_validation_required by _is_validation_scratch_path."""
    from aura.conversation.manager import _is_validation_scratch_path

    # Scratch paths should be marked as scratch
    assert _is_validation_scratch_path(".aura/tmp/dump_doubleclick.py"), "dump_* should be scratch"
    assert _is_validation_scratch_path(".aura/tmp/_check_something.py"), "_check_* should be scratch"
    assert _is_validation_scratch_path(".aura/tmp/_tmp_inspect_wear.py"), "_tmp_inspect* should be scratch"
    assert _is_validation_scratch_path("_tmp_inspect_wear.py"), "root _tmp_inspect* should be scratch"
    assert _is_validation_scratch_path(".aura/tmp/tmp_tempfile.py"), "tmp_* in .aura/tmp/ should be scratch"
    assert _is_validation_scratch_path(".aura/tmp/check_test.py"), "check* in .aura/tmp/ should be scratch"

    # Product paths should NOT be scratch
    assert not _is_validation_scratch_path("graph_main_window.py"), "product paths should NOT be scratch"
    assert not _is_validation_scratch_path(".aura/tools/some_tool.py"), "non-tmp .aura paths should NOT be scratch"
    assert not _is_validation_scratch_path(".aura/tmp/foo.py"), "foo.py in .aura/tmp/ should NOT be scratch (name doesn't start with dump/_check/check/tmp)"

    # Non-.py files should not be scratch
    assert not _is_validation_scratch_path(".aura/tmp/dump_data.json"), "non-.py should NOT be scratch"


def test_normalize_worker_path_variants():
    """Test D: _normalize_worker_path handles ./ prefix, slashes, and preserves
    dot-prefixed directories."""
    from aura.conversation.manager import _normalize_worker_path, _is_validation_scratch_path

    # Leading ./ is stripped
    assert _normalize_worker_path("./graph_main_window.py") == "graph_main_window.py"
    assert _normalize_worker_path("./workers.py") == "workers.py"

    # No-op for clean relative paths
    assert _normalize_worker_path("graph_main_window.py") == "graph_main_window.py"
    assert _normalize_worker_path("video_workflow.py") == "video_workflow.py"

    # Dot-prefixed directories preserved
    assert _normalize_worker_path(".aura/tmp/foo.py") == ".aura/tmp/foo.py"
    assert _normalize_worker_path(".venv/lib/site.py") == ".venv/lib/site.py"

    # Backslash normalized to forward slash (Windows path separator)
    # Note: .\\foo means "./foo" (current-dir/foo), NOT ".foo" (dot-prefixed dir)
    assert _normalize_worker_path(".aura\\tmp\\dump_bar.py") == ".aura/tmp/dump_bar.py"
    assert _is_validation_scratch_path(".aura\\tmp\\dump_bar.py"), "normalized backslash path should be scratch"
