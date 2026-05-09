"""Orchestrates file walking, tokenization, mtime-based staleness, and search.

The :class:`CodebaseIndex` lazily builds a BM25 inverted index over the
workspace on first search, then incrementally refreshes on subsequent calls.
"""

from __future__ import annotations
from pathlib import Path

from aura.codebase_index.bm25 import BM25Scorer, tokenize
from aura.config import (
    CODEBASE_INDEX_EXTENSIONS,
    CODEBASE_INDEX_MAX_FILE_BYTES,
    MAX_CODEBASE_INDEX_FILES,
    SKIP_DIRS,
    SKIP_FILE_SUFFIXES,
)


class CodebaseIndex:
    """Lazy-built BM25 index of the workspace codebase.

    Usage::

        index = CodebaseIndex(workspace_root)
        result = index.search("authentication handler")

    The index is built on the first call to :meth:`search` and incrementally
    refreshed on subsequent calls (sub-100ms for small file changes).

    Test cases:
    - Empty workspace (no indexable files): search returns [] without error.
    - Calling search twice: second call refreshes (doesn't crash).
    - Changing workspace root resets the index.
    """

    def __init__(self, workspace_root: Path) -> None:
        """Initialise the indexer.

        Args:
            workspace_root: Absolute path to the workspace root directory.
        """
        self._root = workspace_root.resolve()
        self._scorer = BM25Scorer()
        # Map: workspace-relative path string -> (absolute path, mtime)
        self._files: dict[str, tuple[Path, float]] = {}
        # Extensions to index (from config)
        self._extensions: set[str] = CODEBASE_INDEX_EXTENSIONS
        self._max_files: int = MAX_CODEBASE_INDEX_FILES
        self._max_file_bytes: int = CODEBASE_INDEX_MAX_FILE_BYTES
        self._built: bool = False

    @property
    def built(self) -> bool:
        """Whether the index has been built at least once."""
        return self._built

    @property
    def file_count(self) -> int:
        """Number of files currently in the index."""
        return len(self._files)

    # ---- file filtering ----------------------------------------------------

    def _should_index(self, file_path: Path, rel_path: Path) -> bool:
        """Determine whether *file_path* should be included in the index.

        Rejects hidden files, certain directories, non-indexable extensions,
        and files exceeding the byte limit.

        Args:
            file_path: Absolute path to the candidate file.
            rel_path: Relative path from workspace root.

        Returns:
            True if the file should be indexed.
        """
        # Check extension
        if file_path.suffix.lower() not in self._extensions:
            return False

        # Check file size
        try:
            size = file_path.stat().st_size
        except OSError:
            return False
        if size > self._max_file_bytes or size == 0:
            return False

        # Check path parts for skip dirs / hidden
        parts = rel_path.parts
        for part in parts:
            if part in SKIP_DIRS:
                return False
            if part.startswith("."):
                return False

        # Check suffix against skip suffixes
        if file_path.suffix.lower() in SKIP_FILE_SUFFIXES:
            return False

        return True

    # ---- file reading ------------------------------------------------------

    @staticmethod
    def _read_file_safe(absolute_path: Path) -> str | None:
        """Read file content with fallback encoding.

        Args:
            absolute_path: Absolute path to the file.

        Returns:
            File contents as a string, or None on failure.
        """
        try:
            return absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                return absolute_path.read_text(encoding="latin-1")
            except (OSError, UnicodeDecodeError):
                return None
        except OSError:
            return None

    # ---- file collection ---------------------------------------------------

    def _walk_and_collect(self) -> dict[str, tuple[Path, float]]:
        """Walk the workspace and collect indexable files.

        Returns:
            Dict mapping relative path strings (posix) to ``(absolute_path, mtime)``.
        """
        collected: dict[str, tuple[Path, float]] = {}
        for file_path in self._root.rglob("*"):
            if not file_path.is_file():
                continue
            if len(collected) >= self._max_files:
                break

            try:
                rel_path = file_path.relative_to(self._root)
            except ValueError:
                continue

            rel_str = rel_path.as_posix()

            if not self._should_index(file_path, rel_path):
                continue

            try:
                mtime = file_path.stat().st_mtime
            except OSError:
                continue

            collected[rel_str] = (file_path, mtime)

        return collected

    # ---- build / refresh ---------------------------------------------------

    def build(self) -> None:
        """Build (or rebuild) the index from scratch.

        Idempotent — calling this again fully replaces the index.
        """
        self._scorer = BM25Scorer()
        self._files = {}

        collected = self._walk_and_collect()

        for rel_str, (abs_path, _mtime) in collected.items():
            content = self._read_file_safe(abs_path)
            if content is None:
                continue
            tokens = tokenize(content)
            if not tokens:
                continue
            self._scorer.add_document(rel_str, tokens)
            self._files[rel_str] = (abs_path, _mtime)

        self._built = True

    def refresh(self) -> None:
        """Incrementally update the index based on mtime changes.

        Called on every search after the first build. Fast for small changes.
        """
        current = self._walk_and_collect()
        current_keys = set(current.keys())
        old_keys = set(self._files.keys())

        # Files removed from workspace
        for rel_str in old_keys - current_keys:
            self._scorer.remove_document(rel_str)
            del self._files[rel_str]

        # Files added or changed
        for rel_str in current_keys:
            abs_path, new_mtime = current[rel_str]

            if rel_str in old_keys:
                _, old_mtime = self._files[rel_str]
                if abs(new_mtime - old_mtime) < 0.001:
                    # mtime unchanged — skip
                    continue
                # Remove old version
                self._scorer.remove_document(rel_str)

            # Index new/changed version
            content = self._read_file_safe(abs_path)
            if content is None:
                continue
            tokens = tokenize(content)
            if not tokens:
                continue
            self._scorer.add_document(rel_str, tokens)
            self._files[rel_str] = (abs_path, new_mtime)

    # ---- search ------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> dict:
        """Search the codebase for documents relevant to *query*.

        Builds the index on first call, then refreshes incrementally.

        Args:
            query: Natural language or keyword query.
            top_k: Maximum number of results to return.

        Returns:
            Dict with keys: ``ok``, ``query``, ``results`` (list of dicts with
            ``path``, ``score``, ``snippet``), ``indexed_file_count``,
            ``indexed_term_count``.
        """
        if not self._built:
            self.build()
        else:
            self.refresh()

        query_tokens = tokenize(query)

        if not query_tokens:
            return {
                "ok": True,
                "query": query,
                "results": [],
                "indexed_file_count": self._scorer.doc_count,
                "indexed_term_count": self._scorer.term_count,
            }

        raw_results = self._scorer.search(query_tokens, top_k=top_k)

        results: list[dict] = []
        for rel_str, score in raw_results:
            abs_path = self._files.get(rel_str, (None, None))[0]
            snippet = self._extract_snippet(abs_path, query_tokens)
            results.append(
                {
                    "path": rel_str,
                    "score": round(score, 4),
                    "snippet": snippet,
                }
            )

        return {
            "ok": True,
            "query": query,
            "results": results,
            "indexed_file_count": self._scorer.doc_count,
            "indexed_term_count": self._scorer.term_count,
        }

    @staticmethod
    def _extract_snippet(file_path: Path | None, query_tokens: list[str]) -> str:
        """Extract a relevant snippet from *file_path*.

        Finds lines containing query tokens; falls back to first 3 lines.
        Snippet is capped at 500 characters.

        Args:
            file_path: Absolute path to the file, or None.
            query_tokens: Tokenized query.

        Returns:
            A text snippet from the file.
        """
        if file_path is None:
            return "(file unavailable)"

        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "(file unavailable)"

        lines = text.splitlines()
        if not lines:
            return "(empty file)"

        # Try to find lines containing query tokens
        matched_lines: list[str] = []
        for line in lines:
            line_lower = line.lower()
            if any(tok in line_lower for tok in query_tokens):
                matched_lines.append(line)
                if len("".join(matched_lines)) > 500:
                    break

        if matched_lines:
            snippet = "\n".join(matched_lines)
        else:
            # Fallback: first 3 lines
            snippet = "\n".join(lines[:3])

        if len(snippet) > 500:
            snippet = snippet[:497] + "..."

        return snippet

    # ---- root management ---------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        """Change the workspace root and reset the index.

        Args:
            root: New workspace root directory.
        """
        self._root = root.resolve()
        self._scorer = BM25Scorer()
        self._files = {}
        self._built = False
