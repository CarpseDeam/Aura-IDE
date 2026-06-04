"""Updater logic for Aura, supporting both Git source installs and packaged releases."""

from __future__ import annotations

import hashlib
import logging
import os
import re
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
WINDOWS_UPDATER_HELPER_NAME = "AuraUpdater.cmd"
INSTALLER_LAUNCH_METHOD = "ShellExecuteW/open"


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
    def zip_asset(self) -> GitHubAsset | None:
        """Find a ZIP asset likely to be the Windows packaged app."""
        # Prefer exact match
        for asset in self.assets:
            if asset.name.lower() == "aura-windows-x64.zip":
                return asset
        # Fall back to likely candidates
        for asset in self.assets:
            name = asset.name.lower()
            if name.endswith(".zip") and ("windows" in name or "win" in name) and "x64" in name:
                return asset
        for asset in self.assets:
            name = asset.name.lower()
            if name.endswith(".zip") and ("windows" in name or "win" in name or "aura" in name):
                return asset
        return None

    @property
    def installer_asset(self) -> GitHubAsset | None:
        """Find an installer executable asset for this release."""
        # 1. Exact match for AuraSetup-{version}.exe
        exact_name = f"AuraSetup-{self.version}.exe"
        for asset in self.assets:
            if asset.name == exact_name:
                return asset
        # 2. Regex patterns matching common installer naming conventions
        patterns = [
            r"AuraSetup-\d+\.\d+\.\d+\.exe",
            r"Aura-Setup-\d+\.\d+\.\d+\.exe",
            r"AuraInstaller-\d+\.\d+\.\d+\.exe",
        ]
        for asset in self.assets:
            for pat in patterns:
                if re.match(pat + "$", asset.name):
                    return asset
        # 3. Fallback: any .exe starting with "Aura" containing "Setup" or "Installer"
        for asset in self.assets:
            name = asset.name
            if name.endswith(".exe") and name.startswith("Aura"):
                lower = name.lower()
                if "setup" in lower or "installer" in lower:
                    return asset
        return None

    @property
    def installer_checksum_url(self) -> str | None:
        """URL of a companion SHA-256 checksum file for the installer asset, if any."""
        if self.installer_asset is None:
            return None
        base = self.installer_asset.name
        for suffix in (".sha256", ".sha256sum"):
            companion_name = base + suffix
            for asset in self.assets:
                if asset.name == companion_name:
                    return asset.url
        return None

    @property
    def packaged_asset(self) -> GitHubAsset | None:
        """Deprecated: use installer_asset or zip_asset instead."""
        return self.installer_asset or self.zip_asset


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
    has_installer_asset: bool = False

    @property
    def can_install(self) -> bool:
        """Return True if a packaged update can be installed."""
        return (
            self.is_packaged
            and self.release is not None
            and self.release.installer_asset is not None
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
        return _get_packaged_update_status(output_callback=output_callback)
    return _get_git_update_status(repo_root, output_callback=output_callback)


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
        if release.installer_asset is not None:
            message = f"A newer version of Aura is available: {release.tag}"
            has_installer_asset = True
        elif release.zip_asset is not None:
            message = (
                f"Version {release.tag} is available, but no installer was found for "
                f"this release. You can download it manually or use the legacy ZIP update."
            )
            has_installer_asset = False
        else:
            message = f"Version {release.tag} is available, but no Windows asset was found."
            has_installer_asset = False
    else:
        state = "up_to_date"
        message = "Aura is up to date."
        has_installer_asset = False

    return UpdateStatus(
        repo_root=None,
        is_git_repo=False,
        is_packaged=True,
        state=state,
        message=message,
        current_version=__version__,
        latest_version=release.version,
        release=release,
        has_installer_asset=has_installer_asset,
    )


def find_extracted_app_root(staging_dir: Path) -> Path:
    """Return the directory containing Aura.exe from a flattened or legacy release ZIP."""
    flat_exe = staging_dir / "Aura.exe"
    if flat_exe.exists():
        return staging_dir

    legacy_root = staging_dir / "Aura.dist"
    legacy_exe = legacy_root / "Aura.exe"
    if legacy_exe.exists():
        return legacy_root

    entries = sorted(path.name for path in staging_dir.iterdir())
    raise RuntimeError(
        "Downloaded update archive does not contain Aura.exe in a supported layout. "
        "Expected Aura.exe at archive root or Aura.dist/Aura.exe. "
        f"Found top-level entries: {entries}"
    )


def install_packaged_update(
    release: GitHubRelease,
    output_callback: Callable[[str], None] | None = None,
    prefer_installer: bool = True,
) -> PullResult:
    """Download and install a packaged update, preferring installer when available."""

    # --- Installer-based update path ---
    if prefer_installer and release.installer_asset is not None:
        staging_dir = get_installer_staging_dir(release.version)
        try:
            installer_name = f"AuraSetup-{release.version}.exe"
            installer_path = _download_asset(
                release.installer_asset,
                staging_dir,
                output_callback,
                filename=installer_name,
            )
            if output_callback:
                output_callback(f"Downloaded installer: {installer_path}")

            # Optional checksum verification
            checksum_url = release.installer_checksum_url
            if checksum_url:
                if output_callback:
                    output_callback("Verifying installer checksum...")
                checksum_asset = GitHubAsset(
                    name=release.installer_asset.name + ".sha256",
                    url=checksum_url,
                    size=0,
                )
                checksum_path = _download_asset(checksum_asset, staging_dir, output_callback)
                content = checksum_path.read_text(encoding="utf-8").strip()
                expected_sha256 = content.split()[0] if content else ""
                if not _verify_checksum(installer_path, expected_sha256):
                    msg = "Installer checksum verification failed. The download may be corrupted."
                    return PullResult(False, None, message=msg, error=msg)

            launched = _launch_installer(installer_path, output_callback)
            if not launched:
                msg = "Failed to launch the installer.\n" + _format_installer_launch_details(installer_path)
                return PullResult(False, None, message=msg, error=msg)

            if output_callback:
                output_callback("Installer launched. Aura will now exit to complete the update.")
            return PullResult(True, None, message="Installer launched. Quitting Aura...")
        except Exception as exc:
            logger.exception("Installer update failed")
            msg = (
                f"Installer update failed: {exc}\n"
                f"Downloaded installer: {staging_dir / f'AuraSetup-{release.version}.exe'}"
            )
            return PullResult(False, None, message=msg, error=str(exc))

    # --- Legacy ZIP fallback path ---
    asset = release.zip_asset
    if asset is not None:
        if output_callback:
            output_callback("No installer found; falling back to legacy ZIP update mechanism.")

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
            extract_dir.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_path, "r") as zip_ref:
                for member in zip_ref.infolist():
                    member_path = Path(member.filename)
                    if member_path.is_absolute() or ".." in member_path.parts:
                        logger.warning("Skipping unsafe zip member: %s", member.filename)
                        continue

                    target = (extract_dir / member.filename).resolve()
                    try:
                        if not target.is_relative_to(extract_dir.resolve()):
                            logger.warning("Skipping zip member escaping extract_dir: %s", member.filename)
                            continue
                    except ValueError:
                        logger.warning("Skipping zip member escaping extract_dir: %s", member.filename)
                        continue

                    zip_ref.extract(member, extract_dir)

            try:
                new_app_dir = find_extracted_app_root(extract_dir)
            except RuntimeError as exc:
                return PullResult(False, None, message=str(exc), error=str(exc))

            current_app_dir = get_current_app_dir()

            # --- Strict Validation before Robocopy ---
            if not current_app_dir.exists():
                return PullResult(False, None, message="Current app directory does not exist.")
            if not (current_app_dir / "Aura.exe").exists() or not (current_app_dir / "media").exists():
                return PullResult(
                    False, None, message="Current app is missing critical files (Aura.exe or media folder)."
                )

            has_runtime_marker = any(
                (current_app_dir / marker).exists()
                for marker in (
                    "PySide6",
                    "qt.conf",
                    "python3.dll",
                    "python310.dll",
                    "python311.dll",
                    "python312.dll",
                )
            )
            if not has_runtime_marker:
                return PullResult(False, None, message="Current app directory lacks expected packaged runtime markers.")

            current_name = current_app_dir.name.lower()
            if current_name != "aura.dist" and "aura" not in current_name:
                return PullResult(
                    False, None, message="Current app directory does not look like a packaged Aura install."
                )

            home_dir = Path.home().resolve()
            temp_root = Path(tempfile.gettempdir()).resolve()
            curr_resolved = current_app_dir.resolve()
            if curr_resolved == home_dir or curr_resolved == temp_root or curr_resolved.parent == curr_resolved:
                return PullResult(False, None, message="Current app directory is a protected system directory.")

            if not (new_app_dir / "Aura.exe").exists() or not (new_app_dir / "media").exists():
                return PullResult(False, None, message="Extracted update is missing critical files.")

            try:
                if not new_app_dir.resolve().is_relative_to(temp_dir.resolve()):
                    return PullResult(False, None, message="Extracted update is outside the temporary directory.")
            except ValueError:
                return PullResult(False, None, message="Extracted update is outside the temporary directory.")

            if sys.platform == "win32":
                current_exe = Path(sys.executable).resolve()
                updater_exe = get_windows_updater_helper(current_app_dir)
                cmd = _build_windows_updater_command(
                    updater_exe,
                    new_app_dir,
                    current_app_dir,
                    current_exe,
                    os.getpid(),
                )

                if output_callback:
                    output_callback(f"Found new version in {new_app_dir}")
                    output_callback("Launching external updater...")

                try:
                    _launch_windows_updater(
                        updater_exe=updater_exe,
                        argv=cmd,
                        extracted_dir=new_app_dir,
                        install_dir=current_app_dir,
                        output_callback=output_callback,
                    )
                except Exception as exc:
                    attempted = _format_update_launch_details(updater_exe, cmd)
                    msg = f"Failed to launch updater: {exc}\n{attempted}"
                    logger.exception("Packaged updater launch failed\n%s", attempted)
                    return PullResult(False, None, message=msg, error=str(exc))

                if output_callback:
                    output_callback("Aura will now exit to complete the update.")
                return PullResult(True, None, message="Update helper launched. Quitting Aura...")
            else:
                msg = "Packaged updates are currently only supported on Windows."
                return PullResult(False, None, message=msg, error=msg)

        except Exception as exc:
            logger.exception("Packaged update failed")
            return PullResult(False, None, message=f"Update failed: {exc}", error=str(exc))

    # --- No compatible asset ---
    msg = "No compatible packaged asset found in the latest release."
    return PullResult(False, None, message=msg, error=msg)


