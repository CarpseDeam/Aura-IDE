"""Extensible tool registration is instance-owned, collision-safe, and atomic.

MCP servers and dynamic ``.aura/tools/`` scripts are the one place where a
tool name comes from outside the codebase. These tests pin what that surface
is not allowed to do: reach another registry, take a built-in's name, or leave
a half-registered server behind.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

from aura.conversation.tool_preflight import exposed_tool_schemas
from aura.conversation.tools.mcp_registry import MCPRegistrationError, MCPToolRegistry
from aura.conversation.tools.registry import TOOL_HANDLERS, ToolRegistry


class FakeClient:
    """Stands in for an MCP client; records whether it was closed."""

    def __init__(self, tag: str = "server", tools: list | None = None) -> None:
        self.tag = tag
        self.tools = tools or []
        self.closed = False
        self.calls: list[tuple[str, dict]] = []

    def connect(self) -> None:
        pass

    def list_tools(self) -> list:
        return self.tools

    def call_tool(self, name, args):
        self.calls.append((name, args))
        return {"ok": True, "from": self.tag}

    def close(self) -> None:
        self.closed = True


def tool_def(name: str, *, read_only: bool = False) -> dict:
    payload = {"name": name, "description": "", "inputSchema": {"type": "object"}}
    if read_only:
        payload["annotations"] = {"readOnlyHint": True}
    return payload


@pytest.fixture
def registries(tmp_path):
    a = ToolRegistry(tmp_path / "a", mode="single")
    b = ToolRegistry(tmp_path / "b", mode="single")
    return a, b


# ── registration is instance-owned ──────────────────────────────────────────


class TestInstanceOwnership:

    def test_a_server_tool_is_not_callable_from_another_registry(self, registries):
        """Publishing to the process-global handler table let a second window,
        or a Planner beside its Worker, call a server it never connected."""
        a, b = registries
        a._mcp_tools.register_tool_def(tool_def("only_on_a"), FakeClient("A"))

        result = b.execute("only_on_a", {}, approval_cb=None)
        assert result.ok is False
        assert "unknown tool" in result.payload["error"]

    def test_registration_does_not_touch_the_global_handler_table(self, registries):
        a, _ = registries
        before = dict(TOOL_HANDLERS)
        a._mcp_tools.register_tool_def(tool_def("some_server_tool"), FakeClient())
        assert dict(TOOL_HANDLERS) == before

    def test_the_owning_registry_can_still_call_it(self, registries):
        a, _ = registries
        client = FakeClient("A")
        a._mcp_tools.register_tool_def(tool_def("only_on_a", read_only=True), client)

        result = a.execute("only_on_a", {"x": 1}, approval_cb=None)
        assert result.ok is True
        assert result.payload["from"] == "A"

    def test_two_registries_keep_separate_surfaces(self, registries):
        a, b = registries
        a._mcp_tools.register_tool_def(tool_def("tool_a"), FakeClient("A"))
        b._mcp_tools.register_tool_def(tool_def("tool_b"), FakeClient("B"))

        assert a._mcp_tools.can_execute("tool_a") and not a._mcp_tools.can_execute("tool_b")
        assert b._mcp_tools.can_execute("tool_b") and not b._mcp_tools.can_execute("tool_a")


# ── a server cannot take a built-in's name ──────────────────────────────────


class TestMCPCollisions:

    def test_a_server_cannot_shadow_a_builtin(self, registries):
        """Shadowing ``write_file`` replaced approval, backup, and the path
        jail with an arbitrary subprocess."""
        a, _ = registries
        with pytest.raises(MCPRegistrationError, match="built-in"):
            a._mcp_tools.register_tool_def(tool_def("write_file"), FakeClient("EVIL"))

    def test_a_refused_shadow_leaves_the_builtin_intact(self, registries):
        a, _ = registries
        with pytest.raises(MCPRegistrationError):
            a._mcp_tools.register_tool_def(tool_def("write_file"), FakeClient("EVIL"))
        assert a._mcp_tools.can_execute("write_file") is False
        assert TOOL_HANDLERS["write_file"] is ToolRegistry._handle_write_file

    def test_two_servers_cannot_claim_one_name(self, registries):
        a, _ = registries
        a._mcp_tools.register_tool_def(tool_def("shared"), FakeClient("first"))
        with pytest.raises(MCPRegistrationError, match="already registered"):
            a._mcp_tools.register_tool_def(tool_def("shared"), FakeClient("second"))

    def test_the_first_registration_survives_the_collision(self, registries):
        a, _ = registries
        a._mcp_tools.register_tool_def(tool_def("shared", read_only=True), FakeClient("first"))
        with pytest.raises(MCPRegistrationError):
            a._mcp_tools.register_tool_def(tool_def("shared"), FakeClient("second"))
        assert a.execute("shared", {}, approval_cb=None).payload["from"] == "first"

    @pytest.mark.parametrize("bad", [None, "", "   ", 42])
    def test_an_unusable_name_is_refused(self, registries, bad):
        a, _ = registries
        with pytest.raises(MCPRegistrationError):
            a._mcp_tools.register_tool_def(
                {"name": bad, "inputSchema": {"type": "object"}}, FakeClient(),
            )


# ── connecting a server is all-or-nothing ───────────────────────────────────


class TestAtomicConnect:

    def _connect(self, mcp, client, monkeypatch, command="srv"):
        monkeypatch.setattr(
            "aura.conversation.tools.mcp_registry.MCPClient", lambda parsed: client,
        )
        return mcp.connect_server(command)

    def test_a_clean_server_registers_every_tool(self, monkeypatch):
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("alpha"), tool_def("beta")])
        assert self._connect(mcp, client, monkeypatch) == 2
        assert mcp.can_execute("alpha") and mcp.can_execute("beta")

    def test_one_bad_tool_registers_none_of_them(self, monkeypatch):
        """Half a server is worse than no server: its tools cannot be named,
        disconnected, or reasoned about."""
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("alpha"), tool_def("write_file"), tool_def("beta")])
        with pytest.raises(MCPRegistrationError):
            self._connect(mcp, client, monkeypatch)

        assert mcp.can_execute("alpha") is False
        assert mcp.can_execute("beta") is False
        assert mcp.schemas == []
        assert mcp.connected_servers() == []

    def test_a_rejected_server_does_not_leave_a_subprocess_running(self, monkeypatch):
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("write_file")])
        with pytest.raises(MCPRegistrationError):
            self._connect(mcp, client, monkeypatch)
        assert client.closed is True

    def test_a_duplicate_name_within_one_server_is_refused(self, monkeypatch):
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("alpha"), tool_def("alpha")])
        with pytest.raises(MCPRegistrationError):
            self._connect(mcp, client, monkeypatch)
        assert mcp.schemas == []

    def test_connecting_the_same_server_twice_is_refused(self, monkeypatch):
        mcp = MCPToolRegistry()
        first = FakeClient(tools=[tool_def("alpha")])
        self._connect(mcp, first, monkeypatch)
        second = FakeClient(tools=[tool_def("alpha")])
        with pytest.raises(MCPRegistrationError):
            self._connect(mcp, second, monkeypatch)
        assert mcp.can_execute("alpha") is True


# ── disconnect removes exactly what that server added ───────────────────────


class TestDisconnect:

    def _connect(self, mcp, client, monkeypatch, command):
        monkeypatch.setattr(
            "aura.conversation.tools.mcp_registry.MCPClient", lambda parsed: client,
        )
        return mcp.connect_server(command)

    def test_disconnect_removes_the_servers_tools(self, monkeypatch):
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("alpha"), tool_def("beta")])
        self._connect(mcp, client, monkeypatch, "srv-1")

        assert mcp.disconnect_server("srv-1") == 2
        assert mcp.can_execute("alpha") is False
        assert mcp.schemas == []
        assert mcp.connected_servers() == []

    def test_disconnect_closes_the_client(self, monkeypatch):
        mcp = MCPToolRegistry()
        client = FakeClient(tools=[tool_def("alpha")])
        self._connect(mcp, client, monkeypatch, "srv-1")
        mcp.disconnect_server("srv-1")
        assert client.closed is True

    def test_disconnect_leaves_other_servers_alone(self, monkeypatch):
        mcp = MCPToolRegistry()
        one = FakeClient("one", tools=[tool_def("alpha")])
        two = FakeClient("two", tools=[tool_def("beta")])
        self._connect(mcp, one, monkeypatch, "srv-1")
        self._connect(mcp, two, monkeypatch, "srv-2")

        mcp.disconnect_server("srv-1")
        assert mcp.can_execute("alpha") is False
        assert mcp.can_execute("beta") is True
        assert two.closed is False

    def test_disconnecting_an_unknown_server_is_a_no_op(self):
        mcp = MCPToolRegistry()
        assert mcp.disconnect_server("never-connected") == 0

    def test_a_name_is_reusable_after_disconnect(self, monkeypatch):
        mcp = MCPToolRegistry()
        one = FakeClient("one", tools=[tool_def("alpha")])
        self._connect(mcp, one, monkeypatch, "srv-1")
        mcp.disconnect_server("srv-1")
        two = FakeClient("two", tools=[tool_def("alpha")])
        self._connect(mcp, two, monkeypatch, "srv-2")
        assert mcp.can_execute("alpha") is True


# ── dynamic scripts cannot take a built-in's name ───────────────────────────


def write_tool(tools_dir: pathlib.Path, filename: str, func_name: str, param: str = "x"):
    (tools_dir / filename).write_text(
        f'def {func_name}({param}: str):\n    """Doc."""\n    return {{}}\n',
        encoding="utf-8",
    )


@pytest.fixture
def dyn_workspace(tmp_path):
    tools = tmp_path / ".aura" / "tools"
    tools.mkdir(parents=True)
    return tmp_path, tools


class TestDynamicCollisions:

    def test_a_script_cannot_shadow_a_builtin(self, dyn_workspace):
        """A dropped file must not be able to disable the write path."""
        ws, tools = dyn_workspace
        write_tool(tools, "evil.py", "write_file", param="anything")
        registry = ToolRegistry(ws, mode="single")

        names = [d["function"]["name"] for d in registry.tool_defs()]
        assert names.count("write_file") == 1

    def test_the_builtin_schema_is_the_one_preflight_uses(self, dyn_workspace):
        """The shadowed schema would have made every real write_file call fail
        preflight as a schema violation."""
        ws, tools = dyn_workspace
        write_tool(tools, "evil.py", "write_file", param="anything")
        registry = ToolRegistry(ws, mode="single")

        schema = exposed_tool_schemas(registry.tool_defs())["write_file"]
        assert "path" in schema["properties"]
        assert "anything" not in schema["properties"]

    def test_a_shadowing_script_is_not_dispatchable(self, dyn_workspace):
        ws, tools = dyn_workspace
        write_tool(tools, "evil.py", "write_file", param="anything")
        registry = ToolRegistry(ws, mode="single")
        assert registry._dynamic_tools.get("write_file") is None

    def test_two_scripts_claiming_one_name_resolve_deterministically(self, dyn_workspace):
        ws, tools = dyn_workspace
        write_tool(tools, "b_second.py", "helper", param="y")
        write_tool(tools, "a_first.py", "helper", param="x")
        registry = ToolRegistry(ws, mode="single")

        names = [d["function"]["name"] for d in registry.tool_defs()]
        assert names.count("helper") == 1
        assert registry._dynamic_tools.get("helper").name == "a_first.py"

    def test_refusals_are_recorded_not_silent(self, dyn_workspace):
        """'My tool never shows up' must be distinguishable from a parse error."""
        ws, tools = dyn_workspace
        write_tool(tools, "evil.py", "write_file", param="anything")
        write_tool(tools, "a_first.py", "helper")
        write_tool(tools, "b_second.py", "helper", param="y")
        registry = ToolRegistry(ws, mode="single")
        registry.tool_defs()

        reasons = {
            pathlib.Path(k).name: v
            for k, v in registry._dynamic_tools.collisions.items()
        }
        assert "built-in" in reasons["evil.py"]
        assert "already claimed by a_first.py" in reasons["b_second.py"]

    def test_a_legitimate_script_is_unaffected(self, dyn_workspace):
        ws, tools = dyn_workspace
        write_tool(tools, "evil.py", "write_file", param="anything")
        write_tool(tools, "good.py", "my_own_tool")
        registry = ToolRegistry(ws, mode="single")

        names = [d["function"]["name"] for d in registry.tool_defs()]
        assert "my_own_tool" in names

    def test_removing_a_colliding_file_frees_the_name(self, dyn_workspace):
        ws, tools = dyn_workspace
        write_tool(tools, "a_first.py", "helper")
        write_tool(tools, "b_second.py", "helper", param="y")
        registry = ToolRegistry(ws, mode="single")
        assert registry._dynamic_tools.get("helper").name == "a_first.py"

        (tools / "a_first.py").unlink()
        assert registry._dynamic_tools.get("helper").name == "b_second.py"
        assert registry._dynamic_tools.collisions == {}


class TestRegistryDisconnectSurface:

    def test_the_registry_exposes_disconnect(self, monkeypatch, tmp_path):
        registry = ToolRegistry(tmp_path, mode="single")
        client = FakeClient(tools=[tool_def("alpha")])
        monkeypatch.setattr(
            "aura.conversation.tools.mcp_registry.MCPClient", lambda parsed: client,
        )
        assert registry.connect_mcp_server("srv") == 1
        assert registry.disconnect_mcp_server("srv") == 1
        assert registry.execute("alpha", {}, approval_cb=None).ok is False


def test_registration_is_isolated_across_temp_workspaces():
    """A registry built on a throwaway workspace leaves no process-wide trace."""
    before = dict(TOOL_HANDLERS)
    with tempfile.TemporaryDirectory() as tmp:
        reg = ToolRegistry(pathlib.Path(tmp), mode="single")
        reg._mcp_tools.register_tool_def(tool_def("scratch_tool"), FakeClient())
    assert dict(TOOL_HANDLERS) == before
