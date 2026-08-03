"""Tool definition schemas for OpenAI function-calling.

Each constant is a complete OpenAI tool-definition dict (or list of dicts)
used by ToolRegistry.tool_defs() to build the API tool list.
"""

from __future__ import annotations

from typing import Any

READ_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "inspect_godot_assets",
            "description": (
                "Inspect recognized, project-specific Godot asset catalogs without changing files or the open "
                "scene. Returns generic asset descriptors with resource paths, domains, kinds, tags, semantic "
                "roles, dimensions, sockets, placement modes, calibrations, and catalog diagnostics. Covers the "
                "project's ruin, camp, barrier, building, and prop kits. Catalog adapters remain separate from "
                "Aura's conversation loop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional exact domain filters, such as ['ruins'] or ['camps'].",
                    },
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional exact asset-kind filters, such as ['wall_corner'].",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Require every listed project tag.",
                    },
                    "semantic_roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Require every listed generic role, such as entrance, barrier, or cover.",
                    },
                    "query": {
                        "type": "string",
                        "description": "Optional case-insensitive text search across identity and semantics.",
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                        "description": "Maximum matching asset records to return. Summaries still cover all assets.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_godot_asset_preview",
            "description": (
                "Inspect AuraPreview in the scene currently open in Godot. Returns catalog identities, "
                "transforms, semantic roles, and conservative footprint-overlap diagnostics. This is read-only "
                "and reflects the live scene, including changes made by edit_godot_asset_preview."
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_godot_asset_preview",
            "description": (
                "Capture a viewport rendering of the current Godot AuraPreview scene and return capture "
                "metadata, live preview facts, and local structural decompile evidence. Parameters are "
                "optional. Returns capture_set_id, scene_path, scene_fingerprint, preview facts from "
                "preview.snapshot, structural validation, and per-capture entries with view name, "
                "workspace-relative path, dimensions, sha256 digest, and textual visual structure from "
                "local decompilation. No image bytes or base64 data appear in the result. "
                "Read-only — no approval needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "capture_set_id": {
                        "type": "string",
                        "description": "Stable label for this capture set. Must not contain '..', '/', or '\\'. Default: auto-generated timestamp.",
                    },
                    "width": {
                        "type": "integer",
                        "minimum": 64,
                        "maximum": 1920,
                        "default": 1280,
                        "description": "Viewport width in pixels.",
                    },
                    "height": {
                        "type": "integer",
                        "minimum": 64,
                        "maximum": 1080,
                        "default": 720,
                        "description": "Viewport height in pixels.",
                    },
                    "modes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 4,
                        "description": "One or more of 'current_editor', 'overview', 'top_down'. Default: ['current_editor'].",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_godot_api",
            "description": (
                "Query the exact engine API exposed by the live Godot editor's ClassDB. Search engine class "
                "names or inspect a class's methods, argument/default signatures, properties, signals, integer "
                "constants, and enums. This is read-only and version-exact. ClassDB does not include project "
                "script-defined class_name types."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "class_name": {
                        "type": "string",
                        "description": "Exact engine class to inspect. Omit to search class names.",
                    },
                    "member_query": {
                        "type": "string",
                        "description": "Case-insensitive class-name or member-name filter.",
                    },
                    "include_inherited": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include inherited members when inspecting one class.",
                    },
                    "max_items": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 200,
                        "default": 50,
                        "description": "Maximum returned matches in each bounded member section.",
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_godot_editor",
            "description": (
                "Inspect the scene currently open in the live Godot editor. Returns the exact scene tree, "
                "selected nodes, node types, scripts, transforms, and editor-visible properties. This is "
                "read-only. Requires the Aura Editor Bridge to be installed (install_godot_editor_bridge) and "
                "Godot to have the project open; otherwise the call fails with that reason."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "include_properties": {
                        "type": "boolean",
                        "description": "Include serialized Inspector properties for every node.",
                        "default": True,
                    },
                    "max_nodes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 2000,
                        "description": "Maximum number of scene nodes to return. Default: 500.",
                        "default": 500,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from the workspace. By default returns the full contents "
                "(capped at 200KB). Pass offset/limit to read a specific window of lines instead — "
                "far more context-efficient than re-reading a whole file when you already know which "
                "lines you need, and it can reach any line in a file too large to return in full. "
                "To read several files, issue several read_file calls in the same round. "
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
                "Batched version of read_file — read multiple files in a single call. "
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
                "Find files and directories matching a glob pattern relative to the workspace "
                "root. '*' matches within one path segment, '**' recurses: use '**/*.gd' or "
                "'scripts/**/*.py' to walk the tree, and '*' or 'aura/gui/*' to list one "
                "directory's immediate contents. Returns 'matches' (files) and 'directories' "
                "(trailing slash), capped at 200 entries total."
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
    },
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
                        "Rank whole files by how well they match a description, using a local BM25 "
                        "index. Use this only when you cannot name the text to search for: "
                        "\"the file that handled authentication\", \"the database migration script\". "
                        "If you know the string, path, or symbol, use grep_search — it is exact and "
                        "immediate, where this is ranked and builds an index on first use."
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
                        },
                        "required": ["query"],
                    },
                },
            },
            {
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
            },
            {
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
            },
            {
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
            },
            {
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
            },
        ]
