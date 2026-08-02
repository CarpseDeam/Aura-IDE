"""Read-only diagnostic command execution over one canonical argv.

The tool used to rewrite the command into a shell string, reparse that string
with :func:`shlex.split`, validate the *raw* first token, and then hand the
still-quoted Windows tokens to ``subprocess.run(..., shell=False)``.  Three
things went wrong at once:

* ``shlex.split(..., posix=False)`` keeps the quote characters inside the
  token, so a project interpreter at ``"C:\\Program Files\\proj\\.venv\\...``
  was executed as a literal ``"C:\\Program`` — every rewritten command failed;
* the allowlist, the git restrictions and the ``python -c`` restrictions were
  applied to the raw token while the normalized name was computed and thrown
  away, so a quoted or absolute ``git``/``python`` slipped past them;
* the project-venv acceptance test was a string-suffix match, which rejected a
  legitimate relative ``.venv\\Scripts\\python.exe`` and accepted any lookalike
  interpreter living outside the workspace.

So this module now owns exactly one argv.  The command is rewritten for the
project environment first, parsed **once** with the platform's real argument
rules, canonicalized (outer quotes removed, a path-shaped executable resolved
to an absolute path), and then the *same list* is what every safety check reads
and what ``subprocess.run`` executes.

Rejections are structured and actionable: a ``failure_class``, the offending
token or component, the requested command, the safe normalized details, and one
concise correction.  Nothing here is a shell — pipes, redirects and chaining are
refused rather than silently executed as literal arguments, and the model is
pointed at ``run_terminal_command`` when it genuinely wanted a shell.
"""
from __future__ import annotations

import ntpath
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any

from aura.project_env import (
    build_project_command,
    resolve_workspace_cwd,
    workspace_relative_cwd,
)
from aura.python_env import PYTHON_VENV_CANDIDATES

#: Programs that exist and mean the same thing on every platform Aura runs on.
PORTABLE_EXECUTABLES = {
    "python",
    "python3",
    "py",
    "pytest",
    "ruff",
    "mypy",
    "npm",
    "cargo",
    "go",
    "make",
    "cmake",
    "git",
    "rg",
}

#: Coreutils. Absent from a stock Windows install, and ``find`` is worse than
#: absent there — ``find.exe`` ships with Windows and is an unrelated
#: string-search program, so allowing the name cross-platform means the same
#: command runs two different tools. Offered only where they are real.
POSIX_ONLY_EXECUTABLES = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
}

#: Read-only subcommands of tools whose ordinary use is to build.
#: ``cargo build``/``go build``/``make`` write build output into the workspace,
#: which is a mutation however it is spelled, so these tools are allowed only
#: for the inspection subcommands they also happen to provide.
READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"ls", "list", "view", "outdated", "why", "doctor", "root", "prefix"}),
    "cargo": frozenset({"check", "tree", "metadata", "verify-project", "locate-project"}),
    "go": frozenset({"version", "env", "list", "vet", "doc"}),
    "make": frozenset({"-n", "--dry-run", "--just-print", "--recon", "-p", "--print-data-base"}),
    "cmake": frozenset({"-E", "--help-module-list", "--system-information"}),
}

#: Flags that only ever report and never act, accepted for any allowed tool.
VERSION_FLAGS = frozenset({"--version", "-V", "-v", "--help", "-h"})


def allowed_executables() -> set[str]:
    """The diagnostic allowlist for the platform this is running on."""
    if os.name == "nt":
        return set(PORTABLE_EXECUTABLES)
    return PORTABLE_EXECUTABLES | POSIX_ONLY_EXECUTABLES


#: Backwards-compatible snapshot of the allowlist for this platform.
ALLOWED_EXECUTABLES = allowed_executables()
BLOCKED_GIT_SUBCOMMANDS = {
    "reset",
    "clean",
    "commit",
    "push",
    "pull",
    "merge",
    "rebase",
    "checkout",
    "switch",
    "branch",
    "tag",
    "add",
    "rm",
    "mv",
    "cherry-pick",
    "revert",
    "stash",
    "apply",
    "am",
    "init",
    "clone",
    "fetch",
}

