from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    tomllib = None  # type: ignore[assignment]


# Filename sets used for scanning

_MANIFEST_FILENAMES: tuple[str, ...] = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
)

_LOCKFILE_FILENAMES: tuple[str, ...] = (
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "Cargo.lock",
    "go.sum",
)

_PYTHON_MANIFESTS = frozenset(
    {"pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.py", "setup.cfg", "Pipfile"}
)
_NODE_MANIFESTS = frozenset({"package.json"})
_RUST_MANIFESTS = frozenset({"Cargo.toml"})
_GO_MANIFESTS = frozenset({"go.mod"})

_MANIFEST_TO_TYPE: dict[str, str] = {}
for _n in _PYTHON_MANIFESTS:
    _MANIFEST_TO_TYPE[_n] = "python"
for _n in _NODE_MANIFESTS:
    _MANIFEST_TO_TYPE[_n] = "node"
for _n in _RUST_MANIFESTS:
    _MANIFEST_TO_TYPE[_n] = "rust"
for _n in _GO_MANIFESTS:
    _MANIFEST_TO_TYPE[_n] = "go"


@dataclass(frozen=True)
class ProjectProfile:
    workspace_root: str
    project_types: tuple[str, ...]
    manifests: tuple[str, ...]
    lockfiles: tuple[str, ...]
    package_manager: str | None
    has_venv: bool
    python_venv_path: str | None
    python_executable: str | None
    declared_dependencies: tuple[str, ...]
    setup_command: str | None
    validation_commands: tuple[str, ...]
    node_scripts: tuple[tuple[str, str], ...]

    def summarize(self) -> str:
        lines: list[str] = []
        lines.append("Project types: " + (", ".join(self.project_types) if self.project_types else "(none)"))
        lines.append("Manifests: " + (", ".join(self.manifests) if self.manifests else "(none)"))
        if self.lockfiles:
            lines.append("Lockfiles: " + ", ".join(self.lockfiles))
        if self.package_manager:
            lines.append("Package manager: " + self.package_manager)
        if self.has_venv:
            lines.append("Virtual env: " + (self.python_venv_path or ".venv"))
        else:
            lines.append("Virtual env: (none)")
        if self.declared_dependencies:
            dep_list = ", ".join(self.declared_dependencies)
            lines.append(f"Dependencies ({len(self.declared_dependencies)}): {dep_list}")
        if self.setup_command:
            lines.append("Setup: " + self.setup_command)
        if self.validation_commands:
            lines.append("Validation: " + "  |  ".join(self.validation_commands))
        if self.node_scripts:
            script_names = ", ".join(name for name, _cmd in self.node_scripts)
            lines.append(f"Node scripts: {script_names}")
        return "\n".join(lines)


def _normalize_package_name(value: str) -> str:
    import re

    name = re.split(r"\s*(?:[<>=!~]=?|;|\[)", str(value or "").strip(), maxsplit=1)[0]
    return re.sub(r"[-_.]+", "-", name).lower()


