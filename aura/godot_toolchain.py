"""Godot project detection, executable resolution, and validation commands."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping

GODOT_SETUP_MESSAGE = (
    "Godot executable not found. Set godot_executable or GODOT_BIN to enable Godot validation."
)
GODOT_PATH_NAMES = (
    "godot",
    "godot.exe",
    "godot4",
    "godot4.exe",
    "godot-mono",
    "godot-mono.exe",
)
_WINDOWS_DESKTOP_PATTERNS = (
    "Godot_v*_win64.exe",
    "Godot_v*_mono_win64.exe",
)


@dataclass(frozen=True)
class GodotExecutableResolution:
    path: Path | None
    source: str | None = None
    message: str = ""

    @property
    def available(self) -> bool:
        return self.path is not None


def find_godot_project_root(workspace_root: str | Path) -> Path | None:
    """Return the nearest root containing project.godot, preferring the workspace."""
    start = Path(workspace_root).expanduser().resolve(strict=False)
    if start.is_file():
        start = start.parent
    for candidate in (start, *start.parents):
        if (candidate / "project.godot").is_file():
            return candidate
    return None


def load_godot_executable_setting(project_root: str | Path) -> str | None:
    """Read the machine-local Godot path from Aura project metadata."""
    path = Path(project_root) / ".aura" / "project.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("godot_executable")
    return str(value).strip() if value and str(value).strip() else None


def save_godot_executable_setting(project_root: str | Path, value: str) -> None:
    """Persist a Godot path through Aura's local project metadata model."""
    from aura.projects.store import ProjectStore

    root = Path(project_root).expanduser().resolve(strict=False)
    metadata_path = root / ".aura" / "project.json"
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        data["godot_executable"] = str(value or "").strip()
        metadata_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return

    store = ProjectStore()
    project = store.create_or_update_project(root)
    project.godot_executable = str(value or "").strip()
    store.save_project(project)


def resolve_godot_executable(
    project_root: str | Path,
    *,
    configured_path: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    home: str | Path | None = None,
    platform: str | None = None,
) -> GodotExecutableResolution:
    """Resolve Godot in configured, environment, PATH, then Desktop order."""
    root = Path(project_root).expanduser().resolve(strict=False)
    environment = os.environ if environ is None else environ
    platform_name = os.name if platform is None else platform

    configured = configured_path
    if configured is None:
        configured = load_godot_executable_setting(root)
    for raw, source in (
        (configured, "godot_executable"),
        (environment.get("GODOT_BIN"), "GODOT_BIN"),
    ):
        candidate = _path_candidate(raw)
        if candidate is not None and _is_invokable(candidate, platform_name):
            return GodotExecutableResolution(candidate, source)

    for name in GODOT_PATH_NAMES:
        found = which(name)
        candidate = _path_candidate(found)
        if candidate is not None and _is_invokable(candidate, platform_name):
            return GodotExecutableResolution(candidate, "PATH")

    if platform_name == "nt":
        seen: set[Path] = set()
        for desktop in _windows_desktop_directories(
            home,
            environment,
            include_registered=environ is None,
        ):
            for pattern in _WINDOWS_DESKTOP_PATTERNS:
                for candidate in sorted(desktop.glob(pattern)):
                    resolved = candidate.resolve(strict=False)
                    if resolved in seen:
                        continue
                    seen.add(resolved)
                    if _is_invokable(resolved, platform_name):
                        return GodotExecutableResolution(resolved, "Desktop")

    return GodotExecutableResolution(None, message=GODOT_SETUP_MESSAGE)


def filesystem_path_to_res_path(file_path: str | Path, project_root: str | Path) -> str:
    """Convert a filesystem path inside a Godot project to a res:// URI."""
    root = Path(project_root).expanduser().resolve(strict=False)
    path = Path(file_path).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve(strict=False)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path is outside Godot project root: {file_path}") from exc
    return "res://" + relative.as_posix()


def build_godot_check_command(
    executable: str | Path,
    project_root: str | Path,
    script_path: str | Path,
    *,
    platform: str | None = None,
) -> str | None:
    """Build a focused Godot parse command, or None for a non-GDScript file."""
    if Path(script_path).suffix.lower() != ".gd":
        return None
    root = Path(project_root).expanduser().resolve(strict=False)
    res_path = filesystem_path_to_res_path(script_path, root)
    return _shell_join(
        [str(executable), "--headless", "--path", str(root), "--check-only", "--script", res_path],
        platform,
    )