#: Words that name a mutation when they stand alone as a whole argument.
#: Matched on the *whole token*, never as a substring: ``pip install`` is a
#: mutation, ``tests/test_delete_user.py::test_remove`` is a node ID.
MUTATING_ARGUMENTS = {
    "install",
    "uninstall",
    "remove",
    "delete",
    "wipe",
    "format",
    "rm",
    "rmdir",
    "del",
}
DESTRUCTIVE_FLAGS = {"-rf", "-fr", "--force", "--hard", "-D", "--delete", "-delete"}

#: Characters that only mean anything in a shell.  Rejected when they appear
#: *unquoted*, because this tool never runs a shell and would otherwise pass
#: ``|`` or ``>`` to the program as a plain argument and quietly do the wrong
#: thing.  Quoted occurrences are ordinary data (``rg "a|b"``) and are kept.
SHELL_OPERATORS = {"|", "&", ";", "<", ">", "`", "\n", "\r"}

#: Names treated as a Python interpreter for identity purposes.
PYTHON_EXECUTABLE_NAMES = {"python", "python3", "py", "pythonw"}

#: ``python -c`` is refused outright rather than screened.
#:
#: The screen used to be a substring blacklist ("exec", "eval", "open(", …),
#: which is wrong in both directions: ``chr(101)+chr(120)+chr(101)+chr(99)``
#: reaches ``exec`` without containing it, while ``print('compiler design')``
#: was rejected for containing "compile". Deciding whether a program is
#: read-only by looking for words in its source is not a decision that can be
#: made correctly, so the tool does not try — ``python -c`` is arbitrary code,
#: and arbitrary code belongs to ``run_terminal_command``, which is approved.
#:
#: ``python -m pytest``, ``python --version`` and the rest are unaffected.
PYTHON_C_FLAGS = frozenset({"-c"})

_EXECUTABLE_SUFFIXES = (".exe", ".cmd", ".bat", ".com")


