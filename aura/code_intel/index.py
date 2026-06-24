"""Incremental workspace code-intelligence index.

Caches FileInfo, symbols, outlines, and reference edges per file.  Lazy:
files are only parsed when first requested.  ``refresh()`` triggers a full
workspace walk or re-parses a given list of changed files.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from aura.config import MAX_READ_BYTES
from aura.fs_utils import (
    MAX_DIRS_VISITED,
    MAX_FILES_CONSIDERED,
    MAX_SCAN_SECONDS,
    SKIP_DIRS,
    SKIP_FILE_SUFFIXES,
)

logger = logging.getLogger(__name__)

# Maximum file size (in bytes) we are willing to parse at all.
_MAX_PARSE_BYTES = MAX_READ_BYTES


class CodeIntelIndex:
    """Language-neutral, incremental workspace code-intelligence index.

    Usage::

        index = CodeIntelIndex(workspace_root)
        index.refresh()                           # full walk
        outline = index.get_outline("aura/config.py")
        deps = index.get_dependents("aura/config.py")
    """

    def __init__(self, workspace_root: Path) -> None:
        self._root = workspace_root.resolve()
        self._files: dict[str, Any] = {}  # path -> FileInfo
        self._outlines: dict[str, dict[str, Any]] = {}  # path -> outline dict
        self._symbols: dict[str, list[Any]] = {}  # path -> list[SymbolInfo]
        self._refs: dict[str, list[Any]] = {}  # path -> list[ReferenceEdge]
        # Reverse index: symbol_name -> set[file_path] (files that define it)
        self._symbol_defs: dict[tuple[str, str], set[str]] = defaultdict(set)
        # Forward index: file -> set[symbol_names] it defines
        self._file_defines: dict[str, set[tuple[str, str]]] = defaultdict(set)
        # File -> set[file] that it references (dependency edges)
        self._dep_edges: dict[str, set[str]] = defaultdict(set)
        # Reverse: file -> set[file] that depend on it
        self._rev_dep_edges: dict[str, set[str]] = defaultdict(set)
        # path -> list[ParseDiagnostic] from last parse
        self._diagnostics: dict[str, list[Any]] = {}

    # -- public API ---------------------------------------------------------

    def refresh(self, changed_files: list[str] | None = None) -> None:
        """Parse (or re-parse) files.

        If *changed_files* is given, only those paths are re-parsed and their
        dependent entries are invalidated.  If *None*, a full workspace walk
        is performed.
        """
        if changed_files is not None:
            self._refresh_changed(changed_files)
        else:
            self._refresh_full()

    def file_count(self) -> int:
        return len(self._files)

    def symbol_count(self) -> int:
        total = 0
        for syms in self._symbols.values():
            total += len(syms)
        return total

    def file_paths(self) -> list[str]:
        """Return sorted list of all known workspace-relative file paths."""
        return sorted(self._files.keys())

    def get_file(self, path: str) -> Any | None:
        """Return FileInfo for *path*, or None."""
        return self._files.get(path)

    def get_symbols(self, path: str) -> list[Any]:
        """Return list of SymbolInfo for *path*, or [].

        Lazy-parses the file if not already indexed.
        """
        if path not in self._symbols:
            self._lazy_parse(path)
        return list(self._symbols.get(path, []))

    def get_outline(self, path: str) -> dict[str, Any]:
        """Return structural outline dict for *path*, or empty outline.

        Lazy-parses the file if not already indexed.
        """
        if path not in self._outlines:
            self._lazy_parse(path)
        return self._outlines.get(
            path, {"language": "unknown", "imports": [], "classes": [], "functions": []}
        )

    def get_references_to(self, symbol: str, file: str | None = None) -> list[Any]:
        """Return all ReferenceEdge objects targeting *symbol*.

        If *file* is given, only edges originating from that file are returned.
        """
        from aura.code_intel.models import ReferenceEdge

        results: list[ReferenceEdge] = []
        for src_file, edges in self._refs.items():
            if file is not None and src_file != file:
                continue
            for edge in edges:
                if edge.target_symbol == symbol or edge.target_symbol.rsplit(".", 1)[-1] == symbol:
                    results.append(edge)
        return results

    def get_dependents(self, path: str) -> list[str]:
        """Return sorted list of files that directly depend on *path*."""
        return sorted(self._rev_dep_edges.get(path, set()))

    def get_blast_radius(self, path: str) -> list[str]:
        """BFS from *path* through reverse dependency edges.

        Returns all files that transitively depend on *path* (excluding
        *path* itself).
        """
        visited: set[str] = {path}
        queue: deque[str] = deque([path])

        while queue:
            current = queue.popleft()
            for dependent in self._rev_dep_edges.get(current, set()):
                if dependent not in visited:
                    visited.add(dependent)
                    queue.append(dependent)

        visited.discard(path)
        return sorted(visited)

    def get_diagnostics(self, path: str) -> list[Any]:
        """Return list of ParseDiagnostic for *path*, or [].

        Lazy-parses the file if not already indexed.
        """
        if path not in self._diagnostics:
            self._lazy_parse(path)
        return list(self._diagnostics.get(path, []))

    # -- internal: lazy single-file parse -----------------------------------

    def _lazy_parse(self, path: str) -> None:
        """Parse a single file on demand and cache results."""
        abs_path = self._root / path
        if not abs_path.is_file():
            return

        try:
            file_size = abs_path.stat().st_size
            if file_size > _MAX_PARSE_BYTES:
                return
            raw = abs_path.read_bytes()
            content = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError, PermissionError):
            return

        if not content.strip():
            return

        self._index_file(path, content)

    def _evict_file(self, path: str) -> None:
        """Remove all cached data for *path* from this index."""
        # Remove symbol definition mappings
        old_defines = self._file_defines.pop(path, set())
        for sym in old_defines:
            self._symbol_defs[sym].discard(path)
            if not self._symbol_defs[sym]:
                del self._symbol_defs[sym]

        # Remove dependency edge mappings
        old_deps = self._dep_edges.pop(path, set())
        for dep in old_deps:
            self._rev_dep_edges[dep].discard(path)

        # Remove core caches
        self._files.pop(path, None)
        self._outlines.pop(path, None)
        self._symbols.pop(path, None)
        self._refs.pop(path, None)
        self._diagnostics.pop(path, None)

    def _index_file(self, path: str, content: str) -> None:
        """Parse *content* for *path* and populate all caches."""
        from aura.code_intel.adapter import get_adapter
        from aura.code_intel.models import FileInfo

        # Evict any prior data for this path so renames/rewrites don't leave
        # stale symbol definitions or reverse dependency edges behind.
        self._evict_file(path)

        adapter = get_adapter(path, content=content)
        if adapter is None:
            return

        # --- FileInfo ---
        abs_path = self._root / path
        try:
            st = abs_path.stat()
            mtime = st.st_mtime
            size = st.st_size
        except OSError:
            mtime = 0.0
            size = len(content.encode("utf-8"))

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self._files[path] = FileInfo(
            path=path,
            language=adapter.language_id,
            content_hash=content_hash,
            mtime=mtime,
            size=size,
        )

        # --- Outline ---
        self._outlines[path] = adapter.outline(path, content)

        # --- Symbols & reference edges ---
        symbols, refs, diags = adapter.parse(path, content)
        self._symbols[path] = symbols
        self._refs[path] = refs

        # --- Parse diagnostics ---
        self._diagnostics[path] = diags

        # Update symbol definition indices
        for sym in symbols:
            key = (adapter.language_id, sym.name)
            self._symbol_defs[key].add(path)
            self._file_defines[path].add(key)

        # --- Dependency edges ---
        raw_deps = adapter.dependencies(path, content)
        dep_set: set[str] = set()
        for dep in raw_deps:
            # Try direct path first
            dep_path = dep.replace("\\", "/")
            if (self._root / dep_path).is_file():
                dep_set.add(dep_path)
            # Try as dotted module -> file path
            elif "." in dep_path and dep_path.endswith(".py"):
                # e.g. "aura.config" -> "aura/config.py"
                alt = dep_path.replace(".", "/")
                if (self._root / alt).is_file():
                    dep_set.add(alt)
        self._dep_edges[path] = dep_set

        # Update reverse dependency edges
        for dep in dep_set:
            self._rev_dep_edges[dep].add(path)

    # -- internal: full walk -------------------------------------------------

    def _refresh_full(self) -> None:
        """Walk the workspace and index every parseable file."""
        from aura.code_intel.adapter import get_adapter

        start = time.monotonic()
        dirs_visited = 0
        files_considered = 0
        budget_exceeded = False

        for dirpath, dirnames, filenames in os.walk(self._root):
            dirs_visited += 1
            if dirs_visited > MAX_DIRS_VISITED or time.monotonic() - start > MAX_SCAN_SECONDS:
                budget_exceeded = True
            if budget_exceeded:
                break

            # Prune skipped dirs
            dirnames[:] = [
                d
                for d in dirnames
                if not d.startswith(".")
                and d not in SKIP_DIRS
                and (self._root / d).parts[-1] not in SKIP_DIRS
            ]

            rel_dir = os.path.relpath(dirpath, self._root)
            if rel_dir == ".":
                rel_dir = ""

            for fname in sorted(filenames):
                suffix = Path(fname).suffix.lower()
                if suffix in SKIP_FILE_SUFFIXES:
                    continue
                if fname.startswith("."):
                    continue

                files_considered += 1
                if files_considered > MAX_FILES_CONSIDERED:
                    budget_exceeded = True
                    break

                rel_path = os.path.join(rel_dir, fname).replace("\\", "/")
                abs_path = os.path.join(dirpath, fname)

                try:
                    st = os.stat(abs_path)
                    file_size = st.st_size
                    file_mtime = st.st_mtime
                except OSError:
                    continue

                # Stat-based skip: if FileInfo exists and the file hasn't changed, skip
                existing = self._files.get(rel_path)
                if existing is not None and existing.mtime == file_mtime and existing.size == file_size:
                    continue

                if file_size > _MAX_PARSE_BYTES:
                    continue

                try:
                    with open(abs_path, "rb") as f:
                        raw = f.read(_MAX_PARSE_BYTES)
                except (OSError, PermissionError):
                    continue

                try:
                    content = raw.decode("utf-8")
                except UnicodeDecodeError:
                    continue

                if not content.strip():
                    continue

                adapter = get_adapter(rel_path, content=content)
                if adapter is None:
                    continue

                self._index_file(rel_path, content)

        if budget_exceeded:
            logger.info(
                "CodeIntelIndex walk truncated: root=%s dirs_visited=%d files_considered=%d elapsed_ms=%.0f",
                self._root, dirs_visited, files_considered,
                (time.monotonic() - start) * 1000,
            )

    # -- internal: incremental update ---------------------------------------

    def _refresh_changed(self, changed_files: list[str]) -> None:
        """Re-parse changed files and invalidate downstream caches."""
        for path_str in changed_files:
            norm = path_str.replace("\\", "/")
            self._evict_file(norm)

            # Re-parse
            abs_path = self._root / norm
            if not abs_path.is_file():
                continue

            try:
                file_size = abs_path.stat().st_size
                if file_size > _MAX_PARSE_BYTES:
                    continue
                raw = abs_path.read_bytes()
                content = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError, PermissionError):
                continue

            if content.strip():
                self._index_file(norm, content)


# ---------------------------------------------------------------------------
# Per-workspace index cache (dispatch/worker thread only; single-threaded)
# ---------------------------------------------------------------------------
_index_cache: dict[str, CodeIntelIndex] = {}


def get_cached_index(root: Path) -> CodeIntelIndex:
    """Return a cached CodeIntelIndex for *root* (construct + refresh on miss).

    The returned index is shared across audit and code-intel tool calls
    running on the dispatch/worker thread.  Callers must still call
    ``index.refresh()`` to pick up new/changed files \u2014 the stat-based
    skip gate makes subsequent full walks cheap.
    """
    resolved = str(root.resolve())
    if resolved not in _index_cache:
        idx = CodeIntelIndex(root)
        idx.refresh()
        _index_cache[resolved] = idx
    return _index_cache[resolved]
