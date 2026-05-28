from __future__ import annotations

from unittest.mock import MagicMock, patch

from aura.conversation.tools._types import ApprovalDecision, ToolExecResult
from aura.conversation.tools.fs_write import propose_patch_file
from aura.conversation.tools.registry import ToolRegistry


def _approve():
    return MagicMock(return_value=ApprovalDecision(action="approve"))


def test_patch_file_applies_multiple_hunks_atomically(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
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
    target.write_text("value = 1\nvalue = 1\n", encoding="utf-8")

    result = propose_patch_file(
        tmp_workspace,
        target,
        [{"old": "value = 1\n", "new": "value = 2\n", "occurrence": 2}],
    )

    assert result["ok"] is True
    assert result["new_content"] == "value = 1\nvalue = 2\n"
    assert target.read_text(encoding="utf-8") == "value = 1\nvalue = 1\n"


def test_patch_file_runs_craft_once_for_multiple_hunks(tmp_workspace):
    target = tmp_workspace / "a.py"
    target.write_text("alpha = 1\nbeta = 2\n", encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")

    with patch("aura.conversation.tools._write_mixin._run_compiler_pipeline") as craft:
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


def test_patch_file_compiler_bounce_is_quality_bounce_without_write(tmp_workspace):
    target = tmp_workspace / "a.py"
    original = "value = 1\n"
    target.write_text(original, encoding="utf-8")
    registry = ToolRegistry(tmp_workspace, mode="worker")
    bounce = ToolExecResult(
        ok=True,
        payload={
            "ok": True,
            "applied": False,
            "quality_bounce": True,
            "path": "a.py",
            "tool_name": "patch_file",
            "repair_instructions": "Define missing.",
        },
    )

    with patch("aura.conversation.tools._write_mixin._run_compiler_pipeline", return_value=bounce):
        result = registry.execute(
            "patch_file",
            {
                "path": "a.py",
                "edits": [{"old": "value = 1\n", "new": "value = missing\n"}],
            },
            _approve(),
            False,
        )

    assert result.ok is True
    assert result.payload["quality_bounce"] is True
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
