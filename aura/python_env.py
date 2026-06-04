from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


PYTHON_VENV_CANDIDATES = (
    (".venv", "Scripts", "python.exe"),
    ("venv", "Scripts", "python.exe"),
    (".venv", "bin", "python"),
    ("venv", "bin", "python"),
)

PROJECT_PYTHON_MODULE_TOOLS = {
    "pytest": "pytest",
    "ruff": "ruff",
    "mypy": "mypy",
}

PYTHON_PROJECT_MARKERS = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "requirements-dev.txt",
    "Pipfile",
    "poetry.lock",
    "uv.lock",
    "tox.ini",
    "pytest.ini",
    "mypy.ini",
    "ruff.toml",
    ".ruff.toml",
    ".python-version",
)

SKIPPED_PROJECT_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    ".aura",
}


@dataclass(frozen=True)
class ProjectPythonEnv:
    root: Path
    python: Path | None

    @property
    def has_venv(self) -> bool:
        return self.python is not None

    @property
    def python_for_compile(self) -> Path:
        return self.python or Path(sys.executable)


@dataclass(frozen=True)
class PythonCommandPlan:
    command: str
    missing_dependency: str | None = None
    original_command: str = ""

    @property
    def ok(self) -> bool:
        return self.missing_dependency is None


def detect_project_python_env(workspace_root: Path) -> ProjectPythonEnv:
    root = Path(workspace_root)
    for parts in PYTHON_VENV_CANDIDATES:
        candidate = root.joinpath(*parts)
        if candidate.is_file():
            return ProjectPythonEnv(root=root, python=candidate)
    return ProjectPythonEnv(root=root, python=None)


def detect_python_toolchain(workspace_root: Path) -> ProjectPythonEnv | None:
    """Return the Python environment only when this workspace looks Python-related."""
    root = Path(workspace_root)
    env = detect_project_python_env(root)
    if env.has_venv or python_project_detected(root):
        return env
    return None


def python_project_detected(workspace_root: Path) -> bool:
    root = Path(workspace_root)
    if any((root / marker).exists() for marker in PYTHON_PROJECT_MARKERS):
        return True
    return _has_python_file(root)


def python_relevant_to_command(workspace_root: Path, command: str) -> bool:
    if python_project_detected(workspace_root) or detect_project_python_env(workspace_root).has_venv:
        return True
    return _python_interpreter_command_uses_python_tool(command)


def _has_python_file(root: Path) -> bool:
    if not root.exists() or not root.is_dir():
        return False
    try:
        for child in root.iterdir():
            if child.name in SKIPPED_PROJECT_DIRS:
                continue
            if child.is_file() and child.suffix == ".py":
                return True
            if child.is_dir() and _has_python_file(child):
                return True
    except OSError:
        return False
    return False


