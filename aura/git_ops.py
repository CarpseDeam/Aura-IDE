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


def snapshot(workspace_root: Path) -> str | None:
    """Capture the current HEAD SHA. Returns None if no commits exist."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=5,
            **get_subprocess_kwargs(),
        )
        sha = result.stdout.strip()
        if sha and result.returncode == 0:
            return sha
        return None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None


def restore_to_snapshot(workspace_root: Path, sha: str) -> tuple[bool, str]:
    """Hard-reset to the given SHA. Destructive — discards uncommitted changes.
    Returns (success, message)."""
    if not is_git_repo(workspace_root):
        return False, "Not a git repository."
    try:
        subprocess.run(
            ["git", "reset", "--hard", sha],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
        return True, f"Restored to {sha[:8]}."
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        return False, f"git reset failed: {err}"
    except subprocess.TimeoutExpired:
        return False, "git reset timed out."


def git_init(workspace_root: Path) -> tuple[bool, str]:
    """Initialize a git repository and create an initial commit.
    Returns (success, message)."""
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        return False, f"git init failed: {err}"
    except subprocess.TimeoutExpired:
        return False, "git init timed out."

    # Stage all files
    try:
        subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        # If there's nothing to add, that's fine — we'll still try to commit
        pass

    # Create initial commit
    try:
        result = subprocess.run(
            ["git", "commit", "-m", "Initial commit"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
        if result.returncode == 0:
            return True, "git init complete — initial commit created."
        else:
            stderr = result.stderr.strip()
            # If nothing to commit (empty dir), still return success
            if "nothing to commit" in stderr.lower() or "nothing added" in stderr.lower():
                return True, "git init complete (no files to commit yet)."
            return False, f"git commit failed: {stderr}"
    except subprocess.TimeoutExpired:
        return False, "git commit timed out."


def ensure_aura_gitignored(workspace_root: Path) -> None:
    """Ensure `.aura/` is listed in the workspace's .gitignore file.

    If no .gitignore exists, create one with `.aura/` as its content.
    If a .gitignore exists but does not mention `.aura`, append `.aura/` to it.
    The operation is idempotent — repeated calls are safe.

    All failures (missing permissions, disk full, etc.) are silently ignored
    so callers never need to handle exceptions from this function.
    """
    gitignore_path = workspace_root / ".gitignore"
    aura_pattern = ".aura/"

    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            return

        # Check if .aura or .aura/ is already present (as a line or part of a glob)
        lines = content.splitlines()
        for line in lines:
            stripped = line.strip()
            if stripped == ".aura" or stripped == ".aura/":
                return  # Already present, nothing to do
            # Also catch cases like ".aura/*" or similar
            if stripped.startswith(".aura") and (len(stripped) == 5 or stripped[5] in ("/", "*", "!")):
                return  # Already present in some form

        # Append .aura/ to the existing .gitignore
        # Ensure we start on a new line if the file doesn't end with one
        if content and not content.endswith("\n"):
            content += "\n"
        content += f"{aura_pattern}\n"
        try:
            gitignore_path.write_text(content, encoding="utf-8")
        except OSError:
            pass
    else:
        # Create a new .gitignore with just .aura/
        try:
            gitignore_path.write_text(f"{aura_pattern}\n", encoding="utf-8")
        except OSError:
            pass
