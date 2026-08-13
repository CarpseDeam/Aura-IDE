"""Production execution projects one continuous conversation neutrally."""
from __future__ import annotations

from aura.bridge.production_execution import ProductionExecutionSession
from aura.client import ReasoningDelta, ToolCallStart


class _ApprovalProxy:
    def consume_last_event(self):
        return None


def test_session_uses_execution_signals() -> None:
    session = ProductionExecutionSession(_ApprovalProxy())
    started: list[str] = []
    reasoning: list[tuple[str, str]] = []
    session.executionStarted.connect(started.append)
    session.executionReasoningDelta.connect(lambda *args: reasoning.append(args))

    run_id = session.begin(model="test-model", run_id="prod-1")
    session.handle_event(ReasoningDelta(text="Inspecting the implementation."))
    session.handle_event(ToolCallStart(index=0, id="tool-1", name="read_file"))

    assert run_id == "prod-1"
    assert started == ["prod-1"]
    assert reasoning == [("prod-1", "Inspecting the implementation.")]
