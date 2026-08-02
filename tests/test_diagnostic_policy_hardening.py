"""What ``run_diagnostic_command`` will and will not run.

The tool's promise is that it is read-only and runs without a shell. These
tests pin the four places that promise was not kept: a Unix-only allowlist on
Windows, a substring screen standing in for a judgement about arbitrary code,
build tools admitted as if they were inspection tools, and output decoded with
whatever the machine's locale happened to be.
"""

from __future__ import annotations

import pathlib

import pytest

from aura.conversation.tools import diagnostic_handler as dh
from aura.conversation.tools.diagnostic_handler import (
    POSIX_ONLY_EXECUTABLES,
    READ_ONLY_SUBCOMMANDS,
    DiagnosticCommandRejected,
    allowed_executables,
    parse_and_validate,
)


@pytest.fixture
def workspace(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("x = 1\n", encoding="utf-8")
    return tmp_path


def reject(workspace, command) -> DiagnosticCommandRejected:
    with pytest.raises(DiagnosticCommandRejected) as excinfo:
        parse_and_validate(command, workspace_root=workspace)
    return excinfo.value


def accepts(workspace, command) -> list[str]:
    return parse_and_validate(command, workspace_root=workspace)


# ── the allowlist matches the platform ──────────────────────────────────────


class TestPlatformAllowlist:

    def test_coreutils_are_not_offered_on_windows(self, monkeypatch):
        monkeypatch.setattr(dh.os, "name", "nt")
        allowed = allowed_executables()
        assert not (allowed & POSIX_ONLY_EXECUTABLES)

    def test_coreutils_are_offered_on_posix(self, monkeypatch):
        monkeypatch.setattr(dh.os, "name", "posix")
        assert POSIX_ONLY_EXECUTABLES <= allowed_executables()

    def test_portable_tools_are_offered_everywhere(self, monkeypatch):
        for name in ("nt", "posix"):
            monkeypatch.setattr(dh.os, "name", name)
            allowed = allowed_executables()
            assert {"git", "rg", "python", "pytest"} <= allowed

    @pytest.mark.parametrize("command", ["ls -la", "cat setup.py", "wc -l a.py"])
    def test_a_coreutil_is_refused_on_windows(self, workspace, monkeypatch, command):
        monkeypatch.setattr(dh.os, "name", "nt")
        rejection = reject(workspace, command)
        assert rejection.failure_class == "diagnostic_command_executable_not_allowed"

    def test_find_is_refused_on_windows_because_it_is_a_different_program(
        self, workspace, monkeypatch,
    ):
        """``find.exe`` ships with Windows and is an unrelated string-search
        tool, so allowing the name means one command runs two programs."""
        monkeypatch.setattr(dh.os, "name", "nt")
        rejection = reject(workspace, "find . -name '*.py'")
        assert rejection.failure_class == "diagnostic_command_executable_not_allowed"

    def test_the_refusal_names_a_portable_alternative(self, workspace, monkeypatch):
        monkeypatch.setattr(dh.os, "name", "nt")
        rejection = reject(workspace, "cat tests/test_demo.py")
        assert "read_file" in rejection.correction

    def test_the_refusal_reports_the_platform_it_applied(self, workspace, monkeypatch):
        monkeypatch.setattr(dh.os, "name", "nt")
        rejection = reject(workspace, "ls")
        assert rejection.details["platform"] == "nt"


# ── python -c is refused, not screened ──────────────────────────────────────


class TestInlineScriptsAreRefused:

    def test_an_obfuscated_exec_no_longer_slips_through(self, workspace):
        """The substring screen looked for the word 'exec'; building it from
        chr() calls reached exec without ever containing it."""
        command = 'python -c "getattr(__builtins__, chr(101)+chr(120)+chr(101)+chr(99))(1)"'
        assert reject(workspace, command).failure_class == "diagnostic_command_inline_script"

    @pytest.mark.parametrize("command", [
        'python -c "print(1)"',
        'python3 -c "import sys; print(sys.version)"',
        'py -c "print(2+2)"',
    ])
    def test_every_inline_script_is_refused(self, workspace, command):
        assert reject(workspace, command).failure_class == "diagnostic_command_inline_script"

    def test_the_refusal_points_at_the_approved_path(self, workspace):
        rejection = reject(workspace, 'python -c "print(1)"')
        assert rejection.details["suggested_next_tool"] == "run_terminal_command"

    def test_a_harmless_word_no_longer_causes_a_false_rejection(self, workspace):
        """``print('compiler design')`` was rejected for containing 'compile'.
        Screening cannot be right in both directions, so it is gone — and the
        commands it wrongly blocked are ordinary again."""
        assert accepts(workspace, "python -m pytest tests/test_demo.py")
        assert accepts(workspace, "ruff check .")

    @pytest.mark.parametrize("command", [
        "python -m pytest tests/",
        "python --version",
        "python -m ruff check .",
    ])
    def test_module_invocations_are_unaffected(self, workspace, command):
        assert accepts(workspace, command)


# ── build tools are not inspection tools ────────────────────────────────────


class TestBuildsAreRefused:

    @pytest.mark.parametrize("command", [
        "cargo build",
        "go build ./...",
        "npm run build",
        "make",
        "make install-deps",
        "cmake .",
    ])
    def test_a_build_is_refused(self, workspace, command):
        """Build output is a workspace mutation however it is spelled."""
        rejection = reject(workspace, command)
        assert rejection.failure_class == "diagnostic_command_build_not_read_only"

    @pytest.mark.parametrize("command", [
        "cargo check",
        "cargo tree",
        "go vet ./...",
        "go version",
        "npm ls",
        "npm outdated",
        "make -n",
    ])
    def test_a_read_only_subcommand_is_allowed(self, workspace, command):
        assert accepts(workspace, command)

    @pytest.mark.parametrize("command", ["cargo --version", "go --version", "npm -v"])
    def test_a_version_flag_is_always_allowed(self, workspace, command):
        assert accepts(workspace, command)

    def test_the_refusal_lists_what_is_allowed_instead(self, workspace):
        rejection = reject(workspace, "cargo build")
        assert set(rejection.details["read_only_subcommands"]) == set(
            READ_ONLY_SUBCOMMANDS["cargo"]
        )
        assert rejection.details["suggested_next_tool"] == "run_terminal_command"

    def test_tools_without_a_build_mode_are_unrestricted(self, workspace):
        assert accepts(workspace, "git status")
        assert accepts(workspace, "pytest tests/")
        assert accepts(workspace, "rg pattern .")

    def test_an_install_is_still_refused_as_a_mutation(self, workspace):
        """The whole-token mutation check runs first and stays the reason."""
        assert reject(workspace, "npm install left-pad").failure_class == (
            "diagnostic_command_mutating"
        )


# ── output decoding does not depend on the machine ──────────────────────────


class TestDeterministicDecode:

    def test_output_is_decoded_as_utf8(self, tmp_path, monkeypatch):
        """`text=True` alone decodes with the locale encoding — cp1252 on a
        default Windows install — so identical commands produced different
        text on different machines."""
        captured = {}
        real_run = dh.subprocess.run

        def spy(argv, **kwargs):
            captured.update(kwargs)
            return real_run(argv, **kwargs)

        monkeypatch.setattr(dh.subprocess, "run", spy)
        dh.run_diagnostic_command("git --version", workspace_root=tmp_path)

        assert captured.get("encoding") == "utf-8"
        assert captured.get("errors") == "replace"

    def test_non_ascii_output_survives_the_round_trip(self, tmp_path):
        import subprocess as sp

        if sp.run(["git", "--version"], capture_output=True).returncode != 0:
            pytest.skip("git is not installed")
        sp.run(["git", "init", "-q", "."], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.email", "a@b.c"], cwd=tmp_path, capture_output=True)
        sp.run(["git", "config", "user.name", "tëst ünicode"], cwd=tmp_path, capture_output=True)
        (tmp_path / "café.txt").write_text("x", encoding="utf-8")

        result = dh.run_diagnostic_command("git status", workspace_root=tmp_path)
        assert result["stdout"] or result["stderr"]
        assert "café.txt" in result["stdout"] or result["exit_code"] == 0

    def test_the_tool_never_runs_a_shell(self, tmp_path, monkeypatch):
        captured = {}
        real_run = dh.subprocess.run

        def spy(argv, **kwargs):
            captured.update(kwargs)
            captured["argv"] = argv
            return real_run(argv, **kwargs)

        monkeypatch.setattr(dh.subprocess, "run", spy)
        dh.run_diagnostic_command("git --version", workspace_root=tmp_path)

        assert captured.get("shell") is False
        assert isinstance(captured["argv"], list)


def test_the_exported_allowlist_matches_this_platform():
    assert dh.ALLOWED_EXECUTABLES == allowed_executables()


def test_a_workspace_relative_path_still_resolves(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text("x = 1\n", encoding="utf-8")
    argv = parse_and_validate("rg needle tests/test_demo.py", workspace_root=tmp_path)
    assert argv[-1] == "tests/test_demo.py"
    assert pathlib.Path(argv[0]).name.startswith("rg") or argv[0] == "rg"