class DiagnosticCommandRejected(ValueError):
    """A diagnostic command that must not run, with the reason machine-readable."""

    def __init__(
        self,
        *,
        failure_class: str,
        error: str,
        correction: str,
        component: str = "command",
        offending_token: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(error)
        self.failure_class = failure_class
        self.error = error
        self.correction = correction
        self.component = component
        self.offending_token = offending_token
        self.details = dict(details or {})

    def payload(self, **context: Any) -> dict[str, Any]:
        """Return the structured, actionable result for this rejection."""
        payload: dict[str, Any] = {
            "ok": False,
            "recoverable": True,
            "failure_class": self.failure_class,
            "error": self.error,
            "correction": self.correction,
            "offending_component": self.component,
            "offending_token": self.offending_token,
            "exit_code": -1,
            "timed_out": False,
            "stdout": "",
            "stderr": self.error,
        }
        payload.update(self.details)
        payload.update(context)
        return payload


# ── parsing ─────────────────────────────────────────────────────────────────


def split_command_line(command: str) -> list[str]:
    """Parse *command* into argv exactly once, using the platform's own rules.

    On Windows this is ``CommandLineToArgvW`` semantics — the inverse of
    :func:`subprocess.list2cmdline`, which is what the project-environment
    rewrite uses to quote the interpreter path.  Round-tripping through the same
    rules is what makes a path with spaces survive as a single argument with its
    backslashes intact and its syntactic quotes gone.
    """
    if os.name != "nt":
        return shlex.split(command, posix=True)
    return _split_windows_command_line(command)


def _split_windows_command_line(command: str) -> list[str]:
    argv: list[str] = []
    current: list[str] = []
    started = False
    in_quotes = False
    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if char == "\\":
            run_end = index
            while run_end < length and command[run_end] == "\\":
                run_end += 1
            slashes = run_end - index
            if run_end < length and command[run_end] == '"':
                current.append("\\" * (slashes // 2))
                started = True
                if slashes % 2:
                    current.append('"')
                    index = run_end + 1
                else:
                    index = run_end
                continue
            current.append("\\" * slashes)
            started = True
            index = run_end
            continue

        if char == '"':
            if in_quotes and index + 1 < length and command[index + 1] == '"':
                current.append('"')
                index += 2
            else:
                in_quotes = not in_quotes
                index += 1
            started = True
            continue

        if not in_quotes and char in " \t\r\n":
            if started:
                argv.append("".join(current))
                current = []
                started = False
            index += 1
            continue

        current.append(char)
        started = True
        index += 1

    if in_quotes:
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_unparsable",
            error="Command rejected: unbalanced quote — the command could not be parsed into arguments.",
            correction="Close every quote, or drop the quotes entirely when the argument has no spaces.",
            component="quoting",
        )
    if started:
        argv.append("".join(current))
    return argv


def find_unquoted_shell_operator(command: str) -> str | None:
    """Return the first shell operator appearing outside quotes, if any.

    This must agree with :func:`split_command_line` about where the quotes are,
    or the two disagree about the same string: on Windows a backslash run before
    a quote can escape it, so ``python -c "print(\\"a|b\\")"`` never leaves the
    quoted argument and its ``|`` is data, not a pipe.
    """
    if os.name != "nt":
        return _find_posix_unquoted_shell_operator(command)
    return _find_windows_unquoted_shell_operator(command)


def _find_posix_unquoted_shell_operator(command: str) -> str | None:
    in_single = False
    in_double = False
    index = 0
    length = len(command)

    while index < length:
        char = command[index]
        if char == "\\" and not in_single:
            index += 2
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            index += 1
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            index += 1
            continue
        if not in_single and not in_double:
            operator = _operator_at(command, index)
            if operator is not None:
                return operator
        index += 1
    return None


def _find_windows_unquoted_shell_operator(command: str) -> str | None:
    """Scan with ``CommandLineToArgvW`` quoting rules, the ones argv is built on."""
    in_quotes = False
    index = 0
    length = len(command)

    while index < length:
        char = command[index]

        if char == "\\":
            run_end = index
            while run_end < length and command[run_end] == "\\":
                run_end += 1
            if run_end < length and command[run_end] == '"' and (run_end - index) % 2:
                # An odd backslash run escapes the quote: it is a literal quote
                # character and the quoting state does not change.
                index = run_end + 1
            else:
                index = run_end
            continue

        if char == '"':
            if in_quotes and index + 1 < length and command[index + 1] == '"':
                index += 2
                continue
            in_quotes = not in_quotes
            index += 1
            continue

        if not in_quotes:
            operator = _operator_at(command, index)
            if operator is not None:
                return operator
        index += 1
    return None


def _operator_at(command: str, index: int) -> str | None:
    char = command[index]
    if char == "$" and index + 1 < len(command) and command[index + 1] == "(":
        return "$("
    if char in SHELL_OPERATORS:
        return char
    return None


# ── executable identity ─────────────────────────────────────────────────────


def executable_identity(token: str) -> str:
    """Return the normalized program name for *token*.

    ``"C:\\proj\\.venv\\Scripts\\python.exe"``, ``.venv/bin/python`` and
    ``python`` all reduce to ``python``.  Every safety decision — allowlisting,
    git restrictions, ``python -c`` restrictions, the grep refusal — reads this
    single value, so quoting or spelling the executable as a path can no longer
    route around a check.
    """
    name = token.replace("\\", "/").rsplit("/", 1)[-1].strip().lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def _looks_like_path(token: str) -> bool:
    return "/" in token or "\\" in token or token.startswith(".")


def _path_candidates(raw: str) -> list[str]:
    candidates = [raw]
    if os.name != "nt" and "\\" in raw:
        # A Windows-spelled workspace path is still a workspace path when the
        # same command is exercised on POSIX.
        candidates.append(raw.replace("\\", "/"))
    return candidates


def _resolve_against(raw: str, working_directory: Path) -> Path | None:
    for candidate in _path_candidates(raw):
        try:
            if ntpath.isabs(candidate) or Path(candidate).is_absolute():
                resolved = Path(candidate).resolve()
            else:
                resolved = (working_directory / candidate).resolve()
        except (OSError, ValueError):
            continue
        return resolved
    return None


def project_venv_interpreters(workspace_root: Path) -> list[Path]:
    """Return the interpreters that really exist in this workspace's venv."""
    interpreters: list[Path] = []
    for parts in PYTHON_VENV_CANDIDATES:
        candidate = Path(workspace_root).joinpath(*parts)
        try:
            if candidate.is_file():
                interpreters.append(candidate.resolve())
        except OSError:
            continue
    return interpreters


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _accepted_interpreters(workspace_root: Path, working_directory: Path) -> list[Path]:
    """Interpreters the project-environment rewrite could legitimately pick.

    The rewrite resolves the venv from the command's working directory, which
    is the workspace root unless a workspace-relative ``cwd`` narrowed it, so
    both roots are accepted — and nothing outside the workspace ever is.
    """
    accepted: list[Path] = []
    for root in (workspace_root, working_directory):
        for interpreter in project_venv_interpreters(root):
            if not any(_same_path(interpreter, seen) for seen in accepted):
                accepted.append(interpreter)
    return accepted


def resolve_project_venv_python(
    raw: str, workspace_root: Path, working_directory: Path
) -> Path | None:
    """Return the workspace venv interpreter *raw* names, or ``None``.

    Relative (``.venv\\Scripts\\python.exe``) and absolute spellings are both
    accepted, and only when the path actually resolves to the interpreter this
    workspace's ``.venv``/``venv`` contains.  A lookalike — ``my.venv``, a venv
    in a sibling checkout, a system interpreter — resolves elsewhere and is
    refused, so suffix matching can no longer be used to smuggle one in.
    """
    resolved = _resolve_against(raw, working_directory)
    if resolved is None:
        return None
    for interpreter in _accepted_interpreters(Path(workspace_root), working_directory):
        if _same_path(resolved, interpreter):
            return interpreter
    return None


# ── validation ──────────────────────────────────────────────────────────────


def canonical_argv(
    command: str,
    *,
    workspace_root: Path,
    working_directory: Path,
) -> list[str]:
    """Return the one argv this command will be validated on *and* executed as."""
    if not command or not command.strip():
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_empty",
            error="Command rejected: the command is empty.",
            correction="Pass a short read-only command, e.g. 'git status'.",
        )

    # Surrounding whitespace is not shell syntax, whatever it contains.
    command = command.strip()

    operator = find_unquoted_shell_operator(command)
    if operator is not None:
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_shell_syntax",
            error=(
                f"Command rejected: {_operator_description(operator)} "
                f"('{operator}') needs a shell, and diagnostics run without one."
            ),
            correction=(
                "Run one program per call, or use run_terminal_command when you "
                "genuinely need shell behavior."
            ),
            component="shell_operator",
            offending_token=operator,
            details={"suggested_next_tool": "run_terminal_command"},
        )

    argv = split_command_line(command)
    if not argv:
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_empty",
            error="Command rejected: the command parsed to no arguments.",
            correction="Pass a short read-only command, e.g. 'git status'.",
        )

    # Identity first: what the program *is* decides whether it may run at all,
    # before any filesystem question about where it lives.
    _validate_executable_identity(argv[0])
    argv[0] = _canonical_executable(argv[0], workspace_root, working_directory)
    _validate_argv(argv, workspace_root=workspace_root, working_directory=working_directory)
    return argv


