"""Git integration for /undo and repository queries.

Provides:
- is_git_repo: check if a directory is inside a git working tree
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


def build_commit_message(goal: str, files: list[str], summary: str) -> str:
    """Build a clean commit message from goal, file list, and summary.

    Subject is derived from the first line of *goal*, truncated at 72 chars
    on a word boundary.  Internal terms (AI, Worker, Planner, etc.) are
    stripped from the subject.
    """
    import re

    subject = goal.splitlines()[0].strip().rstrip(".")

    # Strip internal implementation terms from the subject
    subject = re.sub(
        r"\b(AI|Worker|Planner|Aura|agent|tool)\b",
        "",
        subject,
        flags=re.IGNORECASE,
    )
    subject = re.sub(r"\s+", " ", subject).strip()

    # Truncate at word boundary near 72 chars
    if len(subject) > 72:
        last_space = subject[:72].rfind(" ")
        if last_space >= 40:
            subject = subject[:last_space] + "..."
        else:
            subject = subject[:72] + "..."

    parts = [subject]

    if summary.strip():
        parts.append("")
        parts.append(summary.strip())

    if files:
        parts.append("")
        parts.append("Files:")
        for path in files:
            parts.append(f"- {path}")

    message = "\n".join(parts)

    max_len = 2000
    if len(message) > max_len:
        message = message[:max_len] + "\n... (truncated)"

    return message


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


def working_tree_diff(workspace_root: Path) -> tuple[bool, str, str]:
    """Return git diff output for the workspace."""
    if not is_git_repo(workspace_root):
        return False, "", "Not a git repository."

    try:
        result = subprocess.run(
            ["git", "diff", "--no-ext-diff", "--no-color"],
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
        return False, "", "git diff timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git diff failed."
        return False, "", err

    return True, result.stdout, ""


def recent_commit_log(workspace_root: Path, limit: int = 10) -> tuple[bool, str, str]:
    """Return recent commits as git log --oneline text."""
    if not is_git_repo(workspace_root):
        return False, "", "Not a git repository."

    safe_limit = max(1, min(limit, 50))
    try:
        result = subprocess.run(
            ["git", "log", "--oneline", f"--max-count={safe_limit}"],
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
        return False, "", "git log timed out."

    if result.returncode != 0:
        err = result.stderr.strip() if result.stderr else "git log failed."
        if "does not have any commits yet" in err or "bad default revision" in err:
            return True, "", ""
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


def changes_since(
    workspace_root: Path, sha: str | None
) -> tuple[bool, tuple[str, ...]]:
    """Check whether the repo differs from *sha* and list changed paths.

    Uses the union of:
      - git diff --name-only <sha>   (tracked changes since the snapshot —
        covers commits, staged edits, and unstaged edits)
      - git ls-files --others --exclude-standard  (untracked new files)

    When *sha* is None (no commits at snapshot time), treat any
    tracked-or-untracked change present now as the change set.
    Empty union → (False, ()).

    Returns (has_work, tuple_of_changed_paths).
    """
    if not is_git_repo(workspace_root):
        return False, ()

    changed: set[str] = set()

    try:
        if sha:
            result = subprocess.run(
                ["git", "diff", "--name-only", sha],
                cwd=str(workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                **get_subprocess_kwargs(),
            )
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    p = line.strip()
                    if p:
                        changed.add(p)
        # Untracked files (always include regardless of sha presence)
        result = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                p = line.strip()
                if p:
                    changed.add(p)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, ()

    if changed:
        return True, tuple(sorted(changed))
    return False, ()


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


def commit_all(workspace_root: Path, message: str) -> tuple[bool, str | None, str]:
    """Stage all changes and commit. Returns (ok, new_sha_or_None, message).

    - If nothing to commit: (True, None, "nothing to commit") — not an error.
    - On successful commit: (True, <HEAD sha>, "") with the new HEAD sha.
    - On git failure: (False, None, <stderr>).
    """
    try:
        add_result = subprocess.run(
            ["git", "add", "-A"],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
        if add_result.returncode != 0:
            stderr = add_result.stderr.strip() if add_result.stderr else "git add failed"
            return False, None, stderr
    except subprocess.TimeoutExpired:
        return False, None, "git add timed out."
    except FileNotFoundError:
        return False, None, "git executable not found."

    try:
        commit_result = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(workspace_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            **get_subprocess_kwargs(),
        )
        if commit_result.returncode == 0:
            # Capture the new HEAD sha
            try:
                sha_result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    cwd=str(workspace_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    **get_subprocess_kwargs(),
                )
                sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                sha = None
            return True, sha, ""
        else:
            stderr = commit_result.stderr.strip() if commit_result.stderr else ""
            if "nothing to commit" in stderr.lower():
                return True, None, "nothing to commit"
            return False, None, stderr
    except subprocess.TimeoutExpired:
        return False, None, "git commit timed out."
    except FileNotFoundError:
        return False, None, "git executable not found."


_AURA_GITIGNORE_ENTRIES = (
    "/.aura/backups/",
    "/.aura/conversations/",
    "/.aura/handoffs/",
    "/.aura/startup-smoke-profile/",
    "/.aura/threads/",
    "/.aura/tmp/",
    "/.aura/memory.db",
    "/.aura/planner.txt",
    "/.aura/project.json",
    "/.aura/toolist.txt",
    "/.aura/drones/runs/",
)


def _is_broad_aura_ignore(line: str) -> bool:
    """Return True if line would broadly ignore the entire .aura directory tree."""
    stripped = line.strip()
    if not stripped:
        return False
    # Negation patterns are not broad ignores
    if stripped.startswith("!"):
        return False
    broad_patterns = {
        ".aura",
        ".aura/",
        "/.aura",
        "/.aura/",
        ".aura/*",
        ".aura/**",
        "/.aura/*",
        "/.aura/**",
    }
    return stripped in broad_patterns


def ensure_aura_gitignored(workspace_root: Path) -> None:
    """Manage an explicit allowlist of runtime Aura paths in .gitignore.

    Ensures private/runtime .aura paths are ignored while allowing
    repo-owned paths like .aura/drones/ to be tracked.

    All failures (missing permissions, disk full, etc.) are silently ignored
    so callers never need to handle exceptions from this function.
    """
    gitignore_path = workspace_root / ".gitignore"

    if gitignore_path.exists():
        try:
            content = gitignore_path.read_text(encoding="utf-8")
        except OSError:
            return

        lines = content.splitlines()
        filtered_lines = [l for l in lines if not _is_broad_aura_ignore(l)]

        existing = set()
        for line in filtered_lines:
            stripped = line.strip()
            if stripped:
                existing.add(stripped)

        missing = [e for e in _AURA_GITIGNORE_ENTRIES if e not in existing]

        if not missing and len(lines) == len(filtered_lines):
            return  # No changes needed

        new_lines = list(filtered_lines)
        for entry in missing:
            new_lines.append(entry)

        new_content = "\n".join(new_lines)
        if new_content:
            new_content += "\n"

        if new_content == content:
            return

        try:
            gitignore_path.write_text(new_content, encoding="utf-8")
        except OSError:
            pass
    else:
        try:
            content = "".join(f"{e}\n" for e in _AURA_GITIGNORE_ENTRIES)
            gitignore_path.write_text(content, encoding="utf-8")
        except OSError:
            pass
