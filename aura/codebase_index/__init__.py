"""Codebase index package — BM25-powered semantic search for the workspace."""

from __future__ import annotations

from aura.codebase_index.tool import search_codebase
from aura.codebase_index.indexer import CodebaseIndex

__all__ = ["search_codebase", "CodebaseIndex"]
