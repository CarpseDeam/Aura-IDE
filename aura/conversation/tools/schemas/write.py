"""General filesystem mutation tool schemas."""
from __future__ import annotations

from typing import Any

FILESYSTEM_WRITE_TOOL_DEFS: list[dict[str, Any]] = [
    {
            "type": "function",
            "function": {
                "name": "write_file",
                "description": (
                    "Write the given content to a workspace file, creating it or replacing it whole. "
                    "The user MUST approve every write through a diff dialog before it is applied. "
                    "Existing files are backed up before being overwritten."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path of the file to write.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Full new file content.",
                        },
                    },
                    "required": ["path", "content"],
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "delete_file",
                "description": (
                    "Delete one existing workspace file after user approval. "
                    "Directories, globs, wildcards, workspace metadata paths, and paths outside "
                    "the workspace are rejected. Existing files are backed up before deletion."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative path of the single file to delete.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional short reason for deleting this file.",
                            "default": "",
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        },
    {
            "type": "function",
            "function": {
                "name": "patch_file",
                "description": (
                    "Apply exact-text replacement hunks to one or more existing workspace files as a single "
                    "atomic, approval-gated transaction. Each hunk's old text must match its file exactly. "
                    "Every hunk, across every target file, is applied to an in-memory copy first; if any hunk "
                    "in any file is missing or ambiguous, nothing is written and the result names which file "
                    "and hunk failed. Use occurrence to disambiguate repeated exact text, or give more "
                    "surrounding context in the old block. The user sees one approval diff covering every file "
                    "in the transaction. Call this with either path+edits for one file, or files for two or "
                    "more files — not both."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Workspace-relative file path. Use with edits for a single-file patch.",
                        },
                        "edits": {
                            "type": "array",
                            "description": "Ordered exact-text replacement hunks to apply to path.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "old": {
                                        "type": "string",
                                        "description": "Exact current text block to replace.",
                                    },
                                    "new": {
                                        "type": "string",
                                        "description": "Replacement text for this hunk.",
                                    },
                                    "occurrence": {
                                        "type": "integer",
                                        "description": "Optional 1-based occurrence number when old appears more than once.",
                                        "default": 1,
                                    },
                                    "allow_multiple": {
                                        "type": "boolean",
                                        "description": "If true, replace every occurrence of old for this hunk.",
                                        "default": False,
                                    },
                                },
                                "required": ["old", "new"],
                                "additionalProperties": False,
                            },
                        },
                        "expected_file_hash": {
                            "type": "string",
                            "description": (
                                "Optional SHA-256 hex digest of the current whole file, from read_file "
                                "or read_files. When supplied, the patch is rejected if the file's content "
                                "no longer matches it. Only used with path+edits."
                            ),
                        },
                        "files": {
                            "type": "array",
                            "description": (
                                "Two or more target files patched as one transaction, in place of path+edits. "
                                "Each entry names one existing file and its own ordered hunks."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "description": "Workspace-relative file path for this target.",
                                    },
                                    "edits": {
                                        "type": "array",
                                        "description": "Ordered exact-text replacement hunks to apply to this file.",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "old": {
                                                    "type": "string",
                                                    "description": "Exact current text block to replace.",
                                                },
                                                "new": {
                                                    "type": "string",
                                                    "description": "Replacement text for this hunk.",
                                                },
                                                "occurrence": {
                                                    "type": "integer",
                                                    "description": "Optional 1-based occurrence number when old appears more than once.",
                                                    "default": 1,
                                                },
                                                "allow_multiple": {
                                                    "type": "boolean",
                                                    "description": "If true, replace every occurrence of old for this hunk.",
                                                    "default": False,
                                                },
                                            },
                                            "required": ["old", "new"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "expected_file_hash": {
                                        "type": "string",
                                        "description": (
                                            "Optional SHA-256 hex digest of this file's current content, from "
                                            "read_file or read_files. When supplied, the patch is rejected if "
                                            "the file's content no longer matches it."
                                        ),
                                    },
                                },
                                "required": ["path", "edits"],
                                "additionalProperties": False,
                            },
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional short description of the patch.",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }
]