GIT_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Show the current git working tree status. Returns the current branch "
                "name, remote tracking info (ahead/behind counts, remote URL), and "
                "lists staged, unstaged, and untracked files. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": (
                "Show the git diff of changes in the workspace. By default shows "
                "unstaged changes (working tree vs HEAD). Set staged=true to see "
                "changes staged for commit. Optionally restrict to a single file "
                "with the path parameter. Output is capped at 200KB. Read-only."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "staged": {
                        "type": "boolean",
                        "description": "If true, show changes staged for commit. Default false (working tree changes).",
                        "default": False,
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional workspace-relative path to restrict the diff to a single file.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log",
            "description": (
                "Show recent git commit history. Returns a list of commits "
                "with hash and message. Use this to understand what changed "
                "recently in the repository. Optionally restrict to a single "
                "file with the path parameter."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "max_count": {
                        "type": "integer",
                        "description": "Maximum number of commits to return. Default: 10.",
                        "default": 10,
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional workspace-relative path to show history for a single file.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_show",
            "description": (
                "Show the full diff and metadata (author, date, message) "
                "for a specific commit by its hash. Output is capped at "
                "200KB. Use this to inspect what changed in a prior commit."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "commit_sha": {
                        "type": "string",
                        "description": "The full or abbreviated commit hash to show (e.g., 'abc1234' or 'HEAD~1').",
                    },
                },
                "required": ["commit_sha"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_log_file",
            "description": (
                "Show the commit history for a single file, following "
                "renames. Returns a list of commits that modified the file, "
                "with hash, author, date, and message. Use this to understand "
                "how a file has evolved over time."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to the file (e.g., 'aura/git.py'). Required.",
                    },
                    "max_count": {
                        "type": "integer",
                        "description": "Maximum number of commits to return. Default: 10.",
                        "default": 10,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_branch_list",
            "description": (
                "List all local branches with tracking information. "
                "Returns branch names, whether each is the current HEAD, "
                "the upstream tracking branch, and ahead/behind counts. "
                "Use this to see available branches and their relationship "
                "to remotes."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_stash_list",
            "description": (
                "List all stashes in the repository. Returns a list of stashes "
                "with index, context (branch/commit), and message. Use this "
                "to see what work-in-progress is currently stashed."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_stash_show",
            "description": (
                "Show the diff of a specific stash by its index. Returns the "
                "full diff of the stashed changes. Output is capped at 200KB. "
                "Use this to inspect the contents of a stash before deciding "
                "to apply it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "The index of the stash to show (e.g. 0 for the most recent stash). Default: 0.",
                        "default": 0,
                    },
                },
                "required": [],
            },
        },
    },
]

DISPATCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "dispatch_to_worker",
        "description": (
            "Dispatch a coding task to a worker model with file write access. Use this when "
            "the user has agreed to a code change and you have enough information to specify "
            "the change precisely. The worker has tools to read and edit files in the "
            "workspace. Once the goal, target seam/files, constraints, and acceptance are "
            "known, call this tool instead of continuing to discuss or expand a plan in "
            "chat. The Planner's deliverable for implementation work is this tool call. "
            "If the latest user message accepts a previously proposed actionable phase "
            "with phrases like 'do phase 1', 'start phase 1', 'yes do that', 'go', "
            "'run it', or 'let's do it', bind that to the most recent actionable phase "
            "and dispatch it. "
            "Provide a compact, self-contained worker task capsule — the worker does not "
            "see this conversation. Include: goal, target seam, files allowed, behavior to "
            "preserve, constraints / non-goals, validation commands or checks, and a "
            "summary. Do not write code, sketch patches, plan hunks, or solve exact "
            "implementation details here; the worker owns those decisions. Include a "
            "self-terminating run_command smoke check for any change that affects whether "
            "the app boots or a runnable entry point behaves. The worker will return a "
            "summary of what it did. "
            "Every dispatch becomes a WorkArtifact. "
            "The Planner may either provide explicit work_artifact.items or provide the normal top-level fields. "
            "If work_artifact is omitted, Aura normalises the top-level fields into a one-item WorkArtifact. "
            "One-item and multi-item jobs share the same visible card, lifecycle, receipts, and progress model. "
            "The work_artifact is a visible scope/progress/receipt structure for one approved job. "
            "The user approves the WorkArtifact job once. Aura executes item runs internally under the same approval. "
            "Fill structured contract fields when knowable from "
            "the request or repo context; they power Aura's pre-release quality gate."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "One-sentence statement of what the change accomplishes.",
                },
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative paths the worker should read and/or modify.",
                },
                "target_regions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Workspace-relative file path.",
                            },
                            "symbol": {
                                "type": "string",
                                "description": "Class, function, method, or nearby target name.",
                            },
                            "start_line": {
                                "type": "integer",
                                "description": "Optional 1-based start line for the target range.",
                            },
                            "end_line": {
                                "type": "integer",
                                "description": "Optional 1-based end line for the target range.",
                            },
                            "note": {
                                "type": "string",
                                "description": "Short scope note for this target.",
                            },
                        },
                    },
                    "description": (
                        "Use this when the Planner knows the relevant target, especially in "
                        "large files. Prefer symbol anchoring over raw start_line/end_line: "
                        "provide the class, function, method, or nearby stable symbol the "
                        "Worker should inspect and edit. Include line numbers only when they "
                        "come from a read_file in this dispatch turn; stale line numbers are "
                        "worse than no line numbers. This lets the Worker anchor on the symbol, "
                        "use bounded read_file (offset and limit) windows around the current target "
                        "area, then patch with expected_file_hash from the read."
                    ),
                },
                "spec": {
                    "type": "string",
                    "description": (
                        "Self-contained worker task capsule. Write concise plain English, "
                        "like a senior engineer naming the work boundary for a capable "
                        "builder. Include the target seam, files allowed, behavior to "
                        "preserve, constraints / non-goals, validation commands or checks, "
                        "and necessary context. Keep this lean and direct even for multi-file "
                        "work: identify seams and boundaries, but do not write code, sketch "
                        "patches, plan hunks, or solve exact implementation details. Base "
                        "file paths, symbols, regions, and any line numbers on CURRENT "
                        "read_file results from this dispatch turn for each target, not "
                        "earlier reads; stale paths and line numbers cause worker thrash. "
                        "Preserve "
                        "structured contract fields such as expected_public_symbols, "
                        "expected_dataclass_fields, forbidden_calls, "
                        "forbidden_public_methods, and non_goals when they are knowable. "
                        "Do not use this field to narrate your own thinking; write the "
                        "work order and dispatch it. The worker has not seen the "
                        "conversation, so include necessary context."
                    ),
                },
                "work_artifact": {
                    "type": "object",
                    "description": (
                        "WorkArtifact payload for multi-item jobs. "
                        "If omitted, Aura normalises the top-level fields (goal, files, spec, "
                        "acceptance, summary, validation_commands) into a one-item WorkArtifact. "
                        "For multi-part work, supply items so the user sees "
                        "every bounded item before any Worker runs. "
                        "Items are bounded internal execution units. "
                        "Aura executes item runs internally under the same approval. "
                        "The user approves the WorkArtifact job once. "
                        "There is no manual later-item approval. "
                        "Item acceptance proves the item. "
                        "Top-level acceptance proves the whole job."
                    ),
                    "properties": {
                        "goal": {
                            "type": "string",
                            "description": "Overall goal of the work artifact.",
                        },
                        "constraints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Artifact-level constraints and non-goals.",
                        },
                        "allowed_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Workspace-relative files the artifact may touch.",
                        },
                        "items": {
                            "type": "array",
                            "description": "Ordered list of bounded internal execution units for the job.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "Stable non-empty item id, e.g. 'item-1'.",
                                    },
                                    "title": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "Short user-readable title for this bounded item.",
                                    },
                                    "intent": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "What this specific item should accomplish.",
                                    },
                                    "target_files": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": "Known concrete target files for this item. "
                                        "May be empty for discovery / repo-scope / phase-0 tasks "
                                        "where no specific file is known yet.",
                                    },
                                    "acceptance": {
                                        "type": "string",
                                        "minLength": 1,
                                        "description": "Concrete pass/fail acceptance for this item.",
                                    },
                                    "validation_commands": {
                                        "type": "array",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "command": {
                                                    "type": "string",
                                                    "minLength": 1,
                                                    "description": "Exact shell command to validate the item.",
                                                },
                                                "cwd": {
                                                    "type": "string",
                                                    "description": "Optional working directory relative to repo root.",
                                                },
                                                "expected_outcome": {
                                                    "type": "string",
                                                    "description": "Optional description of the expected pass/fail signal.",
                                                },
                                            },
                                            "required": ["command"],
                                            "additionalProperties": False,
                                        },
                                        "description": "Explicit validation commands declared for this item.",
                                    },
                                },
                                "required": ["id", "title", "intent", "target_files", "acceptance"],
                                "additionalProperties": False,
                            },
                        },
                    },
                    "required": ["goal", "items"],
                    "additionalProperties": False,
                },
                "acceptance": {
                    "type": "string",
                    "description": (
                        "Definition-of-done with concrete pass/fail clauses, not a vague "
                        "summary. Include "
                        "validation commands when possible, concrete output/content checks "
                        "for generated or transformed output, and failure behavior checks "
                        "when parsing, config, user input, or batch processing is involved. "
                        "Item acceptance proves the item. Top-level acceptance proves the whole job."
                    ),
                },
                "summary": {
                    "type": "string",
                    "description": (
                        "A concise, user-friendly summary of the intended changes. This will be "
                        "shown to the user in the UI after the worker completes."
                    ),
                },
                "allowed_responsibilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "What the Worker is expected to own (e.g. ['Full implementation', "
                        "'Validation', 'Error handling'])."
                    ),
                },
                "forbidden_responsibilities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "What the Worker must NOT shove into this task/files "
                        "(e.g. ['Do not refactor unrelated modules', 'Do not add new dependencies'])."
                    ),
                },
                "required_outputs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Concrete artifacts/behaviors the Worker must produce "
                        "(e.g. ['Modified aura/config.py', 'Working CLI entry point'])."
                    ),
                },
                "run_command": {
                    "type": "string",
                    "description": (
                        "An exact, self-terminating shell command run after the worker completes changes, "
                        "via run_and_watch. The command MUST exit on its own with code 0 and no traceback — "
                        "a command that keeps running and merely survives the window is scored as a launch "
                        "failure, so do not declare server-style or long-running commands here. Declare a "
                        "command by default whenever the change could affect whether the application boots "
                        "or a known entry point runs correctly; for this project the canonical smoke command "
                        "is \"python -m aura --selfcheck\". The worker observes the command's startup behavior. "
                        "This is a declared contract intent — the worker cannot choose or override it. "
                        "Leave empty only for changes with no runnable surface, e.g. pure documentation or "
                        "comment edits, or an isolated pure-helper extraction with no import-time effect."
                    ),
                },
                "validation_commands": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Exact shell command to run for validation.",
                            },
                            "cwd": {
                                "type": "string",
                                "description": "Optional working directory relative to repo root.",
                            },
                            "expected_outcome": {
                                "type": "string",
                                "description": "Optional description of the expected pass/fail signal.",
                            },
                        },
                        "required": ["command"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "Exact focused validation commands when known "
                        "(e.g. [{'command': 'python -m compileall aura/'}]). "
                        "When provided, these override extracted acceptance commands."
                    ),
                },
                "risk_notes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Realistic failure, security, or integration risks "
                        "(e.g. ['Breaking change to public API signature'])."
                    ),
                },
                "non_goals": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit scope fences the critic should enforce "
                        "(e.g. ['No new CLI flags', 'No database migration']). "
                        "When provided, these override Non-Goals parsed from spec."
                    ),
                },
                "expected_public_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Names of public symbols (classes, functions, constants) the Worker must define. "
                        "Populate when the task adds, exposes, renames, or requires a public API. "
                        "The pre-release quality gate verifies these exist in the output."
                    ),
                },
                "expected_dataclass_fields": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "description": (
                        "A mapping from class names to lists of required dataclass field names, "
                        "e.g. {'WorkerDispatchRequest': ['goal', 'files', 'spec']}. "
                        "Populate when the task adds or changes dataclass fields. "
                        "The pre-release quality gate verifies these fields exist on the corresponding dataclass."
                    ),
                },
                "forbidden_public_methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Method names the Worker must NOT introduce on public classes, "
                        "e.g. ['to_dict', 'from_dict'] on domain models that shouldn't have serialization. "
                        "Populate when the task explicitly forbids a public method."
                    ),
                },
                "forbidden_calls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Function call names the Worker must NOT use, "
                        "e.g. ['print', 'input'] for backend code or ['eval', 'exec'] for security. "
                        "Populate when the task explicitly forbids a call or dependency entry point."
                    ),
                },
            },
            "required": ["goal", "files", "spec", "acceptance", "summary"],
        },
    },
}


