"""Tests for aura.drones.workshop_runner — parsing only, no real API calls."""

from __future__ import annotations

import json

import pytest

from aura.drones.build_spec import (
    KIND_PROJECT_WORKER,
    KIND_REPORT_DRAFTER,
    STATUS_BUILDABLE_NOW,
    STATUS_NEEDS_CAPABILITY,
)
from aura.drones.workshop_runner import (
    DRONE_WORKSHOP_SYSTEM_PROMPT,
    DroneWorkshopResponse,
    extract_json_object,
    parse_workshop_response,
)


# ===================================================================
# DroneWorkshopResponse
# ===================================================================


class TestDroneWorkshopResponse:
    def test_roundtrip_response_dataclass(self) -> None:
        """Verify fields are accessible on a constructed instance."""
        resp = DroneWorkshopResponse(
            kind="question",
            message="What should it do?",
            raw_text='{"type": "question", "message": "What should it do?"}',
        )
        assert resp.kind == "question"
        assert resp.message == "What should it do?"
        assert resp.spec is None
        assert resp.validation_errors == ()
        assert resp.raw_text is not None

    def test_frozen(self) -> None:
        resp = DroneWorkshopResponse(kind="error", message="oops")
        with pytest.raises(AttributeError):
            resp.kind = "question"  # type: ignore[misc]


# ===================================================================
# extract_json_object
# ===================================================================


class TestExtractJsonObject:
    def test_extract_json_plain(self) -> None:
        """Direct JSON string is parsed."""
        obj = extract_json_object('{"a": 1}')
        assert obj == {"a": 1}

    def test_extract_json_none(self) -> None:
        """Non-JSON text returns None."""
        assert extract_json_object("hello world") is None

    def test_extract_json_with_prose(self) -> None:
        """JSON inside a fenced block is extracted."""
        text = 'Here is the spec:\n```json\n{"type": "question", "message": "ok"}\n```'
        obj = extract_json_object(text)
        assert obj == {"type": "question", "message": "ok"}

    def test_extract_json_fenced_no_lang(self) -> None:
        """Fenced block without language tag still parses."""
        text = "```\n{\"key\": \"value\"}\n```"
        obj = extract_json_object(text)
        assert obj == {"key": "value"}

    def test_extract_json_braces_fallback(self) -> None:
        """Bare braces when neither direct nor fenced works."""
        text = "Some leading text\n{\"a\": 1}\ntrailing"
        obj = extract_json_object(text)
        assert obj == {"a": 1}

    def test_extract_json_empty_string(self) -> None:
        assert extract_json_object("") is None

    def test_extract_json_array_ignored(self) -> None:
        """Top-level arrays should not be returned (only dicts)."""
        assert extract_json_object("[1, 2, 3]") is None


# ===================================================================
# parse_workshop_response
# ===================================================================


