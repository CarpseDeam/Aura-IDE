"""Search and retrieval tool schemas."""
from __future__ import annotations

from typing import Any

SEARCH_TOOL_DEFS: list[dict[str, Any]] = [
    {
                    "type": "function",
                    "function": {
                        "name": "grep_search",
                        "description": (
                            "Search workspace file contents for a string or regex pattern. "
                            "Returns matching file paths, line numbers, the matching line content, "
                            "and the column where the match starts, plus search metadata such as the "
                            "engine used, searched file count, skipped file count, truncation, and regex hint state. "
                            "A returned line is the line as it stood on disk at search time, not a "
                            "guarantee of current content. Anchor with word boundaries for exact symbol "
                            "matches, e.g. '\\bcount_items\\b'."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "pattern": {
                                    "type": "string",
                                    "description": "The string or regex pattern to search for.",
                                },
                                "regex_mode": {
                                    "type": "boolean",
                                    "description": (
                                        "grep_search uses grep/ripgrep pattern behavior by default: pattern is treated "
                                        "as a regex, so alternation like 'foo|bar', anchors like '^def name', and "
                                        "similar grep patterns work. Pass regex_mode=false for literal text search."
                                    ),
                                    "default": True,
                                },
                                "case_sensitive": {
                                    "type": "boolean",
                                    "description": "If true, match case exactly. Default (false) is case-insensitive.",
                                    "default": False,
                                },
                                "max_results": {
                                    "type": "integer",
                                    "description": "Maximum number of matching lines to return.",
                                    "default": 50,
                                },
                                "include_pattern": {
                                    "type": "string",
                                    "description": (
                                        "Optional workspace-relative exact file path or glob pattern restricting "
                                        "which files are searched. Exact paths such as 'aura/gui/main_window.py' "
                                        "search only that file. Glob patterns such as '**/*.py' search matching "
                                        "files anywhere in the repo. Prefer '**/*.py' over '*.py' when you want "
                                        "recursive Python-only search."
                                    ),
                                },
                            },
                            "required": ["pattern"],
                        },
                    },
                },
    {
                    "type": "function",
                    "function": {
                        "name": "find_usages",
                        "description": (
                            "Find all usages of a symbol (function, variable, class, etc.) "
                            "across the workspace. Uses word-boundary matching by default "
                            "so that searching for 'count_items' will NOT match "
                            "'recount_items' or 'count_items_count'. "
                            "Essential for safe refactoring — use this before renaming a symbol "
                            "to see everywhere it is referenced."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "symbol": {
                                    "type": "string",
                                    "description": "The symbol name to search for, e.g. 'count_items'.",
                                },
                                "include_pattern": {
                                    "type": "string",
                                    "description": (
                                        "Optional glob pattern to restrict which files to search "
                                        "(e.g. '**/*.gd' to only search GDScript files)."
                                    ),
                                },
                                "max_results": {
                                    "type": "integer",
                                    "description": "Maximum number of matching lines to return. Default: 100.",
                                    "default": 100,
                                },
                                "case_sensitive": {
                                    "type": "boolean",
                                    "description": (
                                        "If true, match case exactly. Default: false (case-insensitive)."
                                    ),
                                    "default": False,
                                },
                            },
                            "required": ["symbol"],
                        },
                    },
                },
    {
                    "type": "function",
                    "function": {
                        "name": "search_codebase",
                        "description": (
                            "Ranked keyword/natural-language search over a local BM25 index. By "
                            "default it searches the active workspace; source='reference' searches "
                            "the external project explicitly authorized by the user for this turn. "
                            "The index is built from structure-aware retrieval documents: "
                            "each file is partitioned into bounded, non-overlapping source regions "
                            "using parser-derived structure where available, so a result is a "
                            "function-, class-, or block-sized region rather than a whole file. Each "
                            "result carries a root-relative path, a bounded text snippet, its "
                            "start_line/end_line, and, when a parser range backs the region, symbol, "
                            "symbol_kind, and parent. Relevance is BM25 lexical ranking over tokenized "
                            "text, not a semantic or correctness judgment. The index builds lazily on "
                            "first use and refreshes incrementally on later calls."
                        ),
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "query": {
                                    "type": "string",
                                    "description": "Natural language or keyword query, e.g. 'authentication handler', 'database migration', 'error logging setup'."
                                },
                                "top_k": {
                                    "type": "integer",
                                    "description": "Maximum number of results to return. Default: 5.",
                                    "default": 5,
                                },
                                "source": {
                                    "type": "string",
                                    "enum": ["workspace", "reference"],
                                    "description": (
                                        "Search the editable workspace or the user-authorized "
                                        "read-only external reference project. Defaults to workspace."
                                    ),
                                    "default": "workspace",
                                },
                            },
                            "required": ["query"],
                        },
                    },
                }
]
