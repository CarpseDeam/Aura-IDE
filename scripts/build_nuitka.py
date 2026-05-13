"""Build script for Aura EXE using Nuitka."""

from __future__ import annotations

import os
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

    if not missing:
        return

    print(f"Required build dependencies missing: {', '.join(missing)}")
    print("Installing missing dependencies...")
    run([sys.executable, "-m", "pip", "install", *missing])


def validate_project_paths(root: Path) -> None:
    """Validate required project paths before starting the expensive build."""
    required_paths = [
        root / PACKAGE_NAME,
        root / PACKAGE_NAME / "__main__.py",
        root / ICON_PATH,
        root / MEDIA_DIR,
    ]

    missing = [path for path in required_paths if not path.exists()]
    if not missing:
        return

    print("Build cannot continue. Missing required project paths:")
    for path in missing:
        print(f"  - {path}")
    sys.exit(1)


def validate_source_media_files(root: Path) -> None:
    """Validate media assets exist before starting the build."""
    missing: list[Path] = []

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


def clean_previous_dist_dirs(root: Path) -> None:
    """Remove stale Nuitka dist folders so post-build detection is reliable."""
    build_dir = root / OUTPUT_DIR
    build_dir.mkdir(parents=True, exist_ok=True)

    for dist_dir in build_dir.glob("*.dist"):
        print(f"Removing stale dist folder: {dist_dir}")
        shutil.rmtree(dist_dir, ignore_errors=True)


def find_created_dist_dir(root: Path) -> Path:
    """Find the dist folder Nuitka created for this build."""
    build_dir = root / OUTPUT_DIR
    candidates = sorted(
        build_dir.glob("*.dist"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    if not candidates:
        print(f"\nBuild finished, but no .dist folder was found in: {build_dir}")
        sys.exit(1)

    exe_candidates: list[Path] = []
    for dist_dir in candidates:
        exe_path = dist_dir / FINAL_EXE_NAME
        if exe_path.exists():
            exe_candidates.append(dist_dir)

    if exe_candidates:
        return exe_candidates[0]

    print("\nBuild finished, but no dist folder contained the expected executable.")
    print(f"Expected executable name: {FINAL_EXE_NAME}")
    print("Found dist folders:")
    for dist_dir in candidates:
        print(f"  - {dist_dir}")
    sys.exit(1)


def normalize_dist_dir(root: Path, created_dist_dir: Path) -> Path:
    """Ensure the final distribution folder is always build/Aura.dist."""
    final_dist_dir = root / OUTPUT_DIR / FINAL_DIST_NAME

    if created_dist_dir.resolve() == final_dist_dir.resolve():
        return final_dist_dir

    if final_dist_dir.exists():
        print(f"Removing existing final dist folder: {final_dist_dir}")
        shutil.rmtree(final_dist_dir, ignore_errors=True)

    print("Renaming dist folder:")
    print(f"  From: {created_dist_dir}")
    print(f"  To:   {final_dist_dir}")
    created_dist_dir.rename(final_dist_dir)

    return final_dist_dir


def validate_built_distribution(final_dist_dir: Path) -> None:
    """Validate the compiled distribution has the executable and media assets."""
    exe_path = final_dist_dir / FINAL_EXE_NAME
    if not exe_path.exists():
        print(f"\nBuild finished, but expected EXE was not found: {exe_path}")
        sys.exit(1)

    missing_media: list[Path] = []
    for filename in REQUIRED_MEDIA_FILES:
        path = final_dist_dir / MEDIA_DIR / filename
        if not path.exists():
            missing_media.append(path)

    if missing_media:
        print("\nBuild finished, but required media files are missing from the dist:")
        for path in missing_media:
            print(f"  - {path}")
        sys.exit(1)

    print(f"Validated packaged executable: {exe_path}")
    print(f"Validated packaged media files: {len(REQUIRED_MEDIA_FILES)}")


def zip_distribution(root: Path, final_dist_dir: Path) -> None:
    """Package the Aura.dist folder into Aura-Windows-x64.zip."""
    zip_name = "Aura-Windows-x64"
    zip_path = root / OUTPUT_DIR / zip_name

    print(f"Creating release archive: {zip_path}.zip")
    # We want the ZIP to contain the Aura.dist folder.
    # The updater script will locate Aura.dist inside the zip.
    shutil.make_archive(str(zip_path), "zip", root_dir=str(root / OUTPUT_DIR), base_dir=FINAL_DIST_NAME)
    print(f"Release archive created: {zip_path}.zip")


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build() -> None:
    """Build the Aura standalone distribution with Nuitka."""
    print(f"Starting Nuitka build for {APP_NAME}...")

    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    validate_project_paths(root)
    validate_source_media_files(root)
    clean_previous_dist_dirs(root)

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
        "--python-flag=-m",
        PACKAGE_NAME,
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

    created_dist_dir = find_created_dist_dir(root)
    final_dist_dir = normalize_dist_dir(root, created_dist_dir)
    validate_built_distribution(final_dist_dir)

    exe_path = final_dist_dir / FINAL_EXE_NAME

    print("\nBuild successful.")
    print(f"Distribution folder: {final_dist_dir}")
    print(f"Executable:          {exe_path}")
    zip_distribution(root, final_dist_dir)
    print("\nTo distribute, upload build/Aura-Windows-x64.zip to GitHub Releases.")


if __name__ == "__main__":
    ensure_build_dependencies()
    build()