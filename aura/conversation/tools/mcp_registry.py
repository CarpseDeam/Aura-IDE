"""MCP tool registry — owns MCP clients, schemas, and execution dispatch.

Registration is the security boundary for extensible tools, so three
properties hold here rather than being left to whoever connects a server.

**Instance-owned.**  Registered tools live on the registry that connected the
server and nowhere else.  Publishing them to the process-global
``TOOL_HANDLERS`` made every registry in the process — a second window, a
Planner alongside its Worker — able to call a server it never connected, with
another registry's client and another workspace's root.

**Collision-safe.**  A server does not get to name its tool ``write_file``.
The built-in handlers are checked before this registry, so a shadowing name
would either be silently unreachable or, when it reached the global table,
replace the real write path — approval, backup, and path jail included — with
an arbitrary subprocess.  Both outcomes are refusals here instead.

The same holds in the other direction, against the *dynamic* registry.  The
executor dispatches MCP before dynamic tools, so a server that claims a name a
workspace script already owns would silently take that script's calls, and a
script dropped in later that claims a connected server's name would silently
never run.  Both registries consult the other's names through
:attr:`reserved_names`, which :class:`~aura.conversation.tools.registry.ToolRegistry`
wires to point at each other.

**Atomic.**  A server's tools are validated as a set and committed as a set.
A failure part-way through leaves the registry exactly as it was and closes
the client, rather than exposing half a server that cannot be named,
disconnected, or reasoned about.
"""
from __future__ import annotations

import os as _os
import shlex
import threading
from typing import Any, Callable

from aura.conversation.tools._types import ToolExecResult
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


class MCPRegistrationError(ValueError):
    """A server tool was refused registration.

    Raised for a name that collides with a built-in or an already-registered
    tool, and for a malformed tool definition.  Connecting the server fails as
    a whole; nothing partial is left behind.
    """


def _builtin_tool_names() -> frozenset[str]:
    """Names the built-in catalog owns, which no server tool may take.

    Read at call time: the table is populated when
    ``aura.conversation.tools.registry`` finishes importing, and this module
    is imported on the way there.
    """
    from aura.conversation.tools.registry import TOOL_HANDLERS

    return frozenset(TOOL_HANDLERS)


def parse_server_command(server_command: str) -> list[str]:
    """Split a server command string into argv.

    ``shlex`` in non-POSIX mode — which is what Windows paths need, so a
    backslash is a separator and not an escape — keeps the quote characters
    inside each token.  A quoted path would then be handed to ``CreateProcess``
    with its quotes still attached and fail to launch, which is exactly the
    shape a managed install under a user profile containing a space takes.  The
    quotes are stripped back off here, once, in the one place the command is
    parsed.
    """
    tokens = shlex.split(server_command, posix=(_os.name != "nt"))
    stripped: list[str] = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1]
        stripped.append(token)
    return stripped


