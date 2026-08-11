"""Orchestrates file walking, tokenization, mtime-based staleness, and search.

The :class:`CodebaseIndex` lazily builds a BM25 inverted index over the
workspace on first search, then incrementally refreshes on subsequent calls.
"""

from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path

from aura.codebase_index.bm25 import BM25Scorer, tokenize
from aura.config import (
    CODEBASE_INDEX_MAX_FILE_BYTES,
    CODEBASE_INDEX_MAX_WALK_SECONDS,
    MAX_CODEBASE_INDEX_FILES,
)
from aura.paths import config_dir
from aura.repository_inventory import RepositoryFile, build_inventory


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
        self._max_walk_seconds: float = CODEBASE_INDEX_MAX_WALK_SECONDS
        self._index_partial: bool = False
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

    def _should_index(self, rf: RepositoryFile) -> bool:
        """Determine whether *rf* should be included in the BM25 index.

        The canonical inventory already applied repository-wide policy (skip
        dirs, hidden files, sensitive files, workspace jail). This is BM25's
        own capability-specific acceptance on top of that: a byte limit and a
        binary-content sniff. A file the inventory reports may still be
        rejected here without that meaning it isn't a real repository file.

        Args:
            rf: Candidate file from the canonical repository inventory.

        Returns:
            True if the file should be indexed.
        """
        if rf.size > self._max_file_bytes or rf.size == 0:
            return False

        # Binary content sniff: reject files containing a null byte in first 8KB
        try:
            with rf.abs_path.open("rb") as fh:
                head = fh.read(8192)
            if b"\x00" in head:
                return False
        except OSError:
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

    def _collect(self) -> dict[str, tuple[Path, float]]:
        """Collect indexable files from the canonical repository inventory.

        Applies BM25's own file-count and wall-clock budget on top of
        canonical discovery, and inherits the inventory's own partial state
        (a truncated filesystem fallback, or a failed Git command) — either
        source of incompleteness marks the resulting index partial.

        Returns:
            Dict mapping relative path strings (posix) to ``(absolute_path, mtime)``.
        """
        collected: dict[str, tuple[Path, float]] = {}
        start = time.monotonic()

        inventory = build_inventory(self._root)
        self._index_partial = not inventory.complete

        for rf in inventory.files:
            if len(collected) >= self._max_files:
                self._index_partial = True
                break
            if time.monotonic() - start > self._max_walk_seconds:
                self._index_partial = True
                break

            if not self._should_index(rf):
                continue

            collected[rf.rel_path] = (rf.abs_path, rf.mtime)

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

        collected = self._collect()

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
        current = self._collect()
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
            ``indexed_term_count``, ``partial``.
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
                "partial": self._index_partial,
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
            "partial": self._index_partial,
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
