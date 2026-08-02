"""MCP tool registry — owns MCP clients, schemas, and execution dispatch."""
from __future__ import annotations

import json
import os as _os
import shlex
from typing import Any

from aura.conversation.tools._types import ApprovalRequest, ToolExecResult
from aura.conversation.tools.consequential import is_consequential
from aura.conversation.tools.effects import (
    DEFAULT_EXTENSIBLE_TOOL_EFFECT,
    SCHEMA_EFFECT_KEY,
    ToolEffect,
    effect_from_metadata,
)

try:
    from aura.mcp_client import MCPClient, _convert_tool_to_openai_schema
except ModuleNotFoundError as exc:
    _MCP_IMPORT_ERROR = exc

    class MCPClient:  # type: ignore[no-redef]
        def __init__(self, server_command: list[str]) -> None:
            self._server_command = server_command

        def connect(self) -> None:
            raise RuntimeError(f"MCP support is unavailable: {_MCP_IMPORT_ERROR}")

    def _convert_tool_to_openai_schema(tool_def: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool_def["name"],
                "description": tool_def.get("description", ""),
                "parameters": tool_def.get("inputSchema", {"type": "object"}),
            },
        }


def _make_mcp_handler(
    registry: "MCPToolRegistry", mcp_client: MCPClient, tool_name: str
):
    """Create a handler closure for an MCP tool.

    The approval gate is the registry's resolved effect, not the tool's name:
    an unannotated server tool is consequential and must be approved however
    innocuous it is called.
    """
    def handler(self, args, approval_cb, reject_all):
        if registry.requires_approval(tool_name):
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
        self._effects: dict[str, ToolEffect] = {}  # tool_name -> declared effect
        # tool_name -> its OpenAI schema, so exposure can be filtered by the
        # resolved effect without re-deriving anything from the schema list.
        self._schema_by_name: dict[str, dict[str, Any]] = {}

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
            self.register_tool_def(tool_def, client)
            count += 1

        return count

    def register_tool_def(
        self, tool_def: dict[str, Any], client: MCPClient
    ) -> dict[str, Any]:
        """Register one MCP tool definition; returns its OpenAI schema.

        Effect metadata may come from ``x-aura-effect`` or the standard MCP
        ``annotations.readOnlyHint``; absence means the tool resolves to the
        fail-safe consequential default at lookup time.
        """
        schema = _convert_tool_to_openai_schema(tool_def)
        tool_name = tool_def["name"]
        self._schemas.append(schema)
        self._schema_by_name[tool_name] = schema
        self._clients[tool_name] = client
        effect = effect_from_metadata(tool_def.get(SCHEMA_EFFECT_KEY))
        if effect is None:
            annotations = tool_def.get("annotations") or {}
            if annotations.get("readOnlyHint") is True:
                effect = ToolEffect.OBSERVATION
        if effect is not None:
            self._effects[tool_name] = effect
        # Backward compatibility: also register in global TOOL_HANDLERS
        from aura.conversation.tools.registry import TOOL_HANDLERS

        TOOL_HANDLERS[tool_name] = _make_mcp_handler(self, client, tool_name)
        return schema

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Return the list of MCP tool schemas (for tool_defs)."""
        return list(self._schemas)

    @property
    def observation_schemas(self) -> list[dict[str, Any]]:
        """Schemas of only those MCP tools explicitly resolved as observation.

        A read-only registry exposes this list instead of :attr:`schemas`.  An
        unannotated server tool resolves consequential, so it is not offered at
        all under read-only mode — the alternative is presenting a tool whose
        effect nobody has established as though it were safe to call there.
        """
        return [
            schema
            for name, schema in self._schema_by_name.items()
            if self.resolved_effect(name) is ToolEffect.OBSERVATION
        ]

    def can_execute(self, tool_name: str) -> bool:
        """Return True if this tool_name is an MCP-registered tool."""
        return tool_name in self._clients

    def effect(self, tool_name: str) -> ToolEffect | None:
        """Effect an MCP tool *declared* via metadata, or None.

        None means the server supplied no valid metadata.  This is the raw
        declaration; :meth:`resolved_effect` is the authoritative answer.
        """
        return self._effects.get(tool_name)

    def resolved_effect(self, tool_name: str) -> ToolEffect | None:
        """Authoritative effect for a registered MCP tool, or None if unknown here.

        A registered tool always resolves to *some* effect: its declaration when
        it made one, otherwise the fail-safe consequential default.  ``None``
        means only that this registry has no such tool.
        """
        if tool_name not in self._clients:
            return None
        return self._effects.get(tool_name, DEFAULT_EXTENSIBLE_TOOL_EFFECT)

    def requires_approval(self, tool_name: str) -> bool:
        """Whether calling *tool_name* must go through the approval path.

        Driven by the resolved effect, so silence about a tool's effect means
        "ask", not "allow".  Only a tool established as an observation is
        approval-free; a name heuristic is the fallback for names this registry
        does not own at all.
        """
        effect = self.resolved_effect(tool_name)
        if effect is None:
            return is_consequential(tool_name)
        return effect is not ToolEffect.OBSERVATION

    def execute(self, tool_name: str, args: dict[str, Any]) -> ToolExecResult:
        """Execute an MCP tool by name, forwarding args to the MCP client."""
        client = self._clients[tool_name]
        result = client.call_tool(tool_name, args)
        return ToolExecResult(ok=result.get("ok", False), payload=result)
