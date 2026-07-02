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
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file from the workspace. Returns its full contents (capped at 200KB). "
                "Use this to inspect the user's source code, configs, or notes before answering or editing. "
                "The path argument MUST be relative to the workspace root."
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
            "name": "read_files",
            "description": (
                "Batched version of read_file — read multiple files in a single call. "
                "Each file is capped at 200KB. Combined output is capped at 500KB total; "
                "paths beyond the limit will return an error. Returns per-file results "
                "with ok/error or ok/content/content_hash/file_size/truncated/path for each path. "
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
                "Recursively find files matching a glob pattern relative to the workspace root. "
                "Examples: '**/*.gd', 'scripts/**/*.py', '*.md'. Capped at 200 matches."
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
                "Use this after read_file_outline or a previous read_file tells you which line "
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
                        "Discover candidate files and locations by searching workspace file contents "
                        "for a given string or regex pattern. This is a discovery tool, not proof of "
                        "exact edited content; use read_file or read_file_range to verify known files. "
                        "Returns matching file paths, line numbers, the matching line content, "
                        "and the column where the match starts, plus search metadata such as the "
                        "engine used, searched file count, skipped file count, truncation, and regex hint state. "
                        "Use this to find where functions are defined, variables are used, "
                        "error messages, or any text pattern across the codebase."
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
                        "Semantic search over the workspace codebase using a local BM25 index. "
                        "Use this to recall files, functions, or code patterns when you need context "
                        "that may have been pruned from the conversation history. This is NOT a grep — "
                        "it ranks entire files by relevance to your query. Great for re-discovering "
                        "\"the file that handled authentication\" or \"the database migration script\" "
                        "without knowing the exact path or keyword."
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
                        "read_file_outline when you want richer language-specific parsing. "
                        "Always prefer read_file_outline for lightweight use."
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
                "lists staged, unstaged, and untracked files. Use this before "
                "finishing a coding task to review what files were changed, or to verify "
                "the repository state before making edits."
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
                "with the path parameter. Output is capped at 200KB. Use this to "
                "review your own changes for mistakes before finishing, or to verify "
                "exactly what was modified."
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
            "summary of what it did. For implementation work that is multi-file, high-risk, "
            "subsystem/architecture/feature oriented, refactor-like, validation-heavy, or "
            "otherwise non-trivial, provide a decomposed steps campaign. Flat fields without "
            "steps are only a compatibility path for tiny one-file work. Fill structured contract fields when knowable from "
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
                        "use read_file_outline and read_file_range around the current target "
                        "area, then patch with expected_file_hash from the range read."
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
                "steps": {
                    "type": "array",
                    "description": (
                        "Optional ordered campaign for multi-step work, but required for "
                        "non-trivial implementation work: multi-file, high-risk, subsystem, "
                        "architecture, refactor, feature, validation-heavy, or multi-stage "
                        "tasks. Each item is one self-contained bounded Worker run with "
                        "its own id, title, goal, spec, files, and acceptance. Step items "
                        "must not rely on top-level files, spec, or acceptance; those "
                        "top-level fields are campaign context only once steps are present. "
                        "Do not use one giant step that restates the top-level summary. "
                        "When omitted or empty, the top-level flat fields are the "
                        "single-step dispatch path for tiny one-file compatibility work only."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Stable non-empty step id, e.g. 'step-1'.",
                            },
                            "title": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Short specific user-readable title for this bounded edit; do not reuse the top-level summary.",
                            },
                            "goal": {
                                "type": "string",
                                "minLength": 1,
                                "description": "What this specific step should accomplish, narrower than the campaign goal.",
                            },
                            "spec": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Self-contained bounded work order for this step, not the full campaign pasted into one paragraph and not dependent on top-level spec.",
                            },
                            "files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "description": "Workspace-relative files for this step. Every step must name its own bounded file scope.",
                            },
                            "acceptance": {
                                "type": "string",
                                "minLength": 1,
                                "description": "Concrete pass/fail acceptance for this step, not inherited from top-level acceptance.",
                            },
                            "validation_commands": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Focused validation commands for this step.",
                            },
                            "required_outputs": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Concrete artifacts or behaviors this step must produce.",
                            },
                            "non_goals": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Scope fences specific to this step.",
                            },
                            "expected_public_symbols": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Public symbols this step must define or preserve.",
                            },
                            "expected_dataclass_fields": {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "description": "Required dataclass fields by class name for this step.",
                            },
                            "forbidden_calls": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Function calls this step must not introduce.",
                            },
                            "forbidden_public_methods": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Public class methods this step must not introduce.",
                            },
                        },
                        "required": ["id", "title", "goal", "spec", "files", "acceptance"],
                        "additionalProperties": False,
                    },
                },
                "acceptance": {
                    "type": "string",
                    "description": (
                        "Definition-of-done with concrete pass/fail clauses, not a vague "
                        "summary. Include "
                        "validation commands when possible, concrete output/content checks "
                        "for generated or transformed output, and failure behavior checks "
                        "when parsing, config, user input, or batch processing is involved. "
                        "For sliced large refactors, acceptance must prove only the first "
                        "slice that this dispatch owns."
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
                    "items": {"type": "string"},
                    "description": (
                        "Exact focused validation commands when known "
                        "(e.g. ['python -m compileall aura/']). "
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
            "name": "write_file",
            "description": (
                "Write the given content to a workspace file. "
                "Use this for new files, or for an intentional full-file replacement only. "
                "For normal existing-file edits, read the file first and use patch_file instead. "
                "For intentional whole-file replacement of an existing file, set full_replace_existing "
                "to true and provide replacement_reason; these fields are not for patch_file failure recovery. "
                "If a patch_file hunk is missing or ambiguous, recover with read_file/read_file_range and "
                "a corrected patch_file hunk, not write_file. "
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
                "Use this for cleanup files or files intentionally removed during refactors. "
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
                "atomic, approval-gated transaction. Use this for normal existing-file edits after "
                "reading the file. In Worker mode, after reading an existing file, pass the "
                "content_hash returned by read_file, read_files, or read_file_range as "
                "expected_file_hash. Every hunk is applied to an in-memory copy first; "
                "if any hunk is missing or ambiguous, nothing is written. Use occurrence to disambiguate "
                "repeated exact text, or add more surrounding context to the old block. Craft reviews the full "
                "proposed file once and the user sees one approval diff. If a hash mismatch or hunk failure "
                "occurs, re-read and retry patch_file once with a corrected hunk and the new expected_file_hash. "
                "Do not switch to write_file unless the task intentionally requires whole-file replacement."
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
                            "SHA-256 hex digest of the current whole file from read_file, "
                            "read_files, or read_file_range. Required by Worker mode for existing files."
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

TERMINAL_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_terminal_command",
        "description": (
            "Execute a shell command in the workspace or an optional workspace-relative cwd and stream its output. "
            "Use this to run project validation/build commands: linters, type checkers, "
            "test suites explicitly requested by the user, or other validation/build "
            "commands. The command runs with the workspace as its working "
            "directory unless cwd/working_directory is provided. Stdout and stderr are both captured and streamed in real-time, "
            "including periodic status heartbeats if the command is quiet. Returns the "
            "exit code and complete output on completion. Use focused one-shot commands, "
            "not long-running watchers, dev servers, REPLs, or commands that wait for "
            "interactive input. Prefer targeted validation commands over watch mode. "
            "In Worker mode this tool supports validation/build/test commands and "
            "dependency installs; use read_file/read_files/grep_search/"
            "read_file_outline for source inspection. "
            "Prefer detected project-local tools. For Python projects, validation prefers "
            "the project-local .venv interpreter when present. If a dependency is needed "
            "for the current coding task, install it with an appropriate command such as "
            "pip install, python -m pip install, uv sync, poetry install, or pdm install. "
            "IMPORTANT: If the user specifies a test or lint command, you MUST run it "
            "after modifying files. If the command fails, analyze the output and fix the "
            "code before finishing."
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
            "Execute a short, read-only diagnostic command in the workspace or an optional workspace-relative cwd. "
            "Use this to validate code with project-specific read-only commands, inspect git state (status, diff, log), "
            "or search the filesystem (rg, ls, cat). "
            "Rejects mutating, installing, or dangerous commands. "
            "Returns stdout, stderr, exit_code, timed_out, and the original command. "
            "Output is truncated at 100KB. "
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
            "read_file_outline when you want richer language-specific parsing. "
            "Always prefer read_file_outline for lightweight use."
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