def get_windows_updater_helper(install_dir: Path | None = None) -> Path:
    """Return the bundled Windows updater helper path, preferring existing files."""
    candidates: list[Path] = []
    if install_dir is not None:
        candidates.append(install_dir / WINDOWS_UPDATER_HELPER_NAME)
    candidates.append(get_current_app_dir() / WINDOWS_UPDATER_HELPER_NAME)
    candidates.append(Path(__file__).resolve().parent / "windows_updater.cmd")

    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()

    return candidates[0].resolve()


def _build_windows_updater_command(
    updater_exe: Path,
    extracted_dir: Path,
    install_dir: Path,
    current_exe: Path,
    pid: int,
) -> list[str]:
    """Build the exact argv list used to start the Windows updater helper."""
    return [
        str(updater_exe),
        "--source",
        str(extracted_dir),
        "--target",
        str(install_dir),
        "--pid",
        str(pid),
        "--restart",
        str(current_exe),
    ]


def _format_update_launch_details(updater_exe: Path, argv: list[str]) -> str:
    return f"Updater executable: {updater_exe}\nUpdater argv: {argv!r}"


def _target_requires_elevation(install_dir: Path) -> bool:
    """Return True when the install directory cannot be written by this process."""
    probe = install_dir / f".aura-update-write-test-{os.getpid()}"
    try:
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("test")
        probe.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True
    except OSError:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def _shellexecute_windows_updater(updater_exe: Path, argv: list[str]) -> None:
    """Launch the updater through ShellExecuteW with elevation."""
    import ctypes

    parameters = subprocess.list2cmdline(argv[1:])
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "runas",
        str(updater_exe),
        parameters,
        str(updater_exe.parent),
        0,
    )
    if result <= 32:
        raise OSError(f"ShellExecuteW failed with code {result}")