def _operator_description(operator: str) -> str:
    if operator in {"|", "&", ";"}:
        return "shell chaining or piping"
    if operator in {"<", ">"}:
        return "shell redirection"
    if operator in {"`", "$("}:
        return "shell command substitution"
    return "shell syntax"


def _canonical_executable(
    raw: str, workspace_root: Path, working_directory: Path
) -> str:
    """Return argv[0] in the form that is both checked and executed.

    A path-shaped executable is resolved to an absolute path: on Windows a
    relative program path is resolved by ``CreateProcess`` against the *process*
    working directory, not the ``cwd`` handed to :func:`subprocess.run`, so a
    relative ``.venv\\Scripts\\python.exe`` would otherwise be found only by
    accident.
    """
    identity = executable_identity(raw)
    if not _looks_like_path(raw):
        return raw

    if identity in PYTHON_EXECUTABLE_NAMES:
        interpreter = resolve_project_venv_python(raw, workspace_root, working_directory)
        if interpreter is None:
            raise DiagnosticCommandRejected(
                failure_class="diagnostic_command_interpreter_not_project_venv",
                error=(
                    f"Command rejected: '{raw}' is not this workspace's project "
                    "interpreter."
                ),
                correction=(
                    "Use the bare name (e.g. 'python -m pytest') and the project "
                    "'.venv' interpreter is selected automatically, or spell a path "
                    "that resolves inside this workspace's .venv/venv."
                ),
                component="executable",
                offending_token=raw,
                details={
                    "executable": identity,
                    "project_venv_interpreters": [
                        str(path)
                        for path in _accepted_interpreters(
                            Path(workspace_root), working_directory
                        )
                    ],
                },
            )
        return str(interpreter)

    resolved = _resolve_against(raw, working_directory)
    if resolved is None or not resolved.is_file():
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_executable_not_found",
            error=f"Command rejected: executable path '{raw}' does not exist.",
            correction="Use the program's bare name, or a path that exists in this workspace.",
            component="executable",
            offending_token=raw,
            details={"executable": identity},
        )
    return str(resolved)