SUMMON_DRONE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "summon_drone",
        "description": (
            "Suggest launching a saved Drone to handle a focused sub-task independently. "
            "Call this when the user's request matches a saved Drone's purpose. "
            "The Drone runs separately and its receipt appears in the right panel."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drone_id": {
                    "type": "string",
                    "description": (
                        "The id of the saved Drone to launch (from Available Drones list)."
                    ),
                },
                "goal": {
                    "type": "string",
                    "description": (
                        "What the Drone should accomplish this specific run."
                    ),
                },
                "reason": {
                    "type": "string",
                    "description": (
                        "Why this Drone is being summoned — shown to the user for confirmation."
                    ),
                },
            },
            "required": ["drone_id", "goal"],
        },
    },
}


WRITE_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "install_godot_editor_bridge",
            "description": (
                "Install and enable Aura's modular, localhost-only EditorPlugin in the current Godot project. "
                "This creates addons/aura_bridge and writes a token-authenticated local config. By default, "
                "activate it normally in Godot under Project Settings > Plugins. Set enable_plugin only for "
                "headless setup. This is the bundled bridge implementation; it does not author project code."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "port": {
                        "type": "integer",
                        "minimum": 1024,
                        "maximum": 65535,
                        "default": 17891,
                    },
                    "enable_plugin": {
                        "type": "boolean",
                        "description": (
                            "Write the enabled plugin entry into project.godot. Defaults to false so the user "
                            "can activate it normally through Godot's Plugins UI."
                        ),
                        "default": False,
                    },
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_godot_editor",
            "description": (
                "Manipulate the scene currently open in the live Godot editor. Changes use Godot's own "
                "EditorUndoRedoManager. Use action=apply for node/property operations, action=select to focus "
                "nodes in Godot, or action=save to save the active scene. "
                "Values use Godot Variant text such as 'Vector3(1, 2, 3)', 'true', or '\"hello\"'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["apply", "select", "save"]},
                    "label": {"type": "string", "description": "Undo-history label for apply."},
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Root-relative node paths for select; '.' is the scene root.",
                    },
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string", "enum": ["set_property", "create_node"]},
                                "node_path": {"type": "string"},
                                "property": {"type": "string"},
                                "value_text": {"type": "string"},
                                "parent": {"type": "string", "default": "."},
                                "name": {"type": "string"},
                                "type": {"type": "string"},
                                "properties": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_godot_asset_preview",
            "description": (
                "Safely assemble catalog-approved PackedScenes beneath a dedicated AuraPreview Node3D in the "
                "scene currently open in Godot, or clear its children. Every call is approval-gated and one "
                "Godot UndoRedo action. Asset IDs must come from inspect_godot_assets; arbitrary resource paths "
                "are not accepted. This never saves the scene automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["instantiate", "clear", "apply"]},
                    "label": {"type": "string", "description": "Undo-history label."},
                    "placements": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 64,
                        "description": "Required for instantiate and ignored for clear.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "asset_id": {"type": "string"},
                                "domain": {"type": "string", "description": "Required only if the ID is ambiguous."},
                                "name": {"type": "string", "description": "Optional unique node name."},
                                "position": {
                                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                },
                                "rotation_degrees_y": {"type": "number", "default": 0},
                                "scale": {
                                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                },
                            },
                            "required": ["asset_id"],
                            "additionalProperties": False,
                        },
                    },
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 25,
                        "description": "Atomic ordered revision operations required for action=apply.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "operation": {
                                    "type": "string",
                                    "enum": ["set_transform", "instantiate", "remove", "replace"],
                                },
                                "node_path": {
                                    "type": "string",
                                    "description": "Direct child path such as AuraPreview/WestWall.",
                                },
                                "asset_id": {"type": "string"},
                                "domain": {"type": "string"},
                                "name": {"type": "string"},
                                "position": {
                                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                },
                                "rotation_degrees_y": {"type": "number"},
                                "scale": {
                                    "type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3
                                },
                            },
                            "required": ["operation"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_godot_scene",
            "description": (
                "Apply structured node edits to an existing Godot .tscn text scene: add or remove nodes and "
                "change node properties, without hand-editing the scene's text. "
                "Node paths are relative to the scene root: '.' is the root and 'Player/Sprite' is a descendant. "
                "Property values are raw one-line Godot expressions such as 'Vector2(10, 20)', 'true', "
                "'\"Ready\"', or 'ExtResource(\"1_script\")'. All operations are validated in memory and "
                "presented together in one approval diff before the scene is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to an existing .tscn scene.",
                    },
                    "operations": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": [
                                        "add_node",
                                        "remove_node",
                                        "set_property",
                                        "remove_property",
                                    ],
                                },
                                "node_path": {
                                    "type": "string",
                                    "description": "Existing node path for remove/property operations.",
                                },
                                "name": {
                                    "type": "string",
                                    "description": "New node name for add_node.",
                                },
                                "type": {
                                    "type": "string",
                                    "description": "Godot class name for add_node, e.g. CharacterBody2D.",
                                },
                                "parent": {
                                    "type": "string",
                                    "description": "Parent node path for add_node; defaults to '.'.",
                                    "default": ".",
                                },
                                "property": {
                                    "type": "string",
                                    "description": "Godot property name for property operations.",
                                },
                                "value": {
                                    "type": "string",
                                    "description": "Raw one-line Godot value expression for set_property.",
                                },
                                "properties": {
                                    "type": "object",
                                    "description": "Optional property-to-raw-expression map for add_node.",
                                    "additionalProperties": {"type": "string"},
                                },
                                "recursive": {
                                    "type": "boolean",
                                    "description": "Required to remove a node that has descendants.",
                                    "default": False,
                                },
                            },
                            "required": ["action"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["path", "operations"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write the given content to a workspace file, creating it or replacing it whole. "
                "Writing over an existing file REQUIRES full_replace_existing=true and a "
                "replacement_reason; without both the call is rejected. Those fields declare an "
                "intentional whole-file replacement and are rejected as patch_file failure recovery. "
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
                    "full_replace_existing": {
                        "type": "boolean",
                        "description": (
                            "Optional. Only for intentional whole-file replacement of an existing file; "
                            "do not use this to recover from a failed patch_file edit."
                        ),
                        "default": False,
                    },
                    "replacement_reason": {
                        "type": "string",
                        "description": (
                            "Required with full_replace_existing=true. Explain why this existing file "
                            "must be replaced wholesale instead of patched."
                        ),
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
                "Apply multiple exact-text replacement hunks to one existing workspace file as a single "
                "atomic, approval-gated transaction. Each hunk's old text must match the file exactly. "
                "In Worker mode expected_file_hash is required and must be the content_hash returned by "
                "read_file or read_files. Every hunk is applied to an in-memory copy first; if any hunk is "
                "missing or ambiguous, nothing is written and the result names which hunk failed. Use "
                "occurrence to disambiguate repeated exact text, or give more surrounding context in the old "
                "block. Craft reviews the full proposed file once and the user sees one approval diff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path.",
                    },
                    "edits": {
                        "type": "array",
                        "description": "Ordered exact-text replacement hunks to apply to the file.",
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
                            "SHA-256 hex digest of the current whole file from read_file "
                            "or read_files. Required by Worker mode for existing files."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional short description of the patch.",
                    },
                },
                "required": ["path", "edits"],
                "additionalProperties": False,
            },
        },
    },
]


