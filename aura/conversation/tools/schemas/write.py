"""Canonical filesystem mutation tool schema."""
from __future__ import annotations

from typing import Any

APPLY_PATCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "apply_patch",
        "description": (
            "Make one deliberate filesystem edit. Every call names one operation and every "
            "change is approval-gated, backed up, and applied atomically. "
            "operation=\"create\" writes a brand-new file from complete content (path+content). "
            "operation=\"replace\" replaces an existing file's whole content (path+content). "
            "operation=\"patch\" applies exact-text replacement hunks to one existing file "
            "(path+edits) or, as a single atomic transaction, to two or more existing files "
            "(files) — if any hunk in any file is missing or ambiguous, nothing is written. "
            "operation=\"delete\" deletes one existing file (path). "
            "Only the fields the chosen operation uses should be supplied; the wrong fields "
            "for an operation are rejected with a focused correction."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "replace", "patch", "delete"],
                    "description": "Which mutation to perform.",
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Workspace-relative file path. Used by create, replace, delete, and "
                        "single-file patch."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Full file content. Used by create and replace.",
                },
                "edits": {
                    "type": "array",
                    "description": "Ordered exact-text replacement hunks for a single-file patch.",
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
                "files": {
                    "type": "array",
                    "description": (
                        "Two or more target files patched as one atomic transaction, in place "
                        "of path+edits. Used by patch only."
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
                                    "read_file. When supplied, the patch is rejected if the file's "
                                    "content no longer matches it."
                                ),
                            },
                        },
                        "required": ["path", "edits"],
                        "additionalProperties": False,
                    },
                },
                "expected_file_hash": {
                    "type": "string",
                    "description": (
                        "Optional SHA-256 hex digest of the current whole file, from read_file. "
                        "When supplied, the patch is rejected if the file's content no longer "
                        "matches it. Only used with path+edits."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Optional short description of the patch. Used by patch.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason for deleting this file. Used by delete.",
                    "default": "",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}

FILESYSTEM_WRITE_TOOL_DEFS: list[dict[str, Any]] = [APPLY_PATCH_TOOL_DEF]
