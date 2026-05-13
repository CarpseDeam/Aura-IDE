"""GitHub source-install updater helpers for Aura."""
from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from aura.config import get_subprocess_kwargs

logger = logging.getLogger(__name__)

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

    @property
    def can_pull(self) -> bool:
        return (
            self.is_git_repo
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
    """Run a git command in repo_root and return captured output.

    The caller is responsible for running this from a worker thread when used
    by the GUI. ``GIT_TERMINAL_PROMPT=0`` keeps fetch/pull from blocking on
    credential prompts.
    """
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


def get_update_status(
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

    status = get_update_status(root, output_callback=output_callback)
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
