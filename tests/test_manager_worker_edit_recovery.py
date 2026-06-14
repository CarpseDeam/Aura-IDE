from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.registry import ToolRegistry


def test_failed_transaction_returns_typed_blocker_without_recovery_roulette(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    content = json.dumps(
        {
            "ok": False,
            "path": "sample.py",
            "failure_class": "edit_transaction_symbol_not_found",
            "error": "Function 'missing' not found",
        }
    )

    updated = manager._update_worker_recovery_state(
        name="apply_edit_transaction",
        args={"path": "sample.py", "operations": [{"op": "replace_function", "symbol_name": "missing"}]},
        ok=False,
        content=content,
        edit_failed_shapes=set(),
        edit_fallback_required={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )

    payload = json.loads(updated)
    assert payload["failure_class"] == "edit_transaction_symbol_not_found"
    assert payload["recoverable"] is False
    assert "edit_line_range" not in payload.get("suggested_next_action", "")


def test_ambiguous_replace_text_once_is_nonrecoverable_with_specific_guidance(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    content = json.dumps(
        {
            "ok": False,
            "path": "sample.py",
            "failure_class": "edit_transaction_ambiguous_symbol",
            "error": "replace_text_once old text is ambiguous",
            "occurrence_count": 2,
        }
    )

    updated = manager._update_worker_recovery_state(
        name="apply_edit_transaction",
        args={
            "path": "sample.py",
            "operations": [
                {"op": "replace_text_once", "old": "value", "new": "changed"}
            ],
        },
        ok=False,
        content=content,
        edit_failed_shapes=set(),
        edit_fallback_required={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )

    payload = json.loads(updated)
    assert payload["recoverable"] is False
    assert "patch_file" in payload["suggested_next_action"]
    assert "occurrence" in payload["suggested_next_action"]
    assert "allow_multiple" in payload["suggested_next_action"]
    assert "edit_line_range" not in payload["suggested_next_action"]


def test_repeated_ambiguous_replace_text_once_shape_is_blocked(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    args = {
        "path": "sample.py",
        "operations": [
            {"op": "replace_text_once", "old": "value", "new": "changed"}
        ],
    }
    shape = manager._edit_shape_signature("apply_edit_transaction", args)

    blocked = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="apply_edit_transaction",
        args=args,
        edit_failed_shapes={shape, f"ambiguous-replace-text:{shape}"},
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )

    assert blocked is not None
    payload = json.loads(blocked["result_payload"])
    assert payload["failure_class"] == "edit_transaction_ambiguous_symbol"
    assert payload["recoverable"] is False
    assert payload["suggested_next_tool"] == "patch_file"
    assert "occurrence" in payload["suggested_next_action"]
    assert "allow_multiple" in payload["suggested_next_action"]
    assert "edit_line_range" not in payload["suggested_next_action"]


def test_syntax_repair_recovery_steers_to_patch_not_line_range(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair = {"broken.py": {"error": "SyntaxError"}}

    # read_file is always allowed during syntax repair
    blocked = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="read_file",
        args={"path": "other.py"},
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required=syntax_repair,
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )
    assert blocked is None

    # write_file with unrelated path is blocked during syntax repair
    blocked = manager._worker_recovery_block(
        tool_call_id="tc2",
        name="write_file",
        args={"path": "other.py"},
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required=syntax_repair,
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )
    assert blocked is not None
    payload = json.loads(blocked["result_payload"])
    assert payload["failure_class"] == "syntax_invalid"
    assert payload["suggested_next_tool"] == "patch_file"
    assert payload["suggested_next_tool"] != "edit_line_range"
    assert "edit_line_range" not in payload["suggested_next_action"]
    assert "patch_file" in payload["suggested_next_action"]

    # non-py_compile run_terminal_command is blocked during syntax repair
    blocked = manager._worker_recovery_block(
        tool_call_id="tc3",
        name="run_terminal_command",
        args={"command": "echo hello"},
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required=syntax_repair,
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )
    assert blocked is not None
    payload = json.loads(blocked["result_payload"])
    assert payload["failure_class"] == "syntax_invalid"


def test_syntax_repair_allows_targeted_py_compile(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m py_compile database.py"},
        {"database.py"},
    )


def test_syntax_repair_allows_python3_py_compile(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python3 -m py_compile ./pkg/database.py"},
        {"pkg/database.py"},
    )


def test_syntax_repair_blocks_unrelated_terminal_commands(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    # pytest with no specific file matching syntax_paths is still blocked
    assert not manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m pytest tests"},
        {"database.py"},
    )
    # pytest referencing a file whose name matches a syntax_path IS now allowed
    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m pytest tests/test_database.py"},
        {"database.py"},
    )
    # echo is not an allowed terminal command
    assert not manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "echo py_compile database.py"},
        {"database.py"},
    )


def test_syntax_repair_py_compile_path_normalization(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m py_compile ./database.py"},
        {"database.py"},
    )
    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": r"python -m py_compile .\pkg\database.py"},
        {"pkg/database.py"},
    )


