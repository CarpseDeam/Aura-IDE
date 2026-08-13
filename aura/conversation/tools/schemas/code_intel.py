"""Code-intelligence and evidence tool schemas."""
from __future__ import annotations

from typing import Any

INSPECT_CODE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "inspect_code",
        "description": (
            "Return a bounded local-evidence packet about one source location or "
            "symbol, denser than a single read_file or grep_search hit. The result "
            "has three parts. 'target': the best symbol match Aura's parser found "
            "for the given line and/or symbol, with a 'resolution' field stating how "
            "it was found ('exact_symbol_match', 'exact_line_match', "
            "'nearest_preceding_declaration', or 'unresolved') and a 'provenance' "
            "field naming which parser produced it — never claimed as a resolved "
            "language reference. 'source_excerpt': actual source text with its own "
            "start/end lines and a 'truncated' flag. When the resolved target carries "
            "a parser-owned declaration end range, the excerpt is bounded to that "
            "declaration's real body ('parser_bounded': true), still capped by the "
            "same size limits as any other result; when the resolving adapter did "
            "not parse one for this target, it falls back to a fixed-size line "
            "window around the anchor ('bounded_window': true) and the result says "
            "so. 'diagnostics': parser diagnostics for the file, if "
            "any. Works across languages at whatever quality Aura's adapter for that "
            "file provides; an unsupported or partial adapter still returns the "
            "source excerpt. Scoped to one file's local structural evidence — for "
            "other places a symbol's name appears across the workspace, use "
            "grep_search or search_codebase."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path to the file to inspect.",
                },
                "line": {
                    "type": "integer",
                    "description": "Optional 1-based line number to anchor the inspection.",
                },
                "symbol": {
                    "type": "string",
                    "description": "Optional symbol name to anchor the inspection.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
}

CODE_INTEL_OUTLINE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "code_intel_outline",
        "description": (
            "Get a structural outline of a file using language-aware code intelligence. "
            "Returns classes, functions, and imports. Use this as an alternative to "
            "read_file when you want richer language-specific parsing. "
            "Always prefer a bounded read_file (offset and limit) for lightweight use."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative path.",
                }
            },
            "required": ["path"],
        },
    },
}

CODE_INTEL_REFERENCES_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "code_intel_references",
        "description": (
            "Find all references to a symbol across the workspace using the code intelligence "
            "index. Returns list of ReferenceEdge objects with source_file, target_symbol, "
            "line, and kind. Use this for safe refactoring — find every place a symbol is used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Symbol name to search.",
                },
                "file": {
                    "type": "string",
                    "description": "Restrict to this file (optional).",
                },
            },
            "required": ["symbol"],
        },
    },
}

CODE_INTEL_DEPENDENTS_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "code_intel_dependents",
        "description": (
            "Return the list of files that transitively depend on a given file (blast radius). "
            "Use this before editing to understand which downstream files could break."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Workspace-relative file path.",
                }
            },
            "required": ["path"],
        },
    },
}

CODE_INTEL_AUDIT_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "code_intel_audit",
        "description": (
            "Run a structural audit on a list of changed files. Detects parse failures and "
            "structural issues. Returns list of AuditFinding objects sorted by file and line. "
            "Use this after editing to verify no structural regressions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative file paths to audit.",
                },
            },
            "required": ["paths"],
        },
    },
}

CODE_INTEL_TOOL_DEFS: list[dict[str, Any]] = [
    CODE_INTEL_OUTLINE_TOOL_DEF,
    CODE_INTEL_REFERENCES_TOOL_DEF,
    CODE_INTEL_DEPENDENTS_TOOL_DEF,
    CODE_INTEL_AUDIT_TOOL_DEF,
]
