"""Focused tests for aura/conversation/command_normalizer.py.

Covers the core normalization contract — Python interpreter and module-tool
rewriting when a project venv exists, and passthrough for non-Python commands.
"""

from __future__ import annotations

from pathlib import Path

from aura.conversation.command_normalizer import normalize_command


def _make_python_project(tmp_path: Path) -> Path:
    """Turn *tmp_path* into a Python project with a (fake) venv."""
    root = Path(tmp_path)
    (root / "pyproject.toml").write_text("")
    venv_python = root / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")  # existence is enough for detection
    return root


def _make_non_python_project(tmp_path: Path) -> Path:
    """Turn *tmp_path* into a project that does *not* look like Python."""
    root = Path(tmp_path)
    (root / "package.json").write_text("{}")
    return root


# =========================================================================
# Python commands in project with venv
# =========================================================================


class TestPythonCommandInVenvProject:
    """When the project has a venv, Python interpreter commands are rewritten
    to use the venv Python."""

    def test_python_rewritten_to_venv(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("python -m pytest", root)
        assert result.normalized is True
        assert "python.exe" in result.command
        assert ".venv" in result.command or "venv" in result.command
        assert result.original_command == "python -m pytest"

    def test_python3_rewritten_to_venv(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("python3 -m pytest tests/", root)
        assert result.normalized is True
        assert "python.exe" in result.command
        assert ".venv" in result.command or "venv" in result.command
        assert "-m pytest tests/" in result.command

    def test_py_rewritten_to_venv(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("py -m pytest", root)
        assert result.normalized is True
        assert "python.exe" in result.command


class TestPythonModuleToolInVenvProject:
    """Known Python module tools (pytest, ruff, mypy) are rewritten to
    ``python -m tool`` when the project has a venv."""

    def test_pytest_rewritten(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("pytest tests/", root)
        assert result.normalized is True
        assert "python.exe" in result.command
        assert "-m pytest" in result.command

    def test_ruff_rewritten(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("ruff check .", root)
        assert result.normalized is True
        assert "python.exe" in result.command
        assert "-m ruff" in result.command

    def test_mypy_rewritten(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("mypy src/", root)
        assert result.normalized is True
        assert "python.exe" in result.command
        assert "-m mypy" in result.command


# =========================================================================
# Non-Python commands — passthrough
# =========================================================================


class TestNonPythonCommandPassthrough:
    """Commands that are not Python-related must pass through unchanged."""

    def test_npm_command_stays_unchanged(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("npm test", root)
        assert result.normalized is False
        assert result.command == "npm test"

    def test_node_command_stays_unchanged(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("node server.js", root)
        assert result.normalized is False
        assert result.command == "node server.js"

    def test_git_command_stays_unchanged(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("git status", root)
        assert result.normalized is False
        assert result.command == "git status"


# =========================================================================
# No venv / no Python project
# =========================================================================


class TestNoPythonProject:
    """Without a Python project or venv, nothing should be rewritten."""

    def test_non_python_project_leaves_python_unchanged(self, tmp_path: Path) -> None:
        root = _make_non_python_project(tmp_path)
        result = normalize_command("python -m pytest", root)
        assert result.normalized is False
        assert result.command == "python -m pytest"

    def test_non_python_project_leaves_pytest_unchanged(self, tmp_path: Path) -> None:
        root = _make_non_python_project(tmp_path)
        result = normalize_command("pytest tests/", root)
        assert result.normalized is False
        assert result.command == "pytest tests/"


# =========================================================================
# Edge cases
# =========================================================================


class TestEdgeCases:
    """Boundary and edge cases for the normalizer."""

    def test_empty_string(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("", root)
        assert result.normalized is False
        assert result.command == ""

    def test_whitespace_only(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("  ", root)
        assert result.normalized is False
        assert result.command.strip() == ""

    def test_shell_operators_are_preserved(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("pytest tests/ && ruff check .", root)
        # Both segments may get rewritten independently
        assert result.normalized is True
        assert "&&" in result.command
        assert "python.exe" in result.command or "python" in result.command


# =========================================================================
# Shell-dialect validation — reject ambiguous constructs before execution
# =========================================================================


class TestBareCdRejected:
    """Bare cd/chdir without a chained command must be rejected with a
    validation_error."""

    def test_bare_cd_rejected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("cd src", root)
        assert result.valid is False
        assert "bare 'cd'" in result.validation_error

    def test_bare_chdir_rejected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("chdir src", root)
        assert result.valid is False
        assert "bare 'cd'" in result.validation_error

    def test_bare_cd_with_quoted_path_rejected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command('cd "my dir"', root)
        assert result.valid is False
        assert "bare 'cd'" in result.validation_error

    def test_cd_chained_with_and_operator_allowed(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("cd src && pytest", root)
        assert result.valid is True
        assert result.validation_error == ""

    def test_cd_chained_with_semicolon_allowed(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("cd src; pytest", root)
        assert result.valid is True


class TestExportRejected:
    """Leading 'export' keyword must be rejected — it's a Unix shell construct
    not available on Windows cmd."""

    def test_export_assignment_rejected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("export PATH=/usr/bin", root)
        assert result.valid is False
        assert "export" in result.validation_error

    def test_export_then_command_rejected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("export VAR=val && python test.py", root)
        assert result.valid is False
        assert "export" in result.validation_error

    def test_normal_commands_not_affected(self, tmp_path: Path) -> None:
        root = _make_python_project(tmp_path)
        result = normalize_command("pytest tests/", root)
        assert result.valid is True

    def test_shell_var_not_confused_with_export(self, tmp_path: Path) -> None:
        """A command containing 'export' as a substring or argument should
        not be falsely flagged."""
        root = _make_python_project(tmp_path)
        result = normalize_command("python -c 'import export'", root)
        assert result.valid is True

    def test_export_alone_not_flagged_as_tool(self, tmp_path: Path) -> None:
        """Just 'export' with no assignment should not crash the validator."""
        root = _make_python_project(tmp_path)
        result = normalize_command("export", root)
        # Bare "export" with no argument: the validator checks len < 2 and
        # returns empty string (not a meaningful export command).
        assert result.valid is True


class TestGodotCheckOnlyValidation:
    def test_bare_headless_project_start_is_upgraded_to_import(self, tmp_path: Path) -> None:
        result = normalize_command(
            '"C:\\Tools\\Godot_v4.6.3-stable_win64.exe" '
            '--headless --path "C:\\Projects\\Game"',
            tmp_path,
        )

        assert result.valid is True
        assert result.normalized is True
        assert result.command.endswith("--import")
        assert "upgraded to --import" in result.normalization_reason

    def test_validate_project_alias_is_rewritten_to_import(self, tmp_path: Path) -> None:
        result = normalize_command(
            '"C:\\Tools\\Godot_v4.6.3-stable_win64.exe" '
            '--headless --path "C:\\Projects\\Game" --validate-project',
            tmp_path,
        )

        assert result.valid is True
        assert "--validate-project" not in result.command
        assert result.command.endswith("--import")
        assert "alias rewritten" in result.normalization_reason

    def test_check_only_requires_script(self, tmp_path: Path) -> None:
        result = normalize_command(
            '"C:\\Tools\\Godot_v4.6.3-stable_win64.exe" '
            '--headless --check-only --path "C:\\Projects\\Game"',
            tmp_path,
        )

        assert result.valid is False
        assert "--script res://path/to/script.gd" in result.validation_error

    def test_focused_godot_check_is_allowed(self, tmp_path: Path) -> None:
        result = normalize_command(
            '"C:\\Tools\\Godot_v4.6.3-stable_win64.exe" '
            '--headless --path "C:\\Projects\\Game" --check-only '
            '--script "res://scripts/player.gd"',
            tmp_path,
        )

        assert result.valid is True