def test_successful_py_compile_clears_normalized_syntax_state(tmp_workspace):
    (tmp_workspace / "database.py").write_text("")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair_required = {"./database.py": {"error": "SyntaxError"}}
    syntax_validation_required = {r".\database.py"}

    manager._update_syntax_state_from_terminal(
        args={"command": "python -m py_compile database.py"},
        loop_info={
            "_terminal_payload": {
                "ok": True,
                "command": "python -m py_compile database.py",
                "output": "",
            }
        },
        syntax_repair_required=syntax_repair_required,
        syntax_validation_required=syntax_validation_required,
    )

    assert syntax_repair_required == {}
    assert syntax_validation_required == set()


def test_failed_py_compile_records_normalized_syntax_path(tmp_workspace):
    (tmp_workspace / "database.py").write_text("")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair_required = {}
    syntax_validation_required = set()

    manager._update_syntax_state_from_terminal(
        args={"command": "python -m py_compile ./database.py"},
        loop_info={
            "_terminal_payload": {
                "ok": False,
                "command": "python -m py_compile ./database.py",
                "output": 'File "database.py", line 1\n    invalid syntax here\n         ^^^^^\nSyntaxError: invalid syntax',
            }
        },
        syntax_repair_required=syntax_repair_required,
        syntax_validation_required=syntax_validation_required,
    )

    assert set(syntax_repair_required) == {"database.py"}
    assert "SyntaxError: invalid syntax" in syntax_repair_required["database.py"]["error"]
    assert syntax_validation_required == set()


def test_patch_file_failure_does_not_block_different_patch_shape(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    args = {"path": "sample.py", "edits": [{"old": "missing", "new": "value"}]}
    shape = manager._edit_shape_signature("patch_file", args)
    fallback_required = {
        "sample.py": {
            "tool": "patch_file",
            "failure_class": "patch_hunk_not_found",
            "error": "patch_file hunk old block was not found.",
        }
    }

    blocked = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="patch_file",
        args={"path": "sample.py", "edits": [{"old": "current", "new": "value"}]},
        edit_failed_shapes=set(),
        edit_fallback_required=fallback_required,
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        patch_failed_cycles={shape: 1},
    )

    assert blocked is None

    manager._record_reads_for_recovery(
        "read_file",
        {"path": "sample.py"},
        {
            "ok": True,
            "path": "sample.py",
            "truncated": False,
            "content_hash": "hash-sample",
            "file_size": 10,
        },
        {},
        fallback_required,
        {},
    )

    assert fallback_required == {}
    unblocked = manager._worker_recovery_block(
        tool_call_id="tc2",
        name="patch_file",
        args={"path": "sample.py", "edits": [{"old": "current", "new": "value"}]},
        edit_failed_shapes=set(),
        edit_fallback_required=fallback_required,
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )
    assert unblocked is None


