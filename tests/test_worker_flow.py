from __future__ import annotations

import json

from aura.conversation.worker_flow import (
    BROAD_ORIENTATION_TOOLS,
    WORKER_FLOW_LARGE_FILE_SEAM_TEXT,
    WORKER_FLOW_STEERING_TEXT,
    WorkerFlowHarness,
    WorkerFlowPhase,
)


def _tool_defs(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": name}} for name in names]


def _tool_names(tool_defs: list[dict]) -> set[str]:
    return {tool["function"]["name"] for tool in tool_defs}


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


def test_normal_first_pass_worker_inspection_exposes_broad_read_tools() -> None:
    harness = WorkerFlowHarness()
    tool_defs = _tool_defs("read_file", "grep_search", "read_file_range", "patch_file")

    filtered = harness.filter_tool_defs(tool_defs)

    assert filtered == tool_defs
    assert {"read_file", "grep_search"}.issubset(_tool_names(filtered))


def test_dense_planning_after_inventory_lock_filters_broad_orientation_tools() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)
    harness.observe_assistant_message(
        "Let me read the helper again. Now I have the full picture. Let me plan the hunks."
    )

    filtered = harness.filter_tool_defs(
        _tool_defs(
            "read_file",
            "read_files",
            "grep_search",
            "read_file_outline",
            "read_file_range",
            "find_usages",
            "patch_file",
            "run_terminal_command",
        )
    )
    names = _tool_names(filtered)

    assert harness.should_restrict_broad_orientation() is True
    assert not (names & BROAD_ORIENTATION_TOOLS)
    assert {
        "read_file_outline",
        "read_file_range",
        "find_usages",
        "patch_file",
        "run_terminal_command",
    }.issubset(names)


def test_broad_tool_call_during_ratchet_gets_recoverable_block() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)
    harness.observe_assistant_message(
        "Let me read the helper again. Now I have the full picture. Let me plan the hunks."
    )

    block = harness.should_block_tool("read_file", {"path": "aura/conversation/manager.py"})

    assert block is not None
    assert block["ok"] is False
    assert block["recoverable"] is True
    assert block["failure_class"] == "worker_flow_broad_orientation_restricted"
    assert "targeted reads" in block["suggested_next_action"]
    assert "read_file_outline" in block["allowed_tool_groups"]["targeted_reads"]


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


def test_single_dense_planning_message_after_inventory_lock_produces_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    harness.observe_assistant_message(
        "Let me read the helper one more time. Now I have the full picture. "
        "Let me plan the next hunk, then let me verify the imports."
    )

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT


def test_observed_worker_trace_planning_density_produces_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    harness.observe_assistant_message(
        "Let me read aura/conversation/worker_flow.py. Now I have the full contents. "
        "Let me check tests/imports. Now I have the full picture. "
        "Let me analyze imports/constants/regex/helpers and plan helpers module. "
        "Let me plan the hunks."
    )

    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT


def test_large_file_line_number_archaeology_produces_outline_steering() -> None:
    harness = WorkerFlowHarness()

    harness.observe_assistant_message(
        "Let me extract helpers from aura/conversation/manager.py. "
        "I need to find the exact line numbers and calculate exact line ranges. "
        "The method starts at line 1267 and ends at line 1497."
    )
    filtered = harness.filter_tool_defs(
        _tool_defs(
            "read_file",
            "grep_search",
            "read_file_outline",
            "read_file_range",
            "patch_file",
        )
    )
    names = _tool_names(filtered)

    assert harness.pending_steering_message == WORKER_FLOW_LARGE_FILE_SEAM_TEXT
    assert harness.state.pending_steering_reason == "line_number_archaeology"
    assert harness.should_restrict_broad_orientation() is True
    assert not (names & BROAD_ORIENTATION_TOOLS)
    assert {"read_file_outline", "read_file_range", "patch_file"}.issubset(names)


def test_repeated_large_file_line_number_archaeology_accumulates() -> None:
    harness = WorkerFlowHarness()

    harness.observe_assistant_message(
        "I will extract helpers from aura/conversation/manager.py and need exact line numbers."
    )
    assert harness.pending_steering_message == ""

    harness.observe_assistant_message(
        "Now I need line numbers for _run_worker so I can read around lines."
    )

    assert harness.pending_steering_message == WORKER_FLOW_LARGE_FILE_SEAM_TEXT
    assert harness.state.pending_steering_reason == "line_number_archaeology"


def test_extraction_hunk_mechanics_after_inventory_lock_produce_steering() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)

    harness.observe_assistant_message(
        "For the extraction, hunk one will remove imports, hunk two will add imports, "
        "and hunk three will remove helpers."
    )

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
    assert harness.requires_validation_before_final() is True
    assert harness.state.planning_restatements_since_write == 0
    assert harness.pending_steering_message == ""


def test_write_action_clears_dense_planning_pressure() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)
    harness.observe_assistant_message(
        "Let me read the helper again. Now I have the full picture. Let me plan the hunks."
    )
    assert harness.pending_steering_message == WORKER_FLOW_STEERING_TEXT

    harness.observe_tool_call("patch_file", {"path": "aura/conversation/worker_flow.py", "edits": []})

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


def test_successful_validation_clears_validation_required_state() -> None:
    harness = WorkerFlowHarness()
    _lock_inventory(harness)
    harness.observe_tool_call("patch_file", {"path": "aura/conversation/worker_flow.py", "edits": []})
    harness.observe_tool_result(
        "patch_file",
        {"path": "aura/conversation/worker_flow.py", "edits": []},
        True,
        {"ok": True, "path": "aura/conversation/worker_flow.py", "applied": True},
    )
    assert harness.requires_validation_before_final() is True

    args = {"command": "python -m py_compile aura/conversation/worker_flow.py"}
    harness.observe_tool_call("run_terminal_command", args)
    harness.observe_tool_result("run_terminal_command", args, True, {"ok": True, "command": args["command"]})

    assert harness.requires_validation_before_final() is False


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


def test_normal_concise_first_pass_plan_does_not_produce_steering() -> None:
    harness = WorkerFlowHarness()

    harness.observe_assistant_message(
        "I will inspect the relevant code, make the smallest focused edit, and run checks."
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
