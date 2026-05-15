"""Local release helper for Aura Windows builds."""

from __future__ import annotations

import argparse
import ast
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


TAG_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
ZIP_NAME = "Aura-Windows-x64.zip"


def run(
    cmd: list[str],
    *,
    cwd: Path,
    capture_output: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command from the repo root."""
    print(f"Running: {' '.join(cmd)}")
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise SystemExit(f"Required command not found: {cmd[0]}") from exc
    except subprocess.CalledProcessError as exc:
        if capture_output:
            if exc.stdout:
                print(exc.stdout, end="")
            if exc.stderr:
                print(exc.stderr, end="", file=sys.stderr)
        raise


def repo_root() -> Path:
    """Resolve the repository root from this script location."""
    return Path(__file__).resolve().parent.parent


def validate_tag(tag: str) -> str:
    """Validate a release tag and return the clean version."""
    match = TAG_PATTERN.fullmatch(tag.strip())
    if not match:
        raise SystemExit(
            "Invalid tag. Expected format vX.Y.Z, for example v1.3.1."
        )
    return ".".join(match.groups())


def read_version(version_file: Path) -> str:
    """Read __version__ from aura/version.py."""
    try:
        tree = ast.parse(version_file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Version file not found: {version_file}") from exc

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "__version__":
                if isinstance(node.value, ast.Constant) and isinstance(
                    node.value.value, str
                ):
                    return node.value.value
                raise SystemExit("__version__ must be a string literal.")

    raise SystemExit(f"Could not find __version__ in {version_file}")


def ensure_clean_git(root: Path, *, allow_dirty: bool) -> None:
    """Refuse to release with uncommitted changes unless explicitly allowed."""
    result = run(
        ["git", "status", "--porcelain"],
        cwd=root,
        capture_output=True,
    )
    status = result.stdout.strip()
    if not status or allow_dirty:
        if status:
            print("Continuing with dirty git status because --allow-dirty was passed.")
        return

    raise SystemExit(
        "Git working tree is not clean. Commit or stash changes, or pass "
        "--allow-dirty.\n\n"
        f"{status}"
    )


def ensure_gh_ready(root: Path) -> None:
    """Verify the GitHub CLI exists and is authenticated."""
    if shutil.which("gh") is None:
        raise SystemExit(
            "GitHub CLI not found. Install gh and authenticate with `gh auth login`."
        )

    run(["gh", "--version"], cwd=root, capture_output=True)

    try:
        run(["gh", "auth", "status"], cwd=root, capture_output=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            "GitHub CLI is not authenticated. Run `gh auth login` and try again."
        ) from exc


def build_app(root: Path) -> None:
    """Build the Windows app using the existing Nuitka build helper."""
    run([sys.executable, str(root / "scripts" / "build_nuitka.py")], cwd=root)


def verify_artifacts(root: Path) -> Path:
    """Verify the local dist folder and release zip layout."""
    exe_path = root / "build" / "Aura.dist" / "Aura.exe"
    zip_path = root / "build" / ZIP_NAME

    if not exe_path.exists():
        raise SystemExit(f"Expected built executable not found: {exe_path}")
    if not zip_path.exists():
        raise SystemExit(f"Expected release zip not found: {zip_path}")

    verify_zip_layout(zip_path)
    return zip_path


def verify_zip_layout(zip_path: Path) -> None:
    """Validate the release zip has the flattened layout expected by updates."""
    with zipfile.ZipFile(zip_path) as archive:
        names = {name.replace("\\", "/") for name in archive.namelist()}

    if "Aura.exe" not in names:
        raise SystemExit("Release zip is invalid: Aura.exe is not at the ZIP root.")
    if "Aura.dist/Aura.exe" in names:
        raise SystemExit(
            "Release zip is invalid: Aura.dist/Aura.exe must not be present."
        )
    if "media/AurA.ico" not in names:
        raise SystemExit(
            "Release zip is invalid: expected media/AurA.ico in the ZIP."
        )


def release_exists(root: Path, tag: str) -> bool:
    """Return whether a GitHub release exists for the tag."""
    result = run(
        ["gh", "release", "view", tag],
        cwd=root,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def create_release(root: Path, tag: str, *, draft: bool, prerelease: bool) -> None:
    """Create a GitHub release for the tag."""
    cmd = [
        "gh",
        "release",
        "create",
        tag,
        "--title",
        tag,
        "--notes",
        f"Windows release {tag}",
    ]
    if draft:
        cmd.append("--draft")
    if prerelease:
        cmd.append("--prerelease")

    run(cmd, cwd=root)


def upload_asset(root: Path, tag: str, zip_path: Path) -> None:
    """Upload the release zip, replacing any existing asset with the same name."""
    asset_path = zip_path.relative_to(root)
    run(["gh", "release", "upload", tag, str(asset_path), "--clobber"], cwd=root)


def release_url(root: Path, tag: str) -> str | None:
    """Fetch the release URL when gh can provide it."""
    result = run(
        ["gh", "release", "view", tag, "--json", "url", "--jq", ".url"],
        cwd=root,
        capture_output=True,
        check=False,
    )
    url = result.stdout.strip()
    return url or None


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Build Aura locally and publish the Windows release zip."
    )
    parser.add_argument("tag", help="Release tag in vX.Y.Z format, for example v1.3.1")
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow releasing with uncommitted git changes.",
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="Create a new release as a draft when it does not already exist.",
    )
    parser.add_argument(
        "--prerelease",
        action="store_true",
        help="Create a new release as a prerelease when it does not already exist.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the local release workflow."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    root = repo_root()
    tag = args.tag.strip()
    version = validate_tag(tag)

    file_version = read_version(root / "aura" / "version.py")
    if file_version != version:
        raise SystemExit(
            f"Version mismatch: aura/version.py has {file_version} but tag is {tag}."
        )

    ensure_clean_git(root, allow_dirty=args.allow_dirty)
    ensure_gh_ready(root)
    build_app(root)
    zip_path = verify_artifacts(root)

    if release_exists(root, tag):
        print(f"Reusing existing GitHub Release: {tag}")
    else:
        create_release(root, tag, draft=args.draft, prerelease=args.prerelease)

    upload_asset(root, tag, zip_path)
    url = release_url(root, tag)

    print("\nRelease complete.")
    print(f"Tag:     {tag}")
    print(f"Version: {version}")
    print(f"Zip:     {zip_path}")
    if url:
        print(f"Release: {url}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