class TestParseWorkshopResponse:
    def test_parse_question_response(self) -> None:
        text = '{"type": "question", "message": "What should it do?"}'
        resp = parse_workshop_response(text)
        assert resp.kind == "question"
        assert resp.message == "What should it do?"
        assert resp.spec is None

    def test_parse_spec_response(self) -> None:
        spec_dict = {
            "name": "Release Drafter",
            "kind": KIND_REPORT_DRAFTER,
            "job": "Draft release notes",
            "trigger": "tag pushed",
            "required_access": ["read"],
            "write_policy": "normal_diff_approval",
            "action_policy": "",
            "capabilities_needed": [],
            "instructions": "Read commits and produce release notes.",
            "output_contract": "Returns markdown release notes.",
            "success_criteria": ["Covers all commits"],
            "boundaries": ["Last tag range only"],
            "assumptions": ["Conventional commits"],
            "build_status": STATUS_BUILDABLE_NOW,
            "missing_capabilities": [],
            "first_run_test": "",
        }
        payload = json.dumps({
            "type": "spec",
            "message": "Release Dragger proposal",
            "spec": spec_dict,
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "spec"
        assert resp.message == "Release Dragger proposal"
        assert resp.spec is not None
        assert resp.spec.name == "Release Drafter"
        assert resp.spec.kind == KIND_REPORT_DRAFTER
        assert resp.validation_errors == ()

    def test_parse_json_in_fenced_block(self) -> None:
        text = (
            "I propose the following Drone:\n\n"
            "```json\n"
            '{"type": "question", "message": "Which project should it watch?"}\n'
            "```\n\n"
            "Let me know if that works."
        )
        resp = parse_workshop_response(text)
        assert resp.kind == "question"
        assert resp.message == "Which project should it watch?"

    def test_parse_invalid_json(self) -> None:
        text = "not json"
        resp = parse_workshop_response(text)
        assert resp.kind == "error"
        assert "Could not parse" in resp.message
        assert resp.raw_text == "not json"

    def test_parse_missing_type(self) -> None:
        text = '{"message": "hi"}'
        resp = parse_workshop_response(text)
        assert resp.kind == "error"
        assert "missing required 'type'" in resp.message

    def test_parse_unknown_type(self) -> None:
        text = '{"type": "unknown_kind", "message": "hello"}'
        resp = parse_workshop_response(text)
        assert resp.kind == "error"
        assert "Unknown response type" in resp.message

    def test_parse_spec_with_validation_errors(self) -> None:
        """Spec missing name and kind should produce validation errors."""
        spec_dict = {
            "name": "",
            "kind": "",
            "job": "Draft reports",
            "write_policy": "normal_diff_approval",
            "instructions": "",
            "output_contract": "",
            "build_status": STATUS_BUILDABLE_NOW,
        }
        payload = json.dumps({
            "type": "spec",
            "message": "Incomplete spec",
            "spec": spec_dict,
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "spec"
        assert resp.spec is not None
        # Should have validation errors for missing name, kind, instructions,
        # output_contract, and buildable_now constraints
        assert len(resp.validation_errors) > 0
        error_texts = " ".join(resp.validation_errors).lower()
        assert "name" in error_texts
        assert "kind" in error_texts

    def test_parse_spec_with_needs_capability(self) -> None:
        """A spec that needs capabilities should parse without errors
        and include the spec's validation_errors (missing_capabilities empty)."""
        spec_dict = {
            "name": "Browser Watcher",
            "kind": KIND_PROJECT_WORKER,
            "job": "Watch something",
            "write_policy": "read_only",
            "instructions": "Check for changes",
            "output_contract": "Summary of changes",
            "build_status": STATUS_NEEDS_CAPABILITY,
            "missing_capabilities": [],
        }
        payload = json.dumps({
            "type": "spec",
            "message": "Needs extra capabilities",
            "spec": spec_dict,
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "spec"
        assert resp.spec is not None
        # validate() should flag missing_capabilities empty for needs_capability
        assert len(resp.validation_errors) > 0

    def test_parse_empty_text(self) -> None:
        resp = parse_workshop_response("")
        assert resp.kind == "error"

    def test_parse_spec_field_not_dict(self) -> None:
        """When spec field is not a dict, return error."""
        payload = json.dumps({"type": "spec", "message": "bad", "spec": "not-a-dict"})
        resp = parse_workshop_response(payload)
        assert resp.kind == "error"
        assert "not a valid object" in resp.message

    def test_raw_text_preserved(self) -> None:
        """Error responses preserve the original raw_text."""
        text = "garbage input"
        resp = parse_workshop_response(text)
        assert resp.raw_text == "garbage input"

    def test_success_raw_text_preserved(self) -> None:
        """Successful responses also preserve raw_text."""
        text = '{"type": "question", "message": "ok"}'
        resp = parse_workshop_response(text)
        assert resp.raw_text == text


# ===================================================================
# DRONE_WORKSHOP_SYSTEM_PROMPT
# ===================================================================


class TestSystemPrompt:
    def test_is_non_empty_string(self) -> None:
        assert isinstance(DRONE_WORKSHOP_SYSTEM_PROMPT, str)
        assert len(DRONE_WORKSHOP_SYSTEM_PROMPT) > 100

    def test_contains_json_shapes(self) -> None:
        """The prompt should mention the two JSON response shapes."""
        assert '"type": "question"' in DRONE_WORKSHOP_SYSTEM_PROMPT
        assert '"type": "spec"' in DRONE_WORKSHOP_SYSTEM_PROMPT

    def test_mentions_valid_write_policies(self) -> None:
        assert "read_only" in DRONE_WORKSHOP_SYSTEM_PROMPT
        assert "normal_diff_approval" in DRONE_WORKSHOP_SYSTEM_PROMPT