class MCPToolRegistry:
    """Owns MCP server connections, tool schemas, and execution."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clients: dict[str, MCPClient] = {}   # tool_name -> MCPClient
        self._effects: dict[str, ToolEffect] = {}  # tool_name -> declared effect
        # tool_name -> its OpenAI schema, so exposure can be filtered by the
        # resolved effect without re-deriving anything from the schema list.
        # Insertion-ordered, which is also the exposure order.
        self._schema_by_name: dict[str, dict[str, Any]] = {}
        # server key -> the tool names it owns, so a disconnect removes
        # exactly that server's surface and nothing else.
        self._server_tools: dict[str, list[str]] = {}
        self._server_clients: dict[str, MCPClient] = {}
        # server key -> the capability id it contributes while connected.
        self._server_capabilities: dict[str, str] = {}
        # Names owned by a *different* extensible registry, consulted so a
        # collision is refused in both directions.  Never a fixed snapshot: a
        # workspace script can appear between two connections.
        self.reserved_names: Callable[[], frozenset[str]] = frozenset

    # ── registration ────────────────────────────────────────────────────

    def _check_name(self, tool_name: Any, pending: set[str]) -> str:
        """Validate one candidate tool name, or raise MCPRegistrationError."""
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise MCPRegistrationError(
                "MCP tool definition has no usable 'name'"
            )
        name = tool_name.strip()
        if name in _builtin_tool_names():
            raise MCPRegistrationError(
                f"MCP tool '{name}' collides with a built-in tool of the same "
                "name; built-in tools cannot be shadowed by a server"
            )
        if name in self._clients or name in pending:
            raise MCPRegistrationError(
                f"MCP tool '{name}' is already registered; tool names must be "
                "unique across connected servers"
            )
        if name in self._reserved_names():
            raise MCPRegistrationError(
                f"MCP tool '{name}' collides with a dynamic workspace tool of "
                "the same name; the server's tool would silently take that "
                "script's calls"
            )
        return name

    def _reserved_names(self) -> frozenset[str]:
        try:
            return frozenset(self.reserved_names())
        except Exception:  # pragma: no cover - a broken provider must not
            # turn into a *permissive* registration.
            raise MCPRegistrationError(
                "Could not determine which tool names are already claimed; "
                "refusing to register a server surface that may collide"
            )

    @staticmethod
    def _declared_effect(tool_def: dict[str, Any]) -> ToolEffect | None:
        effect = effect_from_metadata(tool_def.get(SCHEMA_EFFECT_KEY))
        if effect is None:
            annotations = tool_def.get("annotations") or {}
            if annotations.get("readOnlyHint") is True:
                effect = ToolEffect.OBSERVATION
        return effect

    def connect_server(
        self,
        server_command: str,
        *,
        tool_filter: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
        capability: str | None = None,
    ) -> int:
        """Launch an MCP server, fetch its tools, and register them atomically.

        Returns the number of tools registered.  Raises ``RuntimeError`` if the
        server fails to launch and :class:`MCPRegistrationError` if any of its
        tools is unregisterable — in which case *none* of them are registered
        and the client is closed, so a rejected server leaves no trace.

        ``tool_filter`` runs against the definitions the *live* server actually
        reported, before any of them is validated or committed.  That is where
        a caller narrows a server to a reviewed surface: what the filter drops
        is never registered, so it cannot be called or approved, and a filter
        that raises rejects the whole connection like any other refusal.

        ``capability`` tags the connection so request-time context can be
        offered while this server is connected and withdrawn the moment it is
        not — see :meth:`capabilities`.
        """
        client = MCPClient(parse_server_command(server_command))
        client.connect()
        try:
            tool_defs = client.list_tools()
            if tool_filter is not None:
                tool_defs = tool_filter(list(tool_defs))
            with self._lock:
                if server_command in self._server_tools:
                    raise MCPRegistrationError(
                        f"MCP server {server_command!r} is already connected"
                    )
                # Validate the whole set before committing any of it.
                pending: set[str] = set()
                prepared: list[tuple[str, dict[str, Any], ToolEffect | None]] = []
                for tool_def in tool_defs:
                    if not isinstance(tool_def, dict):
                        raise MCPRegistrationError(
                            "MCP tool definition must be an object"
                        )
                    name = self._check_name(tool_def.get("name"), pending)
                    pending.add(name)
                    prepared.append((
                        name,
                        _convert_tool_to_openai_schema(tool_def),
                        self._declared_effect(tool_def),
                    ))

                for name, schema, effect in prepared:
                    self._schema_by_name[name] = schema
                    self._clients[name] = client
                    if effect is not None:
                        self._effects[name] = effect
                self._server_tools[server_command] = [n for n, _, _ in prepared]
                self._server_clients[server_command] = client
                if capability:
                    self._server_capabilities[server_command] = capability
                return len(prepared)
        except Exception:
            # Nothing was committed; do not leave the subprocess running.
            try:
                client.close()
            except Exception:
                pass
            raise

    def register_tool_def(
        self, tool_def: dict[str, Any], client: MCPClient
    ) -> dict[str, Any]:
        """Register one MCP tool definition; returns its OpenAI schema.

        Effect metadata may come from ``x-aura-effect`` or the standard MCP
        ``annotations.readOnlyHint``; absence means the tool resolves to the
        fail-safe consequential default at lookup time.

        Raises :class:`MCPRegistrationError` for a name that shadows a built-in
        or is already registered.  The registration is local to this registry:
        another registry in the same process is unaffected and cannot call it.
        """
        with self._lock:
            tool_name = self._check_name(tool_def.get("name"), set())
            schema = _convert_tool_to_openai_schema(tool_def)
            self._schema_by_name[tool_name] = schema
            self._clients[tool_name] = client
            effect = self._declared_effect(tool_def)
            if effect is not None:
                self._effects[tool_name] = effect
            return schema

    def disconnect_server(self, server_command: str) -> int:
        """Remove a connected server's tools and close its client.

        Returns the number of tools removed.  Removal is by the server's own
        recorded tool list, so a server never takes another server's tool with
        it, and the surface the model is offered next turn matches what is
        actually connected.
        """
        with self._lock:
            names = self._server_tools.pop(server_command, None)
            client = self._server_clients.pop(server_command, None)
            self._server_capabilities.pop(server_command, None)
            if names is None:
                return 0
            for name in names:
                self._schema_by_name.pop(name, None)
                self._clients.pop(name, None)
                self._effects.pop(name, None)
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        return len(names)

    def connected_servers(self) -> list[str]:
        """Server commands currently connected to this registry."""
        with self._lock:
            return list(self._server_tools)

    def registered_names(self) -> frozenset[str]:
        """Tool names this registry currently owns."""
        with self._lock:
            return frozenset(self._clients)

    def capabilities(self) -> frozenset[str]:
        """Capability ids contributed by the servers connected right now.

        Derived from live connection state rather than from a flag anyone sets,
        so context keyed off a capability cannot outlive the tools that
        justified it: disconnecting the server drops the entry in the same
        locked step that removes its tools.
        """
        with self._lock:
            return frozenset(self._server_capabilities.values())

    @property
    def schemas(self) -> list[dict[str, Any]]:
        """Return the list of MCP tool schemas (for tool_defs)."""
        with self._lock:
            return list(self._schema_by_name.values())

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
