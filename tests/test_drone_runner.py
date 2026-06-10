from __future__ import annotations

import threading
import time

from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.drones.definition import DroneBudget, DroneDefinition, default_tools_for_policy
from aura.drones.runner import DroneRunner


def _write_capable_drone() -> DroneDefinition:
    return DroneDefinition(
        id="writer",
        name="Writer",
        description="Propose writes.",
        instructions="Update one file.",
        write_policy="normal_diff_approval",
        allowed_tools=default_tools_for_policy("normal_diff_approval"),
        output_contract="Summary.",
        budget=DroneBudget(max_tool_rounds=2, timeout_seconds=30),
    )


def _approval_request() -> ApprovalRequest:
    return ApprovalRequest(
        tool_name="write_file",
        rel_path="a.py",
        old_content="",
        new_content="print('hi')\n",
        is_new_file=True,
    )


def test_drone_approval_callback_times_out_and_records_receipt_error(monkeypatch, tmp_path) -> None:
    import aura.drones.runner as runner_module

    monkeypatch.setattr(runner_module, "DRONE_APPROVAL_TIMEOUT_SECONDS", 0.01)
    runner = DroneRunner(tmp_path, _write_capable_drone())
    requested: list[ApprovalRequest] = []
    runner.approval_requested.connect(requested.append)
    errors: list[str] = []

    decision = runner._build_approval_callback(errors)(_approval_request())

    assert decision.action == "reject"
    assert decision.metadata["approval_timeout"] is True
    assert decision.metadata["failure_class"] == "approval_timeout"
    assert decision.metadata["rel_path"] == "a.py"
    assert errors == ["Drone diff approval timed out after 0s for write_file on a.py."]
    assert len(requested) == 1
    assert requested[0].approval_id
    assert requested[0].approval_timeout_seconds == 0.01


def test_drone_approval_callback_accepts_matching_approval_id(tmp_path) -> None:
    runner = DroneRunner(tmp_path, _write_capable_drone())
    completed: list[ApprovalDecision] = []

    def invoke_callback() -> None:
        completed.append(runner._build_approval_callback([])(_approval_request()))

    thread = threading.Thread(target=invoke_callback)
    thread.start()
    try:
        deadline = time.monotonic() + 1
        while runner._approval_id is None and time.monotonic() < deadline:
            time.sleep(0.001)
        assert runner._approval_id is not None
        runner.set_approval_result(
            ApprovalDecision(action="approve", metadata={"source": "test"}),
            approval_id=runner._approval_id,
        )
    finally:
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert completed == [ApprovalDecision(action="approve", metadata={"source": "test"})]


def test_drone_approval_callback_ignores_stale_approval_id(monkeypatch, tmp_path) -> None:
    import aura.drones.runner as runner_module

    monkeypatch.setattr(runner_module, "DRONE_APPROVAL_TIMEOUT_SECONDS", 0.01)
    runner = DroneRunner(tmp_path, _write_capable_drone())
    requested: list[ApprovalRequest] = []
    runner.approval_requested.connect(requested.append)

    decision = runner._build_approval_callback([])(_approval_request())
    runner.set_approval_result(
        ApprovalDecision(action="approve"),
        approval_id=requested[0].approval_id,
    )

    assert decision.action == "reject"
    assert decision.metadata["approval_timeout"] is True


def test_runner_blocks_tool_not_in_allowed_set(tmp_path) -> None:
    """Prove a DroneRunner's surface would block a tool outside allowed_tools."""
    drone = DroneDefinition(
        id="test",
        name="Test",
        description="",
        instructions="",
        write_policy="read_only",
        allowed_tools=("read_file",),
        output_contract="",
        budget=DroneBudget(max_tool_rounds=1, timeout_seconds=30),
    )
    from aura.drones.tool_surface import build_drone_tool_surface

    surface = build_drone_tool_surface(tmp_path, drone)
    assert "write_file" not in surface.allowed_tools
    assert "read_file" in surface.allowed_tools