def _validate_executable_identity(token: str) -> None:
    """Refuse a program by normalized name, however it was spelled."""
    identity = executable_identity(token)

    if identity == "grep":
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_executable_not_allowed",
            error="Command rejected: 'grep' is not portable on Windows.",
            correction="Use 'rg' for a shell search, or grep_search for structured matches.",
            component="executable",
            offending_token=token,
            details={"executable": identity},
        )

    allowed = allowed_executables()
    if identity not in allowed:
        # A coreutil on Windows is not merely unavailable: `find` names a
        # different program there, so the same command would do something else.
        if identity in POSIX_ONLY_EXECUTABLES:
            error = (
                f"Command rejected: '{identity}' is a Unix tool and is not "
                "available or does not mean the same thing on this platform."
            )
            correction = (
                "Use read_file to read a file, glob or list_directory to list "
                "one, and grep_search or 'rg' to search — all of which work on "
                "every platform."
            )
        else:
            error = (
                f"Command rejected: executable '{identity}' is not in the "
                "diagnostic allowlist."
            )
            correction = (
                "Use a read-only inspection tool "
                f"({', '.join(sorted(allowed))}), or run_terminal_command "
                "for anything else."
            )
        raise DiagnosticCommandRejected(
            failure_class="diagnostic_command_executable_not_allowed",
            error=error,
            correction=correction,
            component="executable",
            offending_token=token,
            details={
                "executable": identity,
                "allowed_executables": sorted(allowed),
                "platform": os.name,
                "suggested_next_tool": "run_terminal_command",
            },
        )


def _validate_argv(
    argv: list[str], *, workspace_root: Path, working_directory: Path
) -> None:
    identity = executable_identity(argv[0])

    for token in argv[1:]:
        lowered = token.lower()
        if lowered in MUTATING_ARGUMENTS:
            raise DiagnosticCommandRejected(
                failure_class="diagnostic_command_mutating",
                error=f"Command rejected: '{token}' mutates state, and diagnostics are read-only.",
                correction=(
                    "Drop the mutating argument, or use run_terminal_command for "
                    "installs and other changes."
                ),
                component="argument",
                offending_token=token,
                details={"suggested_next_tool": "run_terminal_command"},
            )
        if token in DESTRUCTIVE_FLAGS:
            raise DiagnosticCommandRejected(
                failure_class="diagnostic_command_destructive_flag",
                error=f"Command rejected: destructive flag '{token}' is not allowed.",
                correction="Remove the flag; diagnostics inspect, they do not modify.",
                component="argument",
                offending_token=token,
                details={"suggested_next_tool": "run_terminal_command"},
            )

    if identity == "git":
        for token in argv[1:]:
            if token.lower() in BLOCKED_GIT_SUBCOMMANDS:
                raise DiagnosticCommandRejected(
                    failure_class="diagnostic_command_mutating_git",
                    error=f"Command rejected: 'git {token}' changes repository state.",
                    correction=(
                        "Use a read-only git command such as 'git status', 'git diff' "
                        "or 'git log'."
                    ),
                    component="git_subcommand",
                    offending_token=token,
                    details={"suggested_next_tool": "run_terminal_command"},
                )

    if identity in PYTHON_EXECUTABLE_NAMES:
        for token in argv[1:]:
            if token in PYTHON_C_FLAGS:
                raise DiagnosticCommandRejected(
                    failure_class="diagnostic_command_inline_script",
                    error=(
                        "Command rejected: 'python -c' runs an arbitrary program, "
                        "and diagnostics are read-only."
                    ),
                    correction=(
                        "Use read_file or grep_search to inspect, 'python -m <tool>' "
                        "to run a tool, or run_terminal_command when you really need "
                        "to execute a script."
                    ),
                    component="python_script",
                    offending_token=token,
                    details={"suggested_next_tool": "run_terminal_command"},
                )

    _validate_read_only_subcommand(identity, argv)

    for token in argv[1:]:
        offending = _workspace_escape(token, workspace_root, working_directory)
        if offending is not None:
            raise DiagnosticCommandRejected(
                failure_class="diagnostic_command_path_escapes_workspace",
                error=f"Command rejected: path '{token}' resolves outside the workspace.",
                correction="Reference paths inside the workspace, relative to its root.",
                component="argument",
                offending_token=token,
                details={"resolved_path": offending},
            )


