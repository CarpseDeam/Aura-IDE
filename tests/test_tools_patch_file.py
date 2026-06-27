from __future__ import annotations

import hashlib
from unittest.mock import MagicMock, patch

from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools.fs_read import read_file, read_file_range
from aura.conversation.tools.fs_write import propose_patch_file
from aura.conversation.tools.registry import ToolRegistry


def _approve():
    return MagicMock(return_value=ApprovalDecision(action="approve"))


def test_patch_file_applies_multiple_hunks_atomically(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(b"alpha = 1\nbeta = 2\ngamma = 3\n")
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "a.py",
            "edits": [
                {"old": "alpha = 1\n", "new": "alpha = 10\n"},
                {"old": "gamma = 3\n", "new": "gamma = 30\n"},
            ],
        },
        _approve(),
        False,
    )

    assert result.ok is True
    assert result.payload["applied"] is True
    assert result.payload["applied_tool"] == "patch_file"
    assert result.payload["hunk_count"] == 2
    assert target.read_text(encoding="utf-8") == "alpha = 10\nbeta = 2\ngamma = 30\n"


def test_patch_file_matches_lf_hunk_on_crlf_file_and_preserves_crlf(tmp_workspace):
    target = tmp_workspace / "crlf.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\r\ngamma = 3\r\n")
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "crlf.py",
            "edits": [{"old": "beta = 2\n", "new": "beta = 20\n"}],
        },
        _approve(),
        False,
    )

    assert result.ok is True
    assert target.read_bytes() == b"alpha = 1\r\nbeta = 20\r\ngamma = 3\r\n"


def test_patch_file_preserves_lf_file(tmp_workspace):
    target = tmp_workspace / "lf.py"
    target.write_bytes(b"alpha = 1\nbeta = 2\ngamma = 3\n")
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "lf.py",
            "edits": [{"old": "beta = 2\n", "new": "beta = 20\n"}],
        },
        _approve(),
        False,
    )

    assert result.ok is True
    assert target.read_bytes() == b"alpha = 1\nbeta = 20\ngamma = 3\n"


def test_patch_file_leaves_file_unchanged_when_any_hunk_missing(tmp_workspace):
    target = tmp_workspace / "a.py"
    original = "alpha = 1\nbeta = 2\n"
    target.write_text(original, encoding="utf-8")

    result = propose_patch_file(
        tmp_workspace,
        target,
        [
            {"old": "alpha = 1\n", "new": "alpha = 10\n"},
            {"old": "missing = 3\n", "new": "missing = 4\n"},
        ],
    )

    assert result["ok"] is False
    assert result["failure_class"] == "patch_hunk_not_found"
    assert result["hunk_index"] == 1
    assert target.read_text(encoding="utf-8") == original


def test_patch_file_rejects_ambiguous_hunk_without_occurrence(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "value = 1\n", "new": "value = 2\n"}],
    )

    assert result["ok"] is False
    assert result["failure_class"] == "patch_hunk_ambiguous"
    assert result["occurrence_count"] == 2
    assert result["suggested_next_action"] == "Provide occurrence or make the old block more specific."


def test_patch_file_occurrence_replaces_second_occurrence_only(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(b"value = 1\nvalue = 1\n")

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "value = 1\n", "new": "value = 2\n", "occurrence": 2}],
    )

    assert result["ok"] is True
    assert result["new_content"] == "value = 1\nvalue = 2\n"
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_patch_file_applies_whitespace_fuzzy_hunk(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(b"def alpha():\n    return 1\n")

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "def alpha():\n  return 1", "new": "def alpha():\n    return 2\n"}],
    )

    assert result["ok"] is True
    assert result["new_content"] == "def alpha():\n    return 2\n"


def test_patch_file_rejects_ambiguous_fuzzy_hunk(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(
        b"item:\n    value = 1\n"
        b"item:\n  value = 1\n"
    )

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "item:\n value = 1", "new": "item:\n    value = 2\n"}],
    )

    assert result["ok"] is False
    assert result["failure_class"] == "patch_hunk_ambiguous"
    assert result["hunk_index"] == 0
    assert result["match_tier"] == "fuzzy"
    assert result["best_fuzzy_ratio"] == 1.0
    assert result["nearest_candidates"]


def test_patch_file_failed_second_hunk_does_not_write_first_hunk(tmp_workspace):
    target = tmp_workspace / "a.py"
    original = b"alpha = 1\nbeta = 2\n"
    target.write_bytes(original)
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "a.py",
            "edits": [
                {"old": "alpha = 1\n", "new": "alpha = 10\n"},
                {"old": "missing = 3\n", "new": "missing = 4\n"},
            ],
        },
        _approve(),
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_hunk_not_found"
    assert result.payload["hunk_index"] == 1
    assert target.read_bytes() == original


def test_patch_file_expected_hash_matches_read_file_raw_byte_hash(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\r\n")
    read_result = read_file(tmp_workspace, target)

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "beta = 2\n", "new": "beta = 20\n"}],
        expected_file_hash=read_result["content_hash"],
    )

    assert read_result["content_hash"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert result["ok"] is True
    assert result["new_content"] == "alpha = 1\r\nbeta = 20\r\n"


def test_patch_file_expected_hash_accepts_logical_lf_hash_for_crlf_file(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\r\n")
    logical_hash = hashlib.sha256(b"alpha = 1\nbeta = 2\n").hexdigest()

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "beta = 2\n", "new": "beta = 20\n"}],
        expected_file_hash=logical_hash,
    )

    assert result["ok"] is True
    assert result["new_content"] == "alpha = 1\r\nbeta = 20\r\n"


