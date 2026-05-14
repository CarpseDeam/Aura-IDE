"""Tests for DynamicToolRegistry."""
from __future__ import annotations

from pathlib import Path

from aura.conversation.tools.dynamic_registry import DynamicToolRegistry


class TestDynamicToolRegistry:
    """Tests for DynamicToolRegistry scanning, caching, and lookup."""

    def test_scans_valid_dynamic_tools(self, tmp_workspace: Path):
        """Valid .py files in .aura/tools/ are discovered and parsed."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "my_tool.py").write_text(
            '"""A test tool."""\n'
            "def my_tool(query: str) -> dict:\n"
            '    """Search for something.\n\n'
            "    Args:\n"
            "        query: The search query.\n"
            '    """\n'
            '    return {"ok": True, "result": query}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "my_tool" in result
        assert result["my_tool"] == tools_dir / "my_tool.py"

    def test_ignores_underscore_files(self, tmp_workspace: Path):
        """Files starting with _ are skipped."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "_internal.py").write_text(
            "def _internal(x: int) -> dict:\n"
            '    """Internal helper.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "_internal" not in result

    def test_ignores_invalid_tool_files(self, tmp_workspace: Path):
        """Files that fail schema parsing are silently skipped."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        # File with no top-level function
        (tools_dir / "bad_tool.py").write_text("x = 42\n")

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "bad_tool" not in result

    def test_removes_stale_tools(self, tmp_workspace: Path):
        """When a tool file is deleted, it is removed from the cache."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        tool_path = tools_dir / "temp_tool.py"
        tool_path.write_text(
            "def temp_tool(x: int) -> dict:\n"
            '    """A temporary tool.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "temp_tool" in result

        # Delete the file
        tool_path.unlink()

        result = registry.scan()
        assert "temp_tool" not in result

    def test_updates_cached_name_on_change(self, tmp_workspace: Path):
        """When a tool file's function name changes, the cache updates."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        tool_path = tools_dir / "renamed_tool.py"
        tool_path.write_text(
            "def old_name(x: int) -> dict:\n"
            '    """Old name.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "old_name" in result
        assert "new_name" not in result

        # Change the function name
        tool_path.write_text(
            "def new_name(x: int) -> dict:\n"
            '    """New name.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        result = registry.scan()
        assert "old_name" not in result
        assert "new_name" in result

    def test_set_workspace_root_resets_state(self, tmp_workspace: Path, tmp_path: Path):
        """set_workspace_root clears all cached state."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "tool_a.py").write_text(
            "def tool_a(x: int) -> dict:\n"
            '    """Tool A.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert "tool_a" in result

        # Switch to a different workspace
        other_ws = tmp_path / "other_workspace"
        other_ws.mkdir()
        registry.set_workspace_root(other_ws)

        result = registry.scan()
        assert "tool_a" not in result

    def test_schemas_returns_openai_tool_defs(self, tmp_workspace: Path):
        """schemas() returns valid OpenAI tool definitions."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        (tools_dir / "search_tool.py").write_text(
            "def search_tool(query: str) -> dict:\n"
            '    """Search the database.\n\n'
            "    Args:\n"
            "        query: The search query.\n"
            '    """\n'
            '    return {"ok": True, "result": query}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        schemas = registry.schemas()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "search_tool"

    def test_get_returns_path_for_known_tool(self, tmp_workspace: Path):
        """get() returns the file path for a registered tool."""
        tools_dir = tmp_workspace / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        tool_path = tools_dir / "my_tool.py"
        tool_path.write_text(
            "def my_tool(x: int) -> dict:\n"
            '    """My tool.\n\n'
            "    Args:\n"
            "        x: A number.\n"
            '    """\n'
            '    return {"ok": True}\n'
        )

        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.get("my_tool")
        assert result == tool_path

    def test_get_returns_none_for_unknown_tool(self, tmp_workspace: Path):
        """get() returns None for an unregistered tool name."""
        registry = DynamicToolRegistry(tmp_workspace)
        assert registry.get("nonexistent") is None

    def test_empty_tools_dir(self, tmp_workspace: Path):
        """When .aura/tools/ doesn't exist, scan returns empty dict."""
        registry = DynamicToolRegistry(tmp_workspace)
        result = registry.scan()
        assert result == {}
        assert registry.schemas() == []
