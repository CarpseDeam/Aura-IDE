"""Tool registry — workspace-jailed dispatch and OpenAI tool definitions.

The registry is the only place that:
- builds the API tool list (mode + read_only swap which tools are exposed)
- resolves and validates filesystem paths against workspace_root
- calls the GUI approval callback for writes
- creates timestamped backups before approved writes

Modes:
- "single"  — legacy / planner-worker disabled: read + write tools.
- "planner" — read tools + dispatch_to_worker; the planner cannot write.
- "worker"  — read + write tools, no dispatch (workers don't dispatch).
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from aura.conversation.tools.backup import backup_existing
from aura.conversation.tools.dynamic import execute_dynamic_tool, parse_tool_schema
from aura.conversation.tools.find_usages import find_usages
from aura.conversation.tools.fs_handler import FsReadHandler
from aura.conversation.tools.git_handler import GitHandler
from aura.conversation.tools.grep import grep_files
from aura.conversation.tools.fs_edit_structured import propose_edit_symbol
from aura.conversation.tools.fs_write import propose_edit, propose_write
from aura.codebase_index.tool import search_codebase as _search_codebase
from aura.codebase_index.indexer import CodebaseIndex
from aura.config import SEARCH_CODEBASE_TOP_K
from aura.conversation.tools.web_handler import WebHandler
from aura.mcp_client import MCPClient, _convert_tool_to_openai_schema
from aura.memory_db import ProjectMemoryDB
ApprovalAction = Literal["approve", "reject", "reject_all", "approve_all"]
RegistryMode = Literal["single", "planner", "worker", "researcher"]


@dataclass
class ApprovalRequest:
    """Passed to approval_cb when a write is proposed."""
    tool_name: str  # "write_file" or "edit_file"
    rel_path: str
    old_content: str
    new_content: str
    is_new_file: bool


@dataclass
class ApprovalDecision:
    action: ApprovalAction
    note: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


ApprovalCallback = Callable[[ApprovalRequest], ApprovalDecision]

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
                        "and the column where the match starts. "
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
                                "description": "Optional glob pattern to filter which files to search (e.g. '**/*.py' to only search Python files).",
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
            "workspace. Provide a complete, self-contained spec — the worker does not see "
            "this conversation. Include: goal, files involved (use exact paths from your "
            "earlier read_file calls), the specific change to make, any constraints. The "
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
                        "Full prose specification of the change. Be specific. Reference "
                        "function names, line behavior, error cases. The worker has not "
                        "seen the conversation, so include necessary context."
                    ),
                },
                "acceptance": {
                    "type": "string",
                    "description": (
                        "How the worker (and the user) knows the task is done. Concrete, checkable."
                    ),
                },
            },
            "required": ["goal", "files", "spec", "acceptance"],
        },
    },
}

WRITE_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write the given content to a workspace file, replacing it entirely if it exists. "
                "Use this for new files or when an edit would replace most of the file. "
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
            "name": "edit_file",
            "description": (
                "Surgically replace one occurrence of old_str with new_str inside a workspace file. "
                "Provide a Search Block (the code to replace plus a few lines of surrounding context "
                "for uniqueness). The matching is fuzzy — minor whitespace, indentation, or newline "
                "differences are tolerated. The user reviews and approves the diff before it's "
                "applied. Backed up first."
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
                "For non-Python files or partial replacements within a function, use edit_file instead."
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
            "Use this to run linters (e.g. 'ruff check .'), type checkers ('mypy .'), "
            "test suites ('pytest'), install dependencies ('pip install requests'), or "
            "any other CLI tool. The command runs with the workspace as its working "
            "directory. Stdout and stderr are both captured and streamed in real-time. "
            "Returns the exit code and complete output on completion. "
            "IMPORTANT: If the user specifies a test or lint command, you MUST run it "
            "after modifying files. If the command fails, analyze the output and fix the "
            "code before finishing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to execute, e.g. 'pytest tests/' or 'mypy .' or 'pip install requests'. Executed via the system shell.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Maximum seconds to wait before killing the command. Default: 120.",
                    "default": 120,
                },
            },
            "required": ["command"],
        },
    },
}

# Tool handler dispatch table.
# Maps tool name -> unbound method that accepts (self, args, approval_cb, reject_all).
TOOL_HANDLERS: dict[str, Any] = {}

def _make_mcp_handler(mcp_client: MCPClient, tool_name: str):
    """Create a handler closure for an MCP tool.

    Args:
        mcp_client: The MCPClient instance connected to the server.
        tool_name: Name of the tool on the MCP server.

    Returns:
        A handler function with signature (self, args, approval_cb, reject_all)
        suitable for registration in TOOL_HANDLERS.
    """
    def handler(self, args, approval_cb, reject_all):
        result = mcp_client.call_tool(tool_name, args)
        return ToolExecResult(ok=result.get("ok", False), payload=result)
    return handler


@dataclass
class ToolExecResult:
    ok: bool
    payload: dict[str, Any]
    extras: dict[str, Any] = field(default_factory=dict)

    def to_tool_message_content(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False)


class ToolRegistry:
    """Workspace-jailed tool dispatcher.

    `read_only` swaps the API tool list to read-only — the model literally cannot
    propose edits. Toggle it via `set_read_only` between turns.
    """

    def __init__(
        self,
        workspace_root: Path,
        read_only: bool = False,
        mode: RegistryMode = "single",
    ) -> None:
        self._root = workspace_root.resolve()
        self._read_only = read_only
        self._mode: RegistryMode = mode
        self._dynamic_cache: dict[str, Path] = {}
        self._dynamic_cache_mtimes: dict[str, float] = {}
        self._codebase_index: CodebaseIndex | None = None
        self._fs_handler = FsReadHandler(self._root, self._resolve_in_root)
        self._git_handler = GitHandler(self._root)
        self._web_handler = WebHandler()
        self._mcp_clients: dict[str, MCPClient] = {}
        self._mcp_schemas: list[dict[str, Any]] = []

    @property
    def workspace_root(self) -> Path:
        return self._root

    def set_workspace_root(self, root: Path) -> None:
        self._root = root.resolve()
        self._dynamic_cache.clear()
        self._dynamic_cache_mtimes.clear()
        # Reset codebase index for the new workspace
        self._codebase_index = None
        # Refresh handlers with the new root
        self._fs_handler = FsReadHandler(self._root, self._resolve_in_root)
        self._git_handler = GitHandler(self._root)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, value: bool) -> None:
        self._read_only = value

    @property
    def mode(self) -> RegistryMode:
        return self._mode

    def set_mode(self, mode: RegistryMode) -> None:
        self._mode = mode

    def tool_defs(self) -> list[dict[str, Any]]:
        # Read-only is the safety floor — strips writes AND dispatch (since
        # there's nothing for a worker to do without writes).
        tools: list[dict[str, Any]] = []
        if self._read_only:
            tools = list(READ_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        elif self._mode == "researcher":
            tools = list(WEB_TOOL_DEFS)
        elif self._mode == "planner":
            tools = list(READ_TOOL_DEFS) + [dict(DISPATCH_TOOL_DEF)] + list(RESEARCH_TOOL_DEFS) + list(PROJECT_MEMORY_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        elif self._mode == "worker":
            tools = list(READ_TOOL_DEFS) + list(WRITE_TOOL_DEFS) + [dict(WORKER_TODO_TOOL_DEF)] + [dict(TERMINAL_TOOL_DEF)] + list(GIT_TOOL_DEFS)
        else:
            tools = list(READ_TOOL_DEFS) + list(WRITE_TOOL_DEFS) + [dict(TERMINAL_TOOL_DEF)] + list(GIT_TOOL_DEFS)

        # Append dynamic tools (only when not read-only)
        if not self._read_only:
            for file_path in self._scan_dynamic_tools().values():
                try:
                    schema = parse_tool_schema(file_path)
                    tools.append(schema)
                except (ValueError, SyntaxError):
                    pass

        # Append MCP tool schemas (available in all modes)
        tools.extend(self._mcp_schemas)

        return tools

    # ---- MCP server support ------------------------------------------------

    def connect_mcp_server(self, server_command: str) -> int:
        """Launch an MCP server, fetch its tools, and register them.

        Args:
            server_command: Shell command to launch the MCP server, e.g.
                            "python -m my_mcp_server" or "node server.js".

        Returns:
            Number of tools registered from this server.

        Raises:
            RuntimeError: If the server fails to launch or initialize.
        """
        import os as _os
        parsed = shlex.split(server_command, posix=(_os.name != "nt"))
        client = MCPClient(parsed)
        client.connect()
        tool_defs = client.list_tools()

        count = 0
        for tool_def in tool_defs:
            schema = _convert_tool_to_openai_schema(tool_def)
            tool_name = tool_def["name"]
            self._mcp_schemas.append(schema)
            self._mcp_clients[tool_name] = client
            TOOL_HANDLERS[tool_name] = _make_mcp_handler(client, tool_name)
            count += 1

        return count

    # ---- dynamic tools -----------------------------------------------------

    def _scan_dynamic_tools(self) -> dict[str, Path]:
        """Scan .aura/tools/ for .py files and map tool names to file paths.

        Uses per-file mtime caching to avoid re-parsing unchanged files.
        """
        tools_dir = self._root / ".aura" / "tools"
        if not tools_dir.is_dir():
            self._dynamic_cache.clear()
            self._dynamic_cache_mtimes.clear()
            return {}

        current_files: set[str] = set()
        for entry in sorted(tools_dir.iterdir()):
            if not entry.is_file() or entry.suffix != ".py":
                continue
            if entry.name.startswith("_"):
                continue

            key = str(entry)
            current_files.add(key)
            mtime = entry.stat().st_mtime

            # Skip if unchanged since last parse
            if key in self._dynamic_cache_mtimes and self._dynamic_cache_mtimes[key] == mtime:
                continue

            try:
                schema = parse_tool_schema(entry)
                name = schema["function"]["name"]
                # Remove any old mapping for this file path (name may have changed)
                for old_name, old_path in list(self._dynamic_cache.items()):
                    if str(old_path) == key:
                        del self._dynamic_cache[old_name]
                        break
                self._dynamic_cache[name] = entry
                self._dynamic_cache_mtimes[key] = mtime
            except (ValueError, SyntaxError):
                pass

        # Remove entries for files that no longer exist
        stale_keys = set(self._dynamic_cache_mtimes.keys()) - current_files
        for key in stale_keys:
            for name, path in list(self._dynamic_cache.items()):
                if str(path) == key:
                    del self._dynamic_cache[name]
                    break
            del self._dynamic_cache_mtimes[key]

        return dict(self._dynamic_cache)

    # ---- path resolution ---------------------------------------------------

    def _resolve_in_root(self, raw: str) -> Path:
        """Resolve a workspace-relative path; raise if it escapes the jail.

        Rejections:
        - any '..' segment (even if final resolved path lands inside)
        - absolute paths outside the workspace
        - resolved paths not under the workspace root
        """
        if raw is None:
            raise ValueError("path is required")
        s = str(raw).strip()
        if s == "":
            raise ValueError("path must not be empty")

        # Strip leading slashes to prevent absolute path interpretation on Windows/Linux.
        # Models often provide /path/to/file or \path\to\file, which on Windows
        # resolves relative to the drive root, escaping the project jail.
        s = s.lstrip("/\\")

        if ".." in Path(s).parts:
            raise ValueError("'..' is not allowed in tool paths")
        candidate = (self._root / s).resolve() if not Path(s).is_absolute() else Path(s).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"path '{raw}' escapes workspace root")
        return candidate

    # ---- handler methods (one per tool) -----------------------------------

    def _handle_read_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_file(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_read_files(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_files(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_list_directory(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_list_directory(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_glob(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_glob(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_grep_search(self, args, approval_cb, reject_all) -> ToolExecResult:
        pattern = args.get("pattern", "")
        if not pattern:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "pattern is required"})
        return ToolExecResult(
            ok=True,
            payload=grep_files(
                workspace_root=self._root,
                pattern=pattern,
                regex_mode=bool(args.get("regex_mode", False)),
                case_sensitive=bool(args.get("case_sensitive", False)),
                max_results=int(args.get("max_results", 50)),
                include_pattern=args.get("include_pattern"),
            ),
        )

    def _handle_read_file_outline(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._fs_handler.handle_read_file_outline(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_find_usages(self, args, approval_cb, reject_all) -> ToolExecResult:
        symbol = args.get("symbol", "")
        if not symbol:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "symbol is required"})
        return ToolExecResult(
            ok=True,
            payload=find_usages(
                workspace_root=self._root,
                symbol=symbol,
                include_pattern=args.get("include_pattern"),
                max_results=int(args.get("max_results", 100)),
                case_sensitive=bool(args.get("case_sensitive", False)),
            ),
        )

    def _handle_search_codebase(self, args, approval_cb, reject_all) -> ToolExecResult:
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "query is required"})
        top_k = int(args.get("top_k", SEARCH_CODEBASE_TOP_K))
        if self._codebase_index is None:
            self._codebase_index = CodebaseIndex(self._root)
        result = _search_codebase(
            workspace_root=self._root,
            query=query,
            top_k=top_k,
            _index=self._codebase_index,
        )
        return ToolExecResult(ok=result.get("ok", False), payload=result)

    def _handle_git_status(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_status(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_diff(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_diff(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_log(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_log(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_show(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_show(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_git_log_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_log_file(args)
        return ToolExecResult(ok=payload.get("ok", False), payload=payload)

    def _handle_git_branch_list(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_branch_list(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_stash_list(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_stash_list(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_git_stash_show(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._git_handler.handle_git_stash_show(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_web_search(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._web_handler.handle_web_search(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_web_fetch(self, args, approval_cb, reject_all) -> ToolExecResult:
        payload = self._web_handler.handle_web_fetch(args)
        return ToolExecResult(ok=payload.get("ok", True), payload=payload)

    def _handle_write_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Planner cannot write directly — call dispatch_to_worker with a spec instead."})
        return self._handle_write("write_file", args, approval_cb, reject_all)

    def _handle_edit_file(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Planner cannot write directly — call dispatch_to_worker with a spec instead."})
        return self._handle_write("edit_file", args, approval_cb, reject_all)

    def _handle_edit_symbol(self, args, approval_cb, reject_all) -> ToolExecResult:
        if self._read_only:
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Read-Only Mode is enabled — write tools are disabled."})
        if self._mode == "planner":
            return ToolExecResult(ok=False, payload={"ok": False, "error": "Planner cannot write directly — call dispatch_to_worker with a spec instead."})
        return self._handle_write("edit_symbol", args, approval_cb, reject_all)

    def _handle_update_todo_list(self, args, approval_cb, reject_all) -> ToolExecResult:
        tasks = args.get("tasks", [])
        if not isinstance(tasks, list):
            return ToolExecResult(ok=False, payload={"ok": False, "error": "tasks must be an array"})
        for t in tasks:
            if not isinstance(t, dict):
                return ToolExecResult(ok=False, payload={"ok": False, "error": "each task must be an object"})
            if "description" not in t or "status" not in t:
                return ToolExecResult(ok=False, payload={"ok": False, "error": "each task must have description and status"})
            if t["status"] not in ("pending", "active", "done"):
                return ToolExecResult(ok=False, payload={"ok": False, "error": f"invalid status: {t['status']}"})
        return ToolExecResult(
            ok=True,
            payload={"ok": True, "message": "TODO list updated", "tasks": tasks},
            extras={"is_todo_update": True, "tasks": tasks},
        )

    # ---- project memory tools (Tier 2) ------------------------------------

    def _handle_search_project_memory(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Handle search_project_memory tool call."""
        query = str(args.get("query", "")).strip()
        if not query:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "query is required."},
            )
        top_k = int(args.get("top_k", 5))
        try:
            db = ProjectMemoryDB(self._root / ".aura" / "memory.db")
            results = db.search(query, top_k)
            if not results:
                return ToolExecResult(
                    ok=True,
                    payload={"ok": True, "message": "No matching memories found.", "results": []},
                )
            # Format results clearly
            lines: list[str] = []
            lines.append(f"Found {len(results)} result(s):\n")
            for r in results:
                lines.append(f"--- Memory #{r['id']} [{r.get('created_at', '?')}] ---")
                if r.get("metadata"):
                    lines.append(f"Metadata: {r['metadata']}")
                lines.append(r["content"])
                lines.append("")
            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "message": "\n".join(lines),
                    "results": results,
                },
            )
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Memory search failed: {exc}"},
            )

    def _handle_save_to_project_memory(self, args, approval_cb, reject_all) -> ToolExecResult:
        """Handle save_to_project_memory tool call."""
        content = str(args.get("content", "")).strip()
        if not content:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "content is required and must be non-empty."},
            )
        metadata = args.get("metadata")
        try:
            db = ProjectMemoryDB(self._root / ".aura" / "memory.db")
            memory_id = db.insert(content, metadata)
            return ToolExecResult(
                ok=True,
                payload={
                    "ok": True,
                    "message": f"Memory saved with ID #{memory_id}.",
                    "memory_id": memory_id,
                },
            )
        except Exception as exc:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": f"Failed to save memory: {exc}"},
            )

    # ---- main dispatch -----------------------------------------------------

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        try:
            # Static dispatch via TOOL_HANDLERS
            handler = TOOL_HANDLERS.get(name)
            if handler is not None:
                return handler(self, args, approval_cb, reject_all)

            # Check dynamic tools before giving up
            dynamic = self._scan_dynamic_tools()
            if name in dynamic:
                result = execute_dynamic_tool(dynamic[name], name, args, self._root)
                return ToolExecResult(ok=result.get("ok", False), payload=result)

            return ToolExecResult(
                ok=False, payload={"ok": False, "error": f"unknown tool: {name}"}
            )
        except (ValueError, OSError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})

    def _handle_write(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool,
    ) -> ToolExecResult:
        if reject_all:
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "User rejected all writes in this turn."},
                extras={"rejected_all": True},
            )

        path_arg = args.get("path", "")
        target = self._resolve_in_root(path_arg)

        if name == "write_file":
            content = args.get("content", "")
            if not isinstance(content, str):
                return ToolExecResult(
                    ok=False, payload={"ok": False, "error": "content must be a string"}
                )
            proposal = propose_write(self._root, target, content)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="write_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=proposal["is_new_file"],
            )
        elif name == "edit_file":
            old_str = args.get("old_str", "")
            new_str = args.get("new_str", "")
            if not isinstance(old_str, str) or not isinstance(new_str, str):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "old_str and new_str must be strings"},
                )
            proposal = propose_edit(self._root, target, old_str, new_str)
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="edit_file",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )
        else:  # edit_symbol
            symbol_type = args.get("symbol_type", "")
            symbol_name = args.get("symbol_name", "")
            new_definition = args.get("new_definition", "")
            class_name = args.get("class_name")
            if not isinstance(symbol_type, str) or not isinstance(symbol_name, str) or not isinstance(new_definition, str):
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "error": "symbol_type, symbol_name, and new_definition must be strings"},
                )
            proposal = propose_edit_symbol(
                self._root, target, symbol_type, symbol_name, new_definition, class_name
            )
            if not proposal.get("ok", False):
                return ToolExecResult(ok=False, payload=proposal)
            req = ApprovalRequest(
                tool_name="edit_symbol",
                rel_path=proposal["rel_path"],
                old_content=proposal["old_content"],
                new_content=proposal["new_content"],
                is_new_file=False,
            )

        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "User rejected this change."},
                extras={
                    "approval": "reject",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "User rejected this change and all further writes in this turn.",
                },
                extras={
                    "approval": "reject_all",
                    "rel_path": req.rel_path,
                    "approval_metadata": decision.metadata,
                },
            )

        # Approve — back up if file exists, write new content.
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = backup_existing(self._root, target)
        target.write_text(req.new_content, encoding="utf-8")
        rel_backup = (
            backup_path.relative_to(self._root).as_posix() if backup_path is not None else None
        )
        return ToolExecResult(
            ok=True,
            payload={
                "ok": True,
                "path": req.rel_path,
                "applied": name,
                "is_new_file": req.is_new_file,
                "backup": rel_backup,
            },
            extras={
                "approval": "approve",
                "rel_path": req.rel_path,
                "approval_metadata": decision.metadata,
            },
        )


