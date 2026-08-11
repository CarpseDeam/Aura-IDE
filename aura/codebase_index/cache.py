"""Disk cache serialization for :class:`~aura.codebase_index.indexer.CodebaseIndex`.

Owns the on-disk schema for file/document ownership plus BM25 scorer state.
``CodebaseIndex`` owns the build/refresh/search lifecycle; this module only
reads and writes its persisted state.

Schema v2: one file may own many BM25 documents (see
``aura/codebase_index/documents.py``), so the cache tracks which document
ids belong to which file plus each document's structural metadata. Raw
source text is never persisted — document bodies are re-sliced from disk
by line range when a snippet is needed.

Any incompatibility (missing keys, wrong schema version, wrong workspace
root, corrupt JSON) is treated as a cache miss: the caller rebuilds from
scratch rather than trusting a partially-usable cache.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from aura.codebase_index.bm25 import BM25Scorer
from aura.codebase_index.documents import RetrievalDocument

#: Bumped whenever the on-disk shape changes. A cache written by a prior
#: version is never partially trusted — it is ignored and rebuilt cleanly.
CACHE_SCHEMA_VERSION = 2

_MTIME_EPSILON = 0.001


@dataclass
class FileRecord:
    """One indexed file: identity, staleness metadata, and document ownership."""

    abs_path: Path
    mtime: float
    size: int
    doc_ids: list[str] = field(default_factory=list)


@dataclass
class CacheData:
    files: dict[str, FileRecord]
    documents: dict[str, RetrievalDocument]
    scorer: BM25Scorer


def load_cache(cache_path: Path, workspace_root: Path) -> CacheData | None:
    """Load and validate a cache file for *workspace_root*, or None.

    Files whose on-disk mtime no longer matches the cached value are
    dropped. Any document whose owning file didn't survive validation, or
    whose own metadata is malformed, is dropped from both the document map
    and the scorer so the two never disagree about what's indexed.
    """
    if not cache_path.is_file():
        return None

    try:
        raw = cache_path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(data, dict):
        return None
    if data.get("version") != CACHE_SCHEMA_VERSION:
        return None
    if data.get("workspace_root") != str(workspace_root):
        return None
    if not all(k in data for k in ("files", "documents", "scorer")):
        return None

    try:
        scorer = BM25Scorer.from_dict(data["scorer"])
    except (KeyError, TypeError):
        return None

    fresh_files: dict[str, FileRecord] = {}
    for rel_str, entry in data.get("files", {}).items():
        record = _parse_file_entry(entry)
        if record is None:
            continue
        if not record.abs_path.is_file():
            continue
        try:
            current_mtime = record.abs_path.stat().st_mtime
        except OSError:
            continue
        if abs(current_mtime - record.mtime) >= _MTIME_EPSILON:
            continue
        fresh_files[rel_str] = record

    valid_doc_ids: set[str] = set()
    for record in fresh_files.values():
        valid_doc_ids.update(record.doc_ids)

    # Anything the scorer still holds outside a fresh file's doc_ids is
    # stale (belonged to a since-changed/removed file, or was orphaned) —
    # drop it so search results only ever come from files we trust.
    scored_doc_ids = set(data["scorer"].get("doc_lengths", {}).keys())
    for stale_doc_id in scored_doc_ids - valid_doc_ids:
        scorer.remove_document(stale_doc_id)

    documents: dict[str, RetrievalDocument] = {}
    for doc_id, entry in data.get("documents", {}).items():
        if doc_id not in valid_doc_ids:
            continue
        doc = _parse_document_entry(doc_id, entry)
        if doc is not None:
            documents[doc_id] = doc

    # Final consistency pass: a file whose doc_ids aren't fully backed by
    # document metadata is corrupt in a way we can't safely trust in part —
    # treat it as unseen so the next refresh/build re-indexes it cleanly.
    final_files: dict[str, FileRecord] = {}
    for rel_str, record in fresh_files.items():
        if all(doc_id in documents for doc_id in record.doc_ids):
            final_files[rel_str] = record
        else:
            for doc_id in record.doc_ids:
                documents.pop(doc_id, None)
                scorer.remove_document(doc_id)

    return CacheData(files=final_files, documents=documents, scorer=scorer)


def _parse_file_entry(entry: object) -> FileRecord | None:
    if not isinstance(entry, dict):
        return None
    try:
        return FileRecord(
            abs_path=Path(entry["abs_path"]),
            mtime=float(entry["mtime"]),
            size=int(entry["size"]),
            doc_ids=[str(d) for d in entry["doc_ids"]],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_document_entry(doc_id: str, entry: object) -> RetrievalDocument | None:
    if not isinstance(entry, dict):
        return None
    try:
        return RetrievalDocument(
            doc_id=doc_id,
            path=entry["path"],
            language=entry.get("language"),
            chunk_kind=entry["chunk_kind"],
            symbol=entry.get("symbol"),
            symbol_kind=entry.get("symbol_kind"),
            parent=entry.get("parent"),
            start_line=int(entry["start_line"]),
            end_line=int(entry["end_line"]),
            text="",
        )
    except (KeyError, TypeError, ValueError):
        return None


def save_cache(
    cache_path: Path,
    workspace_root: Path,
    files: dict[str, FileRecord],
    documents: dict[str, RetrievalDocument],
    scorer: BM25Scorer,
) -> None:
    """Persist cache state atomically.

    Failures are silently ignored — the cache is purely an optimisation and
    must never crash the caller.
    """
    data = {
        "version": CACHE_SCHEMA_VERSION,
        "workspace_root": str(workspace_root),
        "files": {
            rel: {
                "abs_path": str(record.abs_path),
                "mtime": record.mtime,
                "size": record.size,
                "doc_ids": list(record.doc_ids),
            }
            for rel, record in files.items()
        },
        "documents": {
            doc_id: {
                "path": doc.path,
                "language": doc.language,
                "chunk_kind": doc.chunk_kind,
                "symbol": doc.symbol,
                "symbol_kind": doc.symbol_kind,
                "parent": doc.parent,
                "start_line": doc.start_line,
                "end_line": doc.end_line,
            }
            for doc_id, doc in documents.items()
        },
        "scorer": scorer.to_dict(),
    }
    tmp_path = cache_path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        tmp_path.replace(cache_path)
    except OSError:
        pass  # cache saves must NEVER crash the caller
