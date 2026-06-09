"""Tests for aura.drones.build_spec — DroneBuildSpec model and validation."""

from __future__ import annotations

import pytest

from aura.drones.build_spec import (
    KIND_BROWSER_WATCHER,
    KIND_CUSTOM_CHORE,
    KIND_DASHBOARD_SUMMARIZER,
    KIND_EMAIL_WATCHER,
    KIND_MARKET_WATCHER,
    KIND_PROJECT_WORKER,
    KIND_REPO_WATCHER,
    KIND_REPORT_DRAFTER,
    STATUS_BUILDABLE_NOW,
    STATUS_NEEDS_CAPABILITY,
    STATUS_NEEDS_MORE_INFO,
    SUPPORTED_SPEC_KINDS,
    VALID_BUILD_STATUSES,
    VALID_WRITE_POLICIES,
    DroneBuildSpec,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_spec(**overrides: object) -> DroneBuildSpec:
    """Return a fully valid DroneBuildSpec with overrides applied."""
    base: dict[str, object] = dict(
        name="Release Drafter",
        kind=KIND_REPORT_DRAFTER,
        job="Draft release notes from commits",
        trigger="tag pushed",
        required_access=("read", "write"),
        write_policy="ask_before_writes",
        action_policy="review_required",
        capabilities_needed=(),
        instructions=(
            "Read recent commits, categorise them, and produce "
            "markdown release notes."
        ),
        output_contract="Returns release notes as markdown text.",
        success_criteria=(
            "Release notes are categorised by type",
            "All commits are covered",
        ),
        boundaries=("Only the last tag range",),
        assumptions=("Conventional commits style used",),
        build_status=STATUS_BUILDABLE_NOW,
        missing_capabilities=(),
        first_run_test="pytest tests/test_release.py -k smoke",
    )
    base.update(overrides)
    return DroneBuildSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestConstants:
    def test_supported_spec_kinds(self) -> None:
        assert SUPPORTED_SPEC_KINDS == (
            KIND_PROJECT_WORKER,
            KIND_BROWSER_WATCHER,
            KIND_EMAIL_WATCHER,
            KIND_DASHBOARD_SUMMARIZER,
            KIND_MARKET_WATCHER,
            KIND_REPO_WATCHER,
            KIND_REPORT_DRAFTER,
            KIND_CUSTOM_CHORE,
        )

    def test_valid_build_statuses(self) -> None:
        assert VALID_BUILD_STATUSES == (
            STATUS_BUILDABLE_NOW,
            STATUS_NEEDS_CAPABILITY,
            STATUS_NEEDS_MORE_INFO,
        )

    def test_valid_write_policies(self) -> None:
        assert VALID_WRITE_POLICIES == (
            "read_only",
            "ask_before_writes",
            "normal_diff_approval",
        )


# ---------------------------------------------------------------------------
# DroneBuildSpec — validate()
# ---------------------------------------------------------------------------


class TestValidate:
    def test_valid_buildable_project_worker(self) -> None:
        spec = DroneBuildSpec(
            name="Project Worker",
            kind=KIND_PROJECT_WORKER,
            job="Run project tasks",
            write_policy="read_only",
            instructions="Inspect the project structure and report.",
            output_contract="Returns a structured report.",
            build_status=STATUS_BUILDABLE_NOW,
        )
        assert spec.validate() == []

    def test_missing_required_fields(self) -> None:
        spec = DroneBuildSpec()
        errors = spec.validate()
        # All 7 required fields should show up
        required = {
            "name",
            "kind",
            "job",
            "write_policy",
            "instructions",
            "output_contract",
            "build_status",
        }
        found = set()
        for e in errors:
            for field in required:
                if f"{field} must be a non-empty string" in e:
                    found.add(field)
        assert found == required, f"Missing errors for: {required - found}"

    def test_unsupported_kind(self) -> None:
        spec = _valid_spec(kind="bogus")
        errors = spec.validate()
        assert any("unsupported kind 'bogus'" in e for e in errors)

    def test_unsupported_build_status(self) -> None:
        spec = _valid_spec(build_status="bogus")
        errors = spec.validate()
        assert any("unsupported build_status 'bogus'" in e for e in errors)

    def test_unsupported_write_policy(self) -> None:
        spec = _valid_spec(write_policy="admin")
        errors = spec.validate()
        assert any("unsupported write_policy 'admin'" in e for e in errors)

    def test_needs_capability_without_missing(self) -> None:
        spec = _valid_spec(
            build_status=STATUS_NEEDS_CAPABILITY,
            missing_capabilities=(),
        )
        errors = spec.validate()
        assert any(
            "missing_capabilities is empty" in e
            and "needs_capability" in e
            for e in errors
        )

    def test_buildable_now_empty_instructions(self) -> None:
        spec = _valid_spec(
            build_status=STATUS_BUILDABLE_NOW,
            instructions="",
            output_contract="Something",
        )
        errors = spec.validate()
        assert any(
            "build_status is 'buildable_now' but instructions is empty" in e
            for e in errors
        )

    def test_buildable_now_empty_output_contract(self) -> None:
        spec = _valid_spec(
            build_status=STATUS_BUILDABLE_NOW,
            instructions="Do something",
            output_contract="",
        )
        errors = spec.validate()
        assert any(
            "build_status is 'buildable_now' but output_contract is empty" in e
            for e in errors
        )


# ---------------------------------------------------------------------------
# DroneBuildSpec — from_dict()
# ---------------------------------------------------------------------------


class TestFromDict:
    def test_coerces_types(self) -> None:
        data: dict[str, object] = {
            "name": "Test",
            "kind": KIND_REPO_WATCHER,
            "job": "Watch repos",
            "write_policy": "read_only",
            "instructions": "Watch git repos for changes.",
            "output_contract": "Summary of changes.",
            "build_status": STATUS_BUILDABLE_NOW,
            # strings coerced to one-item tuples
            "required_access": "read",
            "success_criteria": "Must pass lint",
            # list coerced to tuple
            "boundaries": ["one", "two"],
        }
        spec = DroneBuildSpec.from_dict(data)
        assert spec.name == "Test"
        assert spec.required_access == ("read",)
        assert spec.success_criteria == ("Must pass lint",)
        assert spec.boundaries == ("one", "two")
        assert spec.missing_capabilities == ()
        assert spec.trigger == ""

    def test_missing_keys_empty_defaults(self) -> None:
        spec = DroneBuildSpec.from_dict({})
        assert spec.name == ""
        assert spec.kind == ""
        assert spec.required_access == ()
        assert spec.missing_capabilities == ()
        assert spec.job == ""

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "name": "Ignored",
            "kind": KIND_CUSTOM_CHORE,
            "job": "Job",
            "write_policy": "read_only",
            "instructions": "Do the thing",
            "output_contract": "Result",
            "build_status": STATUS_BUILDABLE_NOW,
            "extra_field": "should be ignored",
            "another_extra": 42,
        }
        spec = DroneBuildSpec.from_dict(data)
        assert spec.name == "Ignored"
        # No KeyError from unknown keys
        assert spec.validate() == []


