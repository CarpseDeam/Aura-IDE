"""Build environment ownership: version resolution, path validation, venv fingerprinting."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import venv
from dataclasses import dataclass
from pathlib import Path

from scripts.aura_build.config import (
    BUILD_REQUIREMENTS,
    BUILD_VENV_MARKER_NAME,
    BUILD_VENV_MARKER_SCHEMA,
    BUILD_VENV_REL,
    BUNDLED_SKILLS_SOURCE_REL,
    ICON_PATH,
    MEDIA_DIR,
    OUTPUT_DIR,
    PACKAGE_NAME,
    PRODUCTION_PROMPT_SOURCE_REL,
    REQUIRED_MEDIA_FILES,
    UPDATER_HELPER_SOURCE,
)

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


# Process helpers


def run(cmd: list[str], *, env: dict[str, str] | None = None) -> None:
    """Run a subprocess command and fail loudly if it errors."""
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True, env=env)


def clean_pip_env() -> dict[str, str]:
    """Return a copy of the current environment with user pip config stripped."""
    env = os.environ.copy()
    for key in ("PIP_CONFIG_FILE", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_TRUSTED_HOST"):
        env.pop(key, None)
    env["PIP_CONFIG_FILE"] = "NUL" if sys.platform == "win32" else "/dev/null"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env


# Version management


def normalize_version(version: str) -> str:
    """Normalize a version string and require X.Y.Z format."""
    clean_version = version.strip().lstrip("vV")
    if not VERSION_PATTERN.fullmatch(clean_version):
        raise SystemExit("Invalid version. Expected X.Y.Z or vX.Y.Z, for example 1.3.4.")
    return clean_version


def read_current_version(root: Path) -> str:
    """Read the current package version from aura/version.py."""
    version_file = root / "aura" / "version.py"
    content = version_file.read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', content)
    if not match:
        raise SystemExit(f"Could not find __version__ in {version_file}")
    return normalize_version(match.group(1))


def write_text_if_changed(path: Path, content: str) -> None:
    """Write text only when the resulting file content changes."""
    if path.read_text(encoding="utf-8") != content:
        path.write_text(content, encoding="utf-8")


def update_files(root: Path, new_version: str) -> None:
    """Update version strings in all required files."""
    # 1. aura/version.py
    version_file = root / "aura" / "version.py"
    v_content = version_file.read_text(encoding="utf-8")
    write_text_if_changed(
        version_file,
        re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new_version}"', v_content),
    )

    # 2. pyproject.toml
    toml_file = root / "pyproject.toml"
    t_content = toml_file.read_text(encoding="utf-8")
    write_text_if_changed(
        toml_file,
        re.sub(r'^version = "[^"]+"', f'version = "{new_version}"', t_content, flags=re.MULTILINE),
    )

    # 3. README.md
    readme_file = root / "README.md"
    r_content = readme_file.read_text(encoding="utf-8")
    write_text_if_changed(
        readme_file,
        re.sub(r"badge/version-([\d.]+)-orange", f"badge/version-{new_version}-orange", r_content),
    )

    print(f"Version updated to {new_version} in version.py, pyproject.toml, and README.md")


def resolve_build_version(
    root: Path,
    requested_version: str | None,
    *,
    skip_version_update: bool = False,
) -> str:
    """Resolve the build version, updating files only when explicitly requested."""
    if requested_version is not None and skip_version_update:
        raise SystemExit("--version and --skip-version-update cannot be used together.")

    if requested_version is not None:
        version = normalize_version(requested_version)
        update_files(root, version)
        return version

    if skip_version_update:
        version = read_current_version(root)
        print(f"Using current version: {version}")
        return version

    # Default to interactive if not skipping and no version provided
    current = read_current_version(root)
    raw = input(f"Enter release version X.Y.Z or leave blank to keep {current}: ").strip()
    if raw:
        version = normalize_version(raw)
        update_files(root, version)
        return version

    print(f"Using current version: {current}")
    return current


# Project validation


def validate_project_paths(root: Path) -> None:
    """Validate required project paths before starting."""
    required_paths = [
        root / PACKAGE_NAME,
        root / PACKAGE_NAME / "__main__.py",
        root / UPDATER_HELPER_SOURCE,
        root / PRODUCTION_PROMPT_SOURCE_REL,
        root / ICON_PATH,
        root / MEDIA_DIR,
    ]
    missing = [path for path in required_paths if not path.exists()]
    media_dir = root / MEDIA_DIR
    missing.extend(media_dir / filename for filename in REQUIRED_MEDIA_FILES if not (media_dir / filename).is_file())
    skills_dir = root / BUNDLED_SKILLS_SOURCE_REL
    if not skills_dir.is_dir():
        missing.append(skills_dir)
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(f"Missing required build files:\n{details}")


def clean_previous_dist_dirs(root: Path) -> None:
    """Remove stale Nuitka dist folders."""
    build_dir = root / OUTPUT_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    for dist_dir in build_dir.glob("*.dist"):
        shutil.rmtree(dist_dir, ignore_errors=True)


# Build venv + compatibility marker


def build_venv_dir(root: Path) -> Path:
    """Return the build venv directory for this repository root."""
    return root / BUILD_VENV_REL


def build_venv_python(root: Path) -> Path:
    """Return the interpreter path inside the build venv."""
    return build_venv_dir(root) / "Scripts" / "python.exe"


def compute_venv_marker(root: Path) -> dict[str, object]:
    """Compute the deterministic compatibility marker for a build venv.

    The marker captures everything that makes an existing venv unusable: the
    interpreter that created it, the project's dependency declaration, the
    build dependency specification, and the marker schema itself. Source
    changes are deliberately excluded - the package is installed editable, so
    edits under ``aura/`` never require a reinstall.
    """
    pyproject = root / "pyproject.toml"
    payload = pyproject.read_bytes() if pyproject.is_file() else b""
    return {
        "schema": BUILD_VENV_MARKER_SCHEMA,
        "python_executable": str(Path(sys.executable).resolve()),
        "python_version": platform.python_version(),
        "python_architecture": f"{platform.machine()}-{struct.calcsize('P') * 8}bit",
        "pyproject_sha256": hashlib.sha256(payload).hexdigest(),
        "build_requirements": list(BUILD_REQUIREMENTS),
    }


def read_venv_marker(venv_dir: Path) -> dict[str, object] | None:
    """Read a stored marker, returning None when absent or malformed."""
    marker_path = venv_dir / BUILD_VENV_MARKER_NAME
    try:
        data = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_venv_marker(venv_dir: Path, marker: dict[str, object]) -> None:
    """Persist the compatibility marker inside the build venv."""
    marker_path = venv_dir / BUILD_VENV_MARKER_NAME
    marker_path.write_text(json.dumps(marker, indent=2, sort_keys=True), encoding="utf-8")


def _marker_mismatch_reason(stored: dict[str, object], expected: dict[str, object]) -> str:
    """Name the marker keys that differ, for a truthful refresh message."""
    changed = sorted(key for key in expected if stored.get(key) != expected[key])
    changed.extend(sorted(key for key in stored if key not in expected))
    return ", ".join(changed) or "unknown difference"


@dataclass(frozen=True)
class BuildVenv:
    """The interpreter to build with, and whether its venv was reused."""

    python_exe: Path
    reused: bool


def prepare_build_venv(root: Path, *, clean: bool = False) -> BuildVenv:
    """Reuse a compatible build venv, or recreate it exactly when it is not."""
    venv_dir = build_venv_dir(root)
    python_exe = build_venv_python(root)
    expected = compute_venv_marker(root)

    if clean:
        print("Clean build requested: recreating the build venv.")
    elif not python_exe.exists():
        print("No usable build venv found: creating one.")
    else:
        stored = read_venv_marker(venv_dir)
        if stored is None:
            print("Build venv compatibility marker is missing or malformed: recreating the build venv.")
        elif stored != expected:
            print(
                "Build venv compatibility marker mismatch "
                f"({_marker_mismatch_reason(stored, expected)}): recreating the build venv."
            )
        else:
            print(f"Reusing compatible build venv: {venv_dir}")
            return BuildVenv(python_exe=python_exe, reused=True)

    _recreate_build_venv(venv_dir)

    if not python_exe.exists():
        raise SystemExit(f"Failed to find python executable in {venv_dir}")

    print("Installing Aura and build dependencies into pristine isolated environment...")
    run([str(python_exe), "-m", "pip", "--isolated", "install", *BUILD_REQUIREMENTS], env=clean_pip_env())

    write_venv_marker(venv_dir, expected)
    return BuildVenv(python_exe=python_exe, reused=False)


def _recreate_build_venv(venv_dir: Path) -> None:
    """Remove any existing build venv and create a pristine one."""
    if venv_dir.exists():
        shutil.rmtree(venv_dir, ignore_errors=True)
    venv_dir.parent.mkdir(parents=True, exist_ok=True)
    print("Creating pristine build environment...")
    venv.create(venv_dir, with_pip=True)
