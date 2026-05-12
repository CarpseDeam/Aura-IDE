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

import shlex
from pathlib import Path
from typing import Any

from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalRequest,
    RegistryMode,
    ToolExecResult,
)
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

from aura.conversation.tools._schemas import (
    DISPATCH_TOOL_DEF,
    GIT_TOOL_DEFS,
    PROJECT_MEMORY_TOOL_DEFS,
    READ_TOOL_DEFS,
    RESEARCH_TOOL_DEFS,
    TERMINAL_TOOL_DEF,
    WEB_TOOL_DEFS,
    WORKER_TODO_TOOL_DEF,
    WRITE_TOOL_DEFS,
)

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
