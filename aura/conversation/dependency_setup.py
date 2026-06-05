"""Project-local dependency setup helpers for Worker recovery."""
from __future__ import annotations

import configparser
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback
    tomllib = None


DEPENDENCY_FILES = (
    "pyproject.toml",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "pdm.toml",
)

_IMPORT_TO_PACKAGE = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dotenv": "python-dotenv",
    "fastapi": "fastapi",
    "google": "google",
    "multipart": "python-multipart",
    "PIL": "pillow",
    "sklearn": "scikit-learn",
    "yaml": "pyyaml",
}


@dataclass(frozen=True)
class DependencySetupPlan:
    module: str
    package: str
    declared: bool
    dependency_file: str
    setup_command: str
    needs_venv: bool = False


def safe_project_environment_setup_command(
    command: str,
    *,
    workspace_root: Path | None = None,
) -> bool:
    """Return True only for dependency setup commands scoped to the workspace."""
    normalized = _normalize_command(command)
    if not normalized:
        return False
    segments = _split_command_segments(normalized)
    if len(segments) != 1:
        return False
    tokens = _split_tokens(segments[0])
    if not tokens:
        return False

    lowered = [_clean_token(token).lower().replace("\\", "/") for token in tokens]

    if lowered in (["uv", "sync"], ["uv", "sync", "--all-extras"], ["uv", "sync", "--dev"]):
        return True
    if lowered == ["poetry", "install"] or lowered == ["pdm", "install"]:
        return True

    if _is_python_m_venv_dotvenv(lowered):
        return workspace_root is not None and not _workspace_venv_exists(Path(workspace_root))

    if _is_workspace_venv_pip_install(tokens, workspace_root=workspace_root):
        return True

    return False


def unsafe_global_environment_setup_command(command: str) -> bool:
    normalized = _normalize_command(command)
    if not normalized:
        return False
    for segment in _split_command_segments(normalized):
        tokens = _split_tokens(segment)
        if not tokens:
            continue
        lowered = [_clean_token(token).lower() for token in tokens]
        executable = lowered[0].replace("\\", "/").rsplit("/", 1)[-1]
        if executable.endswith(".exe"):
            executable = executable[:-4]
        if executable in {"pip", "pip3"} and len(lowered) > 1 and lowered[1] == "install":
            return True
        if executable in {"sudo", "apt", "apt-get", "winget", "choco", "brew"}:
            return True
        if executable == "npm" and "install" in lowered[1:] and "-g" in lowered[1:]:
            return True
        if executable in {"python", "python3", "py"} and _tokens_contain_pip_install(lowered):
            return True
    return False


def plan_dependency_setup(
    workspace_root: Path,
    module_name: str,
) -> DependencySetupPlan | None:
    root = Path(workspace_root)
    module = _top_level_module(module_name)
    if not module:
        return None
    package = _package_for_import(module)
    declared = dependency_declared(root, package)
    dependency_file = preferred_dependency_file(root)
    setup_command = preferred_setup_command(root)
    if setup_command is None:
        return None
    return DependencySetupPlan(
        module=module,
        package=package,
        declared=declared,
        dependency_file=dependency_file,
        setup_command=setup_command,
        needs_venv=setup_command == "python -m venv .venv",
    )


def dependency_declared(workspace_root: Path, package_name: str) -> bool:
    root = Path(workspace_root)
    normalized = _normalize_package_name(package_name)
    for dep in declared_dependencies(root):
        if _normalize_package_name(dep) == normalized:
            return True
    return False


def declared_dependencies(workspace_root: Path) -> set[str]:
    root = Path(workspace_root)
    deps: set[str] = set()
    deps.update(_pyproject_dependencies(root / "pyproject.toml"))
    deps.update(_requirements_dependencies(root / "requirements.txt"))
    deps.update(_requirements_dependencies(root / "requirements-dev.txt"))
    deps.update(_setup_cfg_dependencies(root / "setup.cfg"))
    deps.update(_setup_py_dependencies(root / "setup.py"))
    return {dep for dep in deps if dep}


def preferred_dependency_file(workspace_root: Path) -> str:
    root = Path(workspace_root)
    if (root / "pyproject.toml").exists():
        return "pyproject.toml"
    if (root / "requirements.txt").exists():
        return "requirements.txt"
    if (root / "setup.cfg").exists():
        return "setup.cfg"
    if (root / "setup.py").exists():
        return "setup.py"
    return "pyproject.toml"


def preferred_setup_command(workspace_root: Path) -> str | None:
    root = Path(workspace_root)
    if (root / "uv.lock").exists():
        return "uv sync"
    if (root / "poetry.lock").exists():
        return "poetry install"
    if (root / "pdm.lock").exists() or (root / "pdm.toml").exists():
        return "pdm install"
    if not _workspace_venv_exists(root):
        return "python -m venv .venv"
    python = _relative_workspace_python(root)
    if (root / "requirements.txt").exists():
        return f"{python} -m pip install -r requirements.txt"
    if (root / "pyproject.toml").exists() or (root / "setup.py").exists() or (root / "setup.cfg").exists():
        return f"{python} -m pip install -e ."
    return None


def project_install_command(workspace_root: Path, dependency_file: str | None = None) -> str:
    root = Path(workspace_root)
    python = _relative_workspace_python(root)
    dep_file = str(dependency_file or preferred_dependency_file(root))
    if dep_file in {"requirements.txt", "requirements-dev.txt"}:
        return f"{python} -m pip install -r {dep_file}"
    return f"{python} -m pip install -e ."


