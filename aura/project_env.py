from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from aura import python_env


@dataclass(frozen=True)
class ProjectToolchain:
    name: str
    root: Path
    local_executable: Path | None = None

    @property
    def has_project_local_environment(self) -> bool:
        return self.local_executable is not None


@dataclass(frozen=True)
class ProjectCommandPlan:
    command: str
    original_command: str = ""
    toolchain: str | None = None
    missing_tool: str | None = None
    failure_class: str | None = None
    environment_setup_needed: bool = False

    @property
    def ok(self) -> bool:
        return self.missing_tool is None

    @property
    def missing_dependency(self) -> str | None:
        return self.missing_tool


@dataclass(frozen=True)
class SafeInstallPolicy:
    allow_global_installs: bool = False
    requires_explicit_approval: bool = True


DEFAULT_SAFE_INSTALL_POLICY = SafeInstallPolicy()


def detect_project_toolchains(workspace_root: Path) -> list[ProjectToolchain]:
    root = Path(workspace_root)
    toolchains: list[ProjectToolchain] = []
    py = python_env.detect_python_toolchain(root)
    if py is not None:
        toolchains.append(
            ProjectToolchain(
                name="python",
                root=py.root,
                local_executable=py.python,
            )
        )
    return toolchains


def build_project_command(
    workspace_root: Path,
    command: str,
    *,
    explicit: bool = False,
) -> ProjectCommandPlan:
    original = str(command or "")
    if _python_provider_applies(workspace_root, original):
        plan = python_env.build_project_tool_command(
            workspace_root,
            original,
            explicit=explicit,
        )
        return ProjectCommandPlan(
            command=plan.command,
            original_command=plan.original_command,
            toolchain="python",
            missing_tool=plan.missing_dependency,
            failure_class=(
                "project_environment_missing_dependency"
                if plan.missing_dependency
                else None
            ),
            environment_setup_needed=plan.missing_dependency is not None,
        )

    missing = missing_external_tool_for_command(original)
    if missing:
        return ProjectCommandPlan(
            command=original,
            original_command=original,
            missing_tool=missing,
            failure_class="project_environment_missing_tool",
            environment_setup_needed=True,
        )
    return ProjectCommandPlan(command=original, original_command=original)


def build_project_command_rewrite(
    workspace_root: Path,
    command: str,
) -> ProjectCommandPlan:
    original = str(command or "")
    if _python_provider_applies(workspace_root, original):
        plan = python_env.build_project_python_command(workspace_root, original)
        return ProjectCommandPlan(
            command=plan.command,
            original_command=plan.original_command,
            toolchain="python",
        )
    return ProjectCommandPlan(command=original, original_command=original)


def project_environment_missing_payload(
    command: str,
    missing_tool: str,
    *,
    explicit: bool = False,
    failure_class: str = "project_environment_missing_tool",
    toolchain: str | None = None,
) -> dict[str, object]:
    if failure_class == "project_environment_missing_dependency" and toolchain == "python":
        return python_env.project_env_missing_dependency_payload(
            command,
            missing_tool,
            explicit=explicit,
        )

    requested = "requested validation" if explicit else "validation"
    return {
        "ok": False,
        "failure_class": failure_class,
        "error": (
            f"Project environment is missing tool '{missing_tool}' for {requested}. "
            "Install or configure it in the project-local toolchain before running this command."
        ),
        "recoverable": True,
        "suggested_next_tool": "run_terminal_command",
        "suggested_next_action": (
            "Set up the project's local toolchain or run an explicit user-approved setup command. "
            "Do not install dependencies globally by default."
        ),
        "blocked_command": command,
        "missing_tool": missing_tool,
        "environment_setup_needed": True,
    }


def preferred_python_for_compile(workspace_root: Path) -> Path:
    env = python_env.detect_project_python_env(Path(workspace_root))
    return env.python_for_compile


def quote_command_arg(value: Path | str) -> str:
    text = str(value)
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


def missing_external_tool_for_command(command: str) -> str | None:
    executable = _first_executable(command)
    if not executable or _looks_like_path(executable):
        return None
    if executable in _SHELL_BUILTINS:
        return None
    return executable if _which(executable) is None else None


def _python_provider_applies(workspace_root: Path, command: str) -> bool:
    return python_env.detect_python_toolchain(Path(workspace_root)) is not None or (
        python_env.python_relevant_to_command(Path(workspace_root), command)
    )


def _first_executable(command: str) -> str | None:
    try:
        tokens = shlex.split(str(command or ""), posix=(os.name != "nt"))
    except ValueError:
        tokens = str(command or "").split()
    if not tokens:
        return None
    executable = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if executable.endswith(".exe"):
        executable = executable[:-4]
    return executable


def _looks_like_path(executable: str) -> bool:
    return "/" in executable or "\\" in executable or executable.startswith(".")


def _which(executable: str) -> str | None:
    candidates = [executable]
    if os.name == "nt" and executable == "py":
        candidates.append("py.exe")
    for candidate in candidates:
        found = shutil_which(candidate)
        if found:
            return found
    return None


def shutil_which(executable: str) -> str | None:
    from shutil import which

    return which(executable)


_SHELL_BUILTINS = {
    "cd",
    "dir",
    "echo",
    "exit",
    "set",
}


__all__ = [
    "DEFAULT_SAFE_INSTALL_POLICY",
    "ProjectCommandPlan",
    "ProjectToolchain",
    "SafeInstallPolicy",
    "build_project_command",
    "build_project_command_rewrite",
    "detect_project_toolchains",
    "preferred_python_for_compile",
    "project_environment_missing_payload",
    "quote_command_arg",
]