def _dependency_names_from_list(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {_normalize_package_name(str(item)) for item in value if str(item).strip()}


def _python_deps_from_pyproject(path: Path) -> set[str]:
    if tomllib is None:
        return set()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    deps: set[str] = set()
    project = data.get("project")
    if isinstance(project, dict):
        deps.update(_dependency_names_from_list(project.get("dependencies")))
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for values in optional.values():
                deps.update(_dependency_names_from_list(values))
    tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
    poetry = tool.get("poetry") if isinstance(tool, dict) else None
    if isinstance(poetry, dict):
        for section in ("dependencies", "dev-dependencies"):
            values = poetry.get(section)
            if isinstance(values, dict):
                deps.update(str(name) for name in values if str(name).lower() != "python")
    return {_normalize_package_name(dep) for dep in deps}


def _python_deps_from_setup_cfg(path: Path) -> set[str]:
    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error:
        return set()
    if parser.has_option("options", "install_requires"):
        return _dependency_names_from_list(parser.get("options", "install_requires").splitlines())
    return set()


def _python_deps_from_requirements(path: Path) -> set[str]:
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


def _node_deps_from_package_json(path: Path) -> set[str]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    deps: set[str] = set()
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        obj = data.get(section)
        if isinstance(obj, dict):
            deps.update(str(k) for k in obj)
    return deps


def _node_scripts_from_package_json(path: Path) -> tuple[tuple[str, str], ...]:
    import json

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return ()
    return tuple((str(k), str(v)) for k, v in scripts.items())


def _python_validation_commands(
    root: Path,
    manifests: set[str],
    node_scripts: tuple[tuple[str, str], ...],
) -> list[str]:
    cmds: list[str] = []

    cmds.append("python -m py_compile (touched files)")

    pytest_configs = (
        "pytest.ini",
        "pyproject.toml",
        "tox.ini",
        "setup.cfg",
    )
    has_pytest_config = any((root / f).exists() for f in pytest_configs)
    if has_pytest_config and "pyproject.toml" not in manifests:
        has_pytest_config = False  # can't know without reading
    if "pyproject.toml" in manifests:
        try:
            if tomllib is not None:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
                if isinstance(tool, dict) and "pytest" in tool:
                    has_pytest_config = True
        except (OSError, ValueError):
            pass
    if has_pytest_config:
        cmds.append("pytest")

    if "pyproject.toml" in manifests:
        try:
            if tomllib is not None:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
                if isinstance(tool, dict):
                    if "ruff" in tool:
                        cmds.append("ruff check")
                    if "mypy" in tool:
                        cmds.append("mypy")
        except (OSError, ValueError):
            pass

    if "setup.cfg" in manifests:
        try:
            parser = configparser.ConfigParser()
            parser.read(root / "setup.cfg", encoding="utf-8")
            sections = {s.lower() for s in parser.sections()}
            if "mypy" in sections and "mypy" not in " ".join(cmds):
                cmds.append("mypy")
            if "tool:ruff" in sections and "ruff check" not in cmds:
                cmds.append("ruff check")
        except configparser.Error:
            pass

    script_map = dict(node_scripts)
    for candidate in ("test", "lint", "build", "typecheck"):
        if candidate in script_map and f"npm run {candidate}" not in cmds:
            cmds.append(f"npm run {candidate}")

    return cmds


# Main detection entry point


def detect_project_profile(workspace_root: str | Path) -> ProjectProfile:
    root = Path(workspace_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"Workspace root not found: {root}")

    entries: set[str] = set(os.listdir(root))

    # --- manifests ---
    found_manifests: set[str] = set()
    for filename in _MANIFEST_FILENAMES:
        if filename in entries and (root / filename).is_file():
            found_manifests.add(filename)

    # --- lockfiles ---
    found_lockfiles: set[str] = set()
    for filename in _LOCKFILE_FILENAMES:
        if filename in entries and (root / filename).is_file():
            found_lockfiles.add(filename)

    # --- project types ---
    project_types: set[str] = set()
    for m in found_manifests:
        t = _MANIFEST_TO_TYPE.get(m)
        if t:
            project_types.add(t)

    # --- package manager ---
    package_manager: str | None = None

    if "uv.lock" in found_lockfiles:
        package_manager = "uv"
    elif "poetry.lock" in found_lockfiles:
        package_manager = "poetry"
    elif "pdm.lock" in found_lockfiles:
        package_manager = "pdm"
    elif "pyproject.toml" in found_manifests:
        if tomllib is not None:
            try:
                data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
                tool = data.get("tool", {}) if isinstance(data.get("tool"), dict) else {}
                if isinstance(tool, dict):
                    if "poetry" in tool:
                        package_manager = "poetry"
                    elif "pdm" in tool:
                        package_manager = "pdm"
            except (OSError, ValueError):
                pass
        if package_manager is None:
            package_manager = "pip"
    elif found_manifests & _PYTHON_MANIFESTS:
        package_manager = "pip"

    if package_manager is None and found_manifests & _NODE_MANIFESTS:
        if "pnpm-lock.yaml" in found_lockfiles:
            package_manager = "pnpm"
        elif "yarn.lock" in found_lockfiles:
            package_manager = "yarn"
        elif "package-lock.json" in found_lockfiles:
            package_manager = "npm"
        else:
            package_manager = "npm"

    if package_manager is None and found_manifests & _RUST_MANIFESTS:
        package_manager = "cargo"

    if package_manager is None and found_manifests & _GO_MANIFESTS:
        package_manager = "go"

    # --- .venv ---
    has_venv = ".venv" in entries and (root / ".venv").is_dir()
    python_venv_path: str | None = ".venv" if has_venv else None
    python_executable: str | None = None
    if has_venv:
        if os.name == "nt":
            exe_candidate = root / ".venv" / "Scripts" / "python.exe"
            if exe_candidate.is_file():
                python_executable = ".venv/Scripts/python.exe"
        else:
            exe_candidate = root / ".venv" / "bin" / "python"
            if exe_candidate.is_file():
                python_executable = ".venv/bin/python"

    # --- declared dependencies ---
    declared_deps: set[str] = set()

    if "pyproject.toml" in found_manifests:
        declared_deps.update(_python_deps_from_pyproject(root / "pyproject.toml"))
    if "requirements.txt" in found_manifests:
        declared_deps.update(_python_deps_from_requirements(root / "requirements.txt"))
    if "requirements-dev.txt" in found_manifests:
        declared_deps.update(_python_deps_from_requirements(root / "requirements-dev.txt"))
    if "setup.cfg" in found_manifests:
        declared_deps.update(_python_deps_from_setup_cfg(root / "setup.cfg"))

    if "package.json" in found_manifests:
        declared_deps.update(_node_deps_from_package_json(root / "package.json"))

    # --- node scripts ---
    node_scripts: tuple[tuple[str, str], ...] = ()
    if "package.json" in found_manifests:
        node_scripts = _node_scripts_from_package_json(root / "package.json")

    # --- setup command ---
    setup_command: str | None = None

    if "python" in project_types:
        if package_manager == "uv":
            setup_command = "uv sync"
        elif package_manager == "poetry":
            setup_command = "poetry install"
        elif package_manager == "pdm":
            setup_command = "pdm install"
        elif has_venv:
            if python_executable:
                if "requirements.txt" in found_manifests or "requirements-dev.txt" in found_manifests:
                    setup_command = f"{python_executable} -m pip install -r requirements.txt"
                else:
                    setup_command = f"{python_executable} -m pip install -e ."
            else:
                setup_command = "pip install -r requirements.txt" if "requirements.txt" in found_manifests else None
        else:
            if "pyproject.toml" in found_manifests or "setup.py" in found_manifests or "setup.cfg" in found_manifests:
                setup_command = "python -m venv .venv"
            else:
                setup_command = "python -m venv .venv" if "requirements.txt" in found_manifests else None

    if setup_command is None and "node" in project_types:
        if package_manager == "pnpm":
            setup_command = "pnpm install"
        elif package_manager == "yarn":
            setup_command = "yarn install"
        else:
            setup_command = "npm install"

    if setup_command is None and "rust" in project_types:
        setup_command = "cargo fetch"

    if setup_command is None and "go" in project_types:
        setup_command = "go mod download"

    # --- validation commands ---
    validation_cmds = _python_validation_commands(root, found_manifests, node_scripts)

    if "rust" in project_types and "cargo test" not in validation_cmds:
        validation_cmds.append("cargo test")
    if "go" in project_types and "go test ./..." not in validation_cmds:
        validation_cmds.append("go test ./...")

    return ProjectProfile(
        workspace_root=str(root),
        project_types=tuple(sorted(project_types)),
        manifests=tuple(sorted(found_manifests)),
        lockfiles=tuple(sorted(found_lockfiles)),
        package_manager=package_manager,
        has_venv=has_venv,
        python_venv_path=python_venv_path,
        python_executable=python_executable,
        declared_dependencies=tuple(sorted(declared_deps)),
        setup_command=setup_command,
        validation_commands=tuple(validation_cmds),
        node_scripts=node_scripts,
    )
