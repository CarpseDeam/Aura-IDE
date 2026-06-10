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
                "with ok/error or ok/content for each path. "
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
                    "name": "grep_search",
                    "description": (
                        "Search file contents in the workspace for a given string or regex pattern. "
                        "Returns matching file paths, line numbers, the matching line content, "
                        "and the column where the match starts, plus search metadata such as the "
                        "engine used, searched file count, skipped file count, truncation, and regex retry state. "
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
                                "description": "If true, treat pattern as a regex. If false, plain text substring match.",
                                "default": False,
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
                                    "Optional glob pattern restricting which files are searched. "
                                    "This uses workspace-relative glob matching; use patterns such as "
                                    "'**/*.py' to search Python files anywhere in the repo. "
                                    "Prefer '**/*.py' over '*.py' when you want recursive Python-only search."
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
            "workspace. Provide a complete, self-contained implementation handoff — the "
            "worker does not see this conversation. Include: goal, files involved (use "
            "exact paths from your earlier read_file calls), a concise Builder Note with "
            "the specific change and important constraints, and acceptance checks. The "
            "worker will return a summary of what it did."
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
                "spec": {
                    "type": "string",
                    "description": (
                        "Self-contained Builder Note / implementation handoff. Write concise "
                        "plain English, like a senior engineer handing work to a capable "
                        "builder. Include the important behavior, constraints, and known "
                        "pitfalls. Do not require or default to formal sections such as Core "
                        "Behavior, Failure Behavior, Code Shape, File-by-File Implementation "
                        "Plan, Acceptance Checks, or Non-Goals. A fuller structured spec is "
                        "optional only for broad, risky, or ambiguous work such as cross-file "
                        "refactors, auth/security, subprocess/threading/async behavior, "
                        "persistence/data model changes, destructive file operations, public "
                        "API/signature changes, or build/release/update system work. The "
                        "worker has not seen the conversation, so include necessary context."
                    ),
                },
                "acceptance": {
                    "type": "string",
                    "description": (
                        "Concrete pass/fail checks proving the task is done. Include "
                        "validation commands when possible, concrete output/content checks "
                        "for generated or transformed output, and failure behavior checks "
                        "when parsing, config, user input, or batch processing is involved."
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
                        "Things explicitly not to build "
                        "(e.g. ['No new CLI flags', 'No database migration']). "
                        "When provided, these override Non-Goals parsed from spec."
                    ),
                },
                "expected_public_symbols": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Names of public symbols (classes, functions, constants) the Worker must define. "
                        "The ContractGate will verify these exist in the output."
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
                        "The ContractGate will verify these fields exist on the corresponding dataclass."
                    ),
                },
                "forbidden_public_methods": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Method names the Worker must NOT introduce on public classes, "
                        "e.g. ['to_dict', 'from_dict'] on domain models that shouldn't have serialization."
                    ),
                },
                "forbidden_calls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Function call names the Worker must NOT use, "
                        "e.g. ['print', 'input'] for backend code or ['eval', 'exec'] for security."
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
            "name": "edit_file",
            "description": (
                "Surgically replace one occurrence of old_str with new_str inside a workspace file. "
                "Provide a Search Block (the code to replace plus a few lines of surrounding context "
                "for uniqueness). The matching is fuzzy — minor whitespace, indentation, or newline "
                "differences are tolerated. The user reviews and approves the diff before it's "
                "applied. Backed up first. If matching fails, read nearest_candidates and switch "
                "to edit_line_range or write_file instead of retrying the same old_str."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the file to edit.",
                    },
                    "old_str": {
                        "type": "string",
                        "description": "The Search Block — the code to find and replace. Include a few lines of surrounding context to make it unique. Exact whitespace is not required; the system will find the best match.",
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_symbol",
            "description": (
                "Replace a named function, class, or method in a Python (.py) file by specifying its name "
                "instead of providing exact old code. Uses AST parsing to locate the symbol and replace its "
                "entire definition. This is the preferred way to edit Python code when you know the function "
                "or class name — it avoids indentation and whitespace matching issues. "
                "You must include all original decorators in your new_definition, as the replacement will overwrite "
                "the existing decorators. "
                "For non-Python files or partial replacements within a function, use edit_file instead. "
                "If the symbol is not found, inspect available_symbols and switch to edit_line_range "
                "or write_file instead of retrying the same symbol shape."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path to the Python file.",
                    },
                    "symbol_type": {
                        "type": "string",
                        "enum": ["function", "class", "method"],
                        "description": "Type of symbol to replace: 'function' (top-level), 'class' (top-level), or 'method' (requires class_name).",
                    },
                    "symbol_name": {
                        "type": "string",
                        "description": "Name of the function, class, or method to replace.",
                    },
                    "new_definition": {
                        "type": "string",
                        "description": "The complete new definition including decorators, signature, docstring, and body. It will replace the entire existing definition.",
                    },
                    "class_name": {
                        "type": "string",
                        "description": "Required when symbol_type is 'method' — the name of the class containing the method.",
                    },
                },
                "required": ["path", "symbol_type", "symbol_name", "new_definition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_line_range",
            "description": "Replace or insert at an exact line range in a file. Use after reading the file when you know the exact start and end line numbers. 1-based, inclusive start_line, exclusive end_line (like Python list slicing — replaces lines [start_line, end_line)). start_line == end_line inserts before that line; start_line == end_line == num_lines + 1 appends at EOF. Requires preceding read_file or read_files on this path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path (e.g. 'src/main.py')."
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace (1-based, inclusive)."
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Line after the last line to replace (1-based, exclusive). Replaces lines [start_line, end_line). Equal to start_line for insertion."
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement content to insert in place of the removed lines."
                    },
                    "expected_old_str": {
                        "type": "string",
                        "description": "Strongly preferred for replacements/deletions after reading: the exact current text in [start_line, end_line). If it does not match, the edit is rejected without mutation."
                    },
                    "expected_old_hash": {
                        "type": "string",
                        "description": "Optional SHA-256 hex digest of the exact current text in [start_line, end_line). Use when expected_old_str would be too large."
                    }
                },
                "required": ["path", "start_line", "end_line", "new_str"],
                "additionalProperties": False
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_edit_transaction",
            "description": (
                "Apply a structured, atomic edit transaction to one existing workspace file. "
                "Legacy escape-hatch edit tool; hidden from default Worker mode. "
                "Default Worker mode should use patch_file for existing-file code changes. "
                "The tool reads the file once, "
                "checks expected_file_hash when supplied, applies every operation to an in-memory "
                "copy, validates final Python syntax for .py files, and shows one approval diff. "
                "If any operation cannot be applied safely, nothing is written."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative path of the existing file to edit.",
                    },
                    "expected_file_hash": {
                        "type": "string",
                        "description": "Optional SHA-256 hex digest of the exact current whole file content.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional short description of the transaction.",
                    },
                    "operations": {
                        "type": "array",
                        "description": "Ordered structured edit operations to apply atomically.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "op": {
                                    "type": "string",
                                    "enum": [
                                        "replace_function",
                                        "replace_method",
                                        "replace_class",
                                        "insert_after_symbol",
                                        "replace_text_once",
                                        "remove_text_once",
                                        "remove_text_all",
                                        "remove_between_markers",
                                    ],
                                },
                                "symbol_type": {
                                    "type": "string",
                                    "enum": ["function", "method", "class"],
                                    "description": "Target type for insert_after_symbol.",
                                },
                                "symbol_name": {
                                    "type": "string",
                                    "description": (
                                        "Canonical function, method, or class name. "
                                        "Natural aliases function_name, method_name, name, and class_name "
                                        "are accepted for operations where they identify the target symbol."
                                    ),
                                },
                                "function_name": {
                                    "type": "string",
                                    "description": (
                                        "Alias for symbol_name when targeting a function, including "
                                        "replace_function and insert_after_symbol with symbol_type 'function'."
                                    ),
                                },
                                "method_name": {
                                    "type": "string",
                                    "description": (
                                        "Alias for symbol_name when targeting a method, including "
                                        "replace_method and insert_after_symbol with symbol_type 'method'. "
                                        "May be fully qualified as Class.method; unqualified names resolve "
                                        "when exactly one top-level class has that method."
                                    ),
                                },
                                "name": {
                                    "type": "string",
                                    "description": (
                                        "Ergonomic alias for symbol_name where the operation context "
                                        "makes the target symbol type clear."
                                    ),
                                },
                                "class_name": {
                                    "type": "string",
                                    "description": (
                                        "Optional containing class name for replace_method and method "
                                        "insert operations. "
                                        "For replace_class, and for insert_after_symbol with symbol_type 'class', "
                                        "class_name may identify the target class when symbol_name is omitted."
                                    ),
                                },
                                "new_definition": {
                                    "type": "string",
                                    "description": (
                                        "Complete replacement definition for replace_function, "
                                        "replace_method, or replace_class."
                                    ),
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Content to insert after a symbol.",
                                },
                                "old": {
                                    "type": "string",
                                    "description": "Exact text to replace or remove; escape hatch only.",
                                },
                                "new": {
                                    "type": "string",
                                    "description": "Replacement text for replace_text_once.",
                                },
                                "before": {
                                    "type": "string",
                                    "description": (
                                        "Optional unique surrounding context before stale old text for "
                                        "replace_text_once recovery. Used only when exact/newline/trimmed "
                                        "matching cannot safely locate old."
                                    ),
                                },
                                "after": {
                                    "type": "string",
                                    "description": (
                                        "Optional unique surrounding context after stale old text for "
                                        "replace_text_once recovery. Used only when exact/newline/trimmed "
                                        "matching cannot safely locate old."
                                    ),
                                },
                                "text": {
                                    "type": "string",
                                    "description": "Exact text block to remove for remove_text_once or remove_text_all.",
                                },
                                "start_marker": {
                                    "type": "string",
                                    "description": "Exact unique starting marker for remove_between_markers.",
                                },
                                "end_marker": {
                                    "type": "string",
                                    "description": "Exact unique ending marker for remove_between_markers.",
                                },
                                "occurrence": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "1-based occurrence to replace when replace_text_once old "
                                        "text appears multiple times."
                                    ),
                                },
                                "allow_multiple": {
                                    "type": "boolean",
                                    "description": (
                                        "For replace_text_once: replace all matching old text. "
                                        "For remove_text_all: required and must be true."
                                    ),
                                },
                            },
                            "required": ["op"],
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
            "name": "patch_file",
            "description": (
                "Apply multiple exact-text replacement hunks to one existing workspace file as a single "
                "atomic, approval-gated transaction. Use this for normal existing-file edits after "
                "reading the file. Every hunk is applied to an in-memory copy first; "
                "if any hunk is missing or ambiguous, nothing is written. Craft reviews the full proposed "
                "file once and the user sees one approval diff. If a patch fails, re-read the file before "
                "retrying; do not switch between edit tools trying random tactics."
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
                        "description": "Optional SHA-256 hex digest of the current whole file.",
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

RESEARCH_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "run_research",
            "description": (
                "Dispatch an open-ended research task to a background sub-agent. The agent will "
                "autonomously use search engines and web scraping to gather information before "
                "returning a summarized report. Use this to look up documentation, troubleshooting "
                "steps, or general information not found in the workspace."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective": {
                        "type": "string",
                        "description": "The specific question or goal the researcher should answer.",
                    }
                },
                "required": ["objective"],
            },
        },
    }
]