def test_patch_file_failure_reread_clears_normalized_path(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    fallback_required = {
        "pkg/sample.py": {
            "failure_class": "patch_hunk_not_found",
            "error": "patch_file hunk old block was not found.",
        }
    }
    line_range_required = {
        "pkg/lines.py": {
            "failure_class": "edit_mechanics_stale_line_range",
            "error": "stale range",
        }
    }

    manager._record_reads_for_recovery(
        "read_file",
        {"path": r".\pkg\sample.py"},
        {
            "ok": True,
            "path": r".\pkg\sample.py",
            "truncated": False,
            "content_hash": "hash-sample",
            "file_size": 10,
        },
        line_range_required,
        fallback_required,
        {},
    )
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "./pkg/lines.py"},
        {
            "ok": True,
            "path": "./pkg/lines.py",
            "truncated": False,
            "content_hash": "hash-lines",
            "file_size": 10,
        },
        line_range_required,
        fallback_required,
        {},
    )

    assert fallback_required == {}
    assert line_range_required == {}


def test_shell_failure_with_cd_prefix_does_not_trigger_syntax_repair(tmp_workspace):
    """Shell-level failures (cd /workspace not found) should NOT set syntax_repair_required."""
    (tmp_workspace / "database.py").write_text("dummy content")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair_required = {}
    syntax_validation_required = set()

    manager._update_syntax_state_from_terminal(
        args={"command": "cd /workspace && python -m py_compile database.py"},
        loop_info={
            "_terminal_payload": {
                "ok": False,
                "command": "cd /workspace && python -m py_compile database.py",
                "output": "The system cannot find the path specified.",
            }
        },
        syntax_repair_required=syntax_repair_required,
        syntax_validation_required=syntax_validation_required,
    )

    assert syntax_repair_required == {}


def test_real_py_compile_syntax_error_still_triggers_syntax_repair(tmp_workspace):
    """Real Python syntax errors should still set syntax_repair_required."""
    (tmp_workspace / "database.py").write_text("invalid syntax here")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair_required = {}
    syntax_validation_required = set()

    manager._update_syntax_state_from_terminal(
        args={"command": "python -m py_compile database.py"},
        loop_info={
            "_terminal_payload": {
                "ok": False,
                "command": "python -m py_compile database.py",
                "output": 'File "database.py", line 1\n    invalid syntax here\n         ^^^^^\nSyntaxError: invalid syntax',
            }
        },
        syntax_repair_required=syntax_repair_required,
        syntax_validation_required=syntax_validation_required,
    )

    assert "database.py" in syntax_repair_required
    assert syntax_repair_required["database.py"]["error"] is not None


def test_cd_wrapper_is_stripped_from_terminal_command(tmp_workspace):
    """The _CD_WRAPPER_RE regex correctly strips cd/chdir prefixes from commands."""
    from aura.conversation.tool_runner import _CD_WRAPPER_RE

    cases = [
        ("cd /workspace && python -m py_compile database.py",
         "python -m py_compile database.py"),
        ("cd /workspace ; python -m py_compile database.py",
         "python -m py_compile database.py"),
        ("chdir /workspace && python -m py_compile database.py",
         "python -m py_compile database.py"),
        ("cd frontend && npm test",
         "cd frontend && npm test"),
    ]
    for raw, expected in cases:
        result = _CD_WRAPPER_RE.sub('', raw, count=1).lstrip()
        assert result == expected, f"Failed on {raw!r}: got {result!r}"


def test_syntax_repair_allows_focused_pytest(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m pytest tests/test_database.py"},
        {"database.py"},
    )


def test_syntax_repair_blocks_broad_pytest(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert not manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m pytest tests"},
        {"database.py"},
    )