def missing_import_modules_from_issues(issues: object) -> list[str]:
    modules: list[str] = []
    if not isinstance(issues, list):
        return modules
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        if str(issue.get("code") or "") != "broken-import":
            continue
        message = str(issue.get("message") or "")
        match = re.search(r"Import source '([^']+)' could not be resolved", message)
        if match:
            module = _top_level_module(match.group(1))
            if module and module not in modules:
                modules.append(module)
    return modules


def _is_python_m_venv_dotvenv(tokens: list[str]) -> bool:
    return len(tokens) == 4 and tokens[0] in {"python", "python3", "py"} and tokens[1:4] == ["-m", "venv", ".venv"]


def _is_workspace_venv_pip_install(tokens: list[str], *, workspace_root: Path | None) -> bool:
    if len(tokens) < 5:
        return False
    executable = _clean_token(tokens[0])
    if not _is_workspace_venv_python(executable, workspace_root=workspace_root):
        return False
    lowered = [_clean_token(token).lower() for token in tokens]
    if lowered[1:4] != ["-m", "pip", "install"]:
        return False
    args = lowered[4:]
    if args == ["-e", "."]:
        return True
    if args == ["-e", ".[test]"] or args == ["-e", ".[dev]"]:
        return True
    if args == ["-r", "requirements.txt"] or args == ["-r", "requirements-dev.txt"]:
        return True
    return False


def _is_workspace_venv_python(value: str, *, workspace_root: Path | None) -> bool:
    text = value.strip("'\"").replace("\\", "/")
    lowered = text.lower()
    if lowered in {".venv/scripts/python.exe", ".venv/bin/python"}:
        return True
    if lowered.endswith("/.venv/scripts/python.exe") or lowered.endswith("/.venv/bin/python"):
        if workspace_root is None:
            return True
        try:
            root = Path(workspace_root).resolve()
            candidate = Path(value.strip("'\"")).resolve()
            return root in candidate.parents
        except OSError:
            return False
    return False


def _workspace_venv_exists(workspace_root: Path) -> bool:
    root = Path(workspace_root)
    return any(
        (root / ".venv" / part).exists()
        for part in (Path("Scripts/python.exe"), Path("bin/python"))
    )


def _relative_workspace_python(workspace_root: Path) -> str:
    if os.name == "nt" or (Path(workspace_root) / ".venv" / "Scripts" / "python.exe").exists():
        return r".venv\Scripts\python.exe"
    return ".venv/bin/python"


def _normalize_command(command: str) -> str:
    return " ".join(str(command or "").strip().split())


def _split_command_segments(command: str) -> list[str]:
    return [
        segment.strip()
        for segment in re.split(r"\s*(?:&&|\|\||[;|])\s*", command)
        if segment.strip()
    ]


def _split_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        return command.split()


def _clean_token(token: str) -> str:
    return str(token).strip("'\"")


def _tokens_contain_pip_install(tokens: list[str]) -> bool:
    for idx, token in enumerate(tokens[:-2]):
        if token == "-m" and tokens[idx + 1] == "pip" and tokens[idx + 2] == "install":
            return True
    return False


def _top_level_module(module_name: str) -> str:
    return str(module_name or "").strip().split(".", 1)[0]


def _package_for_import(module_name: str) -> str:
    return _IMPORT_TO_PACKAGE.get(module_name, module_name.replace("_", "-").lower())


def _normalize_package_name(value: str) -> str:
    name = re.split(r"\s*(?:[<>=!~]=?|;|\[)", str(value or "").strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _pyproject_dependencies(path: Path) -> set[str]:
    if not path.exists() or tomllib is None:
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    deps: set[str] = set()
    project = data.get("project")
    if isinstance(project, dict):
        deps.update(_dependency_names_from_list(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for values in optional.values():
                deps.update(_dependency_names_from_list(values))
    poetry = data.get("tool", {}).get("poetry") if isinstance(data.get("tool"), dict) else None
    if isinstance(poetry, dict):
        for section in ("dependencies", "dev-dependencies"):
            values = poetry.get(section)
            if isinstance(values, dict):
                deps.update(str(name) for name in values if str(name).lower() != "python")
    return {_normalize_package_name(dep) for dep in deps}


def _requirements_dependencies(path: Path) -> set[str]:
    if not path.exists():
        return set()
    deps: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith(("-", "git+", "http://", "https://")):
            continue
        deps.add(_normalize_package_name(line))
    return deps


def _setup_cfg_dependencies(path: Path) -> set[str]:
    if not path.exists():
        return set()
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return set()
    deps: set[str] = set()
    if parser.has_option("options", "install_requires"):
        deps.update(_dependency_names_from_list(parser.get("options", "install_requires").splitlines()))
    return deps


def _setup_py_dependencies(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    deps: set[str] = set()
    for match in re.finditer(r"install_requires\s*=\s*\[([^\]]*)\]", text, flags=re.S):
        deps.update(_dependency_names_from_list(re.findall(r"['\"]([^'\"]+)['\"]", match.group(1))))
    return deps


def _dependency_names_from_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {_normalize_package_name(str(item)) for item in value if str(item).strip()}


__all__ = [
    "DependencySetupPlan",
    "dependency_declared",
    "declared_dependencies",
    "missing_import_modules_from_issues",
    "plan_dependency_setup",
    "preferred_dependency_file",
    "preferred_setup_command",
    "project_install_command",
    "safe_project_environment_setup_command",
    "unsafe_global_environment_setup_command",
]