def _launch_windows_updater(
    *,
    updater_exe: Path,
    argv: list[str],
    extracted_dir: Path,
    install_dir: Path,
    output_callback: Callable[[str], None] | None = None,
) -> None:
    """Validate paths, log the launch attempt, and start the updater helper."""
    if not updater_exe.exists():
        raise FileNotFoundError(f"Updater helper does not exist: {updater_exe}")
    if not updater_exe.is_file():
        raise FileNotFoundError(f"Updater helper is not a file: {updater_exe}")
    if not extracted_dir.exists() or not extracted_dir.is_dir():
        raise FileNotFoundError(f"Extracted update directory does not exist: {extracted_dir}")
    if not install_dir.exists() or not install_dir.is_dir():
        raise FileNotFoundError(f"Target install directory does not exist: {install_dir}")
    if not argv or Path(argv[0]) != updater_exe:
        raise ValueError("Updater argv must start with the updater executable path.")

    details = _format_update_launch_details(updater_exe, argv)
    logger.info("Launching packaged updater\n%s", details)
    if output_callback:
        output_callback(f"Updater executable: {updater_exe}")
        output_callback(f"Updater argv: {argv!r}")

    if _target_requires_elevation(install_dir):
        logger.info("Target install directory requires elevation; using ShellExecuteW.")
        _shellexecute_windows_updater(updater_exe, argv)
        return

    try:
        subprocess.Popen(
            argv,
            cwd=str(updater_exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except PermissionError:
        logger.info("Updater launch requires elevation; retrying with ShellExecuteW.")
        _shellexecute_windows_updater(updater_exe, argv)


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
        msg = "Git update is only available for source installs. Please update from GitHub manually."
        return UpdateStatus(None, False, state="not_git", message=msg)

    repo_check = run_git_command(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        msg = "Git update is only available for source installs. Please update from GitHub manually."
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
        msg = "Git update is only available for source installs. Please update from GitHub manually."
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
    output = "\n".join(part for part in (status.git_output, pull_result.output) if part).strip()

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


# --- Helper functions for installer-based updates ---


def get_installer_staging_dir(version: str) -> Path:
    """Return the stable per-user folder used for a downloaded installer."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    local_root = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version)
    return local_root / "Aura" / "updates" / safe_version


def _download_asset(
    asset: GitHubAsset,
    temp_dir: Path,
    output_callback: Callable[[str], None] | None = None,
    *,
    filename: str | None = None,
) -> Path:
    """Download a GitHub asset to a temporary directory and return the local path."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    local_path = temp_dir / (filename or asset.name)
    if output_callback:
        output_callback(f"Downloading {asset.name}...")
    with httpx.Client(follow_redirects=True, timeout=300) as client:
        with client.stream("GET", asset.url) as resp:
            resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in resp.iter_bytes():
                    f.write(chunk)
    return local_path


def _launch_installer(
    installer_path: Path,
    output_callback: Callable[[str], None] | None = None,
) -> bool:
    """Launch a Windows Inno Setup installer through the shell.

    Returns True only when ShellExecuteW reports a successful launch handoff.
    """
    if sys.platform != "win32":
        if output_callback:
            output_callback("Installer updates are only supported on Windows.")
            output_callback(f"If the installer does not appear, run this file manually: {installer_path}")
        return False

    if not installer_path.exists() or not installer_path.is_file():
        if output_callback:
            output_callback(f"Installer file is missing: {installer_path}")
            output_callback(f"If the installer does not appear, run this file manually: {installer_path}")
        return False

    flags = ["/CURRENTUSER", "/LAUNCHAFTERUPDATE=1"]
    details = _format_installer_launch_details(installer_path)
    logger.info("Launching installer\n%s", details)
    if output_callback:
        output_callback(f"Launching installer: {installer_path}")
        output_callback(f"Launch method: {INSTALLER_LAUNCH_METHOD}")
        output_callback(f"If the installer does not appear, run this file manually: {installer_path}")

    try:
        result = _shellexecute_installer(installer_path, flags)
    except Exception as exc:
        logger.exception("ShellExecuteW installer launch failed")
        if output_callback:
            output_callback(f"Installer launch failed: {exc}")
        return False

    if result <= 32:
        msg = f"Installer launch failed: ShellExecuteW returned {result}"
        logger.error("%s\n%s", msg, details)
        if output_callback:
            output_callback(msg)
        return False

    return True


def _format_installer_launch_details(installer_path: Path) -> str:
    return (
        f"Downloaded installer: {installer_path}\n"
        f"Launch method: {INSTALLER_LAUNCH_METHOD}\n"
        f"If the installer does not appear, run this file manually: {installer_path}"
    )


def _shellexecute_installer(installer_path: Path, flags: list[str]) -> int:
    """Use ShellExecuteW/open to start the GUI installer visibly."""
    import ctypes

    parameters = subprocess.list2cmdline(flags)
    result = ctypes.windll.shell32.ShellExecuteW(
        None,
        "open",
        str(installer_path),
        parameters,
        str(installer_path.parent),
        1,
    )
    return int(result)


def _verify_checksum(
    file_path: Path | str | None,
    expected_sha256: str | None,
) -> bool:
    """Verify a file's SHA-256 checksum against an expected value.

    Returns True if no checksum was provided (skip), or if the checksum matches.
    """
    if not expected_sha256:
        logger.info("No checksum provided, skipping verification.")
        return True
    if file_path is None:
        return False
    path = Path(file_path)
    if not path.exists():
        return False
    sha256 = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha256.update(chunk)
    actual = sha256.hexdigest()
    if actual.lower() == expected_sha256.lower():
        return True
    logger.error("Checksum mismatch: expected %s, got %s", expected_sha256, actual)
    return False
