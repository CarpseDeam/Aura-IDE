"""Structure-aware retrieval documents for the BM25 codebase index.

Owns turning one file's source plus its CodeIntel-derived structural
symbols into the non-overlapping, size-bounded chunks that
:class:`~aura.codebase_index.indexer.CodebaseIndex` actually indexes.

The key invariant enforced here: **the same source body is never emitted
twice merely because structural ranges nest.** A method's body belongs to
the method's document, not to its enclosing class's document as well. An
enclosing definition (e.g. a class) only ever owns the source *outside* its
contained definitions — header, class-level attributes, gaps between
methods. Source outside any structural definition (imports, module-level
code, top-level statements between functions) becomes fallback documents.

This module does not walk repositories, parse source, score documents, or
touch cache IO — see ``aura/codebase_index/indexer.py`` and
``aura/codebase_index/cache.py`` for those responsibilities.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

#: Chunk kinds treated as structural partition boundaries. Everything else
#: a CodeIntel adapter emits (variables, imports, exports, ...) is ignored
#: as a boundary — it stays part of whatever fallback/enclosing chunk it
#: falls inside.
_STRUCTURAL_KINDS = frozenset({"class", "function", "method"})

#: Centralized line-count ceiling for one retrieval document. A structural
#: chunk (or a fallback gap) larger than this is split deterministically
#: into consecutive, non-overlapping parts — never overlapping windows.
MAX_DOCUMENT_LINES = 200


@dataclass(frozen=True, slots=True)
class RetrievalDocument:
    """One indexable BM25 unit: a bounded, non-overlapping slice of a file."""

    doc_id: str
    path: str  # workspace-relative, posix
    language: str | None
    chunk_kind: str  # "class" | "function" | "method" | "fallback"
    symbol: str | None
    symbol_kind: str | None
    parent: str | None
    start_line: int  # 1-indexed, inclusive
    end_line: int  # 1-indexed, inclusive
    text: str


@dataclass(frozen=True, slots=True)
class _RawSpec:
    """Pre-split, pre-sliced partition piece — internal to this module."""

    start_line: int
    end_line: int
    chunk_kind: str
    symbol: str | None
    symbol_kind: str | None
    parent: str | None


@dataclass
class _Node:
    """One structural symbol plus its directly-nested structural children."""

    start: int
    end: int
    name: str
    kind: str
    parent: str | None
    children: list["_Node"]


def make_doc_id(path: str, start_line: int, end_line: int, chunk_kind: str) -> str:
    """Deterministic, debuggable document id for one file range."""
    return f"{path}::{chunk_kind}:{start_line}-{end_line}"


def build_retrieval_documents(
    path: str,
    content: str,
    symbols: list[Any],
    *,
    language: str | None = None,
    max_document_lines: int = MAX_DOCUMENT_LINES,
) -> list[RetrievalDocument]:
    """Partition *content* into non-overlapping retrieval documents.

    *symbols* are the file's ``SymbolInfo`` list from CodeIntel. Only
    structural kinds (class/function/method) with truthful ``line``/``end_line``
    participate as partition boundaries; anything else (variables, imports,
    symbols an adapter could not truthfully range) is ignored as a boundary —
    the source it sits in simply becomes part of a fallback or enclosing
    chunk. Source lines outside every structural definition become fallback
    documents.
    """
    lines = content.splitlines()
    total_lines = len(lines)
    if total_lines == 0:
        return []

    structural = [
        s
        for s in symbols
        if getattr(s, "kind", None) in _STRUCTURAL_KINDS
        and getattr(s, "line", None) is not None
        and getattr(s, "end_line", None) is not None
        and s.line <= s.end_line
    ]

    roots = _build_forest(structural, total_lines)

    specs: list[_RawSpec] = []
    covered: list[tuple[int, int]] = []
    for root in sorted(roots, key=lambda n: n.start):
        specs.extend(_partition_node(root))
        covered.append((root.start, root.end))

    for gap_start, gap_end in _subtract_intervals(1, total_lines, covered):
        specs.append(
            _RawSpec(
                start_line=gap_start,
                end_line=gap_end,
                chunk_kind="fallback",
                symbol=None,
                symbol_kind=None,
                parent=None,
            )
        )

    bounded: list[_RawSpec] = []
    for spec in specs:
        bounded.extend(_split_oversized(spec, max_document_lines))

    bounded.sort(key=lambda s: s.start_line)

    documents: list[RetrievalDocument] = []
    for spec in bounded:
        text = "\n".join(lines[spec.start_line - 1 : spec.end_line])
        documents.append(
            RetrievalDocument(
                doc_id=make_doc_id(path, spec.start_line, spec.end_line, spec.chunk_kind),
                path=path,
                language=language,
                chunk_kind=spec.chunk_kind,
                symbol=spec.symbol,
                symbol_kind=spec.symbol_kind,
                parent=spec.parent,
                start_line=spec.start_line,
                end_line=spec.end_line,
                text=text,
            )
        )
    return documents


# ---- structural forest construction ----------------------------------------


def _build_forest(structural: list[Any], total_lines: int) -> list[_Node]:
    """Build a containment forest from a flat, truthfully-ranged symbol list.

    O(n log n): sorts once, then a single stack pass assigns each symbol to
    its innermost enclosing symbol (or promotes it to a root) — no per-line
    or per-symbol-pair quadratic scan.
    """
    clamped = []
    for s in structural:
        start = max(1, s.line)
        end = min(total_lines, s.end_line)
        if start > end:
            continue
        clamped.append((start, end, s))

    # Parents sort before their children: same start -> larger range first.
    ordered = sorted(clamped, key=lambda t: (t[0], -t[1]))

    roots: list[_Node] = []
    stack: list[_Node] = []
    for start, end, sym in ordered:
        node = _Node(
            start=start,
            end=end,
            name=sym.name,
            kind=sym.kind,
            parent=getattr(sym, "parent", None),
            children=[],
        )
        while stack and stack[-1].end < node.start:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
    return roots


def _partition_node(node: _Node) -> list[_RawSpec]:
    """This symbol's own (non-child-owned) ranges, plus all descendant specs."""
    specs: list[_RawSpec] = []
    covered: list[tuple[int, int]] = []
    for child in sorted(node.children, key=lambda c: c.start):
        specs.extend(_partition_node(child))
        covered.append((child.start, child.end))

    for own_start, own_end in _subtract_intervals(node.start, node.end, covered):
        specs.append(
            _RawSpec(
                start_line=own_start,
                end_line=own_end,
                chunk_kind=node.kind,
                symbol=node.name,
                symbol_kind=node.kind,
                parent=node.parent,
            )
        )
    return specs


def _subtract_intervals(
    start: int, end: int, covered: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Ranges within [start, end] not covered by any interval in *covered*.

    *covered* need not be pre-sorted or pre-merged.
    """
    if start > end:
        return []
    gaps: list[tuple[int, int]] = []
    cursor = start
    for c_start, c_end in sorted(covered):
        if c_start > cursor:
            gaps.append((cursor, min(c_start - 1, end)))
        cursor = max(cursor, c_end + 1)
        if cursor > end:
            break
    if cursor <= end:
        gaps.append((cursor, end))
    return gaps


def _split_oversized(spec: _RawSpec, max_lines: int) -> list[_RawSpec]:
    """Split *spec* into consecutive, non-overlapping parts bounded by *max_lines*."""
    span = spec.end_line - spec.start_line + 1
    if span <= max_lines:
        return [spec]

    parts: list[_RawSpec] = []
    cursor = spec.start_line
    while cursor <= spec.end_line:
        part_end = min(cursor + max_lines - 1, spec.end_line)
        parts.append(replace(spec, start_line=cursor, end_line=part_end))
        cursor = part_end + 1
    return parts
