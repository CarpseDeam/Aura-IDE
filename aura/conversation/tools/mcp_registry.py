"""MCP tool registry — owns MCP clients, schemas, and execution dispatch."""
from __future__ import annotations

import json
import os as _os
import shlex
from typing import Any

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult
from aura.conversation.tools.consequential import is_consequential
from aura.mcp_client import MCPClient, _convert_tool_to_openai_schema


def _make_mcp_handler(mcp_client: MCPClient, tool_name: str):
    """Create a handler closure for an MCP tool."""
    def handler(self, args, approval_cb, reject_all):
        if is_consequential(tool_name):
            if reject_all:
                return ToolExecResult(
                    ok=False,
                    payload={"ok": False, "rejected": True, "error": f"rejected: {tool_name}"},
                )
            if approval_cb is not None:
                request = ApprovalRequest(
                    tool_name=tool_name,
                    rel_path=f"mcp:{tool_name}",
                    old_content="",
                    new_content=json.dumps(args),
                    is_new_file=True,
                )
                decision = approval_cb(request)
                if decision.action in ("reject", "reject_all"):
                    return ToolExecResult(
                        ok=False,
                        payload={
                            "ok": False,
                            "rejected": True,
                            "error": f"rejected: {tool_name}",
                            "decision": decision.action,
                        },
                    )
        result = mcp_client.call_tool(tool_name, args)
        return ToolExecResult(ok=result.get("ok", False), payload=result)
    return handler


class MCPToolRegistry:
    """Owns MCP server connections, tool schemas, and execution."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}   # tool_name -> MCPClient
        self._schemas: list[dict[str, Any]] = []

    def connect_server(self, server_command: str) -> int:
        """Launch an MCP server, fetch its tools, and register them.

        Returns the number of tools registered.
        Raises RuntimeError if the server fails to launch.
        """
        parsed = shlex.split(server_command, posix=(_os.name != "nt"))
        client = MCPClient(parsed)
        client.connect()
        tool_defs = client.list_tools()

        count = 0
        for tool_def in tool_defs:
            schema = _convert_tool_to_openai_schema(tool_def)
            tool_name = tool_def["name"]
            self._schemas.append(schema)
            self._clients[tool_name] = client
            # Backward compatibility: also register in global TOOL_HANDLERS
            from aura.conversation.tools.registry import TOOL_HANDLERS

            TOOL_HANDLERS[tool_name] = _make_mcp_handler(client, tool_name)
            count += 1

        return count

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Return the list of MCP tool schemas (for tool_defs)."""
        return list(self._schemas)

    def can_execute(self, tool_name: str) -> bool:
        """Return True if this tool_name is an MCP-registered tool."""
        return tool_name in self._clients

    def execute(self, tool_name: str, args: dict[str, Any]) -> ToolExecResult:
        """Execute an MCP tool by name, forwarding args to the MCP client."""
        client = self._clients[tool_name]
        result = client.call_tool(tool_name, args)
        return ToolExecResult(ok=result.get("ok", False), payload=result)
