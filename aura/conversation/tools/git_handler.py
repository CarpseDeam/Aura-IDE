"""Git tool handlers — workspace repository introspection."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.conversation.tools.git_tools import (
    git_branch_list,
    git_diff,
    git_log,
    git_log_file,
    git_show,
    git_stash_list,
    git_stash_show,
    git_status,
)


class GitHandler:
    """Read-only git tool handlers.

    Each handle_* method receives the raw args dict and returns a payload dict
    (the same shape as the underlying git_tools.py functions return).
    """

    def __init__(self, workspace_root: Path) -> None:
        """Args:
            workspace_root: The repository root path.
        """
        self._root = workspace_root

    def handle_git_status(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show working tree status."""
        return git_status(self._root)

    def handle_git_diff(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show diff of working tree changes."""
        staged = bool(args.get("staged", False))
        path = args.get("path")
        return git_diff(self._root, staged=staged, path=path)

    def handle_git_log(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show commit history."""
        max_count = int(args.get("max_count", 10))
        path = args.get("path")
        return git_log(self._root, max_count=max_count, path=path)

    def handle_git_show(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show a single commit's diff and metadata."""
        commit_sha = args.get("commit_sha", "")
        if not commit_sha:
            return {"ok": False, "error": "Missing required parameter: commit_sha"}
        return git_show(self._root, commit_sha)

    def handle_git_log_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show commit history for a single file."""
        path = args.get("path", "")
        if not path:
            return {"ok": False, "error": "Missing required parameter: path"}
        max_count = int(args.get("max_count", 10))
        return git_log_file(self._root, path, max_count=max_count)

    def handle_git_branch_list(self, args: dict[str, Any]) -> dict[str, Any]:
        """List all local branches with tracking information."""
        return git_branch_list(self._root)

    def handle_git_stash_list(self, args: dict[str, Any]) -> dict[str, Any]:
        """List all stashes."""
        return git_stash_list(self._root)

    def handle_git_stash_show(self, args: dict[str, Any]) -> dict[str, Any]:
        """Show the diff of a specific stash."""
        index = int(args.get("index", 0))
        return git_stash_show(self._root, index=index)
