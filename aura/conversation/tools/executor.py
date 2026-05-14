"""Tool execution dispatcher — routes tool calls to static, MCP, or dynamic handlers."""
from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import ApprovalCallback, ToolExecResult
from aura.conversation.tools.dynamic import execute_dynamic_tool


class ToolExecutor:
    """Dispatches tool execution across static handlers, MCP, and dynamic tools."""

    def __init__(
        self,
        *,
        owner: Any,
        dynamic_tools: Any,  # DynamicToolRegistry
        mcp_tools: Any,      # MCPToolRegistry
    ) -> None:
        self._owner = owner
        self._dynamic_tools = dynamic_tools
        self._mcp_tools = mcp_tools

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
    ) -> ToolExecResult:
        """Dispatch a tool call to the appropriate handler.

        Priority:
        1. Static handlers (TOOL_HANDLERS)
        2. MCP tools
        3. Dynamic tools
        4. Unknown tool error
        """
        try:
            from aura.conversation.tools.registry import TOOL_HANDLERS

            # 1. Static dispatch via TOOL_HANDLERS
            handler = TOOL_HANDLERS.get(name)
            if handler is not None:
                return handler(self._owner, args, approval_cb, reject_all)

            # 2. MCP tools
            if self._mcp_tools.can_execute(name):
                return self._mcp_tools.execute(name, args)

            # 3. Dynamic tools
            dynamic_path = self._dynamic_tools.get(name)
            if dynamic_path is not None:
                result = execute_dynamic_tool(
                    dynamic_path, name, args, self._owner.workspace_root
                )
                return ToolExecResult(ok=result.get("ok", False), payload=result)

            # 4. Unknown
            return ToolExecResult(
                ok=False, payload={"ok": False, "error": f"unknown tool: {name}"}
            )
        except (ValueError, OSError) as exc:
            return ToolExecResult(ok=False, payload={"ok": False, "error": str(exc)})
