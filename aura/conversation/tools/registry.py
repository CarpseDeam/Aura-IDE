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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from aura.conversation.tools.backup import backup_existing
from aura.conversation.tools.find_usages import find_usages
from aura.conversation.tools.fs_read import glob_files, list_directory, read_file, read_file_outline
from aura.conversation.tools.git_tools import git_diff, git_status
from aura.conversation.tools.grep import grep_files
from aura.conversation.tools.fs_write import propose_edit, propose_write
from aura.conversation.tools.web import web_search, web_fetch

ApprovalAction = Literal["approve", "reject", "reject_all", "approve_all"]
RegistryMode = Literal["single", "planner", "worker"]


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
        ]
GIT_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "git_status",
            "description": (
                "Show the current git working tree status. Returns the current branch "
                "name and lists staged, unstaged, and untracked files. Use this before "
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

WEB_TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for a given query and return a list of matching URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return.",
                        "default": 5,
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch the contents of a specific URL and return the readable text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch.",
                    }
                },
                "required": ["url"],
            },
        },
    }
]


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

    @property
    def workspace_root(self) -> Path:
        return self._root

    def set_workspace_root(self, root: Path) -> None:
        self._root = root.resolve()

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
        if self._read_only:
            return list(READ_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        if self._mode == "researcher":
            return list(WEB_TOOL_DEFS)
        if self._mode == "planner":
            return list(READ_TOOL_DEFS) + [dict(DISPATCH_TOOL_DEF)] + list(RESEARCH_TOOL_DEFS) + list(GIT_TOOL_DEFS)
        if self._mode == "worker":
            return list(READ_TOOL_DEFS) + list(WRITE_TOOL_DEFS) + [dict(WORKER_TODO_TOOL_DEF)] + [dict(TERMINAL_TOOL_DEF)] + list(GIT_TOOL_DEFS)
        return list(READ_TOOL_DEFS) + list(WRITE_TOOL_DEFS) + [dict(TERMINAL_TOOL_DEF)] + list(GIT_TOOL_DEFS)

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
        if ".." in Path(s).parts:
            raise ValueError("'..' is not allowed in tool paths")
        candidate = (self._root / s).resolve() if not Path(s).is_absolute() else Path(s).resolve()
        if not candidate.is_relative_to(self._root):
            raise ValueError(f"path '{raw}' escapes workspace root")
        return candidate

    # ---- main dispatch -----------------------------------------------------

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        try:
            if name == "read_file":
                target = self._resolve_in_root(args.get("path", ""))
                return ToolExecResult(ok=True, payload=read_file(self._root, target))
            if name == "list_directory":
                target = self._resolve_in_root(args.get("path", "."))
                return ToolExecResult(ok=True, payload=list_directory(self._root, target))
            if name == "glob":
                pattern = str(args.get("pattern", "")).strip()
                if not pattern:
                    return ToolExecResult(
                        ok=False, payload={"ok": False, "error": "pattern is required"}
                    )
                if ".." in Path(pattern).parts or Path(pattern).is_absolute():
                    return ToolExecResult(
                        ok=False,
                        payload={"ok": False, "error": "glob pattern must be workspace-relative"},
                    )
                return ToolExecResult(ok=True, payload=glob_files(self._root, pattern))
            if name == "grep_search":
                pattern = args.get("pattern", "")
                if not pattern:
                    return ToolExecResult(
                        ok=False, payload={"ok": False, "error": "pattern is required"}
                    )
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
            if name == "read_file_outline":
                target = self._resolve_in_root(args.get("path", ""))
                return ToolExecResult(ok=True, payload=read_file_outline(self._root, target))
            if name == "find_usages":
                symbol = args.get("symbol", "")
                if not symbol:
                    return ToolExecResult(
                        ok=False, payload={"ok": False, "error": "symbol is required"}
                    )
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
            if name == "git_status":
                return ToolExecResult(ok=True, payload=git_status(self._root))
            if name == "git_diff":
                staged = bool(args.get("staged", False))
                path = args.get("path")
                return ToolExecResult(ok=True, payload=git_diff(self._root, staged=staged, path=path))
            if name == "web_search":
                if self._mode != "researcher":
                    return ToolExecResult(ok=False, payload={"ok": False, "error": "Tool web_search is only available in researcher mode."})
                query = args.get("query", "")
                max_results = args.get("max_results", 5)
                return ToolExecResult(ok=True, payload=web_search(query, max_results))
            if name == "web_fetch":
                if self._mode != "researcher":
                    return ToolExecResult(ok=False, payload={"ok": False, "error": "Tool web_fetch is only available in researcher mode."})
                url = args.get("url", "")
                return ToolExecResult(ok=True, payload=web_fetch(url))
            if name in ("write_file", "edit_file"):
                if self._read_only:
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "error": "Read-Only Mode is enabled — write tools are disabled.",
                        },
                    )
                if self._mode == "planner":
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "error": (
                                "Planner cannot write directly — call dispatch_to_worker with "
                                "a spec instead."
                            ),
                        },
                    )
                return self._handle_write(name, args, approval_cb, reject_all)
            if name == "update_todo_list":
                tasks = args.get("tasks", [])
                if not isinstance(tasks, list):
                    return ToolExecResult(ok=False, payload={"ok": False, "error": "tasks must be an array"})
                # Validate each task has required fields
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
        else:  # edit_file
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

        decision = approval_cb(req)

        if decision.action == "reject":
            return ToolExecResult(
                ok=False,
                payload={"ok": False, "error": "User rejected this change."},
                extras={"approval": "reject", "rel_path": req.rel_path},
            )
        if decision.action == "reject_all":
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "error": "User rejected this change and all further writes in this turn.",
                },
                extras={"approval": "reject_all", "rel_path": req.rel_path},
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
            extras={"approval": "approve", "rel_path": req.rel_path},
        )