# ---------------------------------------------------------------------------
# DroneBuildSpec — to_dict() round-trip
# ---------------------------------------------------------------------------


class TestToDict:
    def test_roundtrip(self) -> None:
        original = _valid_spec()
        d = original.to_dict()
        restored = DroneBuildSpec.from_dict(d)
        assert restored.to_dict() == original.to_dict()


# ---------------------------------------------------------------------------
# DroneBuildSpec — is_buildable_now()
# ---------------------------------------------------------------------------


class TestIsBuildableNow:
    def test_buildable_true(self) -> None:
        spec = _valid_spec()
        assert spec.is_buildable_now() is True

    def test_false_for_needs_capability(self) -> None:
        spec = _valid_spec(
            build_status=STATUS_NEEDS_CAPABILITY,
            missing_capabilities=("vision",),
        )
        assert spec.is_buildable_now() is False

    def test_false_for_missing_fields(self) -> None:
        spec = DroneBuildSpec(build_status=STATUS_BUILDABLE_NOW)
        assert spec.is_buildable_now() is False

    def test_false_for_invalid_build_status(self) -> None:
        spec = _valid_spec(build_status="bogus")
        assert spec.is_buildable_now() is False


# ---------------------------------------------------------------------------
# DroneBuildSpec — preview_markdown()
# ---------------------------------------------------------------------------


