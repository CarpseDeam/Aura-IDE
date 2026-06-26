"""Regression tests for WorkerEventRelay."""

from __future__ import annotations

import json
from unittest.mock import Mock

from aura.bridge.event_relay import WorkerEventRelay
from aura.client.events import ToolCallArgsDelta, ToolCallEnd, ToolCallStart, ToolResult


def test_worker_event_relay_tool_call_lifecycle_tracks_index_to_id() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    starts: list[tuple[str, str, str]] = []
    args: list[tuple[str, str, str]] = []
    ends: list[tuple[str, str]] = []

    relay.toolCallStart.connect(lambda parent, worker_id, name: starts.append((parent, worker_id, name)))
    relay.toolCallArgs.connect(lambda parent, worker_id, chunk: args.append((parent, worker_id, chunk)))
    relay.toolCallEnd.connect(lambda parent, worker_id: ends.append((parent, worker_id)))

    relay.relay("dispatch-1", ToolCallStart(index=0, id="worker-tool-1", name="read_file"))
    relay.relay("dispatch-1", ToolCallArgsDelta(index=0, args_chunk='{"path": "a.py"}'))
    relay.relay("dispatch-1", ToolCallEnd(index=0))

    assert starts == [("dispatch-1", "worker-tool-1", "read_file")]
    assert args == [("dispatch-1", "worker-tool-1", '{"path": "a.py"}')]
    assert ends == [("dispatch-1", "worker-tool-1")]


def test_patch_file_craft_block_is_not_touched() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = (
        '{"ok": false, "applied": false, '
        '"path": "a.py", "failure_class": "craft_blocked", '
        '"write_outcome": "not_applied_craft_rejected", '
        '"error": "Repair patch", "craft_issues": []}'
    )

    relay.relay(
        "dispatch-1",
        ToolResult(tool_call_id="worker-tool-1", name="patch_file", ok=False, result=payload),
    )

    assert relay.not_applied_writes[0]["tool"] == "patch_file"
    assert relay.not_applied_writes[0]["failure_class"] == "craft_blocked"
    assert relay.write_results == []
    assert relay.touched_files == set()


def test_read_files_tracks_successful_files_mapping_entries() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = {
        "ok": True,
        "files": {
            "a.py": {
                "ok": True,
                "path": "a.py",
                "content": "a = 1\n",
                "content_hash": "hash-a",
                "file_size": 6,
                "truncated": False,
            },
            "missing.py": {"ok": False, "error": "file not found"},
            "large.py": {
                "ok": True,
                "path": "large.py",
                "content": "x",
                "content_hash": "hash-large",
                "file_size": 300000,
                "truncated": True,
            },
        },
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="read_files",
            ok=True,
            result=json.dumps(payload),
        ),
    )

    assert relay.read_files == {"a.py"}


def test_craft_metadata_is_preserved_on_not_applied_write() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = {
        "ok": False,
        "applied": False,
        "path": "a.py",
        "failure_class": "craft_blocked",
        "write_outcome": "not_applied_craft_rejected",
        "error": "Placeholder",
        "craft_issues": [{"code": "task-shape-placeholder-body"}],
        "craft_metadata": {
            "task_shape": {
                "task_kind": "new_tool_or_app",
                "product_flow": ["run the main action"],
                "state_concepts": ["job/task"],
                "craft_pressure": ["block placeholder/demo/fake production code"],
            }
        },
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="write_file",
            ok=False,
            result=json.dumps(payload),
        ),
    )

    assert relay.not_applied_writes[0]["craft_metadata"]["task_shape"]["task_kind"] == "new_tool_or_app"


def test_delete_file_success_is_tracked_as_applied_write() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = {
        "ok": True,
        "applied": True,
        "deleted": True,
        "path": "old.py",
        "rel_path": "old.py",
        "write_outcome": "deleted",
        "backup": ".aura/backups/ts/old.py",
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="delete_file",
            ok=True,
            result=json.dumps(payload),
        ),
    )

    assert relay.write_results == [
        {
            "tool": "delete_file",
            "path": "old.py",
            "is_new_file": False,
            "deleted": True,
            "applied": True,
            "applied_tool": "delete_file",
            "write_outcome": "deleted",
            "backup": ".aura/backups/ts/old.py",
        }
    ]
    assert relay.touched_files == {"old.py"}
    assert relay.edited_existing_files == []


def test_terminal_results_include_capped_output_and_preview() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    output = "x" * 5000
    payload = {
        "ok": True,
        "command": "python -m py_compile a.py",
        "exit_code": 0,
        "output": output,
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="run_terminal_command",
            ok=True,
            result=json.dumps(payload),
        ),
    )

    assert relay.terminal_results[0]["output"] == output[:4000]
    assert relay.terminal_results[0]["output_preview"] == output[:200]
    assert relay.validation_results == relay.terminal_results


def test_raw_rg_terminal_result_is_not_validation() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = {
        "ok": False,
        "command": 'rg "show_response" app/tray.py | rg "No recent"',
        "exit_code": 1,
        "output": "",
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="run_terminal_command",
            ok=False,
            result=json.dumps(payload),
        ),
    )

    assert relay.terminal_results == [
        {
            "command": 'rg "show_response" app/tray.py | rg "No recent"',
            "ok": False,
            "exit_code": 1,
            "output": "",
            "output_preview": "",
        }
    ]
    assert relay.validation_results == []


def test_shell_assertion_search_terminal_result_is_validation() -> None:
    relay = WorkerEventRelay(approval_proxy=Mock(), worker_model="test-model")
    payload = {
        "ok": True,
        "command": 'rg "old" app/tray.py && exit 1 || exit 0',
        "exit_code": 0,
        "output": "",
    }

    relay.relay(
        "dispatch-1",
        ToolResult(
            tool_call_id="worker-tool-1",
            name="run_terminal_command",
            ok=True,
            result=json.dumps(payload),
        ),
    )

    assert relay.validation_results == relay.terminal_results