def test_stale_validation_note_on_passing_py_compile(tmp_workspace):
    (tmp_workspace / "database.py").write_text("x = 1")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    syntax_repair_required = {
        "database.py": {"awaiting_validation": False, "failed_repairs": 1}
    }
    syntax_validation_required = set()
    stale_notes: list[str] = []

    manager._update_syntax_state_from_terminal(
        args={"command": "python -m py_compile database.py"},
        loop_info={
            "_terminal_payload": {
                "ok": True,
                "command": "python -m py_compile database.py",
                "output": "",
            }
        },
        syntax_repair_required=syntax_repair_required,
        syntax_validation_required=syntax_validation_required,
        stale_validation_notes=stale_notes,
    )

    assert len(stale_notes) == 1
    assert "Stale validation cleared" in stale_notes[0]
    assert "database.py" in stale_notes[0]
    assert syntax_repair_required == {}


def test_read_only_tools_available_after_syntax_failure(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    for tool in (
        "read_file",
        "read_files",
        "grep_search",
        "read_file_outline",
        "search_codebase",
        "find_usages",
        "glob",
        "list_directory",
    ):
        assert manager._syntax_repair_tool_allowed(
            tool,
            {"path": "any.py"},
            {"broken.py"},
        ), f"{tool} should be allowed during syntax repair"


def test_patch_file_allowed_for_broken_file_after_syntax_failure(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "patch_file",
        {"path": "broken.py"},
        {"broken.py"},
    )
    # Unrelated path is still blocked
    assert not manager._syntax_repair_tool_allowed(
        "patch_file",
        {"path": "other.py"},
        {"broken.py"},
    )


def test_py_compile_allowed_for_broken_file_after_syntax_failure(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    assert manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "python -m py_compile broken.py"},
        {"broken.py"},
    )
    # Non-py_compile terminal command is still blocked
    assert not manager._syntax_repair_tool_allowed(
        "run_terminal_command",
        {"command": "echo hello"},
        {"broken.py"},
    )


def test_workspace_root_updates_propagate_to_tool_runner(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    assert manager._tool_runner._workspace_root == tmp_workspace

    new_root = Path("/different/path")
    manager.set_workspace_root(new_root)
    assert manager._tool_runner._workspace_root == new_root


def test_patch_file_hash_mismatch_is_recoverable_fallback(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required: dict = {}
    line_range_reread_required: dict = {}
    content = json.dumps({
        "ok": False,
        "path": "sample.py",
        "failure_class": "patch_file_hash_mismatch",
        "error": "patch_file hash mismatch — re-read and retry",
    })

    updated = manager._update_worker_recovery_state(
        name="patch_file",
        args={"path": "sample.py", "edits": [{"old": "x", "new": "y"}]},
        ok=False,
        content=content,
        edit_failed_shapes=set(),
        edit_fallback_required=edit_fallback_required,
        line_range_reread_required=line_range_reread_required,
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )

    payload = json.loads(updated)
    assert payload["failure_class"] == "patch_file_hash_mismatch"
    assert payload["recoverable"] is True
    assert payload["suggested_next_tool"] == "read_file"
    assert "sample.py" in edit_fallback_required
    assert "sample.py" not in line_range_reread_required


def test_worker_patch_file_without_expected_hash_is_blocked_for_existing_files(tmp_workspace):
    (tmp_workspace / "sample.py").write_text("value = 1\n", encoding="utf-8")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))

    blocked = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="patch_file",
        args={"path": "sample.py", "edits": [{"old": "value = 1\n", "new": "value = 2\n"}]},
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state={},
        patch_failed_cycles={},
    )

    assert blocked is not None
    payload = json.loads(blocked["result_payload"])
    assert payload["failure_class"] == "patch_file_missing_expected_hash"
    assert payload["recoverable"] is True
    assert payload["applied"] is False
    assert payload["suggested_next_tool"] == "read_file"
    assert "expected_file_hash" in payload["suggested_next_action"]


