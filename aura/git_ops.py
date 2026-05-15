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
            text=True,
            encoding="utf-8",
            errors="replace",
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
            text=True,
            encoding="utf-8",
            errors="replace",
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
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            **get_subprocess_kwargs(),
        )
        if result.returncode == 0:
            # No changes to commit — unstage and return
            subprocess.run(
                ["git", "reset", "--"] + files,
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
            text=True,
            encoding="utf-8",
            errors="replace",
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
            text=True,
            encoding="utf-8",
            errors="replace",
            **get_subprocess_kwargs(),
        )
        return False, "git commit failed."


def recent_commits(workspace_root: Path, limit: int = 30) -> tuple[bool, list[dict], str]:
    """Return recent commits for the workspace repository.

    Each commit dict includes sha, short_sha, subject, author, relative_date,
    changed_files_count, and changed_files.
    """
    if not is_git_repo(workspace_root):
        return False, [], "Not a git repository."

    safe_limit = max(1, min(limit, 200))
    record_sep = "\x1e"
    field_sep = "\x1f"
    fmt = f"{record_sep}%H{field_sep}%h{field_sep}%s{field_sep}%an{field_sep}%cr"
    try:
        result = subprocess.run(
            [
                "git",
                "log",
                f"-n{safe_limit}",
                "--date=relative",
                f"--format={fmt}",
                "--name-only",
            ],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except FileNotFoundError:
        return False, [], "git executable not found."
    except subprocess.TimeoutExpired:
        return False, [], "git log timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git log failed."
        if "does not have any commits yet" in err or "bad default revision" in err:
            return True, [], ""
        return False, [], err

    commits: list[dict] = []
    for raw_record in result.stdout.split(record_sep):
        record = raw_record.strip()
        if not record:
            continue
        lines = record.splitlines()
        fields = lines[0].split(field_sep)
        if len(fields) < 5:
            continue

        changed_files = [line.strip() for line in lines[1:] if line.strip()]
        commits.append(
            {
                "sha": fields[0],
                "short_sha": fields[1],
                "subject": fields[2],
                "author": fields[3],
                "relative_date": fields[4],
                "changed_files_count": len(changed_files),
                "changed_files": changed_files,
            }
        )

    return True, commits, ""


def commit_changed_files(workspace_root: Path, sha: str) -> tuple[bool, list[str], str]:
    """Return file paths changed by a commit."""
    if not is_git_repo(workspace_root):
        return False, [], "Not a git repository."

    try:
        result = subprocess.run(
            [
                "git",
                "diff-tree",
                "--root",
                "--no-commit-id",
                "--name-only",
                "-r",
                sha,
            ],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except FileNotFoundError:
        return False, [], "git executable not found."
    except subprocess.TimeoutExpired:
        return False, [], "git diff-tree timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git diff-tree failed."
        return False, [], err

    return True, [line.strip() for line in result.stdout.splitlines() if line.strip()], ""


def commit_diff(workspace_root: Path, sha: str) -> tuple[bool, str, str]:
    """Return a readable patch for a commit."""
    if not is_git_repo(workspace_root):
        return False, "", "Not a git repository."

    try:
        result = subprocess.run(
            [
                "git",
                "show",
                "--format=fuller",
                "--stat",
                "--patch",
                "--no-ext-diff",
                "--no-color",
                sha,
            ],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **get_subprocess_kwargs(),
        )
    except FileNotFoundError:
        return False, "", "git executable not found."
    except subprocess.TimeoutExpired:
        return False, "", "git show timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git show failed."
        return False, "", err

    return True, result.stdout, ""


def working_tree_status(workspace_root: Path) -> tuple[bool, str, str]:
    """Return git status --porcelain output for restore warnings."""
    if not is_git_repo(workspace_root):
        return False, "", "Not a git repository."

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
    except FileNotFoundError:
        return False, "", "git executable not found."
    except subprocess.TimeoutExpired:
        return False, "", "git status timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git status failed."
        return False, "", err

    return True, result.stdout, ""


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
            encoding="utf-8",
            errors="replace",
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
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=10,
            **get_subprocess_kwargs(),
        )
        return True, "Undo complete — last commit reverted, changes are staged."
    except subprocess.CalledProcessError as e:
        err = e.stderr.strip() if e.stderr else str(e)
        return False, f"git reset failed: {err}"


def snapshot(workspace_root: Path) -> str | None:
    """Capture the current HEAD SHA. Returns None if no commits exist."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
            encoding="utf-8",
            errors="replace",
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