def build_godot_import_command(
    executable: str | Path,
    project_root: str | Path,
    *,
    platform: str | None = None,
) -> str:
    """Build an explicit Godot project import validation command."""
    root = Path(project_root).expanduser().resolve(strict=False)
    return _shell_join([str(executable), "--headless", "--path", str(root), "--import"], platform)


def build_godot_resource_check_command(
    executable: str | Path,
    project_root: str | Path,
    resource_paths: Iterable[str | Path],
    *,
    platform: str | None = None,
    validator_script: str | Path | None = None,
) -> str | None:
    """Build a focused command that loads touched text scenes/resources.

    Godot's ``--check-only`` applies only to scripts.  Scene validation uses a
    tiny bundled ``SceneTree`` script which asks Godot's own ``ResourceLoader``
    to parse every touched ``.tscn``/``.tres`` and returns a non-zero status if
    any resource cannot be loaded.
    """
    root = Path(project_root).expanduser().resolve(strict=False)
    res_paths: list[str] = []
    for resource_path in resource_paths:
        if Path(resource_path).suffix.lower() not in {".tscn", ".tres"}:
            continue
        res_paths.append(filesystem_path_to_res_path(resource_path, root))
    res_paths = list(dict.fromkeys(res_paths))
    if not res_paths:
        return None
    validator = (
        Path(validator_script).expanduser().resolve(strict=False)
        if validator_script is not None
        else Path(__file__).parent / "validation" / "godot_resource_validator.gd"
    )
    return _shell_join(
        [
            str(executable),
            "--headless",
            "--path",
            str(root),
            "--script",
            str(validator),
            "--",
            *res_paths,
        ],
        platform,
    )


def _path_candidate(value: str | Path | None) -> Path | None:
    if value is None or not str(value).strip():
        return None
    return Path(str(value).strip().strip('"')).expanduser().resolve(strict=False)


def _is_invokable(path: Path, platform: str) -> bool:
    if not path.is_file():
        return False
    if platform == "nt":
        return path.suffix.lower() in {".exe", ".com", ".bat", ".cmd"}
    return os.access(path, os.X_OK)


def _windows_desktop_directories(
    home: str | Path | None,
    environ: Mapping[str, str],
    *,
    include_registered: bool,
) -> Iterable[Path]:
    """Yield Windows Desktop locations, including redirected OneDrive folders."""
    home_path = Path(home).expanduser() if home is not None else Path.home()
    candidates: list[Path] = []

    if include_registered:
        registered_desktop = _windows_registered_desktop(environ)
        if registered_desktop is not None:
            candidates.append(registered_desktop)

    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = environ.get(variable)
        if value:
            candidates.append(Path(value).expanduser() / "Desktop")

    user_profile = environ.get("USERPROFILE")
    if user_profile:
        candidates.append(Path(user_profile).expanduser() / "Desktop")
    candidates.extend((home_path / "Desktop", home_path / "OneDrive" / "Desktop"))

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve(strict=False)
        if resolved not in seen:
            seen.add(resolved)
            yield resolved


def _windows_registered_desktop(environ: Mapping[str, str]) -> Path | None:
    """Read Explorer's per-user Desktop path when running on Windows."""
    if os.name != "nt":
        return None
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _value_type = winreg.QueryValueEx(key, "Desktop")
    except (ImportError, OSError):
        return None

    expanded = os.path.expandvars(str(value))
    for name, replacement in environ.items():
        expanded = expanded.replace(f"%{name}%", replacement)
    return Path(expanded).expanduser()


def _shell_join(args: list[str], platform: str | None) -> str:
    platform_name = os.name if platform is None else platform
    if platform_name == "nt":
        return subprocess.list2cmdline(args)
    return shlex.join(args)


__all__ = [
    "GODOT_PATH_NAMES",
    "GODOT_SETUP_MESSAGE",
    "GodotExecutableResolution",
    "build_godot_check_command",
    "build_godot_import_command",
    "build_godot_resource_check_command",
    "filesystem_path_to_res_path",
    "find_godot_project_root",
    "load_godot_executable_setting",
    "resolve_godot_executable",
    "save_godot_executable_setting",
]
