"""Workspace snapshot tool schema."""
from __future__ import annotations

from typing import Any

WORKSPACE_SNAPSHOT_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_workspace_snapshot",
        "description": "Get a compact snapshot of the current workspace: root path, project identity, recent threads, git branch/status, changed files count, and project type hints (pyproject.toml, package.json, etc.). Use this at the start of ambiguous tasks instead of calling git_status, list_directory, and multiple reads separately. Fast, read-only, no file contents.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}
