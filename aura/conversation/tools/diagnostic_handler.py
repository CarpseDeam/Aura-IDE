import os
import shlex
import subprocess
from pathlib import Path

from aura.project_env import build_project_command

ALLOWED_EXECUTABLES = {
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
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
}
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
FORBIDDEN_WORDS = {"install", "uninstall", "remove", "delete", "wipe", "format"}
DESTRUCTIVE_FLAGS = {"-rf", "--force", "--hard", "-D", "--delete"}
SHELL_METACHRACTERS = {"|", "&", ";", "$", "`", "(", ")", "<", ">", "\n"}


def parse_and_validate(command: str) -> list[str]:
    if not command or not command.strip():
        raise ValueError("Command cannot be empty")

    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except Exception:
        import sys

        parse_err = sys.exc_info()[1]
        raise ValueError(f"Failed to parse command shell tokens: {parse_err}")

    if not tokens:
        raise ValueError("Command cannot be empty")

    exe = tokens[0].strip("'\"")
    exe_name = exe.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if exe_name.endswith(".exe"):
        exe_name = exe_name[:-4]
    if exe == "grep":
        raise ValueError("Command rejected: use 'rg' or grep_search instead of bare 'grep' for Windows portability.")
    if exe not in ALLOWED_EXECUTABLES and not _is_project_venv_python(exe):
        raise ValueError(f"Command rejected: executable '{exe}' is not in the allowed list.")

    for token in tokens:
        for char in token:
            if char in SHELL_METACHRACTERS:
                raise ValueError(f"Command rejected: token contains shell metacharacter '{char}'")

    for token in tokens:
        t_low = token.lower()
        for word in FORBIDDEN_WORDS:
            if word in t_low:
                raise ValueError(f"Command rejected: argument contains forbidden word '{word}'")

    for token in tokens:
        if token.startswith("-"):
            if token in DESTRUCTIVE_FLAGS:
                raise ValueError(f"Command rejected: destructive flag '{token}' is not allowed")

    if exe == "git":
        for token in tokens[1:]:
            if token in BLOCKED_GIT_SUBCOMMANDS:
                raise ValueError(f"Command rejected: 'git {token}' is not allowed (blocked git subcommand: {token})")

    if exe in {"python", "python3", "py"} or _is_project_venv_python(exe):
        for i, token in enumerate(tokens):
            if token == "-c" and i + 1 < len(tokens):
                script = tokens[i + 1].lower()
                for forbidden in ["exec", "compile", "__import__", "open(", "write", "eval", "breakpoint"]:
                    if forbidden in script:
                        raise ValueError(
                            f"Command rejected: python -c script contains forbidden substring '{forbidden}'"
                        )

    return tokens


def _is_project_venv_python(executable: str) -> bool:
    normalized = executable.strip("'\"").replace("\\", "/").lower()
    return (
        normalized.endswith("/.venv/scripts/python.exe")
        or normalized.endswith("/venv/scripts/python.exe")
        or normalized.endswith("/.venv/bin/python")
        or normalized.endswith("/venv/bin/python")
    )


def run_diagnostic_command(command: str, timeout: int = 30, workspace_root: Path | None = None) -> dict:
    if workspace_root is None:
        raise ValueError("workspace_root is required")
    workspace_root = Path(workspace_root).resolve()

    command_plan = build_project_command(workspace_root, command, explicit=True)
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
            "command": command,
            "failure_class": command_plan.failure_class,
            "missing_tool": command_plan.missing_tool,
            "environment_setup_needed": True,
        }
    command = command_plan.command
    tokens = parse_and_validate(command)

    from aura.paths import safe_is_relative_to

    for token in tokens[1:]:
        if token.startswith("/") or token.startswith("\\") or ".." in token:
            if not safe_is_relative_to(token, workspace_root):
                raise ValueError(f"Command rejected: path '{token}' escapes the workspace root.")

    try:
        proc = subprocess.run(tokens, capture_output=True, timeout=timeout, cwd=workspace_root, text=True, shell=False)
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        exit_code = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        import sys

        timeout_err = sys.exc_info()[1]
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
    except Exception:
        import sys

        run_err = sys.exc_info()[1]
        return {
            "ok": False,
            "stdout": "",
            "stderr": f"Execution error: {run_err}",
            "exit_code": -1,
            "timed_out": False,
            "command": command,
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
        "command": command,
    }
