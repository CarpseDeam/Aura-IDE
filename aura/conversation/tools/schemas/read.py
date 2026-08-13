"""Filesystem and repository read tool schemas."""
from __future__ import annotations

from typing import Any

CORE_READ_TOOL_DEFS: list[dict[str, Any]] = [
    {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "Read one known UTF-8 text file from the workspace, or one targeted line window "
                    "from that file. By default returns the full contents "
                    "(capped at 200KB). Pass offset/limit to read a specific window of lines instead — "
                    "far more context-efficient than re-reading a whole file when you already know which "
                    "lines you need, and it can reach any line in a file too large to return in full. "
                    "When several known files are needed, use read_files to gather them in one evidence batch. "
                    "This reads the specified path; it does not discover files. "
                    "The path argument MUST be relative to the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path, e.g. 'scripts/player.gd'.",
                        },
                        "offset": {
                            "type": "integer",
                            "description": "Optional first line to read (1-based, inclusive).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional number of lines to read starting at offset.",
                        },
                    },
                    "required": ["path"],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "read_files",
                "description": (
                    "Read several already-known relevant files in one coherent evidence batch. "
                    "Use this to gather one evidence packet before the next reasoning step; it reads "
                    "specified paths rather than performing repository discovery. "
                    "EVERY requested path always comes back with metadata, even when its "
                    "content did not fit: path, file_size, content_hash, line_count, "
                    "status (complete | summarized | truncated | omitted | error), reason, "
                    "included_range, and continuation (the exact follow-up call for the rest). "
                    "Small files are returned in full; large files return a bounded head slice "
                    "plus a structural outline. When status is not 'complete', use the "
                    "continuation call rather than re-issuing the same read_files. "
                    "All paths must be relative to the workspace root."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "paths": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Array of workspace-relative file paths to read, e.g. ['src/main.py', 'README.md'].",
                        },
                    },
                    "required": ["paths"],
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
                    "(trailing slash), capped at 200 entries total. Use read_file or read_files "
                    "after discovery to retrieve file contents."
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