class TestPreviewMarkdown:
    def test_contains_title(self) -> None:
        spec = _valid_spec(name="Release Drafter")
        md = spec.preview_markdown()
        assert md.startswith("# Release Drafter")

    def test_untitled_drone(self) -> None:
        spec = _valid_spec(name="")
        md = spec.preview_markdown()
        assert md.startswith("# Untitled Drone")

    def test_contains_sections(self) -> None:
        spec = _valid_spec()
        md = spec.preview_markdown()
        assert "**Kind**:" in md
        assert "**Job**:" in md
        assert "**Build Status**:" in md
        assert "**Permissions**:" in md
        assert "**What it will do**:" in md
        assert "**Output**:" in md

    def test_buildable_now_status_note(self) -> None:
        spec = _valid_spec(build_status=STATUS_BUILDABLE_NOW)
        md = spec.preview_markdown()
        assert "✅ Ready to build" in md

    def test_needs_capability_status_note(self) -> None:
        spec = _valid_spec(
            build_status=STATUS_NEEDS_CAPABILITY,
            missing_capabilities=("vision",),
        )
        md = spec.preview_markdown()
        assert "⚠️ Needs new capability" in md

    def test_needs_more_info_status_note(self) -> None:
        spec = _valid_spec(build_status=STATUS_NEEDS_MORE_INFO)
        md = spec.preview_markdown()
        assert "❓ Needs more information" in md

    def test_success_criteria_numbered(self) -> None:
        spec = _valid_spec(
            success_criteria=("Criterion A", "Criterion B"),
        )
        md = spec.preview_markdown()
        assert "1. Criterion A" in md
        assert "2. Criterion B" in md

    def test_skips_empty_sections(self) -> None:
        spec = _valid_spec(
            action_policy="",
            trigger="",
            required_access=(),
            success_criteria=(),
            boundaries=(),
            assumptions=(),
            missing_capabilities=(),
            first_run_test="",
        )
        md = spec.preview_markdown()
        assert "**Action Policy**:" not in md
        assert "**Trigger**:" not in md
        assert "**Required Access**:" not in md
        assert "**Success Criteria**:" not in md
        assert "**Boundaries**:" not in md
        assert "**Assumptions**:" not in md
        assert "**Missing Capabilities**:" not in md
        assert "**First Run Test**:" not in md

    def test_required_access_as_bullets(self) -> None:
        spec = _valid_spec(required_access=("read", "write"))
        md = spec.preview_markdown()
        assert "- read" in md
        assert "- write" in md

    def test_first_run_test_shown(self) -> None:
        spec = _valid_spec(first_run_test="pytest smoke")
        md = spec.preview_markdown()
        assert "**First Run Test**: pytest smoke" in md


# ---------------------------------------------------------------------------
# DroneBuildSpec — misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_frozen_dataclass(self) -> None:
        spec = DroneBuildSpec(name="Frozen")
        with pytest.raises(AttributeError):
            spec.name = "Mutated"  # type: ignore[misc]

    def test_empty_kind_validate_error(self) -> None:
        """Empty kind plus invalid build_status should both be reported."""
        spec = DroneBuildSpec(
            name="N",
            kind="",
            job="J",
            write_policy="read_only",
            instructions="I",
            output_contract="O",
            build_status="bogus",
        )
        errors = spec.validate()
        kind_errors = [e for e in errors if "kind" in e]
        assert any("non-empty string" in e for e in kind_errors)
        status_errors = [e for e in errors if "build_status" in e]
        assert any("unsupported build_status 'bogus'" in e for e in status_errors)