def _validate_read_only_subcommand(identity: str, argv: list[str]) -> None:
    """Restrict build tools to the subcommands that only inspect.

    ``cargo build``, ``go build``, ``npm run build`` and a bare ``make`` all
    write build output into the workspace. That is a workspace mutation, and a
    diagnostic that mutates is exactly the thing this tool exists not to be —
    so these tools are admitted only for the inspection subcommands they
    happen to provide, and everything else is redirected to the approved
    terminal path.
    """
    allowed = READ_ONLY_SUBCOMMANDS.get(identity)
    if allowed is None:
        return

    subcommand: str | None = None
    for token in argv[1:]:
        if token in VERSION_FLAGS:
            return
        subcommand = token
        break

    if subcommand is not None and subcommand in allowed:
        return

    raise DiagnosticCommandRejected(
        failure_class="diagnostic_command_build_not_read_only",
        error=(
            f"Command rejected: '{identity} {subcommand}' builds or changes the "
            f"workspace."
            if subcommand
            else f"Command rejected: bare '{identity}' builds the workspace."
        ),
        correction=(
            f"Use a read-only {identity} subcommand "
            f"({', '.join(sorted(allowed))}), or run_terminal_command to build."
        ),
        component="subcommand",
        offending_token=subcommand or identity,
        details={
            "read_only_subcommands": sorted(allowed),
            "suggested_next_tool": "run_terminal_command",
        },
    )


def _workspace_escape(
    token: str, workspace_root: Path, working_directory: Path
) -> str | None:
    """Return the resolved path when *token* is a path that leaves the workspace."""
    if token.startswith("-"):
        return None
    parts = token.replace("\\", "/").split("/")
    absolute = ntpath.isabs(token) or token.startswith(("/", "\\"))
    if not absolute and ".." not in parts:
        return None

    resolved = _resolve_against(token, working_directory)
    if resolved is None:
        return None
    try:
        root = Path(workspace_root).resolve()
        relative = os.path.relpath(resolved, root)
    except (OSError, ValueError):
        return str(resolved)
    if relative.startswith("..") or os.path.isabs(relative):
        return str(resolved)
    return None


def parse_and_validate(
    command: str,
    *,
    workspace_root: Path | str | None = None,
    cwd: str | Path | None = None,
) -> list[str]:
    """Return the canonical argv for *command*, or raise ``DiagnosticCommandRejected``."""
    root = Path(workspace_root).resolve() if workspace_root is not None else Path.cwd()
    working_directory = resolve_workspace_cwd(root, cwd)
    return canonical_argv(command, workspace_root=root, working_directory=working_directory)


# ── execution ───────────────────────────────────────────────────────────────


