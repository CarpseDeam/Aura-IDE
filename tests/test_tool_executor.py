"""Tests for ToolExecutor dispatch logic."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.dynamic_registry import DynamicToolRegistry
from aura.conversation.tools.executor import ToolExecutor
from aura.conversation.tools.mcp_registry import MCPToolRegistry
from aura.sandbox import SandboxResult


class FakeOwner:
    """Minimal owner for ToolExecutor with a workspace_root attribute."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root


class TestToolExecutor:
    """Tests for ToolExecutor dispatch priority and error handling."""

    @pytest.fixture
    def owner(self, tmp_workspace: Path) -> FakeOwner:
        return FakeOwner(tmp_workspace)

    @pytest.fixture
    def dynamic_tools(self, tmp_workspace: Path) -> DynamicToolRegistry:
        return DynamicToolRegistry(tmp_workspace)

    @pytest.fixture
    def mcp_tools(self) -> MCPToolRegistry:
        return MCPToolRegistry()

    @pytest.fixture
    def executor(self, owner, dynamic_tools, mcp_tools) -> ToolExecutor:
        return ToolExecutor(
            owner=owner,
            dynamic_tools=dynamic_tools,
            mcp_tools=mcp_tools,
        )

    @pytest.fixture(autouse=True)
    def _cleanup_handlers(self):
        """Clean up TOOL_HANDLERS after each test."""
        yield
        from aura.conversation.tools.registry import TOOL_HANDLERS

        for key in list(TOOL_HANDLERS.keys()):
            if key.startswith("test_"):
                del TOOL_HANDLERS[key]

    def test_dispatches_to_static_handler(self, executor):
        """Static TOOL_HANDLERS take priority over MCP and dynamic tools."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        called = []

        def handler(self, args, approval_cb, reject_all):
            called.append(args)
            return ToolExecResult(ok=True, payload={"ok": True})

        TOOL_HANDLERS["test_static"] = handler

        result = executor.execute("test_static", {"x": 1}, None)
        assert result.ok is True
        assert called == [{"x": 1}]

    def test_dispatches_to_mcp_when_no_static_handler(self, executor, mcp_tools):
        """When no static handler exists, MCP tools are tried next."""
        with patch("aura.conversation.tools.mcp_registry.MCPClient") as mock_cls:
            from tests.test_mcp_tool_registry import FakeMCPClient

            fake_tools = [
                {"name": "test_mcp_tool", "description": "A test MCP tool",
                 "inputSchema": {"type": "object", "properties": {}}},
            ]
            mock_client = FakeMCPClient(fake_tools)
            mock_cls.return_value = mock_client

            mcp_tools.connect_server("python fake_server.py")

        result = executor.execute("test_mcp_tool", {"a": 1}, None)
        assert result.ok is True
        assert "called test_mcp_tool" in str(result.payload)

    def test_dispatches_to_dynamic_tool(self, executor, tmp_workspace: Path):
        """When no static or MCP handler exists, dynamic tools are tried."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "test_dynamic.py").write_text(
            "def test_dynamic(x: int) -> dict:\n"
            '    """A dynamic tool.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True, "result": x * 2}\n'
        )

        mock_res = SandboxResult(ok=True, stdout='{"ok": true, "result": 10}', stderr="", exit_code=0)
        with patch("aura.sandbox.SandboxExecutor.run_dynamic_tool", return_value=mock_res):
            result = executor.execute("test_dynamic", {"x": 5}, None)

        assert result.ok is True
        assert result.payload["result"] == 10


    def test_unknown_tool_returns_error(self, executor):
        """Unknown tool names return ok=False with an error message."""
        result = executor.execute("nonexistent_tool", {}, None)
        assert result.ok is False
        assert "unknown tool" in result.payload.get("error", "")

    def test_value_error_from_handler_returns_error(self, executor):
        """ValueError from a handler is caught and returned as ok=False."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        def handler(self, args, approval_cb, reject_all):
            raise ValueError("bad input")

        TOOL_HANDLERS["test_bad"] = handler

        result = executor.execute("test_bad", {}, None)
        assert result.ok is False
        assert "bad input" in result.payload.get("error", "")

    def test_os_error_from_handler_returns_error(self, executor):
        """OSError from a handler is caught and returned as ok=False."""
        from aura.conversation.tools.registry import TOOL_HANDLERS

        def handler(self, args, approval_cb, reject_all):
            raise OSError("file not found")

        TOOL_HANDLERS["test_oserr"] = handler

        result = executor.execute("test_oserr", {}, None)
        assert result.ok is False
        assert "file not found" in result.payload.get("error", "")
