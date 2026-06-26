from __future__ import annotations

import json

from aura.conversation.worker_smoothness import (
    WorkerSmoothnessState,
    observe_tool_result,
    phase_boundary_payload,
)


def test_repeated_reads_hit_recoverable_smoothness_boundary() -> None:
    state = WorkerSmoothnessState(max_reads_per_path_without_write=2)

    first = observe_tool_result(
        state,
        name="read_file",
        args={"path": r".\pkg\sample.py"},
        ok=True,
        payload_text=json.dumps({"ok": True}),
    )
    second = observe_tool_result(
        state,
        name="read_file",
        args={"path": "./pkg/sample.py"},
        ok=True,
        payload_text=json.dumps({"ok": True}),
    )

    assert first.allowed is True
    assert second.phase_boundary is True
    payload = phase_boundary_payload(second)
    assert payload["recoverable"] is True
    assert payload["reason"] == "worker_smoothness_phase_boundary"
    assert payload["details"]["budget_reason"] == "max_reads_per_path_without_write"
    assert payload["details"]["path"] == "pkg/sample.py"


def test_successful_terminal_payload_counts_as_progress() -> None:
    state = WorkerSmoothnessState(max_calls_without_progress=2)

    observe_tool_result(
        state,
        name="read_file",
        args={"path": "sample.py"},
        ok=True,
        payload_text=json.dumps({"ok": True}),
    )
    decision = observe_tool_result(
        state,
        name="run_terminal_command",
        args={"command": "python -m py_compile sample.py"},
        ok=True,
        payload_text=json.dumps({"_terminal_payload": {"ok": True}}),
    )

    assert decision.allowed is True
    assert state._total_calls_since_progress == 0
    assert state._last_progress_note.startswith("Terminal command succeeded:")
