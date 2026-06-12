"""Read-only filesystem tool handlers, gated by workspace-jail path resolution.

Each handle_* method receives the raw args dict (as passed by the LLM)
and returns a payload dict (same shape as the underlying fs_read.py functions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from aura.conversation.tools.fs_read import glob_files, list_directory, read_file, read_file_outline, read_file_range


class FsReadHandler:
    """Read-only filesystem tool handlers, gated by workspace-jail path resolution.

    Each handle_* method receives the raw args dict (as passed by the LLM)
    and returns a payload dict (same shape as the underlying fs_read.py functions).
    """

    def __init__(self, workspace_root: Path, resolve_fn: Callable[[str], Path]) -> None:
        """Args:
            workspace_root: The jail root for path resolution.
            resolve_fn: A callable that takes a user-provided path string and
                        returns an absolute Path inside workspace_root, or raises
                        ValueError if the path is invalid or escapes.
        """
        self._root = workspace_root
        self._resolve = resolve_fn

    def handle_read_file(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a single file from the workspace.

        Args:
            args: Must contain "path" key with a workspace-relative path string.

        Returns:
            Payload dict from read_file().
        """
        target = self._resolve(args.get("path", ""))
        return read_file(self._root, target)

    def handle_read_files(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read multiple files in a single call, respecting a 500KB total size cap.

        Args:
            args: Must contain "paths" key with a non-empty list of path strings.

        Returns:
            A dict with "ok": True and "files" mapping path keys to per-file results,
            or {"ok": False, "error": ...} on validation failure.
        """
        paths = args.get("paths")
        if not isinstance(paths, list) or len(paths) == 0:
            return {"ok": False, "error": "paths is required and must be a non-empty array"}

        TOTAL_SIZE_CAP = 500 * 1024
        accumulated = 0
        files: dict[str, dict] = {}

        for path in paths:
            path_key = str(path)
            if accumulated >= TOTAL_SIZE_CAP:
                files[path_key] = {"ok": False, "error": "exceeded total size limit"}
                continue
            try:
                target = self._resolve(str(path))
            except ValueError as e:
                files[path_key] = {"ok": False, "error": str(e)}
                continue

            result = read_file(self._root, target)
            if result.get("ok"):
                content = result["content"]
                if accumulated + len(content) > TOTAL_SIZE_CAP:
                    files[path_key] = {"ok": False, "error": "exceeded total size limit"}
                    accumulated = TOTAL_SIZE_CAP
                else:
                    files[path_key] = {"ok": True, "content": content}
                    accumulated += len(content)
            else:
                files[path_key] = {"ok": False, "error": result.get("error", "unknown error")}

        return {"ok": True, "files": files}

    def handle_list_directory(self, args: dict[str, Any]) -> dict[str, Any]:
        """List files and subdirectories of a workspace directory.

        Args:
            args: May contain "path" key (defaults to ".").

        Returns:
            Payload dict from list_directory().
        """
        target = self._resolve(args.get("path", "."))
        return list_directory(self._root, target)

    def handle_glob(self, args: dict[str, Any]) -> dict[str, Any]:
        """Recursively find files matching a glob pattern.

        Args:
            args: Must contain "pattern" key with a glob pattern string.

        Returns:
            Payload dict from glob_files(), or {"ok": False, "error": ...}
            if pattern is missing, empty, absolute, or contains '..'.
        """
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            return {"ok": False, "error": "pattern is required"}
        if ".." in Path(pattern).parts or Path(pattern).is_absolute():
            return {"ok": False, "error": "glob pattern must be workspace-relative"}
        return glob_files(self._root, pattern)

    def handle_read_file_outline(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a file's structural outline without loading the full content.

        Args:
            args: Must contain "path" key with a workspace-relative path string.

        Returns:
            Payload dict from read_file_outline().
        """
        target = self._resolve(args.get("path", ""))
        return read_file_outline(self._root, target)

    def handle_read_file_range(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read a specific line range from a file (1-based, inclusive).

        Args:
            args: Must contain "path", "start_line", and "end_line".

        Returns:
            Payload dict from read_file_range().
        """
        start_line = args.get("start_line")
        end_line = args.get("end_line")
        if not isinstance(start_line, int) or not isinstance(end_line, int):
            return {"ok": False, "error": "start_line and end_line must be integers"}
        try:
            target = self._resolve(args.get("path", ""))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return read_file_range(self._root, target, start_line, end_line)
