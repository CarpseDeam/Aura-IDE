"""Filesystem and repository read tool schemas."""
from __future__ import annotations

from typing import Any

CORE_READ_TOOL_DEFS: list[dict[str, Any]] = [
    {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read one known UTF-8 text file from the workspace, or several known files in one "
                    "coherent evidence batch. For a single file, pass 'path' — by default returns the "
                    "full contents (capped at 200KB); pass offset/limit to read a specific window of "
                    "lines instead, far more context-efficient than re-reading a whole file when you "
                    "already know which lines you need, and it can reach any line in a file too large "
                    "to return in full. For several known files, pass 'paths' (an array of "
                    "workspace-relative paths) instead of 'path' — offset/limit do not apply to that "
                    "shape. Every requested path in a 'paths' call comes back with metadata even when "
                    "its content did not fully fit: path, file_size, content_hash, line_count, status "
                    "(complete | summarized | truncated | omitted | error), reason, included_range, "
                    "and continuation (the exact follow-up call for the rest). Small files are returned "
                    "in full; large files return a bounded head slice plus a structural outline. "
                    "Pass exactly one of 'path' or 'paths'. "
                    "This reads specified paths; it does not discover files. "
                    "All paths MUST be relative to the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path for a single file, e.g. 'scripts/player.gd'.",
                        },
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "description": (
                                "Workspace-relative paths for several known files to read in one "
                                "evidence batch, e.g. ['src/main.py', 'README.md']. Use this instead of "
                                "'path' when several known files are needed."
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Optional first line to read (1-based, inclusive). Only valid with 'path'.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional number of lines to read starting at offset. Only valid with 'path'.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "read_task_context",
                "description": (
                    "Read a compact, read-only task context packet in one call. "
                    "Use this when you need several file summaries, query hits, symbol hits, "
                    "test hints, or dependency hints before planning or editing. "
                    "No files are modified. Output context is capped by max_chars."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional workspace-relative file paths to summarize.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional natural-language or keyword query for bounded workspace text hits.",
                        },
                        "symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional symbol names to locate with word-boundary matching.",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "Maximum characters in the returned context string. Default: 16000.",
                            "default": 16000,
                        },
                        "include_dependents": {
                            "type": "boolean",
                            "description": "Include dependency/dependent hints for requested files when available.",
                            "default": True,
                        },
                        "include_tests": {
                            "type": "boolean",
                            "description": "Include likely test file hints for requested files.",
                            "default": True,
                        },
                    },
                    "required": [],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "list_directory",
                "description": (
                    "List files and subdirectories of a workspace directory. Hidden files and "
                    "build/cache directories (.git, .venv, __pycache__, .import) are excluded. "
                    "Use '.' for the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative directory path. Use '.' for the root.",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "glob",
                "description": (
                    "Discover workspace paths by finding files and directories matching a glob pattern relative to the workspace "
                    "root. '*' matches within one path segment, '**' recurses: use '**/*.gd' or "
                    "'scripts/**/*.py' to walk the tree, and '*' or 'aura/gui/*' to list one "
                    "directory's immediate contents. Returns 'matches' (files) and 'directories' "
                    "(trailing slash), capped at 200 entries total. Use read_file after discovery to "
                    "retrieve file contents — pass 'paths' with several files to gather them in one call."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "Glob pattern, e.g. '**/*.gd' or 'res/**/*.tscn'.",
                        }
                    },
                    "required": ["pattern"],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "read_file_outline",
                "description": (
                    "Read a file's structural outline — class names, function signatures, "
                    "and import/extends lines — without loading the full content. "
                    "Uses AST parsing for Python files."
                    "Returns a compact text summary plus structured data. "
                    "Use this when you need to understand a file's structure without "
                    "reading every line. The path argument MUST be relative to the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path, e.g. 'scripts/player.gd'.",
                        }
                    },
                    "required": ["path"],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "read_file_range",
                "description": (
                    "Read a specific range of lines from a file (1-based, inclusive). "
                    "Use this after a bounded read_file (offset and limit) tells you which line "
                    "numbers to inspect — it is far more context-efficient than re-reading the whole "
                    "file when you only need a specific function or section. "
                    "Also use this to recover when a previous read_file result was truncated: "
                    "the truncation marker tells you the original length so you can calculate "
                    "which line ranges remain unread. "
                    "Returns the selected lines plus the whole-file content_hash and file_size "
                    "for the exact file version the range came from. "
                    "The path argument MUST be relative to the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path, e.g. 'aura/config.py'.",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "First line to read (1-based, inclusive).",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "Last line to read (1-based, inclusive).",
                        },
                    },
                    "required": ["path", "start_line", "end_line"],
                },
            },
        }
]
