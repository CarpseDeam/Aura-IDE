"""Git integration for auto-commit on worker writes and /undo command.

Provides:
- is_git_repo: check if a directory is inside a git working tree
- auto_commit: stage and commit changed files with an AI-generated message
- undo_last_commit: soft-reset HEAD~1, keeping changes in the working directory
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from aura.config import get_subprocess_kwargs


def is_git_repo(workspace_root: Path) -> bool:
    """Return True if workspace_root is inside a git working tree."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(workspace_root),
            capture_output=True,
            check=True,
            timeout=5,
            **get_subprocess_kwargs(),
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def auto_commit(workspace_root: Path, goal: str, files: list[str], summary: str) -> tuple[bool, str]:
    """Stage the listed files and create a commit. Returns (success, message)."""
    if not is_git_repo(workspace_root):
        return False, "Not a git repository."
    if not files:
        return False, "No files to commit."

    # Stage files
    try:
        subprocess.run(
            ["git", "add", "--"] + files,
            cwd=str(workspace_root),
            capture_output=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False, "git add failed."

    # Check if there are staged changes
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(workspace_root),
            capture_output=True,
            timeout=5,
            **get_subprocess_kwargs(),
        )
        if result.returncode == 0:
            # No changes to commit — unstage and return
            subprocess.run(
                ["git", "reset", "--"] + files,
                cwd=str(workspace_root),
                capture_output=True,
                **get_subprocess_kwargs(),
            )
            return False, "No changes to commit."
    except subprocess.CalledProcessError:
        pass

    # Build commit message
    message = f"{goal}\n\n{summary}"
    # Truncate to a reasonable size
    max_len = 2000
    if len(message) > max_len:
        message = message[:max_len] + "\n... (truncated)"

    try:
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(workspace_root),
            capture_output=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
        return True, f"Committed: {goal}"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # Unstage on failure
        subprocess.run(
            ["git", "reset", "--"] + files,
            cwd=str(workspace_root),
            capture_output=True,
            **get_subprocess_kwargs(),
        )
        return False, "git commit failed."


def undo_last_commit(workspace_root: Path) -> tuple[bool, str]:
    """Soft-reset HEAD~1, keeping changes in the working directory.

    Returns (success, message_string).
    """
    if not is_git_repo(workspace_root):
        return False, "Not a git repository."

    # Check there is at least one commit
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
            **get_subprocess_kwargs(),
        )
        if int(result.stdout.strip()) == 0:
            return False, "No commits to undo."
    except (subprocess.CalledProcessError, ValueError, subprocess.TimeoutExpired):
        return False, "Could not check git history."

    try:
        subprocess.run(
            ["git", "reset", "--soft", "HEAD~1"],
            cwd=str(workspace_root),
            capture_output=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
        return True, "Undo complete — last commit reverted, changes are staged."
    except subprocess.CalledProcessError as e:
        err = e.stderr.decode() if e.stderr else str(e)
        return False, f"git reset failed: {err}"
