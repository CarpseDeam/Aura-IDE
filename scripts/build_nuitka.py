"""Build script for Aura EXE using Nuitka."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


# Configuration
APP_NAME = "Aura"
ENTRY_POINT = "aura/__main__.py"
ICON_PATH = "media/AurA.ico"
MEDIA_DIR = "media"
OUTPUT_DIR = "build"

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
# Example:
#   $env:AURA_SIGN_CERT="C:\path\to\cert.pfx"
#   $env:AURA_SIGN_PASS="password"
SIGN_CERT = os.environ.get("AURA_SIGN_CERT")
SIGN_PASS = os.environ.get("AURA_SIGN_PASS")


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

    if not missing:
        return

    print(f"Required build dependencies missing: {', '.join(missing)}")
    print("Installing missing dependencies...")
    run([sys.executable, "-m", "pip", "install", *missing])


def validate_project_paths(root: Path) -> None:
    """Validate required project paths before starting the expensive build."""
    required_paths = [
        root / ENTRY_POINT,
        root / ICON_PATH,
        root / MEDIA_DIR,
        root / "aura",
    ]

    missing = [path for path in required_paths if not path.exists()]
    if not missing:
        return

    print("Build cannot continue. Missing required project paths:")
    for path in missing:
        print(f"  - {path}")
    sys.exit(1)


def validate_media_files(root: Path) -> None:
    """Validate media assets that must be bundled into the distribution."""
    missing = []

    for filename in REQUIRED_MEDIA_FILES:
        path = root / MEDIA_DIR / filename
        if not path.exists():
            missing.append(path)

    if missing:
        print("Build cannot continue. Missing required media files:")
        for path in missing:
            print(f"  - {path}")
        sys.exit(1)

    print(f"Validated {len(REQUIRED_MEDIA_FILES)} required media files.")


def build() -> None:
    """Build the Aura standalone distribution with Nuitka."""
    print(f"Starting Nuitka build for {APP_NAME}...")

    # Ensure we are in the project root.
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    validate_project_paths(root)
    validate_media_files(root)

    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        "--enable-plugin=pyside6",
        "--windows-console-mode=disable",
        f"--windows-icon-from-ico={ICON_PATH}",
        f"--include-data-dir={MEDIA_DIR}={MEDIA_DIR}",
        "--include-package=aura",
        f"--output-dir={OUTPUT_DIR}",
        f"--output-filename={APP_NAME}",
        "--clean-cache=all",
        "--assume-yes-for-downloads",
        ENTRY_POINT,
    ]

    if SIGN_CERT:
        print(f"Adding code signing using certificate: {SIGN_CERT}")
        cmd.append(f"--windows-sign-certificate={SIGN_CERT}")

        if SIGN_PASS:
            cmd.append(f"--windows-sign-certificate-password={SIGN_PASS}")
    else:
        print(
            "Warning: No signing certificate found in environment "
            "(AURA_SIGN_CERT). EXE will be unsigned."
        )

    try:
        run(cmd)
    except subprocess.CalledProcessError as exc:
        print(f"\nBuild failed with exit code {exc.returncode}")
        sys.exit(exc.returncode)

    dist_dir = root / OUTPUT_DIR / f"{APP_NAME}.dist"
    exe_path = dist_dir / f"{APP_NAME}.exe"

    if not dist_dir.exists():
        print(f"\nBuild finished, but expected dist folder was not found: {dist_dir}")
        sys.exit(1)

    if not exe_path.exists():
        print(f"\nBuild finished, but expected EXE was not found: {exe_path}")
        sys.exit(1)

    print(f"\nBuild successful! The compiled application is in: {dist_dir}")
    print(f"Executable: {exe_path}")
    print("To distribute, zip the entire .dist directory.")


if __name__ == "__main__":
    ensure_build_dependencies()
    build()