WORKER_TODO_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_todo_list",
        "description": (
            "Update the worker's pinned TODO list. Call this before making file changes "
            "to establish your execution plan, and update as task statuses change. "
            "The list is displayed prominently to the user."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tasks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string", "description": "Short description of the task."},
                            "status": {"type": "string", "enum": ["pending", "active", "done"]},
                        },
                        "required": ["description", "status"],
                    },
                }
            },
            "required": ["tasks"],
        },
    },
}

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

WEB_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the web using Tavily. Returns a list of result objects "
                "with title, url, and content (snippet)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query, e.g. 'python 3.13 features'.",
                    },
                    "max_results": {
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
            "name": "web_fetch",
            "description": (
                "Fetch and scrape the text content of a URL. Returns the page title "
                "and a cleaned, truncated text version of the content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full URL to fetch, e.g. 'https://docs.python.org/3/whatsnew/3.13.html'.",
                    }
                },
                "required": ["url"],
            },
        },
    },
]

TERMINAL_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_terminal_command",
        "description": (
            "Execute a shell command in the workspace directory and stream its output. "
            "Use this to run project validation/build commands: linters, type checkers, "
            "test suites explicitly requested by the user, or other validation/build "
            "commands. The command runs with the workspace as its working "
            "directory. Stdout and stderr are both captured and streamed in real-time, "
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
            },
            "required": ["command"],
        },
    },
}

