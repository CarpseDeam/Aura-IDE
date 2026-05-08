"""Git tools for the tool registry — read-only repository introspection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any


def git_status(workspace_root: Path) -> dict[str, Any]:
    """Return the current branch plus lists of staged, unstaged, and untracked files."""
    try:
        # Get current branch name.
        branch_result = subprocess.run(
            ["git", "branch", "--show-current"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(workspace_root),
        )
        if branch_result.returncode != 0:
            return {"ok": False, "error": "Not a git repository (or git not found)."}
        branch = branch_result.stdout.strip()

        # Get porcelain status.
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(workspace_root),
        )
        if status_result.returncode != 0:
            return {"ok": False, "error": "Not a git repository (or git not found)."}

        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []

        for line in status_result.stdout.splitlines():
            if not line:
                continue
            # First two characters are the status codes XY.
            x = line[0] if len(line) >= 1 else " "
            y = line[1] if len(line) >= 2 else " "
            # The filename starts after the 3rd character (index 2 is a space).
            raw_path = line[3:].strip()

            # Remove surrounding quotes if present.
            if len(raw_path) >= 2 and raw_path[0] == raw_path[-1] == '"':
                raw_path = raw_path[1:-1]

            # Untracked files: "??"
            if x == "?" and y == "?":
                untracked.append(raw_path)
                continue

            # Staged changes: X is not space and not "?".
            if x != " " and x != "?":
                staged.append(raw_path)

            # Unstaged changes: Y is not space and not "?".
            if y != " " and y != "?":
                unstaged.append(raw_path)

        return {
            "ok": True,
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "clean": not staged and not unstaged and not untracked,
        }
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git status timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_diff(
    workspace_root: Path,
    staged: bool = False,
    path: str | None = None,
) -> dict[str, Any]:
    """Return the git diff of changes in the workspace.

    By default shows unstaged changes (working tree vs HEAD).
    Set staged=True to see changes staged for commit.
    Optionally restrict to a single file with the path parameter.
    Output is capped at 200KB.
    """
    try:
        cmd = ["git", "diff"]
        if staged:
            cmd.append("--cached")
        if path:
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(workspace_root),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git diff failed."}

        stdout = result.stdout
        max_bytes = 200_000
        truncated = len(stdout.encode("utf-8")) > max_bytes

        if truncated:
            # Truncate at byte boundary. Encode, slice, decode replacing errors.
            encoded = stdout.encode("utf-8")
            # Walk back to avoid splitting a multi-byte character.
            while len(encoded) > max_bytes:
                encoded = encoded[:max_bytes]
                try:
                    encoded.decode("utf-8")
                except UnicodeDecodeError:
                    max_bytes -= 1
                else:
                    break
            stdout = encoded.decode("utf-8") + "\n... [truncated at 200KB]"

        return {"ok": True, "diff": stdout, "truncated": truncated}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git diff timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
