"""Tests for aura.drones.build_spec — DroneBuildBrief model and validation."""

from __future__ import annotations

import pytest

from aura.drones.build_spec import DroneBuildBrief

# ---------------------------------------------------------------------------
# DroneBuildBrief — construction
# ---------------------------------------------------------------------------


class TestConstruction:
    def test_valid_question(self) -> None:
        brief = DroneBuildBrief(
            response_type="question",
            message="What should it watch?",
        )
        assert brief.response_type == "question"
        assert brief.message == "What should it watch?"
        assert brief.ready_to_build is False
        assert brief.build_brief == ""

    def test_valid_brief(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Here is the plan.",
            ready_to_build=True,
            build_brief="Build a drone that monitors the workspace.",
        )
        assert brief.response_type == "brief"
        assert brief.message == "Here is the plan."
        assert brief.ready_to_build is True
        assert brief.build_brief == "Build a drone that monitors the workspace."

    def test_frozen_dataclass(self) -> None:
        brief = DroneBuildBrief(response_type="question", message="hi")
        with pytest.raises(AttributeError):
            brief.response_type = "brief"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DroneBuildBrief — validate()
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_question_passes(self) -> None:
        brief = DroneBuildBrief(response_type="question", message="What?")
        assert brief.validate() == []

    def test_valid_brief_passes(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Plan ready.",
            ready_to_build=True,
            build_brief="Build the watcher.",
        )
        assert brief.validate() == []

    def test_invalid_response_type(self) -> None:
        brief = DroneBuildBrief(response_type="spec", message="bad")
        errors = brief.validate()
        assert any("response_type must be 'question' or 'brief'" in e for e in errors)

    def test_empty_build_brief_when_ready(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Ready.",
            ready_to_build=True,
            build_brief="",
        )
        errors = brief.validate()
        assert any("build_brief must not be empty" in e for e in errors)

    def test_whitespace_only_build_brief_when_ready(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Ready.",
            ready_to_build=True,
            build_brief="   ",
        )
        errors = brief.validate()
        assert any("build_brief must not be empty" in e for e in errors)

    def test_not_ready_build_brief_empty_allowed(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Not ready yet.",
            ready_to_build=False,
            build_brief="",
        )
        assert brief.validate() == []


# ---------------------------------------------------------------------------
# DroneBuildBrief — to_dict() / from_dict() roundtrip
# ---------------------------------------------------------------------------


class TestDictRoundtrip:
    def test_question_roundtrip(self) -> None:
        original = DroneBuildBrief(response_type="question", message="What?")
        d = original.to_dict()
        restored = DroneBuildBrief.from_dict(d)
        assert restored.response_type == "question"
        assert restored.message == "What?"
        assert restored.ready_to_build is False
        assert restored.build_brief == ""

    def test_brief_roundtrip(self) -> None:
        original = DroneBuildBrief(
            response_type="brief",
            message="Plan ready.",
            ready_to_build=True,
            build_brief="Build the watcher.",
        )
        d = original.to_dict()
        restored = DroneBuildBrief.from_dict(d)
        assert restored.response_type == "brief"
        assert restored.message == "Plan ready."
        assert restored.ready_to_build is True
        assert restored.build_brief == "Build the watcher."

    def test_empty_dict_defaults(self) -> None:
        restored = DroneBuildBrief.from_dict({})
        assert restored.response_type == ""
        assert restored.message == ""
        assert restored.ready_to_build is False
        assert restored.build_brief == ""

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "response_type": "question",
            "message": "hi",
            "extra": "ignored",
        }
        restored = DroneBuildBrief.from_dict(data)
        assert restored.response_type == "question"
        assert restored.message == "hi"
        assert restored.ready_to_build is False


# ---------------------------------------------------------------------------
# DroneBuildBrief — is_ready_to_build()
# ---------------------------------------------------------------------------


class TestIsReadyToBuild:
    def test_ready_true(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Ready.",
            ready_to_build=True,
            build_brief="Build it.",
        )
        assert brief.is_ready_to_build() is True

    def test_question_not_ready(self) -> None:
        brief = DroneBuildBrief(response_type="question", message="What?")
        assert brief.is_ready_to_build() is False

    def test_brief_not_ready_flag(self) -> None:
        brief = DroneBuildBrief(
            response_type="brief",
            message="Needs more info.",
            ready_to_build=False,
        )
        assert brief.is_ready_to_build() is False

    def test_wrong_response_type(self) -> None:
        brief = DroneBuildBrief(
            response_type="question",
            message="What?",
            ready_to_build=True,
        )
        assert brief.is_ready_to_build() is False
