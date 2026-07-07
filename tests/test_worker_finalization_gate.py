from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from aura.client import Event
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.worker_finalization_gate import handle_worker_candidate_finalization
from aura.conversation.worker_flow import WorkerFlowHarness


def _finalize(
    state: _SendState,
    tmp_path: Path,
    *,
    explicit_validation_commands: list[str] | None = None,
    declared_run_command: str | None = None,
    on_event: list[Event] | None = None,
    finish=None,
):
    history = History()
    finish = finish or MagicMock()
    events = on_event if on_event is not None else []
    
    msg = {
        "role": "assistant",
        "content": "Changed files: a.py\nValidation: pytest passed.",
        "reasoning_content": None,
    }
    
    action = handle_worker_candidate_finalization(
        state=state,
        full_message=msg,
        history=history,
        workspace_root=tmp_path,
        on_event=events.append,
        finish_worker_recoverable_followup=finish,
        handle_worker_flow_steering=lambda _s, _e: "none",
        handle_worker_zero_work_final=lambda _s, _e: "none",
        explicit_validation_commands=explicit_validation_commands,
        declared_run_command=declared_run_command,
    )
    return action, history, finish


def test_multi_problem_structural_tier_batching(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_py_compile", 
        lambda paths, workspace_root: (False, "Aggregate Error"))
    
    def mock_py_compile(paths, workspace_root):
        if len(paths) > 1: return False, "Aggregate error"
        if paths[0] == "a.py": return False, "Syntax error in a.py"
        return True, ""
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_py_compile", mock_py_compile)
    
    def mock_import(root, paths):
        if len(paths) > 1: return False, "Aggregate import error"
        if paths[0] == "b.py": return False, "Import error in b.py"
        return True, ""
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_import_check", mock_import)
    
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.syntax_validation_required.update(["a.py", "b.py"])
    
    action, history, finish = _finalize(s, tmp_path)
    
    assert action == "continue"
    assert finish.call_count == 0
    
    user_msg = history.messages[-1]["content"]
    assert "Validation found 2 problems" in user_msg
    assert "Syntax error in a.py" in user_msg
    assert "Import error in b.py" in user_msg
    
    assert "a.py" in s.syntax_repair_required
    assert "b.py" in s.import_verification_required
    assert not s.syntax_validation_required


def test_structural_fingerprint_skip(tmp_path: Path, monkeypatch):
    mock_py_compile = MagicMock(return_value=(True, ""))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_py_compile", mock_py_compile)
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_import_check", MagicMock(return_value=(True, "")))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_dependent_import_check", MagicMock(return_value=([], "", "")))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.compute_dependents", MagicMock(return_value=[]))
    
    (tmp_path / "a.py").write_text("x=1")
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.syntax_validation_required.add("a.py")
    
    _finalize(s, tmp_path)
    assert mock_py_compile.call_count == 1
    assert s.last_structural_ok_fingerprint is not None
    
    # second pass
    s.syntax_validation_required.add("a.py")
    _finalize(s, tmp_path)
    assert mock_py_compile.call_count == 1 # unchanged


def test_escalation_paths_unchanged(tmp_path: Path):
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.syntax_repair_required = {"a.py": {"repair_failed": True, "error": "Syntax!"}}
    s.worker_recovery_nudge_sent = True
    action, history, finish = _finalize(s, tmp_path)
    assert action == "finished"
    finish.assert_called_once()
    assert finish.call_args[1]["failure_class"] == "syntax_invalid"


def test_product_validation_failed_escalation(tmp_path: Path, monkeypatch):
    from aura.conversation.validation_failure_routing import ValidationFailureVerdict

    mock_val = MagicMock(
        ok=False,
        command="pytest",
        diagnostics="Failed!",
        runs=None,
    )
    monkeypatch.setattr(
        "aura.conversation.worker_finalization_gate.run_explicit_validation_commands",
        MagicMock(return_value=mock_val),
    )
    monkeypatch.setattr(
        "aura.conversation.worker_finalization_gate.route_validation_failure",
        MagicMock(return_value=ValidationFailureVerdict(
            action="handback",
            handback_details={
                "failure_class": "product_validation_failed",
                "error": "stall detected",
                "details": {"command": "pytest", "diagnostics": "Failed!"},
            },
        )),
    )
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    action, history, finish = _finalize(s, tmp_path, explicit_validation_commands=["pytest"])
    assert action == "finished"
    finish.assert_called_once()
    assert finish.call_args[1]["failure_class"] == "product_validation_failed"


def test_clean_structural_pass_no_import_failure_emitted(tmp_path: Path, monkeypatch):
    mock_py_compile = MagicMock(return_value=(True, ""))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_py_compile", mock_py_compile)
    mock_import = MagicMock(return_value=(True, ""))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_import_check", mock_import)
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_dependent_import_check", MagicMock(return_value=([], "", "")))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.compute_dependents", MagicMock(return_value=[]))
    mock_emit_import = MagicMock()
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.emit_auto_import_result", mock_emit_import)

    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.syntax_validation_required.add("a.py")
    
    events = []
    _finalize(s, tmp_path, on_event=events)
    
    assert mock_py_compile.call_count == 1
    mock_emit_import.assert_not_called()


def test_partial_structural_failure_does_not_satisfy_validation(tmp_path: Path, monkeypatch):
    def mock_py_compile(paths, workspace_root):
        if len(paths) > 1: return False, "Aggregate error"
        if paths[0] == "a.py": return False, "Syntax error in a.py"
        return True, ""
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_py_compile", mock_py_compile)
    
    def mock_import(root, paths):
        return True, ""
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_focused_import_check", mock_import)
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.run_dependent_import_check", MagicMock(return_value=([], "", "")))
    monkeypatch.setattr("aura.conversation.worker_finalization_gate.compute_dependents", MagicMock(return_value=[]))
    
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.worker_flow.observe_tool_result("write_file", {"path": "a.py"}, True, {"ok": True, "path": "a.py", "applied": True})
    
    s.syntax_validation_required.update(["a.py", "b.py"])
    
    assert s.worker_flow.requires_validation_before_final()
    
    action, history, finish = _finalize(s, tmp_path)
    
    assert action == "continue"
    assert finish.call_count == 0
    assert s.worker_flow.requires_validation_before_final()