def run_diagnostic_command(
    command: str,
    timeout: int = 30,
    workspace_root: Path | None = None,
    cwd: str | Path | None = None,
) -> dict:
    if workspace_root is None:
        raise ValueError("workspace_root is required")
    requested_command = command
    workspace_root = Path(workspace_root).resolve()
    working_directory = resolve_workspace_cwd(workspace_root, cwd)
    relative_cwd = workspace_relative_cwd(workspace_root, working_directory)

    command_plan = build_project_command(working_directory, command, explicit=True)
    if command_plan.missing_tool:
        missing_text = (
            f"Project environment is missing dependency '{command_plan.missing_tool}'. "
            "Install it into the project .venv before running this command."
            if command_plan.failure_class == "project_environment_missing_dependency"
            else (
                f"Project environment is missing tool '{command_plan.missing_tool}'. "
                "Install or configure it in the project-local toolchain before running this command."
            )
        )
        return {
            "ok": False,
            "stdout": "",
            "stderr": missing_text,
            "exit_code": -1,
            "timed_out": False,
            "command": requested_command,
            "requested_command": requested_command,
            "cwd": relative_cwd,
            "working_directory": relative_cwd,
            "failure_class": command_plan.failure_class,
            "missing_tool": command_plan.missing_tool,
            "environment_setup_needed": True,
        }

    normalized_command = command_plan.command
    try:
        argv = canonical_argv(
            normalized_command,
            workspace_root=workspace_root,
            working_directory=working_directory,
        )
    except DiagnosticCommandRejected as rejected:
        return rejected.payload(
            command=requested_command,
            requested_command=requested_command,
            normalized_command=normalized_command,
            cwd=relative_cwd,
            working_directory=relative_cwd,
        )

    try:
        # Explicit UTF-8: `text=True` alone decodes with the locale encoding,
        # which is cp1252 on a default Windows install. The same command then
        # produces different output text on different machines, and any
        # non-Latin-1 byte in a test name or a diff either mojibakes or raises.
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            cwd=working_directory,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as timeout_err:
        t_stdout = getattr(timeout_err, "stdout", None)
        t_stderr = getattr(timeout_err, "stderr", None)
        stdout = (
            t_stdout if isinstance(t_stdout, str) else (t_stdout.decode("utf-8", errors="replace") if t_stdout else "")
        )
        stderr = (
            t_stderr if isinstance(t_stderr, str) else (t_stderr.decode("utf-8", errors="replace") if t_stderr else "")
        )
        exit_code = -1
        timed_out = True
    except OSError as run_err:
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Execution error: {run_err}",
            "exit_code": -1,
            "timed_out": False,
            "failure_class": "diagnostic_command_execution_error",
            "correction": (
                "Check the program name and the workspace-relative paths, then retry "
                "the corrected command."
            ),
            "command": requested_command,
            "requested_command": requested_command,
            "normalized_command": normalized_command,
            "argv": list(argv),
            "cwd": relative_cwd,
            "working_directory": relative_cwd,
        }

    limit = 100 * 1024
    if len(stdout) + len(stderr) > limit:
        max_stdout = int(limit * 0.8)
        max_stderr = int(limit * 0.2)
        if len(stdout) > max_stdout:
            stdout = stdout[:max_stdout] + "\n[stdout truncated...]"
        if len(stderr) > max_stderr:
            stderr = stderr[:max_stderr] + "\n[stderr truncated...]"

    return {
        "ok": exit_code == 0 and not timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "command": requested_command,
        "requested_command": requested_command,
        "normalized_command": normalized_command,
        "argv": list(argv),
        "cwd": relative_cwd,
        "working_directory": relative_cwd,
    }


__all__ = [
    "ALLOWED_EXECUTABLES",
    "BLOCKED_GIT_SUBCOMMANDS",
    "DESTRUCTIVE_FLAGS",
    "DiagnosticCommandRejected",
    "MUTATING_ARGUMENTS",
    "PORTABLE_EXECUTABLES",
    "POSIX_ONLY_EXECUTABLES",
    "PYTHON_C_FLAGS",
    "PYTHON_EXECUTABLE_NAMES",
    "READ_ONLY_SUBCOMMANDS",
    "SHELL_OPERATORS",
    "VERSION_FLAGS",
    "allowed_executables",
    "canonical_argv",
    "executable_identity",
    "find_unquoted_shell_operator",
    "parse_and_validate",
    "project_venv_interpreters",
    "resolve_project_venv_python",
    "run_diagnostic_command",
    "split_command_line",
]
