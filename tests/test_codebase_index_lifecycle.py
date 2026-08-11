"""Lifecycle invariants for the structure-aware CodebaseIndex.

Covers: one file owning multiple BM25 documents while indexed_file_count
stays a file count, update/delete document-ownership churn, partial- vs
complete-inventory deletion semantics, cache schema round-tripping, and
that CodebaseIndex hands CodeIntel already-read content instead of causing
a second disk read.
"""

from __future__ import annotations

import os
from pathlib import Path

import aura.code_intel  # noqa: F401 — triggers adapter registration
import aura.repository_inventory as repo_inv
from aura.code_intel.index import CodeIntelIndex
from aura.codebase_index.indexer import CodebaseIndex


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


_MULTI_DOC_SOURCE = (
    "import os\n"
    "\n"
    "class Widget:\n"
    "    def render(self):\n"
    "        return 'a'\n"
    "\n"
    "    def close(self):\n"
    "        return 'b'\n"
)


def test_one_file_can_own_multiple_documents_while_file_count_is_one(tmp_path: Path) -> None:
    _write(tmp_path / "widget.py", _MULTI_DOC_SOURCE)

    index = CodebaseIndex(tmp_path)
    index.build()

    assert index.file_count == 1
    record = index._files["widget.py"]
    assert len(record.doc_ids) > 1
    assert index._scorer.doc_count == len(record.doc_ids)

    result = index.search("render")
    assert result["ok"] is True
    assert result["indexed_file_count"] == 1
    assert result["indexed_document_count"] == len(record.doc_ids)
    assert any(r["symbol"] == "render" for r in result["results"])


def test_updating_a_file_removes_its_previous_document_ids(tmp_path: Path) -> None:
    path = tmp_path / "widget.py"
    _write(path, "class Widget:\n    def render(self):\n        return 1\n")

    index = CodebaseIndex(tmp_path)
    index.build()
    old_doc_ids = set(index._files["widget.py"].doc_ids)
    assert old_doc_ids

    _write(path, "def helper():\n    return 42\n")
    st = path.stat()
    os.utime(path, (st.st_mtime + 5, st.st_mtime + 5))

    index.refresh()

    new_doc_ids = set(index._files["widget.py"].doc_ids)
    assert new_doc_ids
    assert old_doc_ids.isdisjoint(new_doc_ids)
    for doc_id in old_doc_ids:
        assert doc_id not in index._documents
        assert doc_id not in index._scorer.to_dict()["doc_lengths"]

    result = index.search("helper")
    assert any(r["symbol"] == "helper" for r in result["results"])
    result_old = index.search("render")
    assert all(r.get("symbol") != "render" for r in result_old["results"])


def test_complete_inventory_removes_stale_files_and_documents(tmp_path: Path) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "def a(): pass\n")
    _write(b, "def b(): pass\n")

    index = CodebaseIndex(tmp_path)
    index.build()
    assert "b.py" in index._files
    b_doc_ids = set(index._files["b.py"].doc_ids)

    b.unlink()
    index.refresh()

    assert index._index_partial is False
    assert "b.py" not in index._files
    for doc_id in b_doc_ids:
        assert doc_id not in index._documents


def test_partial_inventory_does_not_evict_unseen_cached_files(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "a.py"
    b = tmp_path / "b.py"
    _write(a, "def a(): pass\n")
    _write(b, "def b(): pass\n")

    index = CodebaseIndex(tmp_path)
    index.build()
    assert set(index._files) == {"a.py", "b.py"}
    b_doc_ids = set(index._files["b.py"].doc_ids)

    # A partial inventory that only saw "a.py" — absence of "b.py" here must
    # not be treated as proof that "b.py" was deleted.
    full = repo_inv.build_inventory(tmp_path)
    only_a = tuple(f for f in full.files if f.rel_path == "a.py")
    partial = repo_inv.RepositoryInventory(
        root=tmp_path, files=only_a, source="git", complete=False, incomplete_reason="test-forced"
    )
    monkeypatch.setattr("aura.codebase_index.indexer.build_inventory", lambda root: partial)

    index.refresh()

    assert index._index_partial is True
    assert "b.py" in index._files
    assert set(index._files["b.py"].doc_ids) == b_doc_ids
    for doc_id in b_doc_ids:
        assert doc_id in index._documents


def test_cache_schema_round_trips_file_and_document_ownership(tmp_path: Path) -> None:
    _write(tmp_path / "widget.py", _MULTI_DOC_SOURCE)

    first = CodebaseIndex(tmp_path)
    first.build()
    doc_ids_before = set(first._files["widget.py"].doc_ids)
    assert len(doc_ids_before) > 1

    second = CodebaseIndex(tmp_path)
    assert second.built  # restored from disk cache without a fresh build()

    assert set(second._files["widget.py"].doc_ids) == doc_ids_before
    assert second._scorer.doc_count == first._scorer.doc_count
    for doc_id in doc_ids_before:
        assert doc_id in second._documents
        assert second._documents[doc_id].symbol == first._documents[doc_id].symbol
        assert second._documents[doc_id].chunk_kind == first._documents[doc_id].chunk_kind

    result = second.search("render")
    assert result["ok"] is True
    assert any(r["symbol"] == "render" for r in result["results"])


def test_shared_content_ingestion_reads_changed_source_only_once(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "app.py"
    _write(path, "def only_fn(): pass\n")

    read_calls: list[Path] = []
    original_read_text = Path.read_text

    def counting_read_text(self, *args, **kwargs):
        if self == path:
            read_calls.append(self)
        return original_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    code_intel = CodeIntelIndex(tmp_path)
    index = CodebaseIndex(tmp_path, code_intel_index=code_intel)
    index.build()

    assert len(read_calls) == 1
    # CodeIntel actually got the structural facts from the shared content,
    # not from a re-read/re-parse of its own.
    names = [s.name for s in code_intel.get_symbols("app.py")]
    assert "only_fn" in names
