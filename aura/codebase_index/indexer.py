"""Orchestrates file walking, structure-aware document construction, and search.

The :class:`CodebaseIndex` lazily builds a BM25 inverted index over the
workspace on first search, then incrementally refreshes on subsequent calls.
Each indexed file may own multiple BM25 documents — see
``aura/codebase_index/documents.py`` for how a file's source is partitioned
into non-overlapping, structurally-bounded retrieval documents using facts
from the shared :class:`~aura.code_intel.index.CodeIntelIndex`.
"""

from __future__ import annotations
import hashlib
import time
from dataclasses import replace
from pathlib import Path

from aura.code_intel.index import CodeIntelIndex
from aura.codebase_index.bm25 import BM25Scorer, tokenize
from aura.codebase_index.cache import FileRecord, load_cache, save_cache
from aura.codebase_index.documents import RetrievalDocument, build_retrieval_documents
from aura.config import (
    CODEBASE_INDEX_MAX_FILE_BYTES,
    CODEBASE_INDEX_MAX_WALK_SECONDS,
    MAX_CODEBASE_INDEX_FILES,
)
from aura.paths import config_dir
from aura.repository_inventory import RepositoryFile, build_inventory

_SNIPPET_MAX_CHARS = 500


def _cache_path(workspace_root: Path) -> Path:
    """Return the cache file path for a given workspace root.

    The file path is deterministic based on a SHA-256 hash of the
    resolved workspace root, truncated to 16 hex characters. A cache
    written by an older schema version is rejected by ``load_cache``'s
    own version check (see ``aura/codebase_index/cache.py``) and cleanly
    rebuilt in place — no separate path scheme is needed per schema.

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

        index = CodebaseIndex(workspace_root, code_intel_index=shared_index)
        result = index.search("authentication handler")

    The index is built on the first call to :meth:`search` and incrementally
    refreshed on subsequent calls (sub-100ms for small file changes).

    Test cases:
    - Empty workspace (no indexable files): search returns [] without error.
    - Calling search twice: second call refreshes (doesn't crash).
    - Changing workspace root resets the index.
    """

    def __init__(
        self, workspace_root: Path, code_intel_index: CodeIntelIndex | None = None
    ) -> None:
        """Initialise the indexer.

        Attempts to load a cached BM25 index from disk. If the cache is
        missing or stale, the index will be built lazily on first search.

        Args:
            workspace_root: Absolute path to the workspace root directory.
            code_intel_index: Shared :class:`CodeIntelIndex` to feed changed
                source into and read structural symbols back from. When the
                production caller is :class:`~aura.conversation.tools.registry.ToolRegistry`,
                this must be its one workspace ``CodeIntelIndex`` instance —
                never a second, independently-owned one. If omitted, a
                private instance is created (e.g. for standalone/test use).
        """
        self._root = workspace_root.resolve()
        self._code_intel = code_intel_index if code_intel_index is not None else CodeIntelIndex(self._root)
        self._scorer = BM25Scorer()
        # Map: workspace-relative path string -> FileRecord (abs_path, mtime, size, doc_ids)
        self._files: dict[str, FileRecord] = {}
        # Map: doc_id -> RetrievalDocument metadata (text always "" here —
        # bodies are re-sliced from disk by line range when needed, never
        # retained in memory or on disk).
        self._documents: dict[str, RetrievalDocument] = {}
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
        """Number of indexed repository files (not BM25 document count)."""
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

    def _collect(self) -> dict[str, tuple[Path, float, int]]:
        """Collect indexable files from the canonical repository inventory.

        Applies BM25's own file-count and wall-clock budget on top of
        canonical discovery, and inherits the inventory's own partial state
        (a truncated filesystem fallback, or a failed Git command) — either
        source of incompleteness marks the resulting index partial.

        Returns:
            Dict mapping relative path strings (posix) to
            ``(absolute_path, mtime, size)``.
        """
        collected: dict[str, tuple[Path, float, int]] = {}
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

            collected[rf.rel_path] = (rf.abs_path, rf.mtime, rf.size)

        return collected

    # ---- cache layer -------------------------------------------------------

    def _load_cache(self) -> bool:
        """Restore the index from a disk cache if available and fresh.

        Returns:
            True if the cache was successfully loaded; False otherwise.
        """
        cache_data = load_cache(_cache_path(self._root), self._root)
        if cache_data is None:
            return False

        self._files = cache_data.files
        self._documents = cache_data.documents
        self._scorer = cache_data.scorer
        self._built = True
        return True

    def _save_cache(self) -> None:
        """Persist the current index state to disk. Never raises."""
        save_cache(_cache_path(self._root), self._root, self._files, self._documents, self._scorer)

    # ---- document construction ---------------------------------------------

    def _index_one_file(self, rel_path: str, abs_path: Path, mtime: float, size: int) -> list[str]:
        """Read *abs_path* once, feed it to CodeIntel, and index its retrieval documents.

        Returns the list of BM25 document ids produced for this file (may be
        empty, e.g. a file with only whitespace/no tokenizable content).
        """
        content = self._read_file_safe(abs_path)
        if content is None:
            return []

        # Same content handed to CodeIntel as was just read here — CodeIntel
        # never re-reads the file to derive its structural facts.
        self._code_intel.index_content(rel_path, content, mtime=mtime, size=size)

        file_info = self._code_intel.get_file(rel_path)
        language = file_info.language if file_info is not None else None
        symbols = self._code_intel.get_symbols(rel_path)

        docs = build_retrieval_documents(rel_path, content, symbols, language=language)

        doc_ids: list[str] = []
        for doc in docs:
            tokens = tokenize(doc.text)
            if not tokens:
                continue
            self._scorer.add_document(doc.doc_id, tokens)
            # Body text is only ever needed transiently for tokenizing; the
            # stored metadata never retains it (see class docstring).
            self._documents[doc.doc_id] = replace(doc, text="")
            doc_ids.append(doc.doc_id)
        return doc_ids

    def _remove_file_documents(self, rel_path: str) -> None:
        """Remove every BM25 document + metadata entry owned by *rel_path*."""
        record = self._files.get(rel_path)
        if record is None:
            return
        for doc_id in record.doc_ids:
            self._scorer.remove_document(doc_id)
            self._documents.pop(doc_id, None)

    # ---- build / refresh -----------------------------------------------------

    def build(self) -> None:
        """Build (or rebuild) the index from scratch.

        Idempotent — calling this again fully replaces the index.
        The resulting index is persisted to disk.
        """
        self._scorer = BM25Scorer()
        self._files = {}
        self._documents = {}

        collected = self._collect()

        for rel_str, (abs_path, mtime, size) in collected.items():
            doc_ids = self._index_one_file(rel_str, abs_path, mtime, size)
            self._files[rel_str] = FileRecord(abs_path=abs_path, mtime=mtime, size=size, doc_ids=doc_ids)

        self._built = True
        self._save_cache()

    def refresh(self) -> None:
        """Incrementally update the index based on mtime/size changes.

        Called on every search after the first build. Fast for small changes.
        The updated index is persisted to disk.

        Stale-file eviction (removing files no longer seen) only happens
        when this refresh's inventory is complete — a partial inventory's
        silence about a path is not proof the file was deleted, so unseen
        cached files/documents are left untouched (mirrors
        ``CodeIntelIndex._refresh_full``'s truthfulness invariant).
        """
        current = self._collect()
        current_keys = set(current.keys())
        old_keys = set(self._files.keys())

        if not self._index_partial:
            for rel_str in old_keys - current_keys:
                self._remove_file_documents(rel_str)
                del self._files[rel_str]

        for rel_str in current_keys:
            abs_path, new_mtime, new_size = current[rel_str]
            existing = self._files.get(rel_str)

            if existing is not None:
                if abs(new_mtime - existing.mtime) < 0.001 and new_size == existing.size:
                    continue  # unchanged — skip
                self._remove_file_documents(rel_str)

            doc_ids = self._index_one_file(rel_str, abs_path, new_mtime, new_size)
            self._files[rel_str] = FileRecord(abs_path=abs_path, mtime=new_mtime, size=new_size, doc_ids=doc_ids)

        self._save_cache()

    # ---- search --------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> dict:
        """Search the codebase for documents relevant to *query*.

        Builds the index on first call, then refreshes incrementally.

        Args:
            query: Natural language or keyword query.
            top_k: Maximum number of results to return.

        Returns:
            Dict with keys: ``ok``, ``query``, ``results`` (list of dicts with
            ``path``, ``score``, ``snippet``, plus truthful structural metadata
            — ``start_line``, ``end_line``, ``symbol``, ``symbol_kind``,
            ``parent``, ``chunk_kind``), ``indexed_file_count``,
            ``indexed_document_count``, ``indexed_term_count``, ``partial``.
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
                "indexed_file_count": len(self._files),
                "indexed_document_count": self._scorer.doc_count,
                "indexed_term_count": self._scorer.term_count,
                "partial": self._index_partial,
            }

        raw_results = self._scorer.search(query_tokens, top_k=top_k)

        results: list[dict] = []
        for doc_id, score in raw_results:
            doc = self._documents.get(doc_id)
            if doc is None:
                continue
            record = self._files.get(doc.path)
            abs_path = record.abs_path if record is not None else None
            snippet = self._extract_snippet(abs_path, doc.start_line, doc.end_line)
            results.append(
                {
                    "path": doc.path,
                    "score": round(score, 4),
                    "snippet": snippet,
                    "start_line": doc.start_line,
                    "end_line": doc.end_line,
                    "symbol": doc.symbol,
                    "symbol_kind": doc.symbol_kind,
                    "parent": doc.parent,
                    "chunk_kind": doc.chunk_kind,
                }
            )

        return {
            "ok": True,
            "query": query,
            "results": results,
            "indexed_file_count": len(self._files),
            "indexed_document_count": self._scorer.doc_count,
            "indexed_term_count": self._scorer.term_count,
            "partial": self._index_partial,
        }

    @staticmethod
    def _extract_snippet(file_path: Path | None, start_line: int, end_line: int) -> str:
        """Extract the snippet for the winning document's own bounded range.

        Re-reads *file_path* (rather than retaining source in memory or on
        disk) and slices exactly the document's ``[start_line, end_line]``
        range — no arbitrary line-hunting across the whole file.

        Args:
            file_path: Absolute path to the file, or None.
            start_line: 1-indexed inclusive start of the document's range.
            end_line: 1-indexed inclusive end of the document's range.

        Returns:
            A text snippet, capped at 500 characters.
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

        slice_lines = lines[max(0, start_line - 1) : end_line]
        snippet = "\n".join(slice_lines) if slice_lines else "\n".join(lines[:3])

        if len(snippet) > _SNIPPET_MAX_CHARS:
            snippet = snippet[: _SNIPPET_MAX_CHARS - 3] + "..."

        return snippet

    # ---- root management -----------------------------------------------------

    def set_workspace_root(self, root: Path) -> None:
        """Change the workspace root and reset the index.

        Does not replace the injected ``CodeIntelIndex`` — callers that own a
        shared instance (e.g. ``ToolRegistry``) are responsible for pointing
        this index at a fresh ``CodeIntelIndex`` for the new root themselves
        (typically by constructing a new ``CodebaseIndex`` altogether).

        Args:
            root: New workspace root directory.
        """
        self._root = root.resolve()
        self._scorer = BM25Scorer()
        self._files = {}
        self._documents = {}
        self._built = False