def test_patch_file_expected_hash_matches_read_file_range_for_mixed_newlines(tmp_workspace):
    target = tmp_workspace / "mixed.py"
    target.write_bytes(b"alpha = 1\r\nbeta = 2\ngamma = 3\r\n")
    range_result = read_file_range(tmp_workspace, target, 1, 2)
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "mixed.py",
            "expected_file_hash": range_result["content_hash"],
            "edits": [{"old": "beta = 2\n", "new": "beta = 20\n"}],
        },
        _approve(),
        False,
    )

    assert range_result["content_hash"] == hashlib.sha256(
        b"alpha = 1\r\nbeta = 2\ngamma = 3\r\n"
    ).hexdigest()
    assert result.ok is True
    assert target.read_bytes() == b"alpha = 1\r\nbeta = 20\ngamma = 3\r\n"


def test_patch_file_stale_expected_hash_blocks_write(tmp_workspace):
    target = tmp_workspace / "a.py"
    original = b"alpha = 1\n"
    target.write_bytes(original)

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "alpha = 1\n", "new": "alpha = 2\n"}],
        expected_file_hash=hashlib.sha256(b"stale").hexdigest(),
    )

    assert result["ok"] is False
    assert result["failure_class"] == "patch_file_hash_mismatch"
    assert target.read_bytes() == original


def test_patch_file_invalid_python_blocks_before_approval(tmp_workspace, monkeypatch):
    monkeypatch.setenv("AURA_CRAFT", "0")
    target = tmp_workspace / "a.py"
    original = b"def alpha():\n    return 1\n"
    target.write_bytes(original)
    registry = ToolRegistry(tmp_workspace, mode="worker")
    approval = _approve()

    result = registry.execute(
        "patch_file",
        {
            "path": "a.py",
            "edits": [{"old": "    return 1\n", "new": "    return\n        2\n"}],
        },
        approval,
        False,
    )

    assert result.ok is False
    assert result.payload["failure_class"] == "patch_candidate_invalid_syntax"
    assert result.payload["applied"] is False
    assert result.payload["write_outcome"] == "not_applied_edit_mechanics_blocked"
    assert result.payload["suggested_next_tool"] == "read_file_range"
    assert result.payload["suggested_start_line"] >= 1
    assert result.payload["suggested_end_line"] >= result.payload["suggested_start_line"]
    action = result.payload["suggested_next_action"]
    assert "The live file was not changed" in action
    assert "Re-read the suggested range" in action
    assert "larger exact old block" in action
    assert "Do not analyze patch mechanics" in action
    assert "concise blocker" in action
    approval.assert_not_called()
    assert target.read_bytes() == original


def test_patch_file_runs_craft_once_for_multiple_hunks(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")

    with patch("aura.conversation.tools._write_mixin._run_craft_gate") as craft:
        craft.return_value = None
        result = registry.execute(
            "patch_file",
            {
                "path": "a.py",
                "edits": [
                    {"old": "alpha = 1\n", "new": "alpha = 10\n"},
                    {"old": "beta = 2\n", "new": "beta = 20\n"},
                ],
            },
            _approve(),
            False,
        )

    assert result.ok is True
    craft.assert_called_once()
    assert craft.call_args.args[1] == "patch_file"


def test_patch_file_craft_block_is_not_applied_without_write(tmp_workspace):
    target = tmp_workspace / "a.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")
    blocked = ToolExecResult(
        ok=False,
        payload={
            "ok": False,
            "applied": False,
            "path": "a.py",
            "failure_class": "craft_blocked",
            "write_outcome": "not_applied_craft_rejected",
            "craft_issues": [{"code": "stub-body-pass"}],
        },
    )

    with patch("aura.conversation.tools._write_mixin._run_craft_gate", return_value=blocked):
        result = registry.execute(
            "patch_file",
            {
                "path": "a.py",
                "edits": [{"old": "value = 1\n", "new": "value = missing\n"}],
            },
            _approve(),
            False,
        )

    assert result.ok is False
    assert result.payload["failure_class"] == "craft_blocked"
    assert result.payload["applied"] is False
    assert target.read_text(encoding="utf-8") == original


def test_app_tray_watchdog_regression_removes_refs_in_one_transaction(tmp_workspace):
    tray = tmp_workspace / "app"
    tray.mkdir()
    target = tray / "tray.py"
    target.write_text(
        "WATCHDOG_INTERVAL = 10\n"
        "def start():\n"
        "    watchdog.start()\n"
        "    icon.run()\n"
        "def stop():\n"
        "    watchdog.stop()\n",
        encoding="utf-8",
    )
    registry = ToolRegistry(tmp_workspace, mode="worker")

    result = registry.execute(
        "patch_file",
        {
            "path": "app/tray.py",
            "edits": [
                {"old": "WATCHDOG_INTERVAL = 10\n", "new": ""},
                {"old": "    watchdog.start()\n", "new": ""},
                {"old": "    watchdog.stop()\n", "new": "    pass\n"},
            ],
        },
        _approve(),
        False,
    )

    assert result.ok is True
    content = target.read_text(encoding="utf-8")
    assert "watchdog" not in content
    assert content == "def start():\n    icon.run()\ndef stop():\n    pass\n"