def test_worker_patch_file_with_unknown_or_stale_hash_is_blocked(tmp_workspace):
    target = tmp_workspace / "sample.py"
    target.write_text("value = 1\n", encoding="utf-8")
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    current_hash = hashlib.sha256(target.read_bytes()).hexdigest()

    unknown = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="patch_file",
        args={
            "path": "sample.py",
            "edits": [{"old": "value = 1\n", "new": "value = 2\n"}],
            "expected_file_hash": current_hash,
        },
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state={},
        patch_failed_cycles={},
    )
    assert unknown is not None
    unknown_payload = json.loads(unknown["result_payload"])
    assert unknown_payload["failure_class"] == "patch_file_hash_mismatch"
    assert unknown_payload["recoverable"] is True
    assert unknown_payload["stale"] is True

    stale = manager._worker_recovery_block(
        tool_call_id="tc2",
        name="patch_file",
        args={
            "path": "sample.py",
            "edits": [{"old": "value = 1\n", "new": "value = 2\n"}],
            "expected_file_hash": "stale",
        },
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state={
            "sample.py": {
                "content_hash": current_hash,
                "file_size": target.stat().st_size,
                "truncated": False,
                "last_read_tool": "read_file",
                "fresh_for_patch": True,
            }
        },
        patch_failed_cycles={},
    )
    assert stale is not None
    stale_payload = json.loads(stale["result_payload"])
    assert stale_payload["failure_class"] == "patch_file_hash_mismatch"
    assert stale_payload["latest_read_content_hash"] == current_hash
    assert stale_payload["recoverable"] is True
    assert stale_payload["stale"] is True