def project_module_available(workspace_root: Path, module_name: str) -> bool:
    env = detect_project_python_env(workspace_root)
    if env.python is None:
        return False
    try:
        result = subprocess.run(
            [
                str(env.python),
                "-c",
                (
                    "import importlib.util, sys; "
                    f"sys.exit(0 if importlib.util.find_spec({module_name!r}) else 1)"
                ),
            ],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def build_project_python_command(workspace_root: Path, command: str) -> PythonCommandPlan:
    env = detect_project_python_env(workspace_root)
    original = str(command or "")
    if env.python is None:
        return PythonCommandPlan(command=original, original_command=original)

    segments = _split_shell_segments(original)
    if not segments:
        return PythonCommandPlan(command=original, original_command=original)

    rewritten: list[str] = []
    for segment in segments:
        replacement = _rewrite_python_segment(segment.text, env.python)
        rewritten.append(replacement + segment.separator)
    return PythonCommandPlan(
        command="".join(rewritten).strip(),
        original_command=original,
    )


def build_project_tool_command(
    workspace_root: Path,
    command: str,
    *,
    explicit: bool = False,
) -> PythonCommandPlan:
    if not python_relevant_to_command(workspace_root, command):
        original = str(command or "")
        return PythonCommandPlan(command=original, original_command=original)

    env = detect_project_python_env(workspace_root)
    original = str(command or "")
    if env.python is None:
        tool = _first_python_module_tool(original)
        if tool:
            return PythonCommandPlan(
                command=original,
                missing_dependency=tool,
                original_command=original,
            )
        return PythonCommandPlan(command=original, original_command=original)

    segments = _split_shell_segments(original)
    if not segments:
        return PythonCommandPlan(command=original, original_command=original)

    rewritten: list[str] = []
    for segment in segments:
        tool = _python_module_tool_for_segment(segment.text)
        if tool and not project_module_available(workspace_root, PROJECT_PYTHON_MODULE_TOOLS[tool]):
            return PythonCommandPlan(
                command=original,
                missing_dependency=tool,
                original_command=original,
            )
        replacement = _rewrite_python_segment(segment.text, env.python)
        rewritten.append(replacement + segment.separator)

    return PythonCommandPlan(
        command="".join(rewritten).strip(),
        original_command=original,
    )


def project_env_missing_dependency_payload(
    command: str,
    dependency: str,
    *,
    explicit: bool = False,
) -> dict[str, object]:
    requested = "requested validation" if explicit else "validation"
    return {
        "ok": False,
        "failure_class": "project_environment_missing_dependency",
        "error": (
            f"Project environment is missing dependency '{dependency}' for {requested}. "
            "Install it into the project .venv before running this command."
        ),
        "recoverable": True,
        "suggested_next_tool": "run_terminal_command",
        "suggested_next_action": (
            "Create a project-local environment with 'python -m venv .venv' if needed, "
            "then install project dependencies into .venv. Do not install into global Python."
        ),
        "blocked_command": command,
        "missing_dependency": dependency,
        "environment_setup_needed": True,
    }


def quote_command_arg(value: Path | str) -> str:
    text = str(value)
    if os.name == "nt":
        return subprocess.list2cmdline([text])
    return shlex.quote(text)


@dataclass(frozen=True)
class _ShellSegment:
    text: str
    separator: str


def _split_shell_segments(command: str) -> list[_ShellSegment]:
    parts = re.split(r"(\s*(?:&&|\|\||[;|])\s*)", command)
    segments: list[_ShellSegment] = []
    index = 0
    while index < len(parts):
        text = parts[index]
        separator = parts[index + 1] if index + 1 < len(parts) else ""
        if text.strip():
            segments.append(_ShellSegment(text=text.strip(), separator=separator))
        index += 2
    return segments


def _rewrite_python_segment(segment: str, python: Path) -> str:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        return segment
    if not tokens:
        return segment

    first = tokens[0].strip("'\"")
    first_name = first.replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first_name.endswith(".exe"):
        first_name = first_name[:-4]

    if first_name in {"python", "python3", "py"}:
        tokens[0] = quote_command_arg(python)
        return " ".join(tokens)

    tool = first_name
    if tool in PROJECT_PYTHON_MODULE_TOOLS:
        return " ".join(
            [
                quote_command_arg(python),
                "-m",
                tool,
                *tokens[1:],
            ]
        )

    return segment


def _first_python_module_tool(command: str) -> str | None:
    for segment in _split_shell_segments(command):
        tool = _python_module_tool_for_segment(segment.text)
        if tool:
            return tool
    return None


def _python_interpreter_command_uses_python_tool(command: str) -> bool:
    for segment in _split_shell_segments(str(command or "")):
        try:
            tokens = shlex.split(segment.text, posix=False)
        except ValueError:
            tokens = segment.text.split()
        if not tokens:
            continue
        first = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
        if first.endswith(".exe"):
            first = first[:-4]
        if first in {"python", "python3", "py"}:
            lowered = [token.strip("'\"").lower() for token in tokens]
            if "py_compile" in lowered:
                return True
            for idx, token in enumerate(lowered[:-1]):
                if token == "-m" and lowered[idx + 1] in PROJECT_PYTHON_MODULE_TOOLS:
                    return True
    return False


def _python_module_tool_for_segment(segment: str) -> str | None:
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None
    first = tokens[0].strip("'\"").replace("\\", "/").rsplit("/", 1)[-1].lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first in PROJECT_PYTHON_MODULE_TOOLS:
        return first
    if first in {"python", "python3", "py"}:
        lowered = [token.strip("'\"").lower() for token in tokens]
        for idx, token in enumerate(lowered[:-1]):
            if token == "-m" and lowered[idx + 1] in PROJECT_PYTHON_MODULE_TOOLS:
                return lowered[idx + 1]
    return None
