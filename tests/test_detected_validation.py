"""Focused tests for aura.conversation.detected_validation.

Covers command derivation, filtering, and merge logic without
inspecting the filesystem or running product validation.
"""

from __future__ import annotations

from aura.conversation.detected_validation import (
    behavioral_required_commands,
    is_behavioral_required_command,
    is_runnable_detected_validation_command,
    merge_validation_commands,
    normalize_command,
    runnable_detected_validation_commands,
)
from aura.conversation.project_profile import ProjectProfile


class TestNormalizeCommand:
    def test_collapses_whitespace(self) -> None:
        assert normalize_command("python   -m  pytest") == "python -m pytest"

    def test_strips_and_lowercases(self) -> None:
        assert normalize_command("  NPM RUN Build  ") == "npm run build"


class TestIsRunnableDetectedValidationCommand:
    def test_py_compile_with_touched_files_is_non_runnable(self) -> None:
        assert not is_runnable_detected_validation_command(
            "python -m py_compile (touched files)"
        )

    def test_bare_py_compile_is_runnable(self) -> None:
        assert is_runnable_detected_validation_command("python -m py_compile foo.py")

    def test_pytest_is_runnable(self) -> None:
        assert is_runnable_detected_validation_command("pytest")

    def test_any_touched_files_placeholder_is_non_runnable(self) -> None:
        assert not is_runnable_detected_validation_command(
            "some-check (touched files)"
        )


class TestIsBehavioralRequiredCommand:
    def test_pytest_is_behavioral(self) -> None:
        assert is_behavioral_required_command("pytest")

    def test_python_m_pytest_is_behavioral(self) -> None:
        assert is_behavioral_required_command("python -m pytest")

    def test_python_m_pytest_with_args_is_behavioral(self) -> None:
        assert is_behavioral_required_command("python -m pytest tests/ -x")

    def test_npm_test_is_behavioral(self) -> None:
        assert is_behavioral_required_command("npm test")

    def test_npm_run_build_is_behavioral(self) -> None:
        assert is_behavioral_required_command("npm run build")

    def test_cargo_test_is_behavioral(self) -> None:
        assert is_behavioral_required_command("cargo test")

    def test_cargo_build_is_behavioral(self) -> None:
        assert is_behavioral_required_command("cargo build")

    def test_go_test_is_behavioral(self) -> None:
        assert is_behavioral_required_command("go test ./...")

    def test_py_compile_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("python -m py_compile foo.py")

    def test_ruff_check_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("ruff check")

    def test_mypy_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("mypy")

    def test_non_runnable_placeholder_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("python -m py_compile (touched files)")

    def test_pyright_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("pyright")

    def test_pylint_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("pylint")

    def test_flake8_is_not_behavioral(self) -> None:
        assert not is_behavioral_required_command("flake8")


class TestBehavioralRequiredCommands:
    def test_filters_to_behavioral_only(self) -> None:
        commands = [
            "python -m py_compile (touched files)",
            "ruff check",
            "pytest",
            "mypy",
            "python -m pytest tests/",
            "cargo build",
        ]
        result = behavioral_required_commands(commands)
        assert result == ["pytest", "python -m pytest tests/", "cargo build"]

    def test_empty_list(self) -> None:
        assert behavioral_required_commands([]) == []

    def test_no_behavioral_commands(self) -> None:
        assert behavioral_required_commands(["ruff check", "mypy"]) == []


class TestRunnableDetectedValidationCommands:
    def _profile(self, *commands: str) -> ProjectProfile:
        return ProjectProfile(
            workspace_root="/fake",
            project_types=("python",),
            manifests=("pyproject.toml",),
            lockfiles=(),
            package_manager="pip",
            has_venv=True,
            python_venv_path=".venv",
            python_executable=None,
            declared_dependencies=(),
            validation_commands=commands,
            node_scripts=(),
        )

    def test_filters_non_runnable_placeholders(self) -> None:
        profile = self._profile(
            "python -m py_compile (touched files)",
            "pytest",
        )
        result = runnable_detected_validation_commands(profile)
        assert result == ["pytest"]

    def test_preserves_order(self) -> None:
        profile = self._profile("cargo test", "cargo build")
        assert runnable_detected_validation_commands(profile) == [
            "cargo test",
            "cargo build",
        ]

    def test_empty_when_profile_has_no_commands(self) -> None:
        profile = self._profile()
        assert runnable_detected_validation_commands(profile) == []

    def test_all_runnable_kept(self) -> None:
        profile = self._profile("pytest", "cargo test")
        result = runnable_detected_validation_commands(profile)
        assert result == ["pytest", "cargo test"]


class TestMergeValidationCommands:
    def test_planner_commands_come_first(self) -> None:
        merged = merge_validation_commands(
            ["pytest"],
            ["ruff check"],
        )
        assert merged == ["pytest", "ruff check"]

    def test_dedup_by_normalized_form(self) -> None:
        merged = merge_validation_commands(
            ["pytest"],
            ["pytest", "ruff check"],
        )
        assert merged == ["pytest", "ruff check"]

    def test_detected_fills_gaps(self) -> None:
        merged = merge_validation_commands(
            ["ruff check"],
            ["pytest"],
        )
        assert merged == ["ruff check", "pytest"]

    def test_planner_duplicates_dropped(self) -> None:
        merged = merge_validation_commands(
            ["pytest", "ruff check", "pytest"],
            [],
        )
        assert merged == ["pytest", "ruff check"]

    def test_normalize_dedup_ignores_whitespace_and_case(self) -> None:
        merged = merge_validation_commands(
            ["  PYTEST "],
            ["pytest"],
        )
        assert merged == ["  PYTEST "]

    def test_empty_planner_uses_detected(self) -> None:
        merged = merge_validation_commands([], ["pytest", "cargo test"])
        assert merged == ["pytest", "cargo test"]

    def test_both_empty(self) -> None:
        assert merge_validation_commands([], []) == []

    def test_tuple_input(self) -> None:
        merged = merge_validation_commands(("pytest",), ("ruff check",))
        assert merged == ["pytest", "ruff check"]