# Populate the dispatch table after ToolRegistry is defined
TOOL_HANDLERS["read_file"] = ToolRegistry._handle_read_file
TOOL_HANDLERS["read_files"] = ToolRegistry._handle_read_files
TOOL_HANDLERS["list_directory"] = ToolRegistry._handle_list_directory
TOOL_HANDLERS["glob"] = ToolRegistry._handle_glob
TOOL_HANDLERS["grep_search"] = ToolRegistry._handle_grep_search
TOOL_HANDLERS["read_file_outline"] = ToolRegistry._handle_read_file_outline
TOOL_HANDLERS["find_usages"] = ToolRegistry._handle_find_usages
TOOL_HANDLERS["search_codebase"] = ToolRegistry._handle_search_codebase
TOOL_HANDLERS["git_status"] = ToolRegistry._handle_git_status
TOOL_HANDLERS["git_diff"] = ToolRegistry._handle_git_diff
TOOL_HANDLERS["git_log"] = ToolRegistry._handle_git_log
TOOL_HANDLERS["git_show"] = ToolRegistry._handle_git_show
TOOL_HANDLERS["git_log_file"] = ToolRegistry._handle_git_log_file
TOOL_HANDLERS["git_branch_list"] = ToolRegistry._handle_git_branch_list
TOOL_HANDLERS["git_stash_list"] = ToolRegistry._handle_git_stash_list
TOOL_HANDLERS["git_stash_show"] = ToolRegistry._handle_git_stash_show
TOOL_HANDLERS["web_search"] = ToolRegistry._handle_web_search
TOOL_HANDLERS["web_fetch"] = ToolRegistry._handle_web_fetch
TOOL_HANDLERS["write_file"] = ToolRegistry._handle_write_file
TOOL_HANDLERS["edit_file"] = ToolRegistry._handle_edit_file
TOOL_HANDLERS["edit_symbol"] = ToolRegistry._handle_edit_symbol
TOOL_HANDLERS["update_todo_list"] = ToolRegistry._handle_update_todo_list
TOOL_HANDLERS["search_project_memory"] = ToolRegistry._handle_search_project_memory
TOOL_HANDLERS["save_to_project_memory"] = ToolRegistry._handle_save_to_project_memory
