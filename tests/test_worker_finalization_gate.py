from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

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


def test_syntax_failure_repeatable_feedback(tmp_path: Path):
    """Syntax failure no longer escalates to finished — always gives feedback."""
    s = _SendState(mode="worker", research_policy=None)
    s.worker_flow = WorkerFlowHarness()
    s.syntax_repair_required = {"a.py": {"repair_failed": True, "error": "Syntax!"}}
    action, history, finish = _finalize(s, tmp_path)
    assert action == "continue"


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


# ---------------------------------------------------------------------------
# Phase 4 — mark_explicit_validation_passed is a method, not a property setter
# ---------------------------------------------------------------------------


def test_worker_explicit_validation_passed_is_read_only_property() -> None:
    """worker_explicit_validation_passed must raise AttributeError on direct
    assignment — it is a @property without a setter.  Only
    mark_explicit_validation_passed() can set it."""
    s = _SendState(mode="worker", research_policy=None)
    with pytest.raises(AttributeError, match="no setter"):
        s.worker_explicit_validation_passed = True  # type: ignore[attr-defined]


def test_mark_explicit_validation_passed_sets_flag() -> None:
    """After mark_explicit_validation_passed(), the property returns True."""
    s = _SendState(mode="worker", research_policy=None)
    assert s.worker_explicit_validation_passed is False
    s.mark_explicit_validation_passed()
    assert s.worker_explicit_validation_passed is True


def test_mark_explicit_validation_passed_records_at_current_write_snapshot() -> None:
    """mark_explicit_validation_passed records at the current applied-write
    count, so a subsequent write invalidates it."""
    s = _SendState(mode="worker", research_policy=None)
    assert s.worker_flow is not None
    s.worker_flow.observe_tool_result(
        "write_file", {"path": "a.py"}, True,
        {"ok": True, "path": "a.py", "applied": True},
    )
    assert s.applied_write_count() == 1
    s.mark_explicit_validation_passed()
    assert s.worker_explicit_validation_passed is True
    # Another write advances the snapshot — validation is now stale.
    s.worker_flow.observe_tool_result(
        "write_file", {"path": "b.py"}, True,
        {"ok": True, "path": "b.py", "applied": True},
    )
    assert s.worker_explicit_validation_passed is False


def test_worker_explicit_validation_passed_defaults_to_false() -> None:
    """A fresh _SendState has worker_explicit_validation_passed=False."""
    s = _SendState(mode="worker", research_policy=None)
    assert s.worker_explicit_validation_passed is False
