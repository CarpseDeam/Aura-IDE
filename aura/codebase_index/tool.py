"""Callable search_codebase tool for the tool registry."""

from __future__ import annotations

from pathlib import Path


from aura.codebase_index.indexer import CodebaseIndex


def search_codebase(
    workspace_root: Path,
    query: str,
    top_k: int = 5,
    _index: CodebaseIndex | None = None,
) -> dict:
    """Search the workspace codebase using a local BM25 inverted index.

    Use this to recall files, functions, or code patterns that may have been
    pruned from the conversation history. The index is built lazily on first
    call and incrementally refreshed on subsequent calls.

    Args:
        workspace_root: Absolute path to the workspace root directory.
        query: Natural language or keyword query describing what to find.
        top_k: Maximum number of results to return (default 5).
        _index: Optional shared :class:`CodebaseIndex` instance. If None,
            a temporary one is created. Pass a shared instance to preserve
            the index across calls.

    Returns:
        Dict with keys:
        - ``ok`` (bool): Whether the search succeeded.
        - ``query`` (str): The original query.
        - ``results`` (list[dict]): Each result has ``path``, ``score``,
          ``snippet``.
        - ``indexed_file_count`` (int): Number of files in the index.
        - ``indexed_term_count`` (int): Number of unique terms in the index.
    """
    query = query.strip()
    if not query:
        return {
            "ok": False,
            "error": "query is required",
        }

    if _index is None:
        index = CodebaseIndex(workspace_root)
    else:
        index = _index

    return index.search(query, top_k=top_k)
