"""Tests for aura.mcp_client and MCP support in ToolRegistry."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from aura.mcp_client import MCPClient, _convert_tool_to_openai_schema


MOCK_SERVER_CODE = '''
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(name="TestServer")

@mcp.tool()
def echo(message: str) -> str:
    """Echo back the message."""
    return f"Echo: {message}"

@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b

if __name__ == "__main__":
    mcp.run(transport="stdio")
'''


@pytest.fixture(scope="module")
def mock_mcp_server_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Write a mock MCP server script and return its path."""
    script = tmp_path_factory.mktemp("mcp_test") / "mock_server.py"
    script.write_text(MOCK_SERVER_CODE)
    return script


# ---------------------------------------------------------------------------
# _convert_tool_to_openai_schema
# ---------------------------------------------------------------------------


class TestConvertToolToOpenAISchema:
    """Tests for the schema conversion helper."""

    def test_basic_conversion(self):
        tool_def = {
            "name": "echo",
            "description": "Echo back input",
            "inputSchema": {
                "type": "object",
                "properties": {"message": {"type": "string"}},
                "required": ["message"],
            },
        }
        result = _convert_tool_to_openai_schema(tool_def)
        assert result["type"] == "function"
        assert result["function"]["name"] == "echo"
        assert result["function"]["description"] == "Echo back input"
        assert result["function"]["parameters"] == tool_def["inputSchema"]


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class TestMCPClient:
    """Integration tests for MCPClient using a real mock MCP server."""

    def test_connect_and_list_tools(self, mock_mcp_server_script: Path):
        """Connect to the mock server and list available tools."""
        client = MCPClient(["python", str(mock_mcp_server_script)])
        try:
            client.connect()
            tools = client.list_tools()
            tool_names = {t["name"] for t in tools}
            assert "echo" in tool_names
            assert "add" in tool_names
            # Verify structure
            for t in tools:
                assert "name" in t
                assert "description" in t
                assert "inputSchema" in t
        finally:
            client.close()

    def test_call_tool_echo(self, mock_mcp_server_script: Path):
        """Call the echo tool and verify the response."""
        client = MCPClient(["python", str(mock_mcp_server_script)])
        try:
            client.connect()
            result = client.call_tool("echo", {"message": "hello"})
            assert result.get("ok") is True
            assert "content" in result
            assert any("Echo: hello" in str(c) for c in result["content"])
        finally:
            client.close()

    def test_call_tool_add(self, mock_mcp_server_script: Path):
        """Call the add tool and verify the response contains the sum."""
        client = MCPClient(["python", str(mock_mcp_server_script)])
        try:
            client.connect()
            result = client.call_tool("add", {"a": 3, "b": 4})
            assert result.get("ok") is True
            assert "content" in result
            assert any("7" in str(c) for c in result["content"])
        finally:
            client.close()

    def test_call_tool_unknown(self, mock_mcp_server_script: Path):
        """Call an unknown tool and expect an error."""
        client = MCPClient(["python", str(mock_mcp_server_script)])
        try:
            client.connect()
            result = client.call_tool("nonexistent_tool", {})
            assert result.get("ok") is False
            # Error can be in "error" key or in content list
            has_error = "error" in result or any(
                "Unknown tool" in str(c) for c in result.get("content", [])
            )
            assert has_error
        finally:
            client.close()

    def test_close_and_cleanup(self, mock_mcp_server_script: Path):
        """Connect, close, and verify no errors."""
        client = MCPClient(["python", str(mock_mcp_server_script)])
        client.connect()
        tools = client.list_tools()
        assert len(tools) == 2
        client.close()
        # Calling close again should be safe
        client.close()

    def test_connect_invalid_command(self):
        """Connecting with a non-existent command should raise RuntimeError."""
        client = MCPClient(["python", "-c", "import sys; sys.exit(1)"])
        with pytest.raises(RuntimeError):
            client.connect()
        client.close()

    def test_list_tools_before_connect(self):
        """Calling list_tools before connect should raise RuntimeError."""
        client = MCPClient(["python", "-c", ""])
        with pytest.raises(RuntimeError):
            client.list_tools()
        client.close()


# ---------------------------------------------------------------------------
# ToolRegistry MCP integration
# ---------------------------------------------------------------------------


class TestToolRegistryMCP:
    """Tests for ToolRegistry.connect_mcp_server and MCP tool execution."""

    def test_connect_mcp_server_registers_tools(
        self, mock_mcp_server_script: Path
    ):
        """Verify that connect_mcp_server registers tools in the registry."""
        from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry

        registry = ToolRegistry(workspace_root=Path(os.getcwd()))
        try:
            count = registry.connect_mcp_server(
                f"python {mock_mcp_server_script}"
            )
            assert count == 2

            # Tool defs should include the MCP schemas
            defs = registry.tool_defs()
            mcp_schemas = [d for d in defs if d["function"]["name"] in ("echo", "add")]
            assert len(mcp_schemas) == 2

            # TOOL_HANDLERS should have the MCP tools
            assert "echo" in TOOL_HANDLERS
            assert "add" in TOOL_HANDLERS
        finally:
            # Cleanup TOOL_HANDLERS
            TOOL_HANDLERS.pop("echo", None)
            TOOL_HANDLERS.pop("add", None)
            registry._mcp_clients.clear()
            registry._mcp_schemas.clear()

    def test_execute_mcp_tool(
        self, mock_mcp_server_script: Path
    ):
        """Execute an MCP-registered tool through the registry."""
        from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry

        def dummy_approval_cb(req):
            from aura.conversation.tools.registry import ApprovalDecision
            return ApprovalDecision(action="approve")

        registry = ToolRegistry(workspace_root=Path(os.getcwd()))
        try:
            registry.connect_mcp_server(f"python {mock_mcp_server_script}")

            result = registry.execute(
                "echo",
                {"message": "hi"},
                dummy_approval_cb,
                False,
            )
            assert result.ok is True
            payload = result.payload
            assert payload.get("ok") is True
            assert any("hi" in str(c) for c in payload.get("content", []))
        finally:
            TOOL_HANDLERS.pop("echo", None)
            TOOL_HANDLERS.pop("add", None)
            registry._mcp_clients.clear()
            registry._mcp_schemas.clear()

    def test_connect_mcp_server_invalid_command(self):
        """Connecting with a non-existent command should raise RuntimeError."""
        from aura.conversation.tools.registry import ToolRegistry

        registry = ToolRegistry(workspace_root=Path(os.getcwd()))
        with pytest.raises(RuntimeError):
            registry.connect_mcp_server("nonexistent_command_xyz")
