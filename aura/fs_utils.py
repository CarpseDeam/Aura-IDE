"""Shared filesystem traversal utilities."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Directories and file suffixes to skip when traversing.
SKIP_DIRS: set[str] = {
    ".git",
    ".aura",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "venv",
    ".svn",
    ".hg",
    "eggs",
    ".eggs",
}
SKIP_FILE_SUFFIXES: set[str] = {".pyc", ".pyo", ".so", ".o", ".class", ".jar"}

MAX_DIRS_VISITED = 2000
MAX_FILES_CONSIDERED = 5000
MAX_SCAN_SECONDS = 5.0


def get_max_mtime(root: Path) -> float:
    """Return the maximum mtime of all files that would be included."""
    latest = 0.0
    dirs_visited = 0
    files_considered = 0
    start_time = time.monotonic()
    budget_exceeded = False
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            dirs_visited += 1
            if dirs_visited > MAX_DIRS_VISITED or time.monotonic() - start_time > MAX_SCAN_SECONDS:
                budget_exceeded = True
            if budget_exceeded:
                break
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".") and d not in SKIP_DIRS and (root / d).parts[-1] not in SKIP_DIRS
            ]
            for fname in filenames:
                suffix = Path(fname).suffix.lower()
                if suffix not in (".py", ".ts", ".tsx", ".js"):
                    continue
                files_considered += 1
                if files_considered > MAX_FILES_CONSIDERED:
                    budget_exceeded = True
                    break
                fpath = os.path.join(dirpath, fname)
                try:
                    mtime = os.path.getmtime(fpath)
                    if mtime > latest:
                        latest = mtime
                except OSError:
                    continue
            if budget_exceeded:
                break
    except PermissionError:
        pass
    if budget_exceeded:
        logger.info(
            "mtime scan truncated: root=%s dirs_visited=%d files_considered=%d elapsed_ms=%.0f",
            root, dirs_visited, files_considered, (time.monotonic() - start_time) * 1000,
        )
    return latest
