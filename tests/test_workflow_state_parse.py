"""Tests for WorkflowState parsing helpers and integration behavior."""

from __future__ import annotations

import json

from aura.conversation._workflow_parse import (
    _compact_title,
    _environment_caveats,
    _first_error,
    _parse_json_object,
)
from aura.conversation.workflow_state import (
    ValidationStatus,
    WorkflowState,
    WorkflowStatus,
)


class TestCompactTitle:
    """Tests for _compact_title."""

    def test_collapses_extra_whitespace(self):
        assert _compact_title("  hello   world  ") == "hello world"

    def test_preserves_short_titles(self):
        assert _compact_title("Short") == "Short"

    def test_truncates_long_titles(self):
        result = _compact_title("a" * 100, limit=90)
        assert result == ("a" * 89) + "..."
        assert len(result) == 92

    def test_exactly_at_limit_not_truncated(self):
        assert _compact_title("a" * 90, limit=90) == "a" * 90
        assert len(_compact_title("a" * 90, limit=90)) == 90


class TestParseJsonObject:
    """Tests for _parse_json_object."""

    def test_valid_object(self):
        assert _parse_json_object('{"a": 1}') == {"a": 1}

    def test_invalid_json(self):
        assert _parse_json_object("not json") == {}

    def test_non_dict_json(self):
        assert _parse_json_object("[1,2,3]") == {}
        assert _parse_json_object('"hello"') == {}
        assert _parse_json_object("42") == {}

    def test_none(self):
        assert _parse_json_object("") == {}

    def test_type_error(self):
        assert _parse_json_object(b"true") == {}


class TestEnvironmentCaveats:
    """Tests for _environment_caveats."""

    def test_missing_key(self):
        assert _environment_caveats({}) == ()

    def test_not_a_list(self):
        assert _environment_caveats({"pre_existing_environment_issues": "string"}) == ()

    def test_empty_list(self):
        assert _environment_caveats({"pre_existing_environment_issues": []}) == ()

    def test_string_issue(self):
        assert _environment_caveats(
            {"pre_existing_environment_issues": ["Node version too old"]}
        ) == ("Pre-existing environment issue: Node version too old",)

    def test_dict_issue_message_preferred(self):
        assert _environment_caveats(
            {"pre_existing_environment_issues": [{"message": "Disk full", "code": "ENOSPC"}]}
        ) == ("Pre-existing environment issue: Disk full",)

    def test_dict_issue_code_fallback(self):
        assert _environment_caveats(
            {"pre_existing_environment_issues": [{"code": "ENOSPC"}]}
        ) == ("Pre-existing environment issue: ENOSPC",)

    def test_dict_issue_fallback_text(self):
        assert _environment_caveats(
            {"pre_existing_environment_issues": [{"other": "thing"}]}
        ) == ("Pre-existing environment issue: pre-existing environment issue",)


class TestFirstError:
    """Tests for _first_error."""

    def test_errors_from_extras(self):
        assert _first_error("some summary", {"errors": ["real error", "second"]}) == "real error"

    def test_fallback_to_summary_first_line(self):
        assert _first_error("line1\nline2\n", None) == "line1"

    def test_empty_summary(self):
        assert _first_error("", None) == ""

    def test_truncation(self):
        result = _first_error("x" * 600, None)
        assert len(result) == 500

    def test_extras_none(self):
        assert _first_error("hello", None) == "hello"


class TestAbsorbWorkerToolResultIntegration:
    """Integration tests for WorkflowState.absorb_worker_tool_result."""

    def test_successful_write_tool(self):
        state = WorkflowState.intent_captured("test-1", "Test task")
        result = json.dumps({
            "applied": True,
            "path": "src/example.py",
            "write_outcome": "applied_clean",
            "pre_existing_environment_issues": ["Outdated npm"],
        })
        state = state.absorb_worker_tool_result("write_file", ok=True, result=result)

        assert state.status == WorkflowStatus.editing
        assert "src/example.py" in state.changed_files
        assert state.write_outcome == "applied_clean"
        assert "Pre-existing environment issue: Outdated npm" in state.caveats


class TestValidationStatusIntegration:
    """Integration tests for validation status via absorb_worker_tool_result."""

    def test_mixed_validation_results(self):
        state = WorkflowState.intent_captured("test-2", "Validation test")

        state = state.absorb_worker_tool_result(
            "run_terminal_command",
            ok=True,
            result=json.dumps({"command": "python -m compileall .", "ok": True, "exit_code": 0}),
        )

        state = state.absorb_worker_tool_result(
            "run_terminal_command",
            ok=True,
            result=json.dumps({"command": "pytest tests/", "ok": False, "exit_code": 1}),
        )

        assert state.validation_status == ValidationStatus.mixed
        assert state.status == WorkflowStatus.validating
        assert "Validation failed:" in state.blocker_reason

    def test_mixed_validation_results_via_run_and_watch(self):
        state = WorkflowState.intent_captured("test-3", "Validation test run_and_watch")

        state = state.absorb_worker_tool_result(
            "run_and_watch",
            ok=True,
            result=json.dumps({"command": "python -m compileall .", "ok": True, "exit_code": 0}),
        )

        state = state.absorb_worker_tool_result(
            "run_and_watch",
            ok=True,
            result=json.dumps({"command": "pytest tests/", "ok": False, "exit_code": 1}),
        )

        assert state.validation_status == ValidationStatus.mixed
        assert state.status == WorkflowStatus.validating
        assert "Validation failed:" in state.blocker_reason
