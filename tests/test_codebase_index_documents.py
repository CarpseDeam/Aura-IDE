"""Structural partitioning invariants for retrieval-document construction.

The key invariant under test: the same source body must never be indexed
twice merely because structural ranges nest (a class document must not
duplicate its methods' bodies), oversized chunks split into bounded
non-overlapping parts, and source outside any structural definition becomes
fallback documents.
"""

from __future__ import annotations

from aura.code_intel.python_adapter import PythonAdapter
from aura.codebase_index.documents import build_retrieval_documents


def _symbols(source: str) -> list:
    return PythonAdapter().symbols("app.py", source)


def _assert_non_overlapping_and_sorted(docs) -> None:
    for prev, nxt in zip(docs, docs[1:]):
        assert prev.end_line < nxt.start_line, (prev, nxt)


def test_class_and_methods_do_not_duplicate_bodies() -> None:
    source = (
        "class Target:\n"
        "    def method_a(self, x):\n"
        "        return x\n"
        "\n"
        "    def method_b(self):\n"
        "        return 2\n"
    )
    syms = _symbols(source)

    docs = build_retrieval_documents("app.py", source, syms, language="python")
    _assert_non_overlapping_and_sorted(docs)

    # The method bodies must appear exactly once each, and never inside
    # the class's own document text.
    method_a_doc = next(d for d in docs if d.symbol == "method_a")
    method_b_doc = next(d for d in docs if d.symbol == "method_b")
    assert "return x" in method_a_doc.text
    assert "return 2" in method_b_doc.text

    class_docs = [d for d in docs if d.symbol == "Target" and d.symbol_kind == "class"]
    for cd in class_docs:
        assert "return x" not in cd.text
        assert "return 2" not in cd.text

    # No line of source appears in more than one document.
    all_lines = [ln for d in docs for ln in range(d.start_line, d.end_line + 1)]
    assert len(all_lines) == len(set(all_lines))


def test_class_owned_gap_between_methods_becomes_its_own_document() -> None:
    source = (
        "class Target:\n"
        "    def method_a(self):\n"
        "        return 1\n"
        "\n"
        "    # a plain comment between methods, owned by the class\n"
        "    def method_b(self):\n"
        "        return 2\n"
    )
    syms = _symbols(source)
    docs = build_retrieval_documents("app.py", source, syms, language="python")
    _assert_non_overlapping_and_sorted(docs)

    gap_docs = [
        d for d in docs if d.symbol == "Target" and d.symbol_kind == "class" and "comment between" in d.text
    ]
    assert len(gap_docs) == 1
    assert gap_docs[0].chunk_kind == "class"
    assert gap_docs[0].parent is None


def test_source_outside_definitions_becomes_fallback_documents() -> None:
    source = (
        "import os\n"
        "\n"
        "def helper():\n"
        "    return 1\n"
        "\n"
        "TRAILING = 1\n"
    )
    syms = _symbols(source)
    docs = build_retrieval_documents("app.py", source, syms, language="python")
    _assert_non_overlapping_and_sorted(docs)

    fallback_docs = [d for d in docs if d.chunk_kind == "fallback"]
    assert fallback_docs, "expected at least one fallback document"
    for d in fallback_docs:
        assert d.symbol is None
        assert d.symbol_kind is None

    joined_fallback = "\n".join(d.text for d in fallback_docs)
    assert "import os" in joined_fallback
    # TRAILING is a variable, not a structural boundary -> stays fallback.
    assert "TRAILING = 1" in joined_fallback

    helper_doc = next(d for d in docs if d.symbol == "helper")
    assert helper_doc.chunk_kind == "function"
    assert "return 1" in helper_doc.text


def test_variables_are_ignored_as_partition_boundaries() -> None:
    source = "x = 1\ny = 2\nz = 3\n"
    syms = _symbols(source)
    assert all(s.kind == "variable" for s in syms)

    docs = build_retrieval_documents("app.py", source, syms, language="python")

    # No structural symbols at all -> the whole file is one fallback region.
    assert len(docs) == 1
    assert docs[0].chunk_kind == "fallback"
    assert docs[0].text == "x = 1\ny = 2\nz = 3"


def test_oversized_document_splits_into_bounded_nonoverlapping_parts() -> None:
    class _FakeSymbol:
        def __init__(self, name, kind, line, end_line, parent=None):
            self.name = name
            self.kind = kind
            self.line = line
            self.end_line = end_line
            self.parent = parent

    body_lines = [f"    stmt_{i} = {i}" for i in range(450)]
    source = "def big():\n" + "\n".join(body_lines) + "\n"
    total_lines = len(source.splitlines())
    sym = _FakeSymbol("big", "function", 1, total_lines)

    docs = build_retrieval_documents(
        "big.py", source, [sym], language="python", max_document_lines=200
    )

    assert len(docs) == 3  # 200 + 200 + remainder
    for d in docs:
        assert d.end_line - d.start_line + 1 <= 200
        assert d.symbol == "big"
        assert d.symbol_kind == "function"

    _assert_non_overlapping_and_sorted(docs)
    assert docs[0].start_line == 1
    assert docs[-1].end_line == total_lines


def test_document_ids_are_deterministic() -> None:
    source = "def helper():\n    return 1\n"
    syms = _symbols(source)

    docs_first = build_retrieval_documents("app.py", source, syms, language="python")
    docs_second = build_retrieval_documents("app.py", source, syms, language="python")

    assert [d.doc_id for d in docs_first] == [d.doc_id for d in docs_second]
