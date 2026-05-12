"""Orchestrates file walking, tokenization, mtime-based staleness, and search.

The :class:`CodebaseIndex` lazily builds a BM25 inverted index over the
workspace on first search, then incrementally refreshes on subsequent calls.
"""

from __future__ import annotations
import hashlib
import json
from pathlib import Path

from aura.codebase_index.bm25 import BM25Scorer, tokenize
from aura.config import (
    CODEBASE_INDEX_EXTENSIONS,
    CODEBASE_INDEX_MAX_FILE_BYTES,
    MAX_CODEBASE_INDEX_FILES,
    SKIP_DIRS,
    SKIP_FILE_SUFFIXES,
)
from aura.paths import config_dir


def _cache_path(workspace_root: Path) -> Path:
    """Return the cache file path for a given workspace root.

    The file path is deterministic based on a SHA-256 hash of the
    resolved workspace root, truncated to 16 hex characters.

    Args:
        workspace_root: Absolute path to the workspace root directory.

    Returns:
        Absolute ``Path`` to the cache JSON file.
    """
    h = hashlib.sha256(str(workspace_root.resolve()).encode()).hexdigest()[:16]
    cache_dir = config_dir() / "bm25_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"{h}.json"


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

        Attempts to load a cached BM25 index from disk. If the cache is
        missing or stale, the index will be built lazily on first search.

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
        # Attempt to restore from disk cache (sets self._built = True on success)
        self._loaded_from_cache = self._load_cache()

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

    # ---- cache layer -------------------------------------------------------

    def _load_cache(self) -> bool:
        """Restore the index from a disk cache if available and fresh.

        Reads the cache file for this workspace, validates it, restores
        files whose mtime matches, and cleans stale doc_ids from the scorer.

        Returns:
            True if the cache was successfully loaded; False otherwise.
        """
        cache_path = _cache_path(self._root)
        if not cache_path.is_file():
            return False

        try:
            raw = cache_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError):
            return False

        # Validate expected top-level keys
        if not all(k in data for k in ("workspace_root", "files", "scorer")):
            return False

        # Verify workspace root match
        if data.get("workspace_root") != str(self._root):
            return False

        files_data: dict[str, list] = data.get("files", {})
        scorer_data: dict = data.get("scorer", {})

        # Restore files whose mtime still matches
        restored_files: dict[str, tuple[Path, float]] = {}
        for rel_str, (abs_path_str, cached_mtime) in files_data.items():
            abs_path = Path(abs_path_str)
            if not abs_path.is_file():
                continue
            try:
                current_mtime = abs_path.stat().st_mtime
            except OSError:
                continue
            if abs(current_mtime - cached_mtime) < 0.001:
                restored_files[rel_str] = (abs_path, cached_mtime)

        # Reconstruct the scorer
        try:
            self._scorer = BM25Scorer.from_dict(scorer_data)
        except (KeyError, TypeError):
            return False

        self._files = restored_files

        # Remove stale doc_ids from the scorer (files that existed when the
        # cache was saved but whose mtime has since changed).
        cached_keys = set(files_data.keys())
        restored_keys = set(restored_files.keys())
        for stale_rel in cached_keys - restored_keys:
            self._scorer.remove_document(stale_rel)

        self._built = True
        return True

    def _save_cache(self) -> None:
        """Persist the current index state to disk.

        Writes atomically via a temp file + rename. Failures are silently
        ignored — the cache is purely an optimisation.
        """
        cache_path = _cache_path(self._root)
        data = {
            "workspace_root": str(self._root),
            "files": {
                rel: [str(abs_path), mtime]
                for rel, (abs_path, mtime) in self._files.items()
            },
            "scorer": self._scorer.to_dict(),
        }
        tmp_path = cache_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp_path.replace(cache_path)
        except OSError:
            pass  # cache saves must NEVER crash the caller

    # ---- build / refresh ---------------------------------------------------

    def build(self) -> None:
        """Build (or rebuild) the index from scratch.

        Idempotent — calling this again fully replaces the index.
        The resulting index is persisted to disk.
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
        self._save_cache()

    def refresh(self) -> None:
        """Incrementally update the index based on mtime changes.

        Called on every search after the first build. Fast for small changes.
        The updated index is persisted to disk.
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

        self._save_cache()

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