PROJECT_MEMORY_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "search_project_memory",
            "description": (
                "Search the project's archival memory for past dispatch records "
                "and saved documentation. Use this when you need context from "
                "previous work."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Maximum number of results to return. Default: 5.",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_to_project_memory",
            "description": (
                "Save important information to the project's long-term memory "
                "for future retrieval."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The content to save.",
                    },
                    "metadata": {
                        "type": "object",
                        "description": (
                            "Optional structured metadata "
                            "(e.g., {'type': 'architecture_decision', 'tags': ['auth']})."
                        ),
                    },
                },
                "required": ["content"],
            },
        },
    },
]

#: The dedicated, read-only skill-activation tool.
#:
#: One batchable call resolves one or more stable skill ids against the *frozen*
#: candidate index of the current real user turn (the same deterministic
#: selection the initial skill index exposed).  Full bodies never sit in the
#: initial system prompt; they become available only through this tool.  It is
#: deliberately not ``read_file``: bundled, graduated, and refined skills are
#: not necessarily workspace files, and activation needs turn-scoped
#: authorization, provenance, idempotence, telemetry, and protection against
#: arbitrary skill-library reads.
LOAD_SKILLS_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "load_skills",
        "description": (
            "Load the full body of a candidate skill already listed in this "
            "turn's initial skill index. Only skills in that index are loadable; "
            "the index is frozen for this request, so unrelated global skills "
            "are never available. Several ids may be loaded in one call. "
            "This is read-only and never changes any file. "
            "Returned bodies are the exact bodies the index described (each with "
            "a body hash), and re-loading an already-active skill is inert."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": (
                        "Stable skill ids from the current turn's skill index. "
                        "Each must be one of the ids listed in the initial "
                        "### Skills block."
                    ),
                },
            },
            "required": ["skill_ids"],
            "additionalProperties": False,
        },
    },
}