def test_recovery_state_not_cleared_by_failed_or_truncated_read(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required = {"x.py": {"failure_class": "patch_hunk_not_found"}}
    worker_file_state: dict = {}

    # Failed read — state NOT cleared
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "x.py"},
        {"ok": False, "path": "x.py", "truncated": False},
        {},
        edit_fallback_required,
        worker_file_state,
    )
    assert edit_fallback_required != {}

    # Truncated read — state NOT cleared
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "x.py"},
        {
            "ok": True,
            "path": "x.py",
            "truncated": True,
            "content_hash": "hash-x",
            "file_size": 250000,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    assert edit_fallback_required != {}

    # Metadata-free successful read — state NOT cleared
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "x.py"},
        {"ok": True, "path": "x.py", "truncated": False},
        {},
        edit_fallback_required,
        worker_file_state,
    )
    assert edit_fallback_required != {}

    # Successful non-truncated read with version metadata — state IS cleared
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "x.py"},
        {
            "ok": True,
            "path": "x.py",
            "truncated": False,
            "content_hash": "hash-x",
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    assert edit_fallback_required == {}
    assert worker_file_state["x.py"]["content_hash"] == "hash-x"


def test_read_file_missing_hash_or_size_does_not_clear_recovery(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required = {
        "missing_hash.py": {"failure_class": "patch_hunk_not_found"},
        "missing_size.py": {"failure_class": "patch_hunk_not_found"},
    }
    worker_file_state: dict = {}

    manager._record_reads_for_recovery(
        "read_file",
        {"path": "missing_hash.py"},
        {
            "ok": True,
            "path": "missing_hash.py",
            "truncated": False,
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    manager._record_reads_for_recovery(
        "read_file",
        {"path": "missing_size.py"},
        {
            "ok": True,
            "path": "missing_size.py",
            "truncated": False,
            "content_hash": "hash-size",
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )

    assert set(edit_fallback_required) == {"missing_hash.py", "missing_size.py"}
    assert worker_file_state == {}


def test_read_files_only_clears_recovery_for_successful_non_truncated_entries(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required = {
        "a.py": {"failure_class": "patch_hunk_not_found"},
        "b.py": {"failure_class": "patch_hunk_not_found"},
        "c.py": {"failure_class": "patch_hunk_not_found"},
        "d.py": {"failure_class": "patch_hunk_not_found"},
        "e.py": {"failure_class": "patch_hunk_not_found"},
    }
    line_range_required = {"a.py": {"failure_class": "edit_mechanics_stale_line_range"}}
    worker_file_state: dict = {}

    manager._record_reads_for_recovery(
        "read_files",
        {"paths": ["a.py", "b.py", "c.py"]},
        {
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
                "b.py": {
                    "ok": True,
                    "path": "b.py",
                    "content": "b = 1\n",
                    "content_hash": "hash-b",
                    "file_size": 250000,
                    "truncated": True,
                },
                "c.py": {"ok": False, "error": "file not found"},
                "d.py": {
                    "ok": True,
                    "path": "d.py",
                    "content": "d = 1\n",
                    "file_size": 6,
                    "truncated": False,
                },
                "e.py": {
                    "ok": True,
                    "path": "e.py",
                    "content": "e = 1\n",
                    "content_hash": "hash-e",
                    "truncated": False,
                },
            },
        },
        line_range_required,
        edit_fallback_required,
        worker_file_state,
    )

    assert edit_fallback_required == {
        "b.py": {"failure_class": "patch_hunk_not_found"},
        "c.py": {"failure_class": "patch_hunk_not_found"},
        "d.py": {"failure_class": "patch_hunk_not_found"},
        "e.py": {"failure_class": "patch_hunk_not_found"},
    }
    assert line_range_required == {}
    assert set(worker_file_state) == {"a.py"}
    assert worker_file_state["a.py"]["content_hash"] == "hash-a"
    assert worker_file_state["a.py"]["last_read_tool"] == "read_files"


def test_read_file_range_clears_only_hash_mismatch_recovery(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required = {
        "hash.py": {"failure_class": "patch_file_hash_mismatch"},
        "hunk.py": {"failure_class": "patch_hunk_not_found"},
        "missing_hash.py": {"failure_class": "patch_file_hash_mismatch"},
        "missing_size.py": {"failure_class": "patch_file_hash_mismatch"},
    }
    worker_file_state: dict = {}

    manager._record_reads_for_recovery(
        "read_file_range",
        {"path": "hash.py", "start_line": 1, "end_line": 1},
        {
            "ok": True,
            "path": "hash.py",
            "content": "value = 1\n",
            "content_hash": "hash-current",
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    manager._record_reads_for_recovery(
        "read_file_range",
        {"path": "hunk.py", "start_line": 1, "end_line": 1},
        {
            "ok": True,
            "path": "hunk.py",
            "content": "value = 1\n",
            "content_hash": "hunk-current",
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    manager._record_reads_for_recovery(
        "read_file_range",
        {"path": "missing_hash.py", "start_line": 1, "end_line": 1},
        {
            "ok": True,
            "path": "missing_hash.py",
            "content": "value = 1\n",
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    manager._record_reads_for_recovery(
        "read_file_range",
        {"path": "missing_size.py", "start_line": 1, "end_line": 1},
        {
            "ok": True,
            "path": "missing_size.py",
            "content": "value = 1\n",
            "content_hash": "missing-size-current",
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )

    assert edit_fallback_required == {
        "hunk.py": {"failure_class": "patch_hunk_not_found"},
        "missing_hash.py": {"failure_class": "patch_file_hash_mismatch"},
        "missing_size.py": {"failure_class": "patch_file_hash_mismatch"},
    }
    assert set(worker_file_state) == {"hash.py", "hunk.py"}
    assert worker_file_state["hash.py"]["last_read_tool"] == "read_file_range"


def test_successful_read_clears_patch_recovery_state(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required = {"sample.py": {"failure_class": "patch_hunk_not_found"}}
    worker_file_state: dict = {}

    manager._record_reads_for_recovery(
        "read_file",
        {"path": "sample.py"},
        {
            "ok": True,
            "path": "sample.py",
            "truncated": False,
            "content_hash": "hash-sample",
            "file_size": 10,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )

    assert edit_fallback_required == {}
    assert worker_file_state["sample.py"]["content_hash"] == "hash-sample"


def test_different_patch_shape_after_fresh_read_remains_recoverable(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    edit_fallback_required: dict = {}
    patch_failed_cycles: dict[str, int] = {}
    worker_file_state: dict = {}
    first_args = {"path": "sample.py", "edits": [{"old": "missing", "new": "value"}]}

    first = manager._update_worker_recovery_state(
        name="patch_file",
        args=first_args,
        ok=False,
        content=json.dumps({
            "ok": False,
            "path": "sample.py",
            "failure_class": "patch_hunk_not_found",
            "error": "patch_file hunk old block was not found.",
        }),
        edit_failed_shapes=set(),
        edit_fallback_required=edit_fallback_required,
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state=worker_file_state,
        patch_failed_cycles=patch_failed_cycles,
    )
    first_payload = json.loads(first)
    assert first_payload["recoverable"] is True
    first_shape = manager._edit_shape_signature("patch_file", first_args)
    assert patch_failed_cycles == {first_shape: 1}
    assert "sample.py" in edit_fallback_required

    manager._record_reads_for_recovery(
        "read_file",
        {"path": "sample.py"},
        {
            "ok": True,
            "path": "sample.py",
            "content_hash": "fresh-hash",
            "file_size": 10,
            "truncated": False,
        },
        {},
        edit_fallback_required,
        worker_file_state,
    )
    assert edit_fallback_required == {}
    assert patch_failed_cycles == {first_shape: 1}
    second_args = {"path": "sample.py", "edits": [{"old": "still missing", "new": "value"}]}

    second = manager._update_worker_recovery_state(
        name="patch_file",
        args=second_args,
        ok=False,
        content=json.dumps({
            "ok": False,
            "path": "sample.py",
            "failure_class": "patch_hunk_not_found",
            "error": "patch_file hunk old block was not found.",
        }),
        edit_failed_shapes=set(),
        edit_fallback_required=edit_fallback_required,
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state=worker_file_state,
        patch_failed_cycles=patch_failed_cycles,
    )
    second_payload = json.loads(second)
    second_shape = manager._edit_shape_signature("patch_file", second_args)
    assert second_payload["failure_class"] == "patch_hunk_not_found"
    assert second_payload["recoverable"] is True
    assert patch_failed_cycles == {first_shape: 1, second_shape: 1}
    assert "sample.py" in edit_fallback_required


def test_successful_applied_write_resets_failed_patch_counter(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    args = {"path": "sample.py", "edits": [{"old": "a", "new": "b"}]}
    shape = manager._edit_shape_signature("patch_file", args)
    patch_failed_cycles = {shape: 1}
    worker_file_state = {
        "sample.py": {
            "content_hash": "old-hash",
            "file_size": 10,
            "truncated": False,
            "last_read_tool": "read_file",
            "fresh_for_patch": True,
        }
    }

    manager._update_worker_recovery_state(
        name="patch_file",
        args=args,
        ok=True,
        content=json.dumps({"ok": True, "path": "sample.py", "applied": True}),
        edit_failed_shapes=set(),
        edit_fallback_required={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        worker_file_state=worker_file_state,
        patch_failed_cycles=patch_failed_cycles,
    )

    assert patch_failed_cycles == {}
    assert worker_file_state == {}


def test_repeated_patch_file_block_becomes_nonrecoverable(tmp_workspace):
    manager = ConversationManager(History(), ToolRegistry(tmp_workspace, mode="worker"))
    args = {"path": "f.py", "edits": [{"old": "x", "new": "y"}]}
    shape = manager._edit_shape_signature("patch_file", args)

    blocked = manager._worker_recovery_block(
        tool_call_id="tc1",
        name="patch_file",
        args=args,
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required={},
        syntax_validation_required=set(),
        write_attempts_by_path={},
        patch_failed_cycles={shape: 1},
    )

    assert blocked is not None
    payload = json.loads(blocked["result_payload"])
    assert payload["recoverable"] is False
    assert payload["failure_class"] == "patch_file_repeated_failure"
    assert "patch shape" in payload["error"]
