"""Updater logic for Aura, supporting both Git source installs and packaged releases."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

import httpx
from packaging.version import parse as parse_version

from aura.config import get_subprocess_kwargs
from aura.version import __version__

logger = logging.getLogger(__name__)

GITHUB_RELEASES_URL = "https://api.github.com/repos/CarpseDeam/Aura-IDE/releases/latest"


def is_packaged() -> bool:
    """Return True if Aura is running as a packaged executable (Nuitka/PyInstaller)."""
    return getattr(sys, "frozen", False) or "__compiled__" in globals()


def get_current_app_dir() -> Path:
    """Return the directory containing the current executable or source tree."""
    if is_packaged():
        # sys.executable is the path to Aura.exe
        return Path(sys.executable).parent
    # For source installs, walk up from this file to the repo root
    root = get_app_repo_root()
    if root:
        return root
    return Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class GitHubAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True)
class GitHubRelease:
    tag: str
    version: str
    assets: list[GitHubAsset]
    html_url: str

    @property
    def packaged_asset(self) -> GitHubAsset | None:
        """Find a ZIP asset likely to be the Windows packaged app."""
        for asset in self.assets:
            name = asset.name.lower()
            if name.endswith(".zip") and ("windows" in name or "win" in name or "aura" in name):
                return asset
        return None


UpdateState = Literal[
    "not_git",
    "no_upstream",
    "up_to_date",
    "behind",
    "ahead",
    "diverged",
    "error",
]


@dataclass(frozen=True)
class GitCommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def output(self) -> str:
        return "\n".join(part for part in (self.stdout, self.stderr) if part).strip()


@dataclass(frozen=True)
class UpdateStatus:
    repo_root: Path | None
    is_git_repo: bool
    branch: str | None = None
    commit: str | None = None
    upstream: str | None = None
    state: UpdateState = "error"
    ahead: int = 0
    behind: int = 0
    has_local_changes: bool = False
    message: str = ""
    git_output: str = ""
    error: str | None = None
    # Packaged app fields
    current_version: str = __version__
    latest_version: str | None = None
    release: GitHubRelease | None = None
    is_packaged: bool = False

    @property
    def can_install(self) -> bool:
        """Return True if a packaged update can be installed."""
        return (
            self.is_packaged
            and self.release is not None
            and self.release.packaged_asset is not None
            and self.state == "behind"
        )

    @property
    def can_pull(self) -> bool:
        return (
            not self.is_packaged
            and self.is_git_repo
            and self.state == "behind"
            and self.upstream is not None
            and not self.has_local_changes
        )


@dataclass(frozen=True)
class PullResult:
    success: bool
    repo_root: Path | None
    old_commit: str | None = None
    new_commit: str | None = None
    message: str = ""
    git_output: str = ""
    error: str | None = None


def get_latest_release(timeout: int = 15) -> GitHubRelease | None:
    """Fetch latest release info from GitHub API."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(GITHUB_RELEASES_URL)
            resp.raise_for_status()
            data = resp.json()

            tag = data["tag_name"]
            # Clean tag (v1.2.3 -> 1.2.3)
            version = tag.lstrip("vV")

            assets = [
                GitHubAsset(
                    name=asset["name"],
                    url=asset["browser_download_url"],
                    size=asset["size"],
                )
                for asset in data.get("assets", [])
            ]

            return GitHubRelease(
                tag=tag,
                version=version,
                assets=assets,
                html_url=data["html_url"],
            )
    except Exception as exc:
        logger.error("Failed to check GitHub releases: %s", exc)
        return None


def get_update_status(
    repo_root: Path | None = None,
    *,
    output_callback: Callable[[str], None] | None = None,
) -> UpdateStatus:
    """Fetch update status for Aura, automatically choosing the correct backend."""
    if is_packaged():
        return _get_packaged_update_status(output_callback)
    return _get_git_update_status(repo_root, output_callback)


