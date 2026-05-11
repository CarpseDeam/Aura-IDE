"""Synchronous wrapper around an MCP stdio client session.

Manages a background event-loop thread so that the async MCP session
stays alive across multiple sync call_tool() invocations.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _convert_tool_to_openai_schema(tool_def: dict[str, Any]) -> dict[str, Any]:
    """Convert an MCP tool definition dict to OpenAI function-calling format.

    Args:
        tool_def: MCP tool definition dict with keys "name", "description",
                  and "inputSchema".

    Returns:
        An OpenAI-compatible tool schema dict with "type": "function"
        and a "function" block containing name, description, parameters.
    """
    return {
        "type": "function",
        "function": {
            "name": tool_def["name"],
            "description": tool_def["description"],
            "parameters": tool_def["inputSchema"],
        },
    }


class MCPClient:
    """Synchronous wrapper around an MCP stdio client session.

    Manages a background event-loop thread so that the async MCP session
    stays alive across multiple sync call_tool() invocations.
    """

    def __init__(self, server_command: list[str]) -> None:
        """Store the server command; does NOT start the process.

        Args:
            server_command: List of command tokens, e.g. ["python", "-m",
                            "my_mcp_server"].
        """
        self._server_command = server_command
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._transport: Any = None  # the (read, write) stream tuple
        self._transport_cm: Any = None  # the stdio_client async context mgr
        self._tools: list[dict[str, Any]] | None = None
        self._closed = False

    def connect(self) -> None:
        """Launch the MCP server and establish a session.

        Creates a new event loop, starts it in a daemon thread, opens a
        stdio transport, creates a ClientSession, and calls initialize().

        Raises:
            RuntimeError: If the server fails to launch or the session
                          cannot be initialized.
        """
        if self._closed:
            raise RuntimeError("MCPClient has already been closed")

        try:
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._loop.run_forever,
                daemon=True,
                name="mcp-event-loop",
            )
            self._thread.start()

            # Run the connect + initialize coroutine on the background loop
            future = asyncio.run_coroutine_threadsafe(
                self._connect_async(), self._loop
            )
            future.result(timeout=30)
        except Exception as exc:
            self.close()
            raise RuntimeError(
                f"Failed to connect MCP server '{self._server_command[0]}': {exc}"
            ) from exc

    async def _connect_async(self) -> None:
        """Async portion of connect(): open stdio transport and initialize."""
        params = StdioServerParameters(
            command=self._server_command[0],
            args=self._server_command[1:],
        )
        # stdio_client is an async generator function that returns an
        # async context manager. We use __aenter__ manually so that
        # the transport stays alive until we explicitly close it.
        self._transport_cm = stdio_client(params)
        read, write = await self._transport_cm.__aenter__()
        self._transport = (read, write)
        self._session = await ClientSession(read, write).__aenter__()
        await self._session.initialize()

    def list_tools(self) -> list[dict[str, Any]]:
        """Fetch the list of tools from the MCP server.

        Caches the result so subsequent calls are instant.

        Returns:
            List of tool dicts with keys "name", "description", "inputSchema".

        Raises:
            RuntimeError: If not connected.
        """
        if self._session is None:
            raise RuntimeError("Not connected — call connect() first")
        if self._tools is not None:
            return self._tools

        future = asyncio.run_coroutine_threadsafe(
            self._list_tools_async(), self._loop
        )
        result = future.result(timeout=30)
        self._tools = result
        return result

    async def _list_tools_async(self) -> list[dict[str, Any]]:
        """Async: call session.list_tools() and convert to dicts."""
        assert self._session is not None
        result = await self._session.list_tools()
        tools: list[dict[str, Any]] = []
        for tool in result.tools:
            tools.append({
                "name": tool.name,
                "description": tool.description or "",
                "inputSchema": tool.inputSchema,
            })
        return tools

    def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Call a tool on the MCP server synchronously.

        Args:
            name: Tool name.
            arguments: Tool arguments dict.

        Returns:
            On success: {"ok": True, "content": [str, ...]}
            On error: {"ok": False, "error": str(exception)}
        """
        if self._session is None:
            return {"ok": False, "error": "Not connected — call connect() first"}

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, arguments), self._loop
            )
            result = future.result(timeout=30)

            content: list[Any] = []
            for block in result.content:
                # TextContent has a .text attribute
                block_type = getattr(block, "type", None)
                if block_type == "text" or hasattr(block, "text"):
                    content.append(block.text)
                else:
                    content.append({"type": block_type})

            return {"ok": not result.isError, "content": content}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def close(self) -> None:
        """Close the session, transport, and stop the event loop."""
        if self._closed:
            return
        self._closed = True

        if self._loop is not None:
            # Close the session first
            if self._session is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._session.__aexit__(None, None, None), self._loop
                    )
                    future.result(timeout=10)
                except Exception:
                    pass
                self._session = None

            # Then close the transport context manager
            if self._transport_cm is not None:
                try:
                    future = asyncio.run_coroutine_threadsafe(
                        self._transport_cm.__aexit__(None, None, None), self._loop
                    )
                    future.result(timeout=10)
                except Exception:
                    pass
                self._transport_cm = None

            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
            self._thread = None

        self._loop = None
        self._transport = None

    def __del__(self) -> None:
        """Ensure cleanup on garbage collection."""
        self.close()
