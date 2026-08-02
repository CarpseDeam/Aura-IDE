"""Git tools for the tool registry — read-only repository introspection."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from aura.config import get_subprocess_kwargs
from aura.paths import safe_is_relative_to


def _repo_root(workspace_root: Path) -> Path | None:
    """Absolute path of the enclosing git repository, or None."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if top else None


def _to_workspace_relative(
    repo_relative: str,
    repo_root: Path | None,
    workspace_root: Path,
) -> str | None:
    """Map a repo-root-relative git path into the workspace, or None.

    Porcelain output is always relative to the *repository* root, which is not
    the workspace root when the workspace is a subdirectory of a larger repo.
    Reporting those paths unchanged names files the tools cannot open and, for
    siblings of the workspace, files outside it entirely — so anything that
    does not land inside the workspace is dropped rather than renamed.
    """
    if repo_root is None:
        return repo_relative
    absolute = (repo_root / repo_relative).resolve()
    if not safe_is_relative_to(absolute, workspace_root):
        return None
    import os

    return Path(os.path.relpath(absolute, workspace_root)).as_posix()


def _split_status_records(payload: bytes) -> list[str]:
    """Split NUL-delimited porcelain output into fields.

    ``-z`` is what makes the paths trustworthy: without it git escapes any
    path containing a non-ASCII byte, a space, or a quote into a C-quoted
    literal — ``sub/café.txt`` arrives as ``"sub/caf\\303\\251.txt"``, which
    is not a file that exists. With ``-z`` the bytes are the real path and
    there is nothing to unescape.
    """
    fields = payload.split(b"\x00")
    return [field.decode("utf-8", errors="replace") for field in fields]