WORKER_TODO_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_worker_todo",
        "description": (
            "Publish the live TODO checklist as a full snapshot, replacing the previous one. "
            "Requires three to seven action-shaped rows. Exactly one item should be active unless "
            "the work is complete, in which case all items may be done. This tool is only a UI "
            "lens; it never completes, blocks, or gates the task."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "description": "Full ordered TODO snapshot. Reuse each row id across updates.",
                    "minItems": 3,
                    "maxItems": 7,
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "description": "Stable row identity, e.g. 'guard-persistence'.",
                            },
                            "text": {
                                "type": "string",
                                "description": "Short concrete action, e.g. 'Add the replay guard'.",
                            },
                            "status": {
                                "type": "string",
                                "enum": ["pending", "active", "done"],
                                "description": "Current row state.",
                            },
                        },
                        "required": ["id", "text", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
    },
}

TERMINAL_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_terminal_command",
        "description": (
            "Execute a shell command in the workspace or an optional workspace-relative cwd and "
            "stream its output. The full system shell is available: pipes, redirects, and "
            "chaining work, as do linters, type checkers, test suites, build commands, git, "
            "dependency installs (pip install, python -m pip install, uv sync, poetry install, "
            "pdm install), and any command that mutates the workspace. The command runs with the "
            "workspace as its working directory unless cwd/working_directory is provided. Stdout "
            "and stderr are both captured and streamed in real-time, including periodic status "
            "heartbeats if the command is quiet. Returns the exit code and complete output on "
            "completion. The command must self-terminate: long-running watchers, dev servers, "
            "REPLs, and commands that wait for interactive input hit the timeout and are killed. "
            "For Python projects the project-local .venv interpreter is selected when present."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute, e.g. 'python -m py_compile aura/app.py' for touched Python files, 'npm test' for a Node project when available/requested, or another focused validation/build command. Executed via the system shell.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait before killing the command. Default: 45. Prefer short focused runs; very large values may be reduced for safety.",
                    "default": 45,
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace-relative working directory for the command, e.g. 'companion-web'. Absolute paths and '..' escapes are rejected.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Alias for cwd. Must be workspace-relative and stay inside the workspace.",
                },
            },
            "required": ["command"],
        },
    },
}

