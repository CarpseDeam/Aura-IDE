from __future__ import annotations

import pytest

from aura.drones.capabilities import CapabilityBinding
from aura.drones.definition import (
    READ_ONLY_TOOLS,
    WRITE_TOOLS,
    DroneBudget,
    DroneDefinition,
)
from aura.drones.tool_surface import build_drone_tool_surface


def _drone(
    *,
    write_policy: str = "read_only",
    allowed_tools: tuple[str, ...] = (),
    setup_steps: tuple[str, ...] = (),
    capability_bindings: tuple[CapabilityBinding, ...] = (),
) -> DroneDefinition:
    return DroneDefinition(
        id="test",
        name="Test",
        description="",
        instructions="",
        write_policy=write_policy,
        allowed_tools=allowed_tools,
        output_contract="",
        budget=DroneBudget(max_tool_rounds=1, timeout_seconds=30),
        setup_steps=setup_steps,
        capability_bindings=capability_bindings,
    )


class TestDroneToolSurface:
    def test_old_drone_empty_allowed_tools_gets_policy_defaults(self, tmp_path):
        """Drone with empty allowed_tools and read_only policy gets READ_ONLY_TOOLS."""
        drone = _drone(write_policy="read_only", allowed_tools=())
        surface = build_drone_tool_surface(tmp_path, drone)
        assert surface.allowed_tools == frozenset(READ_ONLY_TOOLS)

    def test_read_only_drones_do_not_expose_write_tools(self, tmp_path):
        """Read-only drone strips WRITE_TOOLS even if allowed_tools includes them."""
        drone = _drone(
            write_policy="read_only",
            allowed_tools=READ_ONLY_TOOLS + WRITE_TOOLS,
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        for wt in WRITE_TOOLS:
            assert wt not in surface.allowed_tools
        for rt in READ_ONLY_TOOLS:
            assert rt in surface.allowed_tools

    def test_explicit_allowed_tools_filters_tool_defs(self, tmp_path):
        """Only the explicitly listed tools appear in tool_defs."""
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file", "list_directory"),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        names = {t["function"]["name"] for t in surface.tool_defs}
        assert names == {"read_file", "list_directory"}

    def test_setup_steps_appear_in_surface_setup_notes(self, tmp_path):
        """Drone setup_steps are captured as surface.setup_notes."""
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file",),
            setup_steps=("Install foo", "Run bar"),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert surface.setup_notes == ("Install foo", "Run bar")

    def test_empty_setup_steps_yields_empty_tuple(self, tmp_path):
        """Drone without setup_steps yields empty setup_notes."""
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file",),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert surface.setup_notes == ()

    def test_write_capable_drone_gets_all_tools(self, tmp_path):
        """Write-capable drone with empty allowed_tools gets READ_ONLY + WRITE_TOOLS."""
        drone = _drone(
            write_policy="normal_diff_approval",
            allowed_tools=(),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert surface.allowed_tools == frozenset(READ_ONLY_TOOLS + WRITE_TOOLS)

    def test_surface_is_frozen(self, tmp_path):
        """DroneToolSurface fields cannot be mutated."""
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file",),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        with pytest.raises(Exception):
            surface.allowed_tools = frozenset()  # type: ignore[misc]

    def test_pending_binding_adds_setup_note(self, tmp_path):
        """Pending binding adds setup note and does NOT add its tool_names."""
        binding = CapabilityBinding(
            capability="test_cap",
            route_kind="static_tool",
            source="test",
            tool_names=("write_file",),
            setup_status="pending",
            setup_notes="API key required",
        )
        drone = _drone(
            write_policy="normal_diff_approval",
            allowed_tools=("read_file",),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert any(
            "setup pending" in n and "test_cap" in n and "API key required" in n
            for n in surface.setup_notes
        )
        assert "write_file" not in surface.allowed_tools

    def test_ready_static_binding_adds_tool_when_allowed_empty(self, tmp_path):
        """Ready static binding adds tool_names when allowed_tools is empty."""
        binding = CapabilityBinding(
            capability="git_ops",
            route_kind="static_tool",
            source="test",
            tool_names=("git_log", "git_show"),
            setup_status="ready",
        )
        drone = _drone(
            write_policy="read_only",
            allowed_tools=(),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert "git_log" in surface.allowed_tools
        assert "git_show" in surface.allowed_tools

    def test_ready_binding_respects_explicit_allowed(self, tmp_path):
        """Ready binding does NOT add tools when allowed_tools was explicitly set."""
        binding = CapabilityBinding(
            capability="list_ops",
            route_kind="static_tool",
            source="test",
            tool_names=("list_directory",),
            setup_status="ready",
        )
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file",),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert "list_directory" not in surface.allowed_tools
        assert "read_file" in surface.allowed_tools

    def test_failed_mcp_connect_is_setup_note(self, tmp_path, monkeypatch):
        """MCP connection failure becomes a setup note, not a crash."""
        def _raise(*args, **kwargs):
            raise RuntimeError("connection refused")
        monkeypatch.setattr(
            "aura.conversation.tools.registry.ToolRegistry.connect_mcp_server",
            _raise,
        )
        binding = CapabilityBinding(
            capability="web_search",
            route_kind="mcp",
            source="test",
            tool_names=("brave_web_search",),
            setup_status="ready",
            command="python -m bad-server",
        )
        drone = _drone(
            write_policy="normal_diff_approval",
            allowed_tools=(),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert any("MCP connection failed" in n for n in surface.setup_notes)
        assert "brave_web_search" not in surface.allowed_tools

    def test_successful_mcp_connect_adds_tools(self, tmp_path, monkeypatch):
        """Successful MCP connection adds tool_names when allowed_tools is empty."""
        monkeypatch.setattr(
            "aura.conversation.tools.registry.ToolRegistry.connect_mcp_server",
            lambda self, cmd: 1,
        )
        binding = CapabilityBinding(
            capability="web_search",
            route_kind="mcp",
            source="test",
            tool_names=("brave_web_search",),
            setup_status="ready",
            command="python -m some-server",
        )
        drone = _drone(
            write_policy="normal_diff_approval",
            allowed_tools=(),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert "brave_web_search" in surface.allowed_tools

    def test_ready_binding_write_tool_stripped_for_read_only(self, tmp_path):
        """Write tools from bindings are stripped for read-only drones."""
        binding = CapabilityBinding(
            capability="write_ops",
            route_kind="static_tool",
            source="test",
            tool_names=("write_file",),
            setup_status="ready",
        )
        drone = _drone(
            write_policy="read_only",
            allowed_tools=(),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        assert "write_file" not in surface.allowed_tools

    def test_multiple_bindings_accumulate_setup_notes(self, tmp_path):
        """Multiple pending bindings all appear in setup_notes."""
        b1 = CapabilityBinding(
            capability="cap_one",
            route_kind="static_tool",
            source="test",
            tool_names=(),
            setup_status="pending",
            setup_notes="Need API key",
        )
        b2 = CapabilityBinding(
            capability="cap_two",
            route_kind="mcp",
            source="test",
            tool_names=(),
            setup_status="pending",
            setup_notes="Install CLI tool",
        )
        drone = _drone(
            write_policy="read_only",
            allowed_tools=("read_file",),
            capability_bindings=(b1, b2),
        )
        surface = build_drone_tool_surface(tmp_path, drone)
        cap_one_notes = [n for n in surface.setup_notes if "cap_one" in n]
        cap_two_notes = [n for n in surface.setup_notes if "cap_two" in n]
        assert len(cap_one_notes) == 1
        assert len(cap_two_notes) == 1
        assert "Need API key" in cap_one_notes[0]
        assert "Install CLI tool" in cap_two_notes[0]

    def test_generated_code_dynamic_tool_in_surface(self, tmp_path):
        """Generated code binding with ready setup_status and real .py file flows through."""
        tools_dir = tmp_path / ".aura" / "tools"
        tools_dir.mkdir(parents=True)
        tool_file = tools_dir / "check_status.py"
        tool_file.write_text(
            "def check_status(text: str = \"\") -> dict:\n"
            '    """Check the status of the provided text."""\n'
            '    return {"ok": True, "text": text}\n'
        )

        binding = CapabilityBinding(
            capability="check_status",
            route_kind="generated_code",
            source="aura_codegen",
            tool_names=("check_status",),
            setup_status="ready",
        )
        drone = _drone(
            write_policy="read_only",
            allowed_tools=(),
            capability_bindings=(binding,),
        )
        surface = build_drone_tool_surface(tmp_path, drone)

        # The dynamic tool should be in allowed_tools
        assert "check_status" in surface.allowed_tools

        # The dynamic tool schema should be in tool_defs
        tool_names = {t["function"]["name"] for t in surface.tool_defs}
        assert "check_status" in tool_names

        # Standard read-only tools are still present (binding expanded, not replaced)
        assert "read_file" in surface.allowed_tools
