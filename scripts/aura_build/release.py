"""Installer and GitHub release helpers."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from scripts.aura_build.config import INSTALLER_BASE_NAME, INSTALLER_ISS_PATH, OUTPUT_DIR
from scripts.aura_build.environment import run


def find_iscc() -> Path | None:
    """Find Inno Setup's iscc.exe compiler."""
    exe = shutil.which("iscc")
    if exe:
        return Path(exe)
    candidates = [
        "C:\\Program Files (x86)\\Inno Setup 6\\iscc.exe",
        "C:\\Program Files\\Inno Setup 6\\iscc.exe",
        "C:\\Program Files (x86)\\Inno Setup\\iscc.exe",
        "C:\\Program Files\\Inno Setup\\iscc.exe",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    return None


def validate_release_prerequisites(
    installer_flag: bool | None,
    github_release: bool,
) -> None:
    """Fail before compilation when release-only external tools are unavailable."""
    if github_release and installer_flag is False:
        raise SystemExit(
            "--github-release requires an installer. Remove --no-installer and ensure Inno Setup is available."
        )

    if installer_flag is True or github_release:
        iscc = find_iscc()
        if iscc is None:
            raise SystemExit(
                "Cannot create installer: iscc.exe not found. Install Inno Setup 6 or ensure iscc.exe is on PATH."
            )
        print(f"Inno Setup found: {iscc}")

    if github_release:
        if not shutil.which("gh"):
            raise SystemExit(
                "GitHub CLI (gh) not found.\n"
                "Install it from https://cli.github.com/ and ensure it's on your PATH."
            )
        auth_result = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True,
            text=True,
        )
        if auth_result.returncode != 0:
            raise SystemExit(
                "GitHub CLI is not authenticated. Run 'gh auth login' first.\n"
                f"Error: {auth_result.stderr.strip()}"
            )
        print("GitHub CLI authentication verified.")


def create_installer(root: Path, dist_dir: Path, version: str, installer_flag: bool | None) -> Path | None:
    """Build an Inno Setup installer from the dist directory."""
    if installer_flag is False:
        print("Skipping installer creation (--no-installer).")
        return None

    iscc = find_iscc()
    if installer_flag is True and iscc is None:
        raise SystemExit(
            "Cannot create installer: iscc.exe not found. Install Inno Setup 6 or ensure iscc.exe is on PATH."
        )

    if iscc is None:
        print("iscc.exe not found. Skipping installer creation.")
        print("Install Inno Setup 6 to enable installer builds.")
        return None

    iss_path = root / INSTALLER_ISS_PATH
    if not iss_path.exists():
        raise SystemExit(f"Installer script not found: {iss_path}")

    output_dir = root / OUTPUT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(iscc),
        f"/DMyAppVersion={version}",
        f"/DSourceDir={dist_dir}",
        str(iss_path),
    ]
    run(cmd)

    installer_path = output_dir / f"{INSTALLER_BASE_NAME}-{version}.exe"
    if not installer_path.exists():
        raise SystemExit(
            f"Expected installer not found: {installer_path}\n"
            "Check that the ISS script outputs to the expected location."
        )

    print(f"Installer created: {installer_path}")
    return installer_path


def upload_to_github_release(installer_path: Path, version: str, create_release: bool = False) -> None:
    """Upload the installer to the GitHub release for tag v{version} using gh CLI."""
    tag = f"v{version}"

    # Check gh CLI is available
    if not shutil.which("gh"):
        raise SystemExit(
            "GitHub CLI (gh) not found.\n"
            "Install it from https://cli.github.com/ and ensure it's on your PATH."
        )

    # Check gh auth status
    auth_result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True, text=True,
    )
    if auth_result.returncode != 0:
        raise SystemExit(
            "GitHub CLI is not authenticated. Run 'gh auth login' first.\n"
            f"Error: {auth_result.stderr.strip()}"
        )

    view_result = subprocess.run(
        ["gh", "release", "view", tag, "--json", "tagName"],
        capture_output=True, text=True,
    )
    release_exists = view_result.returncode == 0

    if not release_exists:
        if not create_release:
            raise SystemExit(
                f"GitHub release {tag} does not exist. "
                "Use --create-github-release to create it."
            )
        print(f"Creating GitHub release {tag}...")
        run(["gh", "release", "create", tag, f"--title=Aura v{version}", "--generate-notes"])

    # Upload installer asset
    print(f"Uploading {installer_path.name} to GitHub release {tag}...")
    run(["gh", "release", "upload", tag, str(installer_path), "--clobber"])
    print("Upload complete.")
