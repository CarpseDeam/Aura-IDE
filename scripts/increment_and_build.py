"""Increment version and build Aura Windows release."""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    """Resolve the repository root from this script location."""
    return Path(__file__).resolve().parent.parent


def get_desktop_path() -> Path:
    """Return the user's desktop path."""
    return Path.home() / "Desktop"


def increment_version(version: str) -> str:
    """Increment the patch version (e.g., 1.3.3 -> 1.3.4)."""
    parts = version.split(".")
    if len(parts) != 3:
        raise ValueError(f"Unexpected version format: {version}")
    
    major, minor, patch = parts
    return f"{major}.{minor}.{int(patch) + 1}"


def update_version_py(root: Path, new_version: str) -> None:
    """Update __version__ in aura/version.py."""
    version_file = root / "aura" / "version.py"
    content = version_file.read_text(encoding="utf-8")
    new_content = re.sub(
        r'__version__ = "[^"]+"',
        f'__version__ = "{new_version}"',
        content
    )
    version_file.write_text(new_content, encoding="utf-8")
    print(f"Updated {version_file.relative_to(root)}")


def update_pyproject_toml(root: Path, new_version: str) -> None:
    """Update version in pyproject.toml."""
    toml_file = root / "pyproject.toml"
    content = toml_file.read_text(encoding="utf-8")
    new_content = re.sub(
        r'^version = "[^"]+"',
        f'version = "{new_version}"',
        content,
        flags=re.MULTILINE
    )
    toml_file.write_text(new_content, encoding="utf-8")
    print(f"Updated {toml_file.relative_to(root)}")


def update_readme(root: Path, new_version: str) -> None:
    """Update version badge in README.md."""
    readme_file = root / "README.md"
    content = readme_file.read_text(encoding="utf-8")
    # Matches [![Version](https://img.shields.io/badge/version-1.3.3-orange)]()
    new_content = re.sub(
        r'badge/version-([\d.]+)-orange',
        f'badge/version-{new_version}-orange',
        content
    )
    readme_file.write_text(new_content, encoding="utf-8")
    print(f"Updated {readme_file.relative_to(root)}")


def run_build(root: Path) -> None:
    """Run the Nuitka build script."""
    build_script = root / "scripts" / "build_nuitka.py"
    print(f"Running build: {build_script}")
    subprocess.run([sys.executable, str(build_script)], cwd=root, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Increment version and build Aura.")
    parser.add_argument("version", nargs="?", help="Specific version to set (e.g. 1.4.0). If omitted, auto-increments patch.")
    args = parser.parse_args()

    root = repo_root()
    version_file = root / "aura" / "version.py"
    
    # Read current version
    content = version_file.read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', content)
    if not match:
        print("Could not find current version in aura/version.py")
        sys.exit(1)
    
    current_version = match.group(1)
    
    if args.version:
        new_version = args.version.lstrip("v")
    else:
        new_version = increment_version(current_version)
    
    print(f"Setting version: {current_version} -> {new_version}")
    
    # Update files
    update_version_py(root, new_version)
    update_pyproject_toml(root, new_version)
    update_readme(root, new_version)
    
    # Build
    try:
        run_build(root)
    except subprocess.CalledProcessError as exc:
        print(f"Build failed with exit code {exc.returncode}")
        sys.exit(exc.returncode)
    
    # Copy to desktop
    zip_name = "Aura-Windows-x64.zip"
    source_zip = root / "build" / zip_name
    desktop_zip = get_desktop_path() / zip_name
    
    if source_zip.exists():
        print(f"Copying {zip_name} to Desktop...")
        shutil.copy2(source_zip, desktop_zip)
        print(f"Success! Release ZIP is at: {desktop_zip}")
    else:
        print(f"Error: Could not find build artifact at {source_zip}")
        sys.exit(1)


if __name__ == "__main__":
    main()
