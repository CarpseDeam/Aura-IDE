"""Build script for Aura EXE using Nuitka with version management."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

APP_NAME = "Aura"
PACKAGE_NAME = "aura"

ICON_PATH = "media/AurA.ico"
MEDIA_DIR = "media"
OUTPUT_DIR = "build"

FINAL_DIST_NAME = f"{APP_NAME}.dist"
FINAL_EXE_NAME = f"{APP_NAME}.exe"
ZIP_NAME = "Aura-Windows-x64.zip"

REQUIRED_MEDIA_FILES = [
    "account_tree_.svg",
    "arrow_forward_24dp.svg",
    "AurA.ico",
    "Aura-Working.mp4",
    "commit.svg",
    "diff-view.png",
    "dispatch.png",
    "file-change-dialog.png",
    "file_24.svg",
    "folder_24.svg",
    "fork_right.svg",
    "mermaid.min.js",
    "new_conv.svg",
    "open_conversation.svg",
    "plan_and_code.gif",
    "read_only.svg",
    "settings_24dp.svg",
    "token-cost.png",
    "workflow-complete.png",
    "working.png",
]

# Signing Configuration
SIGN_CERT = os.environ.get("AURA_SIGN_CERT")
SIGN_PASS = os.environ.get("AURA_SIGN_PASS")


# ---------------------------------------------------------------------------
# Version Management
# ---------------------------------------------------------------------------

def increment_patch(version: str) -> str:
    """Increment the patch version (e.g., 1.3.3 -> 1.3.4)."""
    parts = version.split(".")
    if len(parts) != 3:
        return version
    major, minor, patch = parts
    return f"{major}.{minor}.{int(patch) + 1}"


def get_version_from_user(root: Path) -> str:
    """Read current version and prompt user for the new version."""
    version_file = root / "aura" / "version.py"
    content = version_file.read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', content)
    
    current_version = match.group(1) if match else "0.0.0"
    suggested = increment_patch(current_version)
    
    print(f"\nCurrent version: {current_version}")
    user_input = input(f"Enter new version [{suggested}]: ").strip()
    
    new_version = user_input if user_input else suggested
    return new_version.lstrip("v")


def update_files(root: Path, new_version: str) -> None:
    """Update version strings in all required files."""
    # 1. aura/version.py
    version_file = root / "aura" / "version.py"
    v_content = version_file.read_text(encoding="utf-8")
    version_file.write_text(
        re.sub(r'__version__ = "[^"]+"', f'__version__ = "{new_version}"', v_content),
        encoding="utf-8"
    )

    # 2. pyproject.toml
    toml_file = root / "pyproject.toml"
    t_content = toml_file.read_text(encoding="utf-8")
    toml_file.write_text(
        re.sub(r'^version = "[^"]+"', f'version = "{new_version}"', t_content, flags=re.MULTILINE),
        encoding="utf-8"
    )

    # 3. README.md
    readme_file = root / "README.md"
    r_content = readme_file.read_text(encoding="utf-8")
    readme_file.write_text(
        re.sub(r'badge/version-([\d.]+)-orange', f'badge/version-{new_version}-orange', r_content),
        encoding="utf-8"
    )
    
    print(f"Version updated to {new_version} in version.py, pyproject.toml, and README.md")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str]) -> None:
    """Run a subprocess command and fail loudly if it errors."""
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def ensure_build_dependencies() -> None:
    """Install required build dependencies if they are missing."""
    missing: list[str] = []
    try:
        import nuitka  # noqa: F401
    except ImportError:
        missing.append("nuitka")
    try:
        import zstandard  # noqa: F401
    except ImportError:
        missing.append("zstandard")

    if missing:
        print(f"Installing missing dependencies: {', '.join(missing)}")
        run([sys.executable, "-m", "pip", "install", *missing])


def validate_project_paths(root: Path) -> None:
    """Validate required project paths before starting."""
    for path in [root / PACKAGE_NAME, root / PACKAGE_NAME / "__main__.py", root / ICON_PATH, root / MEDIA_DIR]:
        if not path.exists():
            print(f"Missing required path: {path}")
            sys.exit(1)


def clean_previous_dist_dirs(root: Path) -> None:
    """Remove stale Nuitka dist folders."""
    build_dir = root / OUTPUT_DIR
    build_dir.mkdir(parents=True, exist_ok=True)
    for dist_dir in build_dir.glob("*.dist"):
        shutil.rmtree(dist_dir, ignore_errors=True)


def find_created_dist_dir(root: Path) -> Path:
    """Find the dist folder Nuitka created."""
    build_dir = root / OUTPUT_DIR
    candidates = sorted(build_dir.glob("*.dist"), key=lambda p: p.stat().st_mtime, reverse=True)
    for dist_dir in candidates:
        if (dist_dir / FINAL_EXE_NAME).exists():
            return dist_dir
    print("Could not find dist folder with executable.")
    sys.exit(1)


def normalize_dist_dir(root: Path, created_dist_dir: Path) -> Path:
    """Ensure final dist folder is build/Aura.dist."""
    final_dist_dir = root / OUTPUT_DIR / FINAL_DIST_NAME
    if created_dist_dir.resolve() != final_dist_dir.resolve():
        if final_dist_dir.exists():
            shutil.rmtree(final_dist_dir, ignore_errors=True)
        created_dist_dir.rename(final_dist_dir)
    return final_dist_dir


def zip_distribution(root: Path, final_dist_dir: Path) -> Path:
    """Package Aura.dist into a ZIP."""
    zip_base = root / OUTPUT_DIR / "Aura-Windows-x64"
    print(f"Creating release archive: {zip_base}.zip")
    shutil.make_archive(str(zip_base), "zip", root_dir=str(final_dist_dir), base_dir=".")
    return Path(f"{zip_base}.zip")


def copy_to_desktop(zip_path: Path) -> None:
    """Copy the final ZIP to the user's desktop, handling OneDrive redirects."""
    home = Path.home()
    
    # Try common desktop locations, prioritizing OneDrive
    candidates = [
        home / "OneDrive" / "Desktop",
        home / "Desktop",
    ]
    
    target_desktop = None
    for cand in candidates:
        if cand.exists():
            target_desktop = cand
            break
            
    if target_desktop:
        target = target_desktop / ZIP_NAME
        shutil.copy2(zip_path, target)
        print(f"Success! Release ZIP copied to: {target}")
    else:
        print("Could not find Desktop folder to copy the release ZIP.")


