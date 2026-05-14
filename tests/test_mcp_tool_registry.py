"""Tests for MCPToolRegistry."""
from __future__ import annotations

from unittest.mock import patch

import pytest

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.mcp_registry import MCPToolRegistry, _make_mcp_handler


class FakeMCPClient:
    """Fake MCP client for testing without a real server."""

    def __init__(self, tools: list[dict] | None = None):
        self._tools = tools or []
        self._connected = False

    def connect(self):
        self._connected = True

    def list_tools(self):
        return list(self._tools)

    def call_tool(self, name: str, args: dict):
        return {"ok": True, "result": f"called {name} with {args}"}


class TestMCPToolRegistry:
    """Tests for MCPToolRegistry connect, schemas, can_execute, execute."""

    @pytest.fixture(autouse=True)
    def _cleanup_handlers(self):
        """Clean up TOOL_HANDLERS after each test."""
        yield
        from aura.conversation.tools.registry import TOOL_HANDLERS

        for key in list(TOOL_HANDLERS.keys()):
            if key.startswith("fake_"):
                del TOOL_HANDLERS[key]

    def test_connect_server_registers_tools(self):
        """connect_server returns tool count and populates schemas."""
        fake_tools = [
            {"name": "fake_echo", "description": "Echo back input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
            {"name": "fake_add", "description": "Add two numbers",
             "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            count = registry.connect_server("python fake_server.py")
            assert count == 2

    def test_schemas_are_stored(self):
        """schemas property returns stored tool definitions."""
        fake_tools = [
            {"name": "fake_echo", "description": "Echo back input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            registry.connect_server("python fake_server.py")
            schemas = registry.schemas
            assert len(schemas) == 1
            assert schemas[0]["function"]["name"] == "fake_echo"

    def test_can_execute_known_tool(self):
        """can_execute returns True for registered tools."""
        fake_tools = [
            {"name": "fake_echo", "description": "Echo back input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            registry.connect_server("python fake_server.py")
            assert registry.can_execute("fake_echo") is True
            assert registry.can_execute("nonexistent") is False

    def test_execute_forwards_to_client(self):
        """execute() calls the MCP client and returns ToolExecResult."""
        fake_tools = [
            {"name": "fake_echo", "description": "Echo back input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            registry.connect_server("python fake_server.py")
            result = registry.execute("fake_echo", {"text": "hello"})
            assert isinstance(result, ToolExecResult)
            assert result.ok is True
            assert "called fake_echo" in str(result.payload)

    def test_execute_returns_false_when_client_fails(self):
        """execute() returns ok=False when the MCP client result has ok=False."""
        fake_tools = [
            {"name": "fake_fail", "description": "Always fails",
             "inputSchema": {"type": "object", "properties": {}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            # Override call_tool to return failure
            mock_client.call_tool = lambda name, args: {"ok": False, "error": "boom"}
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            registry.connect_server("python fake_server.py")
            result = registry.execute("fake_fail", {})
            assert result.ok is False

    def test_connect_server_populates_global_handlers(self):
        """connect_server also registers handlers in TOOL_HANDLERS."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        fake_tools = [
            {"name": "fake_echo", "description": "Echo back input",
             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
        ]

        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            registry = MCPToolRegistry()
            registry.connect_server("python fake_server.py")
            assert "fake_echo" in TOOL_HANDLERS


class TestMakeMCPHandler:
    """Tests for the _make_mcp_handler helper."""

    def test_handler_calls_client_and_returns_result(self):
        """The handler closure calls the MCP client and wraps the result."""
        fake_client = FakeMCPClient()
        handler = _make_mcp_handler(fake_client, "test_tool")

        result = handler(None, {"x": 1}, None, False)
        assert isinstance(result, ToolExecResult)
        assert result.ok is True
        assert "called test_tool" in str(result.payload)