DIAGNOSTIC_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "run_diagnostic_command",
        "description": (
            "Execute a short, read-only diagnostic command in the workspace. "
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
                            "A read-only diagnostic command. Examples: "
                            "'python -m py_compile aura/gui/left_pane.py', "
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

LAUNCH_READ_ONLY_DRONE_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "launch_read_only_drone",
        "description": (
            "Launch a saved read-only Drone in the background for a focused "
            "investigation sub-task. Returns immediately with a run_id. "
            "Use check_drone_run later to retrieve results. "
            "Use this when the task is a focused side investigation (bug tracing, "
            "impact scouting, test discovery) that would otherwise burn tool calls "
            "or clutter the main conversation. Do NOT use for tiny tasks where "
            "direct inspection is faster."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "drone_id": {
                    "type": "string",
                    "description": "The id of the saved read-only Drone to run (from Available Drones list).",
                },
                "goal": {
                    "type": "string",
                    "description": "What the Drone should investigate or accomplish. Be specific so the Drone's instructions can guide it precisely.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional: why you are launching this Drone. Used only for logging.",
                },
            },
            "required": ["drone_id", "goal"],
        },
    },
}


CHECK_DRONE_RUN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "check_drone_run",
        "description": (
            "Check the status of a previously launched read-only Drone run. "
            "Returns queued/running/completed/failed/timed_out state. "
            "If completed, includes summary, tool call counts, and elapsed time. "
            "Optionally wait a few seconds for completion (capped at 10s)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "run_id": {
                    "type": "string",
                    "description": "The run_id returned from launch_read_only_drone.",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "Optional: seconds to wait for completion (capped at 10). Default 0 (return immediately).",
                },
                "include_receipt": {
                    "type": "boolean",
                    "description": "If true, include the full receipt in the result. Default false.",
                },
            },
            "required": ["run_id"],
        },
    },
}


RESOLVE_CAPABILITY_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "resolve_capability",
        "description": (
            "Ask Aura to resolve which tool route can satisfy a capability requirement. "
            "This is a planning / discovery tool only — it does not install, connect, or execute anything. "
            "Returns candidate routes, selected bindings, final allowed tool names, and setup notes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "capability": {"type": "string", "description": "The capability name, e.g. 'read inbox' or 'run sql query'."},
                            "purpose": {"type": "string", "description": "Why this capability is needed.", "default": ""},
                            "notes": {"type": "string", "description": "Additional notes.", "default": ""},
                        },
                        "required": ["capability"],
                    },
                    "description": "List of capability requirements to resolve.",
                },
            },
            "required": ["requirements"],
        },
    },
}
