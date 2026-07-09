"""Tests for language-neutral syntax repair messages in worker_recovery_block."""
from __future__ import annotations

import json
from pathlib import Path

from aura.conversation.manager_recovery import worker_recovery_block

_WORKSPACE_ROOT = Path.cwd()


def _call_block(name="write_file", args=None, repair_state=None):
    if repair_state is None:
        repair_state = {"broken.py": {"error": "", "repair_failed": False}}
    if args is None:
        args = {"path": "unrelated.py"}
    return worker_recovery_block(
        workspace_root=_WORKSPACE_ROOT,
        tool_call_id="test",
        name=name,
        args=args,
        edit_failed_shapes=set(),
        edit_fallback_required={},
        recovery_block_counts={},
        line_range_reread_required={},
        syntax_repair_required=repair_state,
        syntax_validation_required=set(),
        write_attempts_by_path={},
    )


def _parse_payload(result):
    """Extract the recovery payload dict from the blocked_tool_result wrapper."""
    return json.loads(result["result_payload"])


class TestSyntaxRepairBlockMessages:
    """Tests that syntax repair blocking messages are language-neutral."""

    def test_block_message_is_language_neutral(self):
        result = _call_block(
            repair_state={
                "broken.py": {
                    "error": "SyntaxError: invalid syntax",
                    "repair_failed": False,
                }
            }
        )
        assert result is not None
        payload = _parse_payload(result)
        assert "Syntax probe failed for broken.py" in payload["error"]
        assert "Python syntax is invalid" not in payload["error"]
        assert "Aura will re-run the syntax probe" in payload["error"]
        assert payload["suggested_next_tool"] == "patch_file"
        assert "Aura will re-run the syntax probe" in payload["suggested_next_action"]
        assert "Run py_compile" not in payload["suggested_next_action"]

    def test_block_message_repair_failed(self):
        result = _call_block(
            repair_state={
                "broken.py": {
                    "error": "SyntaxError: invalid syntax",
                    "repair_failed": True,
                }
            }
        )
        assert result is not None
        payload = _parse_payload(result)
        assert "Syntax still fails after one repair attempt" in payload["error"]
        assert "Syntax probe" in payload["error"]
        assert payload["suggested_next_tool"] == "patch_file"

    def test_read_file_not_blocked(self):
        result = _call_block(
            name="read_file",
            args={},
            repair_state={
                "broken.py": {
                    "error": "SyntaxError: invalid syntax",
                    "repair_failed": False,
                }
            },
        )
        assert result is None

    def test_write_to_broken_path_allowed(self):
        result = _call_block(
            name="write_file",
            args={"path": "broken.py"},
            repair_state={
                "broken.py": {
                    "error": "SyntaxError: invalid syntax",
                    "repair_failed": False,
                }
            },
        )
        assert result is None
