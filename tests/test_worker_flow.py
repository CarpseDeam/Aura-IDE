from __future__ import annotations

import json

from aura.conversation.worker_flow import (
    WORKER_FLOW_STEERING_TEXT,
    WorkerFlowHarness,
    WorkerFlowPhase,
)


def _lock_inventory(harness: WorkerFlowHarness) -> None:
    harness.observe_assistant_message(
        {
            "role": "assistant",
            "content": (
                "I will extract helpers from aura/conversation/dispatch.py into "
                "aura/bridge/worker_report.py and run python -m pytest tests/test_dispatch.py."
            ),
        }
    )
    assert harness.state.inventory_locked is True


def test_repeated_broad_reads_of_same_large_file_after_inventory_lock_produce_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    args = {"path": "aura/conversation/dispatch.py"}
    payload = json.dumps(
        {
            "ok": True,
            "path": "aura/conversation/dispatch.py",
            "file_size": 200_000,
            "content": "x",
        }
    )
    harness.observe_tool_call("read_file", args)
    harness.observe_tool_result("read_file", args, True, payload)
    assert harness.pending_steering_message == ""

    harness.observe_tool_call("read_file", args)

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT


def test_repeated_complete_picture_plan_restatements_with_no_writes_produce_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    harness.observe_assistant_message("Now I have the complete picture, I will plan the extraction.")
    assert harness.pending_steering_message == ""

    harness.observe_assistant_message("Let me plan this again now that I have the full picture.")

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT


def test_whole_file_reconstruction_intent_during_move_only_extraction_produces_steering() -> None:
    harness = WorkerFlowHarness()

    harness.observe_assistant_message(
        "For this move-only extraction from dispatch.py, I will reconstruct the entire file from scratch."
    )
    harness.observe_tool_call(
        "write_file",
        {
            "path": "aura/conversation/dispatch.py",
            "content": "# replacement",
            "full_replace_existing": True,
            "replacement_reason": "move-only extraction",
        },
    )

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT


def test_write_action_advances_phase_and_reduces_orientation_pressure() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)
    harness.observe_assistant_message("Now I have the complete picture and will plan the move.")
    assert harness.state.planning_restatements_since_write == 1

    harness.observe_tool_call("patch_file", {"path": "aura/conversation/dispatch.py", "edits": []})
    harness.observe_tool_result(
        "patch_file",
        {"path": "aura/conversation/dispatch.py", "edits": []},
        True,
        {"ok": True, "path": "aura/conversation/dispatch.py", "applied": True},
    )

    assert harness.state.phase == WorkerFlowPhase.editing
    assert harness.state.write_actions == 1
    assert harness.state.planning_restatements_since_write == 0
    assert harness.pending_steering_message == ""


def test_validation_action_advances_phase_to_validating() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    args = {"command": "python -m pytest tests/test_worker_flow.py -q"}
    harness.observe_tool_call("run_terminal_command", args)
    harness.observe_tool_result("run_terminal_command", args, True, {"ok": True, "command": args["command"]})

    assert harness.state.phase == WorkerFlowPhase.validating
    assert harness.state.validation_actions == 1


def test_normal_first_pass_inspection_does_not_produce_steering() -> None:
    harness = WorkerFlowHarness()

    args = {"path": "aura/conversation/manager.py"}
    harness.observe_tool_call("read_file", args)
    harness.observe_tool_result(
        "read_file",
        args,
        True,
        {"ok": True, "path": args["path"], "file_size": 2_000, "content": "small"},
    )

    assert harness.state.inventory_locked is False
    assert harness.pending_steering_message == ""


def test_targeted_reads_after_inventory_lock_do_not_produce_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    args = {
        "path": "aura/conversation/dispatch.py",
        "start_line": 120,
        "end_line": 180,
    }
    harness.observe_tool_call("read_file_range", args)
    harness.observe_tool_result(
        "read_file_range",
        args,
        True,
        {
            "ok": True,
            "path": args["path"],
            "start_line": 120,
            "end_line": 180,
            "total_lines": 2_000,
            "file_size": 200_000,
            "content": "targeted",
        },
    )

    assert harness.state.targeted_reads_by_path["aura/conversation/dispatch.py"] == 1
    assert harness.pending_steering_message == ""


def test_harness_never_reports_a_fatal_or_blocking_outcome() -> None:
    harness = WorkerFlowHarness()

    harness.observe_assistant_message(
        "During this extraction I will replace the complete file dispatch.py."
    )

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT
    assert harness.has_fatal_outcome() is False
    assert harness.has_blocking_outcome() is False
    assert harness.fatal_outcome is None
    assert harness.blocking_outcome is None