def git_status(workspace_root: Path) -> dict[str, Any]:
    """Return the current branch, remote tracking info, and lists of staged, unstaged, and untracked files."""
    try:
        # -z: NUL-delimited, unescaped, unambiguous paths.  Bytes, not text,
        # because the delimiter is a byte and the paths are UTF-8.
        status_result = subprocess.run(
            ["git", "status", "--branch", "--porcelain=v1", "-z"],
            capture_output=True,
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )
        if status_result.returncode != 0:
            return {"ok": False, "error": "Not a git repository (or git not found)."}

        import re

        branch = ""
        tracking = None
        ahead = 0
        behind = 0

        staged: list[str] = []
        unstaged: list[str] = []
        untracked: list[str] = []
        outside_workspace = 0

        repo_root = _repo_root(workspace_root)
        workspace_resolved = Path(workspace_root).resolve()

        def add(bucket: list[str], repo_relative: str) -> None:
            nonlocal outside_workspace
            mapped = _to_workspace_relative(repo_relative, repo_root, workspace_resolved)
            if mapped is None:
                outside_workspace += 1
                return
            if mapped not in bucket:
                bucket.append(mapped)

        fields = _split_status_records(status_result.stdout or b"")
        index = 0
        while index < len(fields):
            entry = fields[index]
            index += 1
            if not entry:
                continue

            if entry.startswith("## "):
                header = entry[3:]
                # "branch...remote/branch [ahead X, behind Y]"
                match = re.match(r'^(\S+?)(?:\.\.\.(\S+?))?(?:\s+\[(.*?)\])?$', header)
                if match:
                    branch = match.group(1)
                    tracking = match.group(2) or None
                    bracket_content = match.group(3) or ""
                    if bracket_content:
                        ahead_match = re.search(r'ahead\s+(\d+)', bracket_content)
                        behind_match = re.search(r'behind\s+(\d+)', bracket_content)
                        if ahead_match:
                            ahead = int(ahead_match.group(1))
                        if behind_match:
                            behind = int(behind_match.group(1))
                else:
                    branch = header.strip()
                continue

            if len(entry) < 3:
                continue
            x, y = entry[0], entry[1]
            # Exactly one space separates the codes from the path; the path is
            # taken verbatim, since a leading or trailing space is part of it.
            path = entry[3:]

            # A rename or copy is two fields: the new path, then the old one.
            if (x in ("R", "C") or y in ("R", "C")) and index < len(fields):
                index += 1  # consume the source path; the new path is the subject

            if x == "?" and y == "?":
                add(untracked, path)
                continue

            if x != " " and x != "?":
                add(staged, path)

            if y != " " and y != "?":
                add(unstaged, path)

        # If no branch was found from ## header (empty repo), try show-current
        if not branch:
            try:
                branch_result = subprocess.run(
                    ["git", "branch", "--show-current"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,                    cwd=str(workspace_root),
                    **get_subprocess_kwargs(),
                )
                if branch_result.returncode == 0:
                    branch = branch_result.stdout.strip()
            except Exception:
                pass

        # Get remote URL if we have a tracking branch
        remote_url = None
        if tracking is not None:
            try:
                remote_result = subprocess.run(
                    ["git", "remote", "get-url", "origin"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=5,                    cwd=str(workspace_root),
                    **get_subprocess_kwargs(),
                )
                if remote_result.returncode == 0:
                    remote_url = remote_result.stdout.strip()
            except Exception:
                pass

        return {
            "ok": True,
            "branch": branch,
            "tracking": tracking,
            "remote_url": remote_url,
            "ahead": ahead,
            "behind": behind,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "clean": not staged and not unstaged and not untracked,
            # Changes in the repo but outside this workspace. Reported as a
            # count so "clean" is not mistaken for "the repo has no changes".
            "outside_workspace": outside_workspace,
        }
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git status timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_log(
    workspace_root: Path,
    max_count: int = 10,
    path: str | None = None,
) -> dict[str, Any]:
    """Return the last N commits (one-line format).

    Optionally restrict history to a single file with the path parameter.
    Returns a list of commit dicts with hash, message, author, and date.
    """
    try:
        cmd = ["git", "log", "--oneline", f"--max-count={max_count}"]
        if path:
            cmd.extend(["--", path])

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git log failed."}

        commits: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            # Format: "<hash> <message>"
            parts = line.split(" ", 1)
            if len(parts) >= 2:
                commits.append({"hash": parts[0], "message": parts[1].strip()})
            elif parts:
                commits.append({"hash": parts[0], "message": ""})

        return {"ok": True, "commits": commits, "count": len(commits)}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git log timed out."}
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
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git diff failed."}

        stdout = result.stdout
        max_bytes = 200_000
        encoded = stdout.encode("utf-8")
        truncated = len(encoded) > max_bytes

        if truncated:
            # Slice to max_bytes, then walk back to the last newline
            sliced = encoded[:max_bytes]
            last_nl = sliced.rfind(b"\n")
            if last_nl > 0:
                sliced = sliced[:last_nl]
            # Walk back to a valid UTF-8 boundary
            while sliced:
                try:
                    sliced.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    sliced = sliced[:-1]
            stdout = sliced.decode("utf-8") + "\n... [truncated at 200KB]\n"

        return {"ok": True, "diff": stdout, "truncated": truncated}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git diff timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_show(
    workspace_root: Path,
    commit_sha: str,
) -> dict[str, Any]:
    """Show the full diff and metadata for a specific commit.

    Returns commit hash, author, date, message, and the diff.
    Output is capped at 200KB.
    """
    try:
        result = subprocess.run(
            ["git", "show", "--format=fuller", commit_sha],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git show failed."}

        stdout = result.stdout
        max_bytes = 200_000
        encoded = stdout.encode("utf-8")
        truncated = len(encoded) > max_bytes

        if truncated:
            sliced = encoded[:max_bytes]
            last_nl = sliced.rfind(b"\n")
            if last_nl > 0:
                sliced = sliced[:last_nl]
            while sliced:
                try:
                    sliced.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    sliced = sliced[:-1]
            stdout = sliced.decode("utf-8") + "\n... [truncated at 200KB]\n"

        return {"ok": True, "output": stdout, "truncated": truncated}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git show timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_log_file(
    workspace_root: Path,
    path: str,
    max_count: int = 10,
) -> dict[str, Any]:
    """Return the commit history for a single file, following renames.

    Returns a list of commits that modified the file, each with hash,
    message, author, and date. Uses --follow to track across renames.
    """
    try:
        cmd = [
            "git",
            "log",
            "--follow",
            f"--max-count={max_count}",
            "--format=%h||%s||%an||%ad",
            "--date=short",
            "--",
            path,
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git log_file failed."}

        commits: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("||", 3)
            if len(parts) >= 4:
                commits.append({
                    "hash": parts[0].strip(),
                    "message": parts[1].strip(),
                    "author": parts[2].strip(),
                    "date": parts[3].strip(),
                })
            elif len(parts) >= 2:
                commits.append({
                    "hash": parts[0].strip(),
                    "message": parts[1].strip(),
                    "author": "",
                    "date": "",
                })
            elif parts:
                commits.append({
                    "hash": parts[0].strip(),
                    "message": "",
                    "author": "",
                    "date": "",
                })

        return {"ok": True, "commits": commits, "count": len(commits), "path": path}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git log_file timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_branch_list(
    workspace_root: Path,
) -> dict[str, Any]:
    """List all local and remote branches with tracking info.

    Returns branch names, whether each is the current HEAD, the upstream
    tracking branch, and ahead/behind counts.
    """
    try:
        # Use for-each-ref for machine-parseable output
        # Format: name|HEAD|upstream|trackshort
        result = subprocess.run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)|%(if)%(HEAD)%(then)*%(end)|%(upstream:short)|%(upstream:trackshort)",
                "refs/heads/",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git branch_list failed."}

        branches: list[dict[str, Any]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 1:
                continue
            name = parts[0].strip()
            is_current = len(parts) > 1 and parts[1].strip() == "*"
            upstream = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
            trackshort = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None

            branches.append({
                "name": name,
                "current": is_current,
                "upstream": upstream,
                "ahead_behind": trackshort,
            })

        return {"ok": True, "branches": branches, "count": len(branches)}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git branch_list timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_stash_list(
    workspace_root: Path,
) -> dict[str, Any]:
    """List all stashes in the repository.

    Returns a list of stashes with index, branch, and description.
    """
    try:
        result = subprocess.run(
            ["git", "stash", "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git stash list failed."}

        stashes: list[dict[str, str]] = []
        for line in result.stdout.strip().splitlines():
            if not line:
                continue
            # Format: stash@{0}: WIP on master: <message>
            import re
            match = re.match(r"stash@\{(\d+)\}: (.*?): (.*)", line)
            if match:
                stashes.append({
                    "index": match.group(1),
                    "context": match.group(2).strip(),
                    "message": match.group(3).strip(),
                })
            else:
                stashes.append({"raw": line})

        return {"ok": True, "stashes": stashes, "count": len(stashes)}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git stash list timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def git_stash_show(
    workspace_root: Path,
    index: int = 0,
) -> dict[str, Any]:
    """Show the diff of a specific stash.

    Returns the diff of the stash at the given index (default 0).
    Output is capped at 200KB.
    """
    try:
        result = subprocess.run(
            ["git", "stash", "show", "-p", f"stash@{{{index}}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            cwd=str(workspace_root),
            **get_subprocess_kwargs(),
        )

        if result.returncode != 0:
            return {"ok": False, "error": result.stderr.strip() or "git stash show failed."}

        stdout = result.stdout
        max_bytes = 200_000
        encoded = stdout.encode("utf-8")
        truncated = len(encoded) > max_bytes

        if truncated:
            sliced = encoded[:max_bytes]
            last_nl = sliced.rfind(b"\n")
            if last_nl > 0:
                sliced = sliced[:last_nl]
            while sliced:
                try:
                    sliced.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    sliced = sliced[:-1]
            stdout = sliced.decode("utf-8") + "\n... [truncated at 200KB]\n"

        return {"ok": True, "diff": stdout, "truncated": truncated}
    except FileNotFoundError:
        return {"ok": False, "error": "git is not installed or not found on PATH."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "git stash show timed out."}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