RUN_AND_WATCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_and_watch",
        "description": (
            "Run the task\'s declared run_command and watch for startup "
            "behavior. Success means the command exits on its own within the "
            "watch window with exit code 0 and no traceback. A command that "
            "survives the window without crashing (still running when the "
            "window expires) is FAILURE — the command must self-terminate. "
            "A crash (Traceback in output) or non-zero exit code is also "
            "failure. This tool takes NO command parameter — the command is "
            "fixed by the task contract (dispatch_to_worker run_command "
            "field). If no run command was declared for this task, it "
            "returns an informational no-op result. Normally you do NOT "
            "need to call this tool yourself — the harness automatically "
            "runs launch verification after you finish. Use "
            "run_terminal_command for your own validation checks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "window_seconds": {
                    "type": "integer",
                    "description": (
                        "How many seconds to watch the process. Default: 10. "
                        "Maximum: 60."
                    ),
                    "default": 10,
                },
            },
            "required": [],
        },
    },
}

DIAGNOSTIC_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_diagnostic_command",
        "description": (
            "Run ONE short, read-only, self-terminating command to inspect or validate the "
            "workspace, in the workspace root or an optional workspace-relative cwd. "
            "This is the default choice for checking your work: test runs, linters, type "
            "checks, git inspection (status, diff, log), and filesystem searches (rg, ls, cat). "
            "There is NO shell: the command is parsed into a single argument list and executed "
            "directly, so pipes, redirects, chaining (| & ; < > ` $(...)), installs, and any "
            "mutating command are rejected — use run_terminal_command for those. "
            "Quoting works normally and quoted arguments arrive unquoted, so pytest node IDs "
            "with spaces or parameters are safe to pass. For Python projects the project-local "
            ".venv interpreter is selected automatically from a bare 'python' or 'pytest'. "
            "Returns stdout, stderr, exit_code, timed_out, the requested command, and the argv "
            "that ran; a rejected command returns a failure_class, the offending token, and one "
            "concrete correction. Output is truncated at 100KB. "
            "Use this instead of putting validation commands into Worker dispatch specs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "A read-only diagnostic command."                            "'python -m py_compile aura/gui/left_pane.py', "
                            "'git status', 'git diff', "
                            "'npm test', 'cargo test', "
                            "'rg \"class LeftPane\" aura/', "
                            "'ls aura/conversation/tools/'. "
                            "Use 'rg' instead of bare grep for shell searches, or use grep_search when you want structured matches. "
                            "For absence checks, make the command exit 0 when the pattern is absent."
                        ),
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait. Default: 30.",
                    "default": 30,
                },
                "cwd": {
                    "type": "string",
                    "description": "Optional workspace-relative working directory for the diagnostic command. Absolute paths and '..' escapes are rejected.",
                },
                "working_directory": {
                    "type": "string",
                    "description": "Alias for cwd. Must be workspace-relative and stay inside the workspace.",
                },
            },
            "required": ["command"],
        },
    },
}

