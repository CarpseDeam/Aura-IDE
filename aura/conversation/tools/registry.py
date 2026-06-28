"""Tool registry facade for Aura conversation tools."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from aura.codebase_index.indexer import CodebaseIndex  # noqa: F401
from aura.codebase_index.tool import search_codebase as _search_codebase  # noqa: F401
from aura.conversation.tools._code_intel_mixin import CodeIntelHandlersMixin
from aura.conversation.tools._diagnostic_mixin import DiagnosticHandlersMixin
from aura.conversation.tools._git_mixin import GitHandlersMixin
from aura.conversation.tools._memory_mixin import MemoryHandlersMixin
from aura.conversation.tools._planner_mixin import PlannerHandlersMixin
from aura.conversation.tools._read_mixin import ReadHandlersMixin
from aura.conversation.tools._search_mixin import SearchHandlersMixin
from aura.conversation.tools._types import (
    ApprovalCallback,
    RegistryMode,
    ToolExecResult,
)
from aura.conversation.tools._write_mixin import WriteHandlersMixin
from aura.conversation.tools.backup import backup_existing  # noqa: F401
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.dynamic_registry import DynamicToolRegistry
from aura.conversation.tools.executor import ToolExecutor
from aura.conversation.tools.find_usages import find_usages  # noqa: F401
from aura.conversation.tools.fs_handler import FsReadHandler
from aura.conversation.tools.fs_write import (  # noqa: F401
    propose_patch_file,
    propose_write,
)
from aura.conversation.tools.git_handler import GitHandler
from aura.conversation.tools.grep import grep_files  # noqa: F401
from aura.conversation.tools.mcp_registry import MCPToolRegistry
from aura.conversation.tools.task_context import TaskContextHandlersMixin

try:
    from aura.craft import ExplicitSpecContract
except ImportError:
    ExplicitSpecContract = None

try:
    from aura.conversation.task_shape import TaskShape
except ImportError:
    TaskShape = None

TOOL_HANDLERS: dict[str, Any] = {}


class ToolRegistry(
    CodeIntelHandlersMixin,
    TaskContextHandlersMixin,
    ReadHandlersMixin,
    SearchHandlersMixin,
    GitHandlersMixin,
    WriteHandlersMixin,
    MemoryHandlersMixin,
    DiagnosticHandlersMixin,
    PlannerHandlersMixin,
):
    """Workspace-scoped tool dispatcher."""

    def __init__(
        self,
        workspace_root: Path,
        read_only: bool = False,
        mode: RegistryMode = "single",
    ) -> None:
        self._root = workspace_root.resolve()
        self._read_only = read_only
        self._mode: RegistryMode = mode
        self._codebase_index: CodebaseIndex | None = None
        self._fs_handler = FsReadHandler(self._root, self._resolve_in_root)
        self._git_handler = GitHandler(self._root)
        self._catalog = ToolCatalog()
        self._dynamic_tools = DynamicToolRegistry(self._root)
        self._mcp_tools = MCPToolRegistry()
        self._contract: ExplicitSpecContract | None = None
        self._task_shape: TaskShape | None = None
        self._executor = ToolExecutor(
            owner=self,
            dynamic_tools=self._dynamic_tools,
            mcp_tools=self._mcp_tools,
        )
        self._drone_budget: dict[str, int] = {}

    @property
    def workspace_root(self) -> Path:
        return self._root

    def set_workspace_root(self, root: Path | None) -> None:
        if root is None:
            return
        self._root = root.resolve()
        self._dynamic_tools.set_workspace_root(self._root)
        self._codebase_index = None
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
        dynamic_schemas = self._dynamic_tools.schemas() if not self._read_only else []
        return self._catalog.build_tool_defs(
            mode=self._mode,
            read_only=self._read_only,
            dynamic_schemas=dynamic_schemas or None,
            mcp_schemas=self._mcp_tools.schemas or None,
        )

    def connect_mcp_server(self, server_command: str) -> int:
        return self._mcp_tools.connect_server(server_command)

    def set_contract(self, contract: ExplicitSpecContract | None) -> None:
        self._contract = contract

    def get_contract(self) -> ExplicitSpecContract | None:
        return self._contract

    def set_task_shape(self, task_shape: TaskShape | None) -> None:
        self._task_shape = task_shape

    def get_task_shape(self) -> TaskShape | None:
        return getattr(self, "_task_shape", None)

    def _resolve_in_root(self, raw: str) -> Path:
        if raw is None:
            raise ValueError("path is required")
        s = str(raw).strip()
        if s == "":
            raise ValueError("path must not be empty")
        s = s.lstrip("/\\")
        if ".." in Path(s).parts:
            raise ValueError("'..' is not allowed in tool paths")
        candidate = (self._root / s).resolve() if not Path(s).is_absolute() else Path(s).resolve()
        from aura.paths import safe_is_relative_to
        if not safe_is_relative_to(candidate, self._root):
            raise ValueError(f"path '{raw}' escapes workspace root")
        return candidate

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        return self._executor.execute(name, args, approval_cb, reject_all)


TOOL_HANDLERS["read_file"] = ToolRegistry._handle_read_file
TOOL_HANDLERS["read_files"] = ToolRegistry._handle_read_files
TOOL_HANDLERS["read_file_range"] = ToolRegistry._handle_read_file_range
TOOL_HANDLERS["read_task_context"] = ToolRegistry._handle_read_task_context
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
TOOL_HANDLERS["write_file"] = ToolRegistry._handle_write_file
TOOL_HANDLERS["delete_file"] = ToolRegistry._handle_delete_file
TOOL_HANDLERS["patch_file"] = ToolRegistry._handle_patch_file
TOOL_HANDLERS["update_todo_list"] = ToolRegistry._handle_update_todo_list
TOOL_HANDLERS["search_project_memory"] = ToolRegistry._handle_search_project_memory
TOOL_HANDLERS["save_to_project_memory"] = ToolRegistry._handle_save_to_project_memory
TOOL_HANDLERS["run_diagnostic_command"] = ToolRegistry._handle_run_diagnostic_command
TOOL_HANDLERS["get_workspace_snapshot"] = ToolRegistry._handle_get_workspace_snapshot
TOOL_HANDLERS["summon_drone"] = ToolRegistry._handle_summon_drone
TOOL_HANDLERS["launch_read_only_drone"] = ToolRegistry._handle_launch_read_only_drone
TOOL_HANDLERS["run_read_only_drone"] = ToolRegistry._handle_run_read_only_drone
TOOL_HANDLERS["check_drone_run"] = ToolRegistry._handle_check_drone_run
TOOL_HANDLERS["register_drone_folder"] = ToolRegistry._handle_register_drone_folder
TOOL_HANDLERS["declare_ui_contract"] = ToolRegistry._handle_declare_ui_contract
TOOL_HANDLERS["code_intel_outline"] = ToolRegistry._handle_code_intel_outline
TOOL_HANDLERS["code_intel_references"] = ToolRegistry._handle_code_intel_references
TOOL_HANDLERS["code_intel_dependents"] = ToolRegistry._handle_code_intel_dependents
TOOL_HANDLERS["code_intel_audit"] = ToolRegistry._handle_code_intel_audit
