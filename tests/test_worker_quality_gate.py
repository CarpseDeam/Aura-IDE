from __future__ import annotations

from pathlib import Path

from aura.client import Done
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.worker_quality import QualityFinding, WorkerQualityDecision
from aura.conversation import worker_quality_gate


def test_worker_quality_gate_sends_cleanup_on_first_warning(tmp_path: Path, monkeypatch):
    state = _state_with_write()
    history = History()
    _prepare_gate(monkeypatch, tmp_path, _warning_decision())

    action = worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=lambda event: None,
    )

    assert action == "cleanup"
    assert state.worker_quality_cleanup_attempted is True
    assert state.last_quality_ok_fingerprint is None
    assert history.messages[-1]["role"] == "user"
    assert "Findings:" in history.messages[-1]["content"]


def test_worker_quality_gate_releases_warning_findings_after_cleanup(
    tmp_path: Path,
    monkeypatch,
):
    state = _state_with_write()
    state.worker_quality_cleanup_attempted = True
    state.worker_quality_nudge_sent = True
    history = History()
    events = []
    _prepare_gate(monkeypatch, tmp_path, _warning_decision())

    action = worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=events.append,
    )

    assert action == "none"
    assert state.last_quality_ok_fingerprint is None
    assert state.last_quality_findings[0]["kind"] == "large_diff_whole_file_rewrite"
    assert state.last_quality_findings[0]["severity"] == "warning"
    assert not any(isinstance(event, Done) and event.finish_reason == "stop" for event in events)
    assert all(
        "worker_quality_unresolved_findings" not in str(message.get("content", ""))
        for message in history.messages
    )


def test_worker_quality_gate_does_not_set_clean_fingerprint_when_findings_remain(
    tmp_path: Path,
    monkeypatch,
):
    state = _state_with_write()
    state.worker_quality_cleanup_attempted = True
    history = History()
    _prepare_gate(monkeypatch, tmp_path, _warning_decision())

    worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=lambda event: None,
    )

    assert state.last_quality_ok_fingerprint is None


def test_worker_quality_gate_records_clean_fingerprint_only_without_findings(
    tmp_path: Path,
    monkeypatch,
):
    state = _state_with_write()
    history = History()
    _prepare_gate(monkeypatch, tmp_path, _clean_decision())

    action = worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=lambda event: None,
    )

    assert action == "none"
    assert state.last_quality_ok_fingerprint == "clean-fingerprint"
    assert state.last_quality_findings == []


def test_worker_quality_gate_passes_dispatch_scope_to_evaluator(
    tmp_path: Path,
    monkeypatch,
):
    state = _state_with_write()
    state.dispatched_target_files = ["aura/expected.py"]
    history = History()
    captured = {}

    def fake_evaluate(*args, **kwargs):
        captured["expected_files"] = kwargs.get("expected_files")
        return _clean_decision()

    _prepare_gate(monkeypatch, tmp_path, _clean_decision())
    monkeypatch.setattr(worker_quality_gate, "evaluate_worker_quality", fake_evaluate)

    worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=lambda event: None,
    )

    assert captured["expected_files"] == ["aura/expected.py"]


def test_worker_quality_gate_passes_none_for_empty_dispatch_scope(
    tmp_path: Path,
    monkeypatch,
):
    state = _state_with_write()
    state.dispatched_target_files = []
    history = History()
    captured = {}

    def fake_evaluate(*args, **kwargs):
        captured["expected_files"] = kwargs.get("expected_files")
        return _clean_decision()

    _prepare_gate(monkeypatch, tmp_path, _clean_decision())
    monkeypatch.setattr(worker_quality_gate, "evaluate_worker_quality", fake_evaluate)

    worker_quality_gate.handle_worker_quality_gate(
        state=state,
        workspace_root=tmp_path,
        history=history,
        on_event=lambda event: None,
    )

    assert captured["expected_files"] is None
    assert state.last_quality_ok_fingerprint == "clean-fingerprint"


def _state_with_write() -> _SendState:
    state = _SendState(mode="worker", research_policy=None)
    state.worker_app_writes.add("aura/changed.py")
    return state


def _prepare_gate(
    monkeypatch,
    tmp_path: Path,
    decision: WorkerQualityDecision,
) -> None:
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(worker_quality_gate, "fingerprint_paths", lambda paths, root: "clean-fingerprint")
    monkeypatch.setattr(worker_quality_gate, "_diff_changed_files", lambda root, changed_files: "")
    monkeypatch.setattr(
        worker_quality_gate,
        "evaluate_worker_quality",
        lambda *args, **kwargs: decision,
    )


def _clean_decision() -> WorkerQualityDecision:
    return WorkerQualityDecision(
        ok=True,
        hard_block=False,
        needs_cleanup=False,
        findings=[],
    )


def _warning_decision() -> WorkerQualityDecision:
    finding = QualityFinding(
        kind="large_diff_whole_file_rewrite",
        severity="warning",
        file="aura/changed.py",
        line=None,
        message="Changed line count is above the review threshold.",
        suggested_action="Narrow the patch.",
    )
    return WorkerQualityDecision(
        ok=False,
        hard_block=False,
        needs_cleanup=True,
        findings=[finding],
        instruction="Findings:\n- aura/changed.py - Narrow the patch.",
    )