WORKSPACE_SNAPSHOT_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_workspace_snapshot",
        "description": "Get a compact snapshot of the current workspace: root path, project identity, recent threads, git branch/status, changed files count, and project type hints (pyproject.toml, package.json, etc.). Use this at the start of ambiguous tasks instead of calling git_status, list_directory, and multiple reads separately. Fast, read-only, no file contents.",
        "parameters": {"type": "object", "properties": {}, "required": []},
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

WEB_SEARCH_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Run live web research for latest/current facts, external docs/API examples, "
            "pricing, versions/releases/changelogs, schedules, current people/roles, "
            "error lookup, URLs, and external references. Returns sourced evidence with "
            "citations. Do not use web research for local repo, file, workspace, git, or "
            "ordinary coding questions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The research question to search the web for.",
                },
                "context": {
                    "type": "string",
                    "description": "Optional relevant context (local findings, user constraints) to include in the search request.",
                },
            },
            "required": ["question"],
        },
    },
}


#: The production SINGLE exit hatch for an attempt that cannot be carried out.
#:
#: An ordinary tool on the stable production catalog, alongside
#: ``report_already_satisfied``. It mutates nothing — it serializes "I cannot
#: make this edit, and here is why" into structured data so the turn can end
#: honestly instead of reopening planning.
REPORT_BLOCKER_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_blocker",
        "description": (
            "Report that the implementation you scoped cannot be carried out, and "
            "end the attempt. Use this ONLY when no edit is possible — not to ask "
            "for more discovery, not to restate a plan, and not instead of a write "
            "you are able to make. Performs no mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "blocker": {
                    "type": "string",
                    "description": (
                        "One concrete sentence naming what prevents the edit."
                    ),
                },
                "needed": {
                    "type": "string",
                    "description": (
                        "Optional: the specific thing that would unblock it."
                    ),
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: the files the edit would have touched."
                    ),
                },
            },
            "required": ["blocker"],
        },
    },
}


#: The production SINGLE exit hatch for an attempt the repository already
#: satisfies: the requested state already exists, so no change is required.
#:
#: An ordinary tool on the stable production catalog, alongside
#: ``report_blocker``.  It mutates nothing — it serializes "the change is
#: already present, and here is the repository evidence" into structured data so
#: the turn can complete truthfully without manufacturing an edit.  The
#: structured result is what the completion receipt reads; plain assistant prose
#: is never proof.
REPORT_ALREADY_SATISFIED_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_already_satisfied",
        "description": (
            "Report that the requested state already exists in the repository, "
            "and end the attempt. Use this ONLY when authoritative repository "
            "evidence you inspected this turn shows the change is already "
            "present and no edit is required — not to avoid a write you are "
            "able to make, and not because you simply chose not to act. "
            "Performs no mutation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "evidence": {
                    "type": "string",
                    "description": (
                        "One concrete sentence naming the authoritative "
                        "repository evidence you inspected that shows the "
                        "requested state already exists (file, symbol, search "
                        "match, or command result)."
                    ),
                },
                "target_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional: the files whose requested state is already present."
                    ),
                },
            },
            "required": ["evidence"],
        },
    },
}