def _get_packaged_update_status(
    output_callback: Callable[[str], None] | None = None,
) -> UpdateStatus:
    """Check for updates for a packaged app via GitHub Releases."""
    if output_callback:
        output_callback("Checking for latest GitHub release...")

    release = get_latest_release()
    if not release:
        msg = "Could not check for updates. Please check your internet connection."
        return UpdateStatus(
            repo_root=None,
            is_git_repo=False,
            is_packaged=True,
            state="error",
            message=msg,
            error=msg,
        )

    current_v = parse_version(__version__)
    latest_v = parse_version(release.version)

    if latest_v > current_v:
        state: UpdateState = "behind"
        asset = release.packaged_asset
        if asset:
            message = f"A newer version of Aura is available: {release.tag}"
        else:
            message = f"Version {release.tag} is available, but no Windows ZIP asset was found."
    else:
        state = "up_to_date"
        message = "Aura is up to date."

    return UpdateStatus(
        repo_root=None,
        is_git_repo=False,
        is_packaged=True,
        state=state,
        message=message,
        current_version=__version__,
        latest_version=release.version,
        release=release,
    )


def install_packaged_update(
    release: GitHubRelease,
    output_callback: Callable[[str], None] | None = None,
) -> PullResult:
    """Download and install a packaged update."""
    asset = release.packaged_asset
    if not asset:
        msg = "No compatible packaged ZIP asset found in the latest release."
        return PullResult(False, None, message=msg, error=msg)

    temp_dir = Path(tempfile.mkdtemp(prefix="aura-update-"))
    try:
        zip_path = temp_dir / asset.name
        if output_callback:
            output_callback(f"Downloading {asset.name}...")

        with httpx.Client(follow_redirects=True, timeout=300) as client:
            with client.stream("GET", asset.url) as resp:
                resp.raise_for_status()
                with open(zip_path, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)

        if output_callback:
            output_callback("Extracting update...")

        extract_dir = temp_dir / "extracted"
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_dir)

        # Locate the new app folder/executable
        new_app_dir: Path | None = None
        if (extract_dir / "Aura.dist" / "Aura.exe").exists():
            new_app_dir = extract_dir / "Aura.dist"
        elif (extract_dir / "Aura.exe").exists():
            new_app_dir = extract_dir
        
        if not new_app_dir:
            # Check for subdirectories (nested ZIP)
            for item in extract_dir.iterdir():
                if item.is_dir():
                    if (item / "Aura.dist" / "Aura.exe").exists():
                        new_app_dir = item / "Aura.dist"
                        break
                    if (item / "Aura.exe").exists():
                        new_app_dir = item
                        break

        if not new_app_dir:
            msg = "Could not find Aura.exe in the extracted update."
            return PullResult(False, None, message=msg, error=msg)

        current_app_dir = get_current_app_dir()
        if output_callback:
            output_callback(f"Found new version in {new_app_dir}")
            output_callback("Launching external updater...")

        # Create the updater script
        script_path = _create_windows_updater(new_app_dir, current_app_dir)
        
        # Launch detached
        if sys.platform == "win32":
            subprocess.Popen(
                ["cmd.exe", "/c", str(script_path)],
                creationflags=subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS,
            )
            if output_callback:
                output_callback("Aura will now exit to complete the update.")
            return PullResult(True, None, message="Update script launched. Quitting Aura...")
        else:
            msg = "Packaged updates are currently only supported on Windows."
            return PullResult(False, None, message=msg, error=msg)

    except Exception as exc:
        logger.exception("Packaged update failed")
        return PullResult(False, None, message=f"Update failed: {exc}", error=str(exc))


def _create_windows_updater(new_app_dir: Path, current_app_dir: Path) -> Path:
    """Generate a .cmd script to replace the app files and relaunch Aura."""
    script_content = f"""@echo off
setlocal
echo Waiting for Aura to exit...
:wait
tasklist /FI "IMAGENAME eq Aura.exe" 2>NUL | find /I /N "Aura.exe">NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 1 /nobreak >nul
    goto wait
)

echo Updating Aura files...
robocopy "{new_app_dir}" "{current_app_dir}" /MIR /R:3 /W:5

if %ERRORLEVEL% GEQ 8 (
    echo Update failed with error %ERRORLEVEL%
    pause
    exit /b %ERRORLEVEL%
)

echo Update successful!
echo Cleaning up...
rmdir /s /q "{new_app_dir.parent.parent}"

echo Relaunching Aura...
start "" "{current_app_dir}\\Aura.exe"
exit
"""
    temp_script = Path(tempfile.gettempdir()) / f"aura_finish_update_{os.getpid()}.cmd"
    temp_script.write_text(script_content, encoding="cp1252")
    return temp_script


