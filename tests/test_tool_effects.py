"""Tests for the authoritative runtime tool-effect model.

Proves the actual production catalog — built-in, dynamic, and MCP schemas —
is fully classified: every built-in tool the catalog exposes has an explicit
effect, and every extensible tool that declares none *fails safe* to a
consequential effect rather than being assumed read-only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aura.conversation.tools._types import ApprovalDecision, ApprovalRequest
from aura.conversation.tools.catalog import MUTATION_TOOL_NAMES, ToolCatalog
from aura.conversation.tools.dynamic_registry import DynamicToolRegistry
from aura.conversation.tools.effects import (
    BUILTIN_TOOL_EFFECTS,
    DEFAULT_EXTENSIBLE_TOOL_EFFECT,
    DYNAMIC_EFFECT_METADATA_NAME,
    SCHEMA_EFFECT_KEY,
    ToolEffect,
    effect_from_metadata,
    parse_dynamic_effect_metadata,
)
from aura.conversation.tools.mcp_registry import MCPToolRegistry
from aura.conversation.tools.registry import ToolRegistry


def _tool_names(tool_defs: list[dict]) -> list[str]:
    names: list[str] = []
    for tool_def in tool_defs:
        fn = tool_def.get("function") or {}
        name = fn.get("name")
        if name:
            names.append(name)
    return names


def _production_catalog_names() -> set[str]:
    """Every tool name the actual production catalog can expose."""
    catalog = ToolCatalog()
    names: set[str] = set()
    for mode in ("single", "worker", "planner"):
        names.update(_tool_names(catalog.build_tool_defs(mode=mode, read_only=False)))
    names.update(_tool_names(catalog.build_tool_defs(mode="single", read_only=True)))
    names.update(_tool_names(catalog.build_focused_action_tool_defs()))
    return names


def _builtin_tool_names() -> set[str]:
    """Every built-in tool name: catalog-exposed union plus static handlers."""
    from aura.conversation.tools.registry import TOOL_HANDLERS

    return _production_catalog_names() | set(TOOL_HANDLERS.keys())


def test_every_built_in_production_tool_is_explicitly_classified() -> None:
    names = _builtin_tool_names()
    assert names, "production tool surface must be non-empty"
    unclassified = sorted(n for n in names if n not in BUILTIN_TOOL_EFFECTS)
    assert not unclassified, f"unclassified built-in tools: {unclassified}"
    # Every explicit classification must name a real tool, so future built-ins
    # cannot be added without classification or removal.
    extras = sorted(set(BUILTIN_TOOL_EFFECTS) - names)
    assert not extras, f"classifications for tools not in the catalog: {extras}"


def test_catalog_effect_for_raises_on_unclassified_builtin() -> None:
    catalog = ToolCatalog()
    with pytest.raises(KeyError):
        catalog.effect_for("not_a_real_tool")


def test_catalog_mutation_set_classifies_as_mutation() -> None:
    catalog = ToolCatalog()
    for name in MUTATION_TOOL_NAMES:
        assert catalog.effect_for(name) is ToolEffect.MUTATION, name


def _write_dynamic_script(tools_dir: Path, name: str, effect_line: str | None) -> Path:
    content = (
        f'def {name}(query: str) -> str:\n'
        '    """A test dynamic tool."""\n'
        '    return "ok"\n'
    )
    if effect_line:
        content = effect_line + "\n" + content
    path = tools_dir / f"{name}.py"
    path.write_text(content, encoding="utf-8")
    return path


def test_dynamic_tool_declared_effect_via_script_metadata(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True)
    _write_dynamic_script(
        tools_dir, "declared_mutator", f'{DYNAMIC_EFFECT_METADATA_NAME} = "mutation"'
    )
    _write_dynamic_script(tools_dir, "plain_observer", None)

    registry = DynamicToolRegistry(tmp_path)
    assert registry.effect("declared_mutator") is ToolEffect.MUTATION
    # Missing metadata -> None -> registry lookup applies observation default.
    assert registry.effect("plain_observer") is None


def test_registry_lookup_unannotated_dynamic_tool_fails_safe(tmp_path: Path) -> None:
    """An undeclared dynamic script is arbitrary local code, not an observation."""
    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True)
    _write_dynamic_script(tools_dir, "plain_observer", None)

    registry = ToolRegistry(tmp_path)
    effect = registry.tool_effect("plain_observer")
    assert effect is DEFAULT_EXTENSIBLE_TOOL_EFFECT
    assert effect is ToolEffect.COMMAND
    assert effect is not ToolEffect.OBSERVATION


def test_dynamic_resolved_effect_keeps_declarations(tmp_path: Path) -> None:
    tools_dir = tmp_path / ".aura" / "tools"
    tools_dir.mkdir(parents=True)
    _write_dynamic_script(
        tools_dir, "declared_reader", f'{DYNAMIC_EFFECT_METADATA_NAME} = "observation"'
    )
    _write_dynamic_script(tools_dir, "undeclared", None)

    registry = DynamicToolRegistry(tmp_path)
    assert registry.resolved_effect("declared_reader") is ToolEffect.OBSERVATION
    assert registry.resolved_effect("undeclared") is DEFAULT_EXTENSIBLE_TOOL_EFFECT
    # Not a dynamic tool at all: this registry has no opinion to give.
    assert registry.resolved_effect("no_such_tool") is None


def test_parse_dynamic_effect_metadata(tmp_path: Path) -> None:
    script = tmp_path / "tool.py"
    script.write_text(
        f'{DYNAMIC_EFFECT_METADATA_NAME} = "bookkeeping/control"\n'
        "def t() -> None:\n    pass\n",
        encoding="utf-8",
    )
    assert parse_dynamic_effect_metadata(script) is ToolEffect.BOOKKEEPING

    script.write_text("def t() -> None:\n    pass\n", encoding="utf-8")
    assert parse_dynamic_effect_metadata(script) is None

    script.write_text(
        f'{DYNAMIC_EFFECT_METADATA_NAME} = "bogus"\n'
        "def t() -> None:\n    pass\n",
        encoding="utf-8",
    )
    assert parse_dynamic_effect_metadata(script) is None


class _FakeMCPClient:
    def call_tool(self, name: str, args: dict) -> dict:
        return {"ok": True, "result": "ok"}


def test_mcp_tool_declared_effect_and_default(tmp_path: Path) -> None:
    mcp = MCPToolRegistry()
    client = _FakeMCPClient()
    mcp.register_tool_def(
        {
            "name": "server_mutator",
            "description": "mutates server side",
            "inputSchema": {"type": "object"},
            SCHEMA_EFFECT_KEY: "mutation",
        },
        client,
    )
    mcp.register_tool_def(
        {
            "name": "server_observer",
            "description": "reads server side",
            "inputSchema": {"type": "object"},
        },
        client,
    )
    mcp.register_tool_def(
        {
            "name": "server_readonly_hint",
            "description": "read only",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
        client,
    )

    assert mcp.effect("server_mutator") is ToolEffect.MUTATION
    assert mcp.effect("server_observer") is None  # no metadata -> default
    assert mcp.effect("server_readonly_hint") is ToolEffect.OBSERVATION

    # Registry lookup resolves the same tool set with observation fallback.
    registry = ToolRegistry(tmp_path)
    registry._mcp_tools.register_tool_def(
        {
            "name": "server_mutator",
            "description": "mutates server side",
            "inputSchema": {"type": "object"},
            SCHEMA_EFFECT_KEY: "mutation",
        },
        client,
    )
    registry._mcp_tools.register_tool_def(
        {
            "name": "server_observer",
            "description": "reads server side",
            "inputSchema": {"type": "object"},
        },
        client,
    )
    assert registry.tool_effect("server_mutator") is ToolEffect.MUTATION
    # Unannotated MCP tools fail safe; a name nobody registered fails safe too.
    assert registry.tool_effect("server_observer") is DEFAULT_EXTENSIBLE_TOOL_EFFECT
    assert registry.tool_effect("server_observer") is not ToolEffect.OBSERVATION
    assert registry.tool_effect("totally_unknown_tool") is DEFAULT_EXTENSIBLE_TOOL_EFFECT
    # A readOnlyHint tool registered on this registry stays an observation.
    registry._mcp_tools.register_tool_def(
        {
            "name": "server_readonly_hint",
            "description": "read only",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
        client,
    )
    assert registry.tool_effect("server_readonly_hint") is ToolEffect.OBSERVATION

    # ``register_tool_def`` back-compat-registers a handler in the module-global
    # TOOL_HANDLERS.  Drop these names so the catalog enumeration test
    # (``_builtin_tool_names`` reads TOOL_HANDLERS) is not order-dependent under
    # pytest-randomly.
    from aura.conversation.tools.registry import TOOL_HANDLERS

    for _name in ("server_mutator", "server_observer", "server_readonly_hint"):
        TOOL_HANDLERS.pop(_name, None)


def test_builtin_spot_checks() -> None:
    registry = ToolRegistry(Path.cwd())
    assert registry.tool_effect("read_file") is ToolEffect.OBSERVATION
    assert registry.tool_effect("write_file") is ToolEffect.MUTATION
    assert registry.tool_effect("run_terminal_command") is ToolEffect.COMMAND
    assert registry.tool_effect("update_worker_todo") is ToolEffect.BOOKKEEPING


# ── the fail-safe foundation: exposure and approval follow the effect ───────


def _register_mcp_fixture(mcp: MCPToolRegistry, client: _FakeMCPClient) -> None:
    """One MCP server offering an annotated reader, a declared mutator, and an
    unannotated tool that says nothing about itself."""
    mcp.register_tool_def(
        {
            "name": "srv_reader",
            "description": "read only",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
        client,
    )
    mcp.register_tool_def(
        {
            "name": "srv_writer",
            "description": "writes",
            "inputSchema": {"type": "object"},
            SCHEMA_EFFECT_KEY: "mutation",
        },
        client,
    )
    mcp.register_tool_def(
        {
            "name": "srv_silent",
            "description": "says nothing about its effect",
            "inputSchema": {"type": "object"},
        },
        client,
    )


@pytest.fixture
def mcp_fixture() -> MCPToolRegistry:
    mcp = MCPToolRegistry()
    _register_mcp_fixture(mcp, _FakeMCPClient())
    yield mcp
    from aura.conversation.tools.registry import TOOL_HANDLERS

    for name in ("srv_reader", "srv_writer", "srv_silent"):
        TOOL_HANDLERS.pop(name, None)


def test_mcp_resolved_effect_is_authoritative(mcp_fixture: MCPToolRegistry) -> None:
    assert mcp_fixture.resolved_effect("srv_reader") is ToolEffect.OBSERVATION
    assert mcp_fixture.resolved_effect("srv_writer") is ToolEffect.MUTATION
    assert mcp_fixture.resolved_effect("srv_silent") is DEFAULT_EXTENSIBLE_TOOL_EFFECT
    assert mcp_fixture.resolved_effect("srv_silent") is not ToolEffect.OBSERVATION
    # Not registered here at all.
    assert mcp_fixture.resolved_effect("srv_absent") is None


def test_mcp_approval_is_driven_by_effect_not_name(
    mcp_fixture: MCPToolRegistry,
) -> None:
    """Approval follows the resolved effect: an innocuous-sounding unannotated
    tool is still approved, and an established observation still is not."""
    assert mcp_fixture.requires_approval("srv_reader") is False
    assert mcp_fixture.requires_approval("srv_writer") is True
    assert mcp_fixture.requires_approval("srv_silent") is True


def test_mcp_handler_asks_approval_for_an_unannotated_tool(
    mcp_fixture: MCPToolRegistry, tmp_path: Path
) -> None:
    from aura.conversation.tools.registry import TOOL_HANDLERS

    asked: list[str] = []

    def approval_cb(request: ApprovalRequest) -> ApprovalDecision:
        asked.append(request.tool_name)
        return ApprovalDecision(action="reject")

    owner = ToolRegistry(tmp_path)

    result = TOOL_HANDLERS["srv_silent"](owner, {}, approval_cb, False)
    assert asked == ["srv_silent"], "an unannotated MCP tool must be approved"
    assert result.ok is False
    assert result.payload.get("rejected") is True

    asked.clear()
    result = TOOL_HANDLERS["srv_reader"](owner, {}, approval_cb, False)
    assert asked == [], "an established observation stays approval-free"
    assert result.ok is True


def test_mcp_handler_reject_all_blocks_unannotated_tool(
    mcp_fixture: MCPToolRegistry, tmp_path: Path
) -> None:
    from aura.conversation.tools.registry import TOOL_HANDLERS

    result = TOOL_HANDLERS["srv_silent"](ToolRegistry(tmp_path), {}, None, True)
    assert result.ok is False
    assert result.payload.get("rejected") is True


def test_read_only_registry_exposes_only_observation_mcp_tools(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(tmp_path, read_only=True)
    _register_mcp_fixture(registry._mcp_tools, _FakeMCPClient())
    try:
        names = set(_tool_names(registry.tool_defs()))
        assert "srv_reader" in names
        assert "srv_writer" not in names, "a mutating MCP tool is not read-only"
        assert "srv_silent" not in names, (
            "an unannotated MCP tool has not been established as read-only"
        )

        # Outside read-only mode the full server surface is still offered.
        registry.set_read_only(False)
        full = set(_tool_names(registry.tool_defs()))
        assert {"srv_reader", "srv_writer", "srv_silent"} <= full
    finally:
        from aura.conversation.tools.registry import TOOL_HANDLERS

        for name in ("srv_reader", "srv_writer", "srv_silent"):
            TOOL_HANDLERS.pop(name, None)


def test_effect_from_metadata_parsing() -> None:
    assert effect_from_metadata("observation") is ToolEffect.OBSERVATION
    assert effect_from_metadata("MUTATION") is ToolEffect.MUTATION
    assert effect_from_metadata("command/validation") is ToolEffect.COMMAND
    assert effect_from_metadata("command") is ToolEffect.COMMAND
    assert effect_from_metadata("bookkeeping/control") is ToolEffect.BOOKKEEPING
    assert effect_from_metadata("bookkeeping") is ToolEffect.BOOKKEEPING
    assert effect_from_metadata("nonsense") is None
    assert effect_from_metadata(None) is None
    assert effect_from_metadata({"effect": "mutation"}) is ToolEffect.MUTATION
    assert effect_from_metadata({"class": "observation"}) is ToolEffect.OBSERVATION