# ---------------------------------------------------------------------------
# Main Build Flow
# ---------------------------------------------------------------------------

def build() -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Versioning
    new_version = get_version_from_user(root)
    update_files(root, new_version)

    # 2. Validation & Cleanup
    validate_project_paths(root)
    clean_previous_dist_dirs(root)

    # 3. Nuitka Command
    cmd = [
        sys.executable, "-m", "nuitka", "--standalone", "--enable-plugin=pyside6",
        "--windows-console-mode=disable", f"--windows-icon-from-ico={ICON_PATH}",
        f"--include-data-dir={MEDIA_DIR}={MEDIA_DIR}", "--include-package=aura",
        f"--output-dir={OUTPUT_DIR}", f"--output-filename={APP_NAME}",
        "--clean-cache=all", "--assume-yes-for-downloads", "--python-flag=-m", PACKAGE_NAME,
    ]
    if SIGN_CERT:
        cmd.extend([f"--windows-sign-certificate={SIGN_CERT}"])
        if SIGN_PASS: cmd.extend([f"--windows-sign-certificate-password={SIGN_PASS}"])

    # 4. Run Build
    print(f"\nStarting Nuitka build for version {new_version}...")
    try:
        run(cmd)
    except subprocess.CalledProcessError:
        print("\nBuild failed.")
        sys.exit(1)

    # 5. Package & Deploy
    dist_dir = find_created_dist_dir(root)
    final_dist_dir = normalize_dist_dir(root, dist_dir)
    zip_path = zip_distribution(root, final_dist_dir)
    copy_to_desktop(zip_path)


if __name__ == "__main__":
    ensure_build_dependencies()
    build()