def get_app_repo_root() -> Path | None:
    """Find Aura's own git checkout by walking upward from the package path."""
    package_dir = Path(__file__).resolve().parent
    for candidate in (package_dir, *package_dir.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def run_git_command(
    repo_root: Path,
    args: list[str],
    *,
    timeout: int = 120,
    output_callback: Callable[[str], None] | None = None,
) -> GitCommandResult:
    """Run a git command in repo_root and return captured output."""
    cmd = ["git", *args]
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **get_subprocess_kwargs(),
        )
        stdout, stderr = proc.communicate(timeout=timeout)
    except FileNotFoundError as exc:
        msg = "git executable was not found."
        logger.exception("Git command failed: %s", msg)
        if output_callback:
            output_callback(msg)
        return GitCommandResult(cmd, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        msg = f"{' '.join(cmd)} timed out after {timeout} seconds."
        logger.error(msg)
        if output_callback:
            output_callback(msg)
        return GitCommandResult(cmd, 124, stdout or "", stderr or msg)
    except OSError as exc:
        logger.exception("Git command failed: %s", " ".join(cmd))
        if output_callback:
            output_callback(str(exc))
        return GitCommandResult(cmd, 1, "", str(exc))

    result = GitCommandResult(cmd, proc.returncode, stdout or "", stderr or "")
    if output_callback and result.output:
        output_callback(result.output)
    if result.returncode != 0:
        logger.error("Git command failed (%s): %s", result.returncode, " ".join(cmd))
    return result


def _short_head(repo_root: Path) -> str | None:
    result = run_git_command(repo_root, ["rev-parse", "--short", "HEAD"], timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _full_head(repo_root: Path) -> str | None:
    result = run_git_command(repo_root, ["rev-parse", "HEAD"], timeout=10)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _get_git_update_status(
    repo_root: Path | None = None,
    *,
    output_callback: Callable[[str], None] | None = None,
) -> UpdateStatus:
    """Fetch upstream refs and classify Aura's source checkout update state."""
    root = repo_root or get_app_repo_root()
    if root is None:
        msg = (
            "Git update is only available for source installs. "
            "Please update from GitHub manually."
        )
        return UpdateStatus(None, False, state="not_git", message=msg)

    repo_check = run_git_command(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        msg = (
            "Git update is only available for source installs. "
            "Please update from GitHub manually."
        )
        return UpdateStatus(root, False, state="not_git", message=msg, error=repo_check.output)

    branch_result = run_git_command(root, ["branch", "--show-current"], timeout=10)
    branch = branch_result.stdout.strip() or "(detached HEAD)"
    commit = _short_head(root)

    upstream_result = run_git_command(
        root,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        timeout=10,
    )
    if upstream_result.returncode != 0:
        msg = "No upstream branch is configured for the current branch."
        return UpdateStatus(
            root,
            True,
            branch=branch,
            commit=commit,
            state="no_upstream",
            message=msg,
            error=upstream_result.output,
        )
    upstream = upstream_result.stdout.strip()

    status_result = run_git_command(root, ["status", "--porcelain"], timeout=10)
    if status_result.returncode != 0:
        msg = "Could not check for uncommitted local changes."
        return UpdateStatus(
            root,
            True,
            branch=branch,
            commit=commit,
            upstream=upstream,
            state="error",
            message=msg,
            error=status_result.output,
        )
    has_local_changes = bool(status_result.stdout.strip())

    fetch_result = run_git_command(root, ["fetch"], output_callback=output_callback)
    git_output = fetch_result.output
    if fetch_result.returncode != 0:
        msg = "Could not fetch from the configured upstream remote."
        return UpdateStatus(
            root,
            True,
            branch=branch,
            commit=commit,
            upstream=upstream,
            state="error",
            has_local_changes=has_local_changes,
            message=msg,
            git_output=git_output,
            error=fetch_result.output,
        )

    compare_result = run_git_command(
        root,
        ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
        timeout=10,
    )
    if compare_result.returncode != 0:
        msg = "Could not compare local HEAD with upstream."
        return UpdateStatus(
            root,
            True,
            branch=branch,
            commit=commit,
            upstream=upstream,
            state="error",
            has_local_changes=has_local_changes,
            message=msg,
            git_output=git_output,
            error=compare_result.output,
        )

    try:
        ahead_s, behind_s = compare_result.stdout.strip().split()
        ahead = int(ahead_s)
        behind = int(behind_s)
    except ValueError:
        msg = f"Unexpected git comparison output: {compare_result.stdout.strip()}"
        return UpdateStatus(
            root,
            True,
            branch=branch,
            commit=commit,
            upstream=upstream,
            state="error",
            has_local_changes=has_local_changes,
            message=msg,
            git_output=git_output,
            error=msg,
        )

    if ahead and behind:
        state: UpdateState = "diverged"
        message = "Local branch has diverged from upstream. Resolve it manually."
    elif behind:
        state = "behind"
        message = f"Aura is behind upstream by {behind} commit(s)."
        if has_local_changes:
            message += " Commit, stash, or discard local changes before pulling."
    elif ahead:
        state = "ahead"
        message = f"Aura is ahead of upstream by {ahead} commit(s)."
    else:
        state = "up_to_date"
        message = "Aura is up to date."

    return UpdateStatus(
        root,
        True,
        branch=branch,
        commit=commit,
        upstream=upstream,
        state=state,
        ahead=ahead,
        behind=behind,
        has_local_changes=has_local_changes,
        message=message,
        git_output=git_output,
    )


def pull_latest(
    repo_root: Path | None = None,
    *,
    output_callback: Callable[[str], None] | None = None,
) -> PullResult:
    """Fast-forward Aura's source checkout when it is safe to do so."""
    root = repo_root or get_app_repo_root()
    if root is None:
        msg = (
            "Git update is only available for source installs. "
            "Please update from GitHub manually."
        )
        return PullResult(False, None, message=msg, error=msg)

    status = _get_git_update_status(root, output_callback=output_callback)
    if not status.is_git_repo:
        return PullResult(False, root, message=status.message, error=status.error)
    if status.has_local_changes:
        msg = "Local changes exist. Commit, stash, or discard them before pulling."
        return PullResult(False, root, message=msg, git_output=status.git_output, error=msg)
    if status.state == "diverged":
        msg = "Local branch has diverged from upstream. Resolve it manually before updating."
        return PullResult(False, root, message=msg, git_output=status.git_output, error=msg)
    if status.state == "no_upstream":
        msg = "No upstream branch is configured for the current branch."
        return PullResult(False, root, message=msg, git_output=status.git_output, error=msg)
    if status.state != "behind":
        return PullResult(False, root, message=status.message, git_output=status.git_output)

    old_commit = _full_head(root)
    pull_result = run_git_command(
        root,
        ["pull", "--ff-only"],
        timeout=180,
        output_callback=output_callback,
    )
    new_commit = _full_head(root)
    output = "\n".join(
        part for part in (status.git_output, pull_result.output) if part
    ).strip()

    if pull_result.returncode != 0:
        msg = "git pull --ff-only failed."
        return PullResult(
            False,
            root,
            old_commit=old_commit,
            new_commit=new_commit,
            message=msg,
            git_output=output,
            error=pull_result.output,
        )

    msg = "Update succeeded. Restart Aura to use the updated code."
    return PullResult(
        True,
        root,
        old_commit=old_commit,
        new_commit=new_commit,
        message=msg,
        git_output=output,
    )
