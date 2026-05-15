"""Build script for Aura EXE using Nuitka."""

from __future__ import annotations

import argparse
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

VERSION_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")


def normalize_version(version: str) -> str:
    """Normalize a version string and require X.Y.Z format."""
    clean_version = version.strip().lstrip("vV")
    if not VERSION_PATTERN.fullmatch(clean_version):
        raise SystemExit(
            "Invalid version. Expected X.Y.Z or vX.Y.Z, for example 1.3.4."
        )
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
        re.sub(r'badge/version-([\d.]+)-orange', f'badge/version-{new_version}-orange', r_content),
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
    required_paths = [
        root / PACKAGE_NAME,
        root / PACKAGE_NAME / "__main__.py",
        root / ICON_PATH,
        root / MEDIA_DIR,
    ]
    missing = [path for path in required_paths if not path.exists()]
    media_dir = root / MEDIA_DIR
    missing.extend(
        media_dir / filename
        for filename in REQUIRED_MEDIA_FILES
        if not (media_dir / filename).is_file()
    )
    if missing:
        details = "\n".join(f"  - {path}" for path in missing)
        raise SystemExit(f"Missing required build files:\n{details}")


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

def resolve_build_version(
    root: Path,
    requested_version: str | None,
    *,
    skip_version_update: bool = False,
    interactive_version: bool = False,
) -> str:
    """Resolve the build version, updating files only when explicitly requested."""
    if requested_version is not None and skip_version_update:
        raise SystemExit("--version and --skip-version-update cannot be used together.")

    if requested_version is not None:
        version = normalize_version(requested_version)
        update_files(root, version)
        return version

    if interactive_version:
        current = read_current_version(root)
        raw = input(
            f"Enter release version X.Y.Z or leave blank to keep {current}: "
        ).strip()
        if raw:
            version = normalize_version(raw)
            update_files(root, version)
            return version
        print(f"Using current version: {current}")
        return current

    if skip_version_update or requested_version is None:
        version = read_current_version(root)
        print(f"Using current version: {version}")
        return version


def build(
    version: str | None = None,
    *,
    skip_version_update: bool = False,
    copy_desktop: bool = True,
    interactive_version: bool = False,
) -> None:
    root = Path(__file__).resolve().parent.parent
    os.chdir(root)

    # 1. Versioning
    new_version = resolve_build_version(
        root,
        version,
        skip_version_update=skip_version_update,
        interactive_version=interactive_version,
    )

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
        if SIGN_PASS:
            cmd.extend([f"--windows-sign-certificate-password={SIGN_PASS}"])

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
    if copy_desktop:
        copy_to_desktop(zip_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Build Aura with Nuitka.")
    parser.add_argument(
        "--version",
        help=(
            "Set project version before building. When omitted, the current "
            "version in aura/version.py is used without prompting."
        ),
    )
    parser.add_argument(
        "--skip-version-update",
        action="store_true",
        help="Use the current project version without prompting or editing files.",
    )
    parser.add_argument(
        "--interactive-version",
        action="store_true",
        help="Prompt for a version before building.",
    )
    parser.add_argument(
        "--no-copy-desktop",
        action="store_true",
        help="Do not copy the release ZIP to the Desktop after packaging.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    ensure_build_dependencies()
    build(
        args.version,
        skip_version_update=args.skip_version_update,
        copy_desktop=not args.no_copy_desktop,
        interactive_version=args.interactive_version,
    )
