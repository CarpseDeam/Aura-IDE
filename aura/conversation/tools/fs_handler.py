"""Read-only filesystem tool handlers, gated by workspace-jail path resolution.

Each handle_* method receives the raw args dict (as passed by the LLM)
and returns a payload dict (same shape as the underlying fs_read.py functions).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from aura.conversation.tools.fs_read import glob_files, list_directory, read_file, read_file_outline, read_file_range

# How many lines read_file returns when given an offset but no limit.
DEFAULT_READ_WINDOW_LINES: int = 2000

# read_files budgeting.
#
# The point of these caps is that *every requested path keeps its metadata* even
# when its content cannot be inlined. A path is never reduced to a bare
# "exceeded total size limit" string: the model still learns the file exists,
# how big it is, what its hash is, what (if anything) was included, and the
# exact call that would fetch the rest. That survives history compaction, which
# only shrinks long string values and leaves the envelope alone.
READ_FILES_TOTAL_CONTENT_CHARS: int = 240 * 1024
# Files at or below this are inlined whole — the common case must stay lossless.
READ_FILES_FULL_INCLUDE_CHARS: int = 48 * 1024
# Larger files get a bounded head slice plus a structural outline.
READ_FILES_BOUNDED_CHARS: int = 8 * 1024
# Caps on the structural summary so a summary never becomes the new problem.
READ_FILES_OUTLINE_ITEMS: int = 60


def _outline_summary(root: Path, target: Path) -> dict[str, Any] | None:
    """Bounded structural summary for a file too large to inline."""
    try:
        outline = read_file_outline(root, target)
    except Exception:
        return None
    if not outline.get("ok"):
        return None

    classes = []
    for cls in (outline.get("classes") or [])[:READ_FILES_OUTLINE_ITEMS]:
        if not isinstance(cls, dict):
            continue
        classes.append({
            "name": cls.get("name"),
            "line": cls.get("line"),
            "methods": (cls.get("methods") or [])[:READ_FILES_OUTLINE_ITEMS],
        })

    functions = [
        {"signature": fn.get("signature"), "line": fn.get("line")}
        for fn in (outline.get("functions") or [])[:READ_FILES_OUTLINE_ITEMS]
        if isinstance(fn, dict)
    ]

    return {
        "language": outline.get("language"),
        "total_lines": outline.get("total_lines"),
        "imports": (outline.get("imports") or [])[:READ_FILES_OUTLINE_ITEMS],
        "classes": classes,
        "functions": functions,
    }


def _head_slice(text: str, max_chars: int) -> tuple[str, int]:
    """Return (head_text, lines_included), cut on a line boundary."""
    if len(text) <= max_chars:
        return text, len(text.splitlines())
    head = text[:max_chars]
    cut = head.rfind("\n")
    if cut > 0:
        head = head[:cut]
    return head, len(head.splitlines())


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
        """Read a file, or a window of it.

        ``offset``/``limit`` select a 1-based line window and route to the
        streaming range reader, which can reach any line in a large file. With
        neither, the whole file comes back (capped at MAX_READ_BYTES).

        Args:
            args: "path" (required), plus optional "offset" and "limit".

        Returns:
            Payload dict from read_file() or read_file_range().
        """
        offset = args.get("offset")
        limit = args.get("limit")
        if offset is None and limit is None:
            target = self._resolve(args.get("path", ""))
            return read_file(self._root, target)

        if offset is not None and not isinstance(offset, int):
            return {"ok": False, "error": "offset must be an integer"}
        if limit is not None and not isinstance(limit, int):
            return {"ok": False, "error": "limit must be an integer"}
        start_line = 1 if offset is None else offset
        if start_line < 1:
            return {"ok": False, "error": "offset must be >= 1"}
        if limit is None:
            end_line = start_line + DEFAULT_READ_WINDOW_LINES - 1
        elif limit < 1:
            return {"ok": False, "error": "limit must be >= 1"}
        else:
            end_line = start_line + limit - 1

        try:
            target = self._resolve(args.get("path", ""))
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        return read_file_range(self._root, target, start_line, end_line)

    def handle_read_files(self, args: dict[str, Any]) -> dict[str, Any]:
        """Read multiple files in one call, keeping metadata for every path.

        Every requested path gets an entry describing what happened to it:
        ``status`` (complete / truncated / summarized / omitted / error),
        ``reason``, ``file_size``, ``content_hash``, ``line_count``,
        ``included_range`` and ``continuation`` — the exact call that retrieves
        what was left out. Small files are inlined whole; large ones get a
        bounded head slice plus a structural outline; paths past the shared
        content budget keep their metadata with no content.

        Args:
            args: Must contain "paths" key with a non-empty list of path strings.

        Returns:
            A dict with "ok": True and "files" mapping requested path keys to
            per-file results, or {"ok": False, "error": ...} on validation
            failure.
        """
        paths = args.get("paths")
        if not isinstance(paths, list) or len(paths) == 0:
            return {"ok": False, "error": "paths is required and must be a non-empty array"}

        remaining = READ_FILES_TOTAL_CONTENT_CHARS
        files: dict[str, dict] = {}

        for path in paths:
            path_key = str(path)
            entry, consumed = self._read_one_for_batch(path_key, remaining)
            files[path_key] = entry
            remaining -= consumed

        return {
            "ok": True,
            "files": files,
            "requested_paths": [str(p) for p in paths],
            "content_budget_chars": READ_FILES_TOTAL_CONTENT_CHARS,
        }

    def _read_one_for_batch(self, path_key: str, remaining: int) -> tuple[dict[str, Any], int]:
        """Build the read_files entry for one path. Returns (entry, chars_used)."""
        try:
            target = self._resolve(path_key)
        except ValueError as e:
            return {
                "ok": False,
                "path": path_key,
                "status": "error",
                "reason": str(e),
                "error": str(e),
                "truncated": False,
                "continuation": "",
            }, 0

        result = read_file(self._root, target)
        if not result.get("ok"):
            error = result.get("error", "unknown error")
            return {
                "ok": False,
                "path": path_key,
                "status": "error",
                "reason": error,
                "error": error,
                "truncated": False,
                "continuation": "",
            }, 0

        rel = result.get("path", path_key)
        content = result.get("content", "")
        file_size = result.get("file_size", 0)
        content_hash = result.get("content_hash", "")
        # read_file already caps at MAX_READ_BYTES; that counts as truncated too.
        capped_by_reader = bool(result.get("truncated"))
        total_lines = len(content.splitlines())

        base: dict[str, Any] = {
            "ok": True,
            "path": rel,
            "requested_path": path_key,
            "file_size": file_size,
            "content_hash": content_hash,
            "line_count": total_lines,
        }

        def continuation(from_line: int) -> str:
            return (
                f"read_file(path='{rel}', offset={from_line}, limit=400) "
                f"— repeat to walk forward, or use grep_search to locate a symbol first."
            )

        # No budget left: keep the metadata, skip the bytes.
        if remaining <= 0:
            return {
                **base,
                "status": "omitted",
                "truncated": True,
                "reason": (
                    "shared read_files content budget of "
                    f"{READ_FILES_TOTAL_CONTENT_CHARS} chars was exhausted by earlier paths"
                ),
                "included_range": None,
                "content": "",
                "continuation": continuation(1),
            }, 0

        # Small file, enough budget: inline it whole.
        if not capped_by_reader and len(content) <= min(READ_FILES_FULL_INCLUDE_CHARS, remaining):
            return {
                **base,
                "status": "complete",
                "truncated": False,
                "reason": "file included in full",
                "included_range": {"start_line": 1, "end_line": total_lines},
                "content": content,
                "continuation": "",
            }, len(content)

        # Large file (or tight budget): bounded head slice + structural outline.
        allowance = max(0, min(READ_FILES_BOUNDED_CHARS, remaining))
        head, head_lines = _head_slice(content, allowance)
        outline = _outline_summary(self._root, target)
        reason = (
            f"file is {file_size} bytes; included the first {len(head)} chars "
            f"({head_lines} of {total_lines} read lines) plus a structural outline"
        )
        if capped_by_reader:
            reason += " — the reader also capped this file at MAX_READ_BYTES"

        entry = {
            **base,
            "status": "summarized" if outline else "truncated",
            "truncated": True,
            "reason": reason,
            "included_range": {"start_line": 1, "end_line": head_lines},
            "content": head,
            "continuation": continuation(head_lines + 1),
        }
        if outline:
            entry["outline"] = outline
        return entry, len(head)

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
