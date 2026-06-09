"""Tests for aura.drones.workshop_runner — parsing only, no real API calls."""

from __future__ import annotations

import json

import pytest

from aura.drones.build_spec import DroneBuildBrief
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
        assert resp.brief is None
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
        assert resp.brief is None

    def test_parse_brief_response(self) -> None:
        payload = json.dumps({
            "type": "brief",
            "message": "Here is the plan.",
            "ready_to_build": True,
            "build_brief": "Build a watcher that monitors the workspace.",
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "brief"
        assert resp.message == "Here is the plan."
        assert resp.brief is not None
        assert isinstance(resp.brief, DroneBuildBrief)
        assert resp.brief.response_type == "brief"
        assert resp.brief.ready_to_build is True
        assert resp.brief.build_brief == "Build a watcher that monitors the workspace."

    def test_parse_brief_without_ready(self) -> None:
        """Brief with ready_to_build=False is still valid."""
        payload = json.dumps({
            "type": "brief",
            "message": "Still need info.",
            "ready_to_build": False,
            "build_brief": "",
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "brief"
        assert resp.brief is not None
        assert resp.brief.ready_to_build is False

    def test_parse_brief_invalid_empty_build_brief(self) -> None:
        """Invalid brief (empty build_brief with ready_to_build=true) returns error."""
        payload = json.dumps({
            "type": "brief",
            "message": "Ready.",
            "ready_to_build": True,
            "build_brief": "",
        })
        resp = parse_workshop_response(payload)
        assert resp.kind == "error"
        assert "build_brief" in resp.message
        assert "not be empty" in resp.message

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

    def test_parse_empty_text(self) -> None:
        resp = parse_workshop_response("")
        assert resp.kind == "error"

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

    def test_contains_build_brief_keywords(self) -> None:
        assert "build brief" in DRONE_WORKSHOP_SYSTEM_PROMPT.lower()

    def test_mentions_json_shapes(self) -> None:
        assert '"type": "question"' in DRONE_WORKSHOP_SYSTEM_PROMPT
        assert '"type": "brief"' in DRONE_WORKSHOP_SYSTEM_PROMPT

    def test_does_not_contain_unsupported_language(self) -> None:
        """The prompt should NOT say 'unsupported', 'cannot build', or 'missing capabilities'."""
        prompt_lower = DRONE_WORKSHOP_SYSTEM_PROMPT.lower()
        assert "unsupported" not in prompt_lower
        assert "cannot build" not in prompt_lower
        assert "missing capabilities" not in prompt_lower
        assert "cannot build" not in prompt_lower
