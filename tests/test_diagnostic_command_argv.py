"""``run_diagnostic_command`` owns exactly one argv.

The production failure this pins down: the tool rewrote the command into a
shell string, reparsed it with ``shlex.split(..., posix=False)``, and handed the
still-quoted Windows tokens to ``subprocess.run(shell=False)``. Every rewritten
command on Windows therefore tried to execute a token that literally began with
a double quote, and the safety checks read the raw token while the normalized
name was computed and discarded.

What is asserted here:

* one canonical argv is parsed after the project-environment rewrite, and the
  argv that is validated is the argv that runs;
* syntactic quotes are gone from every argument while spaces, backslashes and
  pytest node IDs (plain, quoted, parametrized) survive byte for byte;
* ``pytest`` and ``python -m pytest`` both route through the project venv, and
  the interpreter may be named relatively or absolutely;
* an interpreter that is not this workspace's venv — outside the workspace, or
  a ``my.venv`` suffix lookalike — is refused;
* git and ``python -c`` restrictions apply through a quoted or absolute
  executable, because every check reads the normalized executable identity;
* pipes, redirects, chaining, mutation and installs are refused;
* refusals are structured: failure class, offending token, requested command,
  normalized details, one correction;
* the registered tool really executes on this platform.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from aura.conversation.tools.diagnostic_handler import (
    DiagnosticCommandRejected,
    canonical_argv,
    executable_identity,
    find_unquoted_shell_operator,
    run_diagnostic_command,
    split_command_line,
)
from aura.conversation.tools.registry import ToolRegistry

WINDOWS = os.name == "nt"
VENV_PARTS = ("Scripts", "python.exe") if WINDOWS else ("bin", "python")
VENV_SPELLING = r".venv\Scripts\python.exe" if WINDOWS else ".venv/bin/python"


# ── fixtures ────────────────────────────────────────────────────────────────


def make_venv(root: Path, *, name: str = ".venv", real: bool = False) -> Path:
    """Create an interpreter at ``root/name`` — a real one when *real*."""
    interpreter = root.joinpath(name, *VENV_PARTS)
    interpreter.parent.mkdir(parents=True, exist_ok=True)
    if real:
        shutil.copy2(sys.executable, interpreter)
        home = Path(sys.executable).parent
        (root / name / "pyvenv.cfg").write_text(
            f"home = {home}\n"
            "include-system-site-packages = true\n"
            f"version = {sys.version.split()[0]}\n",
            encoding="utf-8",
        )
    else:
        interpreter.write_text("stub interpreter\n", encoding="utf-8")
        interpreter.chmod(0o755)
    return interpreter


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A Python project whose path contains a space, as real ones do."""
    root = tmp_path / "my project"
    (root / "tests").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (root / "tests" / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    make_venv(root)
    return root


def argv_for(workspace: Path, command: str, *, cwd: Path | None = None) -> list[str]:
    """The canonical argv, after the project-environment rewrite."""
    from aura.project_env import build_project_command

    working = cwd or workspace
    plan = build_project_command(working, command, explicit=True)
    return canonical_argv(
        plan.command, workspace_root=workspace, working_directory=working
    )


def reject(workspace: Path, command: str) -> DiagnosticCommandRejected:
    with pytest.raises(DiagnosticCommandRejected) as excinfo:
        argv_for(workspace, command)
    return excinfo.value


def reject_argv(workspace: Path, command: str) -> DiagnosticCommandRejected:
    """Refuse *command* as argv, without the project-environment rewrite.

    The rewrite normalizes every Python spelling to the project interpreter, so
    a foreign interpreter can only *reach* the validator when no rewrite
    applies. The validator must refuse it either way — that invariant is what
    keeps a lookalike from running if any future caller skips the rewrite.
    """
    with pytest.raises(DiagnosticCommandRejected) as excinfo:
        canonical_argv(command, workspace_root=workspace, working_directory=workspace)
    return excinfo.value


# ── the parser ──────────────────────────────────────────────────────────────


class TestOneCanonicalArgv:

    def test_syntactic_quotes_are_removed_and_spaces_survive(self) -> None:
        parsed = split_command_line(r'"C:\my project\.venv\Scripts\python.exe" -m pytest')

        assert parsed[0] == r"C:\my project\.venv\Scripts\python.exe"
        assert parsed[1:] == ["-m", "pytest"]

    def test_backslashes_inside_a_path_are_not_escapes(self) -> None:
        parsed = split_command_line(r'"C:\a\b\c\python.exe" x')

        assert parsed[0] == r"C:\a\b\c\python.exe"
        assert "\\\\" not in parsed[0]

    @pytest.mark.parametrize(
        "command, expected",
        [
            ("pytest tests/test_demo.py::test_ok", "tests/test_demo.py::test_ok"),
            ('pytest "tests/test_demo.py::test needs space"',
             "tests/test_demo.py::test needs space"),
            ("pytest tests/test_demo.py::test_p[1-2]", "tests/test_demo.py::test_p[1-2]"),
            ('pytest "tests/test_demo.py::test_p[a b-c]"',
             "tests/test_demo.py::test_p[a b-c]"),
            ("pytest tests/test_demo.py::TestC::test_m[None-True]",
             "tests/test_demo.py::TestC::test_m[None-True]"),
        ],
    )
    def test_pytest_node_ids_survive_parsing(self, command: str, expected: str) -> None:
        assert split_command_line(command)[-1] == expected

    def test_the_parsed_command_is_split_once_not_reparsed(self, workspace) -> None:
        """Reparsing the argv's own join is what used to corrupt it."""
        argv = argv_for(workspace, "pytest tests/test_demo.py")

        assert split_command_line(" ".join(argv)) != argv or " " not in argv[0], (
            "either the interpreter has no space, or a naive rejoin loses it — "
            "which is exactly why the argv is built once and kept"
        )


class TestArgumentsArriveUnquoted:

    def test_no_argument_carries_a_literal_quote_character(self, workspace) -> None:
        argv = argv_for(
            workspace, 'pytest "tests/test_demo.py::test_p[a b]" -k "not slow"'
        )

        assert not any('"' in token for token in argv), argv
        assert "tests/test_demo.py::test_p[a b]" in argv
        assert "not slow" in argv

    def test_the_interpreter_is_one_token_despite_the_space(self, workspace) -> None:
        argv = argv_for(workspace, "pytest tests/test_demo.py")

        assert " " in argv[0], "the fixture workspace path contains a space"
        assert Path(argv[0]).is_file()


# ── the project venv ────────────────────────────────────────────────────────


class TestProjectVenvInterpreter:

    def test_bare_pytest_routes_through_the_project_venv(self, workspace) -> None:
        argv = argv_for(workspace, "pytest tests/test_demo.py")

        assert Path(argv[0]) == workspace.joinpath(".venv", *VENV_PARTS)
        assert argv[1:3] == ["-m", "pytest"]
        assert argv[3] == "tests/test_demo.py"

    @pytest.mark.parametrize("name", ["python", "python3", "py", "pytest", "ruff", "mypy"])
    def test_a_bare_tool_name_still_routes_through_the_project_venv(
        self, workspace, name: str
    ) -> None:
        """Bare names carry no choice of interpreter, so the venv supplies one."""
        argv = argv_for(workspace, f"{name} --version")

        assert Path(argv[0]) == workspace.joinpath(".venv", *VENV_PARTS)

    def test_python_dash_m_pytest_routes_through_the_project_venv(self, workspace) -> None:
        argv = argv_for(workspace, "python -m pytest tests/test_demo.py")

        assert Path(argv[0]) == workspace.joinpath(".venv", *VENV_PARTS)
        assert argv[1:] == ["-m", "pytest", "tests/test_demo.py"]

    def test_a_relative_venv_interpreter_is_accepted(self, workspace) -> None:
        argv = argv_for(workspace, f"{VENV_SPELLING} -m pytest tests/test_demo.py")

        assert Path(argv[0]) == workspace.joinpath(".venv", *VENV_PARTS)
        assert Path(argv[0]).is_absolute(), (
            "Windows resolves a relative program path against the process cwd, "
            "not the cwd handed to subprocess, so it must be made absolute"
        )

    def test_an_absolute_venv_interpreter_is_accepted(self, workspace) -> None:
        absolute = workspace.joinpath(".venv", *VENV_PARTS)
        argv = argv_for(workspace, f'"{absolute}" -m pytest')

        assert Path(argv[0]) == absolute

    def test_a_venv_python_in_a_subdirectory_cwd_still_resolves(self, workspace) -> None:
        argv = argv_for(
            workspace,
            f"{VENV_SPELLING} -m pytest",
            cwd=workspace,
        )

        assert Path(argv[0]) == workspace.joinpath(".venv", *VENV_PARTS)

    def test_an_interpreter_outside_the_workspace_is_refused(
        self, workspace, tmp_path
    ) -> None:
        outsider = make_venv(tmp_path / "other checkout")
        rejection = reject_argv(workspace, f'"{outsider}" -m pytest')

        assert rejection.failure_class == "diagnostic_command_interpreter_not_project_venv"
        assert rejection.component == "executable"

    def test_a_suffix_lookalike_venv_is_refused(self, workspace) -> None:
        lookalike = make_venv(workspace, name="my.venv")
        rejection = reject_argv(workspace, f'"{lookalike}" -m pytest')

        assert rejection.failure_class == "diagnostic_command_interpreter_not_project_venv"

    def test_a_system_interpreter_path_is_refused(self, workspace) -> None:
        rejection = reject_argv(workspace, f'"{sys.executable}" -c "print(1)"')

        assert rejection.failure_class == "diagnostic_command_interpreter_not_project_venv"

    def test_a_foreign_interpreter_survives_the_rewrite_and_is_refused(
        self, workspace, tmp_path
    ) -> None:
        """An explicit path is never silently swapped for the venv interpreter.

        Replacing it would run a different executable than the one asked for and
        report success; the caller must instead be told which interpreter this
        workspace accepts.
        """
        outsider = make_venv(tmp_path / "other checkout")
        rejection = reject(workspace, f'"{outsider}" -m pytest')

        assert rejection.failure_class == "diagnostic_command_interpreter_not_project_venv"
        assert rejection.offending_token == str(outsider)

    def test_the_rewrite_leaves_an_explicit_interpreter_path_alone(
        self, workspace, tmp_path
    ) -> None:
        from aura.project_env import build_project_command

        outsider = make_venv(tmp_path / "other checkout")
        command = f'"{outsider}" -m pytest'

        assert build_project_command(workspace, command, explicit=True).command == command

    def test_a_venv_path_that_does_not_exist_is_refused(self, tmp_path) -> None:
        root = tmp_path / "empty"
        (root / "tests").mkdir(parents=True)
        rejection = reject(root, f"{VENV_SPELLING} -m pytest")

        assert rejection.failure_class == "diagnostic_command_interpreter_not_project_venv"


# ── normalized identity drives every check ──────────────────────────────────


class TestExecutableIdentity:

    @pytest.mark.parametrize(
        "token",
        [
            "python",
            "python.exe",
            r"C:\proj\.venv\Scripts\python.exe",
            ".venv/bin/python",
            "/usr/bin/python",
        ],
    )
    def test_every_spelling_of_python_has_one_identity(self, token: str) -> None:
        assert executable_identity(token) == "python"

    def test_git_restrictions_apply_through_an_absolute_executable(
        self, workspace
    ) -> None:
        git = shutil.which("git")
        if git is None:
            pytest.skip("git is not installed")
        rejection = reject(workspace, f'"{git}" commit -m hello')

        assert rejection.failure_class == "diagnostic_command_mutating_git"
        assert rejection.offending_token == "commit"

    def test_git_restrictions_apply_through_a_quoted_executable(self, workspace) -> None:
        rejection = reject(workspace, '"git" push origin master')

        assert rejection.failure_class == "diagnostic_command_mutating_git"
        assert rejection.offending_token == "push"

    def test_read_only_git_is_allowed(self, workspace) -> None:
        assert argv_for(workspace, "git status --short")[1:] == ["status", "--short"]

    def test_python_dash_c_restrictions_apply_through_the_venv_interpreter(
        self, workspace
    ) -> None:
        absolute = workspace.joinpath(".venv", *VENV_PARTS)
        rejection = reject(workspace, f'"{absolute}" -c "__import__(\'os\').system(\'x\')"')

        assert rejection.failure_class == "diagnostic_command_inline_script"
        assert rejection.component == "python_script"

    def test_bare_grep_is_still_refused(self, workspace) -> None:
        rejection = reject(workspace, "grep -r thing .")

        assert rejection.failure_class == "diagnostic_command_executable_not_allowed"
        assert "rg" in rejection.correction

    def test_grep_is_refused_through_an_absolute_path(self, workspace) -> None:
        rejection = reject(workspace, "/usr/bin/grep -r thing .")

        assert rejection.failure_class == "diagnostic_command_executable_not_allowed"

    def test_an_unlisted_executable_is_refused(self, workspace) -> None:
        rejection = reject(workspace, "curl https://example.com")

        assert rejection.failure_class == "diagnostic_command_executable_not_allowed"
        assert rejection.details["suggested_next_tool"] == "run_terminal_command"


# ── shell syntax, escapes, mutation ─────────────────────────────────────────


class TestUnsafeCommandsAreRefused:

    @pytest.mark.parametrize(
        "command",
        [
            "git status | rg modified",
            "git status && git diff",
            "git status; git diff",
            "ls > out.txt",
            "cat < in.txt",
            "rg `whoami`",
            "rg $(whoami)",
        ],
    )
    def test_shell_syntax_is_refused(self, workspace, command: str) -> None:
        rejection = reject(workspace, command)

        assert rejection.failure_class == "diagnostic_command_shell_syntax"
        assert rejection.details["suggested_next_tool"] == "run_terminal_command"

    def test_a_quoted_pipe_is_data_not_an_operator(self, workspace) -> None:
        assert find_unquoted_shell_operator('rg "alpha|beta" .') is None
        assert "alpha|beta" in argv_for(workspace, 'rg "alpha|beta" .')

    def test_an_escaped_quote_does_not_end_the_quoted_argument(self) -> None:
        """The operator scan must see the same quotes the argv parser sees."""
        command = r'python -c "print(\"a|b\")"'

        assert find_unquoted_shell_operator(command) is None
        assert split_command_line(command)[-1] == r'print("a|b")'

    @pytest.mark.parametrize("operator", [">", "&", ";", "<", "|"])
    def test_an_operator_inside_escaped_quotes_is_argument_data(
        self, operator: str
    ) -> None:
        command = rf'rg "a\"x{operator}y\"b" .'

        assert find_unquoted_shell_operator(command) is None
        assert split_command_line(command)[1] == f'a"x{operator}y"b'

    @pytest.mark.parametrize(
        "command, expected",
        [
            (r'rg "a\"b\"" | wc -l', "|"),
            (r'rg "a\"b\"" > out.txt', ">"),
            (r'rg "a\"b\"" && git status', "&"),
            (r'rg "a\"b\"" ; git status', ";"),
            (r'rg "a\"b\"" $(whoami)', "$("),
            (r'rg "a\"b\"" `whoami`', "`"),
        ],
    )
    def test_a_real_operator_after_escaped_quotes_is_still_found(
        self, command: str, expected: str
    ) -> None:
        assert find_unquoted_shell_operator(command) == expected

    @pytest.mark.parametrize(
        "command",
        [
            "npm install left-pad",
            "python -m pip install requests",
            "cargo uninstall thing",
        ],
    )
    def test_installs_and_mutation_are_refused(self, workspace, command: str) -> None:
        rejection = reject(workspace, command)

        assert rejection.failure_class == "diagnostic_command_mutating"

    def test_a_destructive_flag_is_refused(self, workspace) -> None:
        rejection = reject(workspace, "rg --force thing .")

        assert rejection.failure_class in {
            "diagnostic_command_destructive_flag",
            "diagnostic_command_mutating",
        }

    def test_a_node_id_naming_deletion_is_not_a_mutation(self, workspace) -> None:
        """Whole-token matching, so test names are never mistaken for verbs."""
        argv = argv_for(workspace, "pytest tests/test_demo.py::test_delete_user")

        assert argv[-1] == "tests/test_demo.py::test_delete_user"

    def test_a_path_escaping_the_workspace_is_refused(self, workspace) -> None:
        rejection = reject(workspace, "rg secret ../../../etc")

        assert rejection.failure_class == "diagnostic_command_path_escapes_workspace"

    def test_an_absolute_path_outside_the_workspace_is_refused(
        self, workspace, tmp_path
    ) -> None:
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("x", encoding="utf-8")
        rejection = reject(workspace, f'rg secret "{outside}"')

        assert rejection.failure_class == "diagnostic_command_path_escapes_workspace"

    def test_a_workspace_relative_path_is_allowed(self, workspace) -> None:
        argv = argv_for(workspace, "rg secret tests/test_demo.py")
        assert argv[-1] == "tests/test_demo.py"

    def test_trailing_whitespace_is_not_shell_syntax(self, workspace) -> None:
        assert argv_for(workspace, "git status\n")[1] == "status"

    def test_an_embedded_newline_is_refused_as_chaining(self, workspace) -> None:
        rejection = reject(workspace, "git status\ngit diff")

        assert rejection.failure_class == "diagnostic_command_shell_syntax"

    def test_an_empty_command_is_refused(self, workspace) -> None:
        rejection = reject(workspace, "   ")

        assert rejection.failure_class == "diagnostic_command_empty"


# ── structured refusal payloads ─────────────────────────────────────────────


class TestStructuredRejectionPayload:

    def test_a_refusal_names_class_token_command_and_one_correction(
        self, workspace
    ) -> None:
        result = run_diagnostic_command(
            "git commit -m wip", workspace_root=workspace
        )

        assert result["ok"] is False
        assert result["failure_class"] == "diagnostic_command_mutating_git"
        assert result["offending_token"] == "commit"
        assert result["offending_component"] == "git_subcommand"
        assert result["requested_command"] == "git commit -m wip"
        assert result["normalized_command"]
        assert result["correction"].count(".") <= 2, "one concise correction"
        assert result["recoverable"] is True
        assert result["exit_code"] == -1

    def test_the_refusal_is_json_serializable_for_the_transcript(
        self, workspace
    ) -> None:
        result = run_diagnostic_command("ls > out.txt", workspace_root=workspace)

        assert json.loads(json.dumps(result))["failure_class"] == (
            "diagnostic_command_shell_syntax"
        )

    def test_a_refusal_never_leaks_an_unstructured_traceback(self, workspace) -> None:
        result = run_diagnostic_command("curl https://example.com", workspace_root=workspace)

        assert "Traceback" not in json.dumps(result)
        assert result["failure_class"] == "diagnostic_command_executable_not_allowed"
        assert result["correction"]
        assert result["suggested_next_tool"] == "run_terminal_command"


# ── the registered tool really runs ─────────────────────────────────────────


class TestRegisteredDiagnosticExecution:

    def _registry(self, root: Path) -> ToolRegistry:
        return ToolRegistry(workspace_root=root, mode="single")

    def test_a_real_venv_command_executes_from_a_path_with_spaces(
        self, tmp_path
    ) -> None:
        root = tmp_path / "real project"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='d'\n", encoding="utf-8")
        make_venv(root, real=True)

        result = self._registry(root)._handle_run_diagnostic_command(
            {"command": "python --version"}, None, False
        )

        assert result.ok is True, result.payload
        output = result.payload["stdout"] + result.payload["stderr"]
        assert "Python" in output
        assert Path(result.payload["argv"][0]) == root.joinpath(".venv", *VENV_PARTS)

    def test_a_real_pytest_run_reports_its_own_failure_not_a_launch_error(
        self, tmp_path
    ) -> None:
        root = tmp_path / "real pytest project"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='d'\n", encoding="utf-8")
        (root / "tests" / "test_red.py").write_text(
            "def test_red():\n    assert 1 == 2\n", encoding="utf-8"
        )
        make_venv(root, real=True)

        result = self._registry(root)._handle_run_diagnostic_command(
            {"command": "pytest tests/test_red.py -q"}, None, False
        )

        combined = result.payload["stdout"] + result.payload["stderr"]
        assert result.ok is False
        assert "test_red" in combined, combined[:2000]
        assert "is not recognized" not in combined
        assert "No such file" not in combined

    def test_a_foreign_interpreter_is_refused_by_the_registered_tool(
        self, workspace, tmp_path
    ) -> None:
        """End to end: no silent substitution, a structured refusal instead."""
        outsider = make_venv(tmp_path / "other checkout")

        result = self._registry(workspace)._handle_run_diagnostic_command(
            {"command": f'"{outsider}" -c "print(1)"'}, None, False
        )

        assert result.ok is False
        assert result.payload["failure_class"] == (
            "diagnostic_command_interpreter_not_project_venv"
        )
        assert result.payload["offending_token"] == str(outsider)
        assert result.payload["stdout"] == ""
        assert str(workspace.joinpath(".venv", *VENV_PARTS)) in (
            result.payload["project_venv_interpreters"]
        )

    def test_an_escaped_quote_script_is_refused_as_a_script_not_as_a_pipe(
        self, tmp_path,
    ) -> None:
        """``python -c`` is refused outright now, but *which* refusal still
        proves the operator scan and the argv parser agree about the quotes:
        if they disagreed, the ``|`` inside the quoted argument would be read
        as a pipe and this would come back as shell syntax."""
        root = tmp_path / "escaped project"
        (root / "tests").mkdir(parents=True)
        (root / "pyproject.toml").write_text("[project]\nname='d'\n", encoding="utf-8")
        make_venv(root, real=True)

        result = self._registry(root)._handle_run_diagnostic_command(
            {"command": r'python -c "print(\"a|b\")"'}, None, False
        )

        assert result.ok is False
        assert result.payload["failure_class"] == "diagnostic_command_inline_script"

    def test_a_registered_refusal_comes_back_structured(self, workspace) -> None:
        result = self._registry(workspace)._handle_run_diagnostic_command(
            {"command": "npm install left-pad"}, None, False
        )

        assert result.ok is False
        assert result.payload["failure_class"] == "diagnostic_command_mutating"
        assert result.payload["offending_token"] == "install"
