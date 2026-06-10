from __future__ import annotations

import json
from pathlib import Path

from aura.drones.capabilities import CapabilityCandidate, CapabilityRequirement, CapabilityResolution
from aura.drones.capability_resolver import (
    AppRouteProvider,
    CapabilityContext,
    CapabilityResolver,
    DynamicToolProvider,
    GeneratedCodeFallbackProvider,
    InstalledMCPProvider,
    MCPDiscoveryProvider,
    StaticToolProvider,
)

_WORKSPACE = Path("/workspace")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ctx(
    *,
    available: tuple[str, ...] = (),
    dynamic: tuple[str, ...] = (),
    mcp: tuple[str, ...] = (),
) -> CapabilityContext:
    return CapabilityContext(
        workspace_root=_WORKSPACE,
        available_tool_names=available,
        dynamic_tool_names=dynamic,
        mcp_tool_names=mcp,
    )


def _req(capability: str) -> tuple[CapabilityRequirement, ...]:
    return (CapabilityRequirement(capability=capability),)


def _candidate(
    capability: str,
    route_kind: str = "test",
    source: str = "test",
    confidence: float = 0.5,
    tool_names: tuple[str, ...] = (),
    setup_required: bool = False,
    setup_notes: str = "",
) -> CapabilityCandidate:
    return CapabilityCandidate(
        capability=capability,
        route_kind=route_kind,
        source=source,
        confidence=confidence,
        tool_names=tool_names,
        setup_required=setup_required,
        setup_notes=setup_notes,
    )


class _FakeProvider:
    """Simple test provider that returns whatever candidates it was given."""

    def __init__(self, candidates: list[CapabilityCandidate]) -> None:
        self._candidates = candidates

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        return tuple(self._candidates)


# ---------------------------------------------------------------------------
# Test: aggregating candidates
# ---------------------------------------------------------------------------


class TestResolverAggregatesCandidates:
    def test_empty_providers_yields_empty_resolution(self) -> None:
        resolver = CapabilityResolver([])
        resolution = resolver.resolve((), _ctx())
        assert isinstance(resolution, CapabilityResolution)
        assert resolution.candidates == ()
        assert resolution.selected_bindings == ()
        assert resolution.allowed_tools == ()
        assert resolution.setup_notes == ()

    def test_single_provider_candidate_appears(self) -> None:
        cand = _candidate("search", route_kind="cli", source="tavily")
        provider = _FakeProvider([cand])
        resolver = CapabilityResolver([provider])

        resolution = resolver.resolve(_req("search"), _ctx())
        assert cand in resolution.candidates

    def test_multiple_providers_all_candidates_aggregated(self) -> None:
        cand_a = _candidate("search", route_kind="cli", source="tavily", confidence=0.9)
        cand_b = _candidate("search", route_kind="api", source="openai", confidence=0.8)
        provider_a = _FakeProvider([cand_a])
        provider_b = _FakeProvider([cand_b])
        resolver = CapabilityResolver([provider_a, provider_b])

        resolution = resolver.resolve(_req("search"), _ctx())
        assert cand_a in resolution.candidates
        assert cand_b in resolution.candidates
        assert len(resolution.candidates) == 2


# ---------------------------------------------------------------------------
# Test: deterministic selection
# ---------------------------------------------------------------------------


class TestResolverDeterministicSelection:
    def test_higher_confidence_wins(self) -> None:
        req = _req("search")
        cand_low = _candidate("search", route_kind="cli", source="tavily", confidence=0.6)
        cand_high = _candidate("search", route_kind="cli", source="openai", confidence=0.9)
        # Order: low first, then high — ensures sort picks high.
        resolver = CapabilityResolver([_FakeProvider([cand_low, cand_high])])

        resolution = resolver.resolve(req, _ctx())
        assert len(resolution.selected_bindings) == 1
        assert resolution.selected_bindings[0].source == "openai"

    def test_tie_break_by_route_kind(self) -> None:
        """Same confidence, same source — route_kind string sort breaks tie."""
        req = _req("search")
        cand_a = _candidate("search", route_kind="alpha", source="test", confidence=0.5)
        cand_b = _candidate("search", route_kind="beta", source="test", confidence=0.5)
        resolver = CapabilityResolver([_FakeProvider([cand_b, cand_a])])

        resolution = resolver.resolve(req, _ctx())
        assert len(resolution.selected_bindings) == 1
        # "alpha" < "beta", so cand_a wins.
        assert resolution.selected_bindings[0].route_kind == "alpha"

    def test_tie_break_by_source(self) -> None:
        """Same confidence and route_kind — source string sort breaks tie."""
        req = _req("search")
        cand_a = _candidate("search", route_kind="cli", source="alpha", confidence=0.5)
        cand_b = _candidate("search", route_kind="cli", source="beta", confidence=0.5)
        resolver = CapabilityResolver([_FakeProvider([cand_b, cand_a])])

        resolution = resolver.resolve(req, _ctx())
        assert len(resolution.selected_bindings) == 1
        assert resolution.selected_bindings[0].source == "alpha"

    def test_tie_break_by_tool_names(self) -> None:
        """Same confidence, route_kind, source — tool_names tuple sort breaks tie."""
        req = _req("search")
        cand_a = _candidate(
            "search", route_kind="cli", source="test", confidence=0.5,
            tool_names=("alpha_tool",),
        )
        cand_b = _candidate(
            "search", route_kind="cli", source="test", confidence=0.5,
            tool_names=("beta_tool",),
        )
        resolver = CapabilityResolver([_FakeProvider([cand_b, cand_a])])

        resolution = resolver.resolve(req, _ctx())
        assert len(resolution.selected_bindings) == 1
        assert resolution.selected_bindings[0].tool_names == ("alpha_tool",)

    def test_missing_requirement_skipped_gracefully(self) -> None:
        """Resolve with no candidates for a requirement — no crash, no binding."""
        req = _req("missing_cap")
        resolver = CapabilityResolver([])
        resolution = resolver.resolve(req, _ctx())
        assert resolution.selected_bindings == ()


# ---------------------------------------------------------------------------
# Test: allowed_tools
# ---------------------------------------------------------------------------


class TestResolverAllowedTools:
    def test_allowed_tools_from_selected_bindings(self) -> None:
        cand_a = _candidate("read", route_kind="builtin", source="core",
                            tool_names=("read_file", "read_files"))
        cand_b = _candidate("write", route_kind="builtin", source="core",
                            tool_names=("write_file", "edit_file"))
        provider = _FakeProvider([cand_a, cand_b])
        resolver = CapabilityResolver([provider])

        requirements = (
            CapabilityRequirement(capability="read"),
            CapabilityRequirement(capability="write"),
        )
        resolution = resolver.resolve(requirements, _ctx())
        assert resolution.allowed_tools == ("edit_file", "read_file", "read_files", "write_file")

    def test_deduplicated_tool_names(self) -> None:
        """Multiple bindings sharing a tool name produce it only once."""
        cand_a = _candidate("read", route_kind="builtin", source="core",
                            tool_names=("read_file", "read_files"))
        cand_b = _candidate("search", route_kind="builtin", source="core",
                            tool_names=("read_file", "grep"))
        provider = _FakeProvider([cand_a, cand_b])
        resolver = CapabilityResolver([provider])

        requirements = (
            CapabilityRequirement(capability="read"),
            CapabilityRequirement(capability="search"),
        )
        resolution = resolver.resolve(requirements, _ctx())
        assert resolution.allowed_tools == ("grep", "read_file", "read_files")

    def test_empty_when_no_bindings(self) -> None:
        resolver = CapabilityResolver([])
        resolution = resolver.resolve((), _ctx())
        assert resolution.allowed_tools == ()


# ---------------------------------------------------------------------------
# Test: MCPDiscoveryProvider
# ---------------------------------------------------------------------------


class TestMCPDiscoveryProvider:
    """Tests for the workspace-local MCP catalog provider."""

    @staticmethod
    def _write_catalog(tmp_path: Path, items: list[dict]) -> Path:
        catalog_dir = tmp_path / ".aura" / "capabilities"
        catalog_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = catalog_dir / "mcp_catalog.json"
        catalog_path.write_text(json.dumps(items), encoding="utf-8")
        return tmp_path

    def test_missing_catalog_returns_empty(self) -> None:
        provider = MCPDiscoveryProvider()
        ctx = CapabilityContext(workspace_root=Path("/nonexistent"))
        result = provider.find_candidates(
            (CapabilityRequirement(capability="read inbox"),), ctx
        )
        assert result == ()

    def test_valid_catalog_returns_mcp_candidates(self, tmp_path: Path) -> None:
        ws = self._write_catalog(tmp_path, [
            {
                "capability": "read inbox",
                "tool_names": ["gmail_search", "gmail_read"],
                "source": "gmail-mcp",
                "setup_required": True,
                "setup_notes": "Connect Gmail MCP.",
                "install_command": "pip install gmail-mcp",
                "docs_url": "https://example.com",
            }
        ])
        provider = MCPDiscoveryProvider()
        ctx = CapabilityContext(workspace_root=ws)
        result = provider.find_candidates(
            (CapabilityRequirement(capability="read inbox"),), ctx
        )
        assert len(result) == 1
        c = result[0]
        assert c.capability == "read inbox"
        assert c.route_kind == "mcp"
        assert c.source == "gmail-mcp"
        assert c.confidence == 0.5
        assert c.setup_required is True
        assert c.setup_notes == "Connect Gmail MCP."
        assert c.tool_names == ("gmail_search", "gmail_read")
        assert c.install_command == "pip install gmail-mcp"
        assert c.docs_url == "https://example.com"

    def test_non_matching_capability_returns_empty(self, tmp_path: Path) -> None:
        ws = self._write_catalog(tmp_path, [
            {
                "capability": "read inbox",
                "tool_names": ["gmail_search"],
                "source": "gmail-mcp",
            }
        ])
        provider = MCPDiscoveryProvider()
        ctx = CapabilityContext(workspace_root=ws)
        result = provider.find_candidates(
            (CapabilityRequirement(capability="write file"),), ctx
        )
        assert result == ()

    def test_malformed_catalog_entries_skipped(self, tmp_path: Path) -> None:
        ws = self._write_catalog(tmp_path, [
            "bare string",
            {"no_capability_key": True},
            {
                "capability": "read inbox",
                "tool_names": ["gmail_search"],
                "source": "gmail-mcp",
            },
            {"wrong_key": "value"},
        ])
        provider = MCPDiscoveryProvider()
        ctx = CapabilityContext(workspace_root=ws)
        result = provider.find_candidates(
            (CapabilityRequirement(capability="read inbox"),), ctx
        )
        assert len(result) == 1
        assert result[0].tool_names == ("gmail_search",)

    def test_setup_notes_and_install_command_roundtrip(self, tmp_path: Path) -> None:
        ws = self._write_catalog(tmp_path, [
            {
                "capability": "deploy",
                "tool_names": ["deploy_tool"],
                "setup_notes": "Do this first.",
                "install_command": "npx @gmail/mcp",
                "source": "custom",
            }
        ])
        provider = MCPDiscoveryProvider()
        ctx = CapabilityContext(workspace_root=ws)
        result = provider.find_candidates(
            (CapabilityRequirement(capability="deploy"),), ctx
        )
        assert len(result) == 1
        assert result[0].setup_notes == "Do this first."
        assert result[0].install_command == "npx @gmail/mcp"

    def test_catalog_surfaces_through_resolver(self, tmp_path: Path) -> None:
        ws = self._write_catalog(tmp_path, [
            {
                "capability": "read inbox",
                "tool_names": ["gmail_search", "gmail_read"],
                "source": "gmail-mcp",
                "setup_required": True,
            }
        ])
        resolver = CapabilityResolver([MCPDiscoveryProvider()])
        ctx = CapabilityContext(workspace_root=ws)
        resolution = resolver.resolve(
            (CapabilityRequirement(capability="read inbox"),), ctx
        )
        assert len(resolution.selected_bindings) == 1
        binding = resolution.selected_bindings[0]
        assert binding.route_kind == "mcp"
        assert binding.tool_names == ("gmail_search", "gmail_read")
        assert binding.setup_status == "pending"
        assert "gmail_search" in resolution.allowed_tools
        assert "gmail_read" in resolution.allowed_tools


# ---------------------------------------------------------------------------
# Test: AppRouteProvider
# ---------------------------------------------------------------------------


class TestAppRouteProvider:
    """Tests for the app-route heuristic provider."""

    def test_browser_capability_returns_candidates(self) -> None:
        provider = AppRouteProvider()
        result = provider.find_candidates(
            (CapabilityRequirement(capability="read inbox using browser"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert len(result) == 1
        candidate = result[0]
        assert candidate.route_kind in {
            "browser_existing_session",
            "chrome_devtools",
            "browser_extension_bridge",
        }
        assert candidate.confidence == 0.4
        assert candidate.setup_required is True
        assert candidate.setup_notes != ""
        assert candidate.tool_names == ()
        assert candidate.source == "app route heuristic"

    def test_local_app_capability_returns_app_route(self) -> None:
        provider = AppRouteProvider()
        result = provider.find_candidates(
            (CapabilityRequirement(capability="open desktop app"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert len(result) == 1
        assert result[0].route_kind == "windows_ui_automation"

    def test_non_app_capability_returns_empty(self) -> None:
        provider = AppRouteProvider()
        result = provider.find_candidates(
            (CapabilityRequirement(capability="git history"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert result == ()

    def test_route_kind_is_plain_string_not_enum(self) -> None:
        provider = AppRouteProvider()
        result = provider.find_candidates(
            (CapabilityRequirement(capability="browser"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert len(result) == 1
        candidate = result[0]
        assert isinstance(candidate.route_kind, str)
        assert candidate.route_kind in {
            "browser_existing_session",
            "chrome_devtools",
            "windows_ui_automation",
            "browser_extension_bridge",
        }

    def test_candidates_have_setup_required_true(self) -> None:
        provider = AppRouteProvider()
        result = provider.find_candidates(
            (CapabilityRequirement(capability="scrape web page"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert len(result) == 1
        assert result[0].setup_required is True

    def test_app_route_surfaces_through_resolver(self) -> None:
        resolver = CapabilityResolver([AppRouteProvider()])
        resolution = resolver.resolve(
            (CapabilityRequirement(capability="open desktop app"),),
            CapabilityContext(workspace_root=_WORKSPACE),
        )
        assert len(resolution.selected_bindings) == 1
        binding = resolution.selected_bindings[0]
        assert binding.route_kind == "windows_ui_automation"
        assert binding.setup_status == "pending"
        assert binding.tool_names == ()
        # allowed_tools should not crash — may be empty since no tool_names
        assert isinstance(resolution.allowed_tools, tuple)


# ---------------------------------------------------------------------------
# Test: generated-code fallback
# ---------------------------------------------------------------------------


class TestGeneratedCodeFallback:
    def test_fallback_candidate_produced(self) -> None:
        resolver = CapabilityResolver([GeneratedCodeFallbackProvider()])
        resolution = resolver.resolve(_req("unknown_op"), _ctx())
        assert len(resolution.candidates) == 1
        assert resolution.candidates[0].route_kind == "generated_code"
        assert resolution.candidates[0].source == "aura_codegen"
        assert resolution.candidates[0].confidence == 0.0

    def test_fallback_selected_when_no_other_provider(self) -> None:
        resolver = CapabilityResolver([GeneratedCodeFallbackProvider()])
        resolution = resolver.resolve(_req("unknown_op"), _ctx())
        assert len(resolution.selected_bindings) == 1
        assert resolution.selected_bindings[0].route_kind == "generated_code"
        assert resolution.selected_bindings[0].setup_status == "pending"

    def test_fallback_not_selected_when_higher_provider_exists(self) -> None:
        static = StaticToolProvider()
        fallback = GeneratedCodeFallbackProvider()
        resolver = CapabilityResolver([static, fallback])
        ctx = _ctx(available=("grep_search",))

        resolution = resolver.resolve(_req("search_code"), ctx)
        assert len(resolution.selected_bindings) == 1
        # StaticToolProvider has confidence 1.0 for "search_code", so it wins.
        assert resolution.selected_bindings[0].route_kind == "static_tool"


# ---------------------------------------------------------------------------
# Test: setup notes
# ---------------------------------------------------------------------------


class TestSetupNotes:
    def test_setup_notes_included(self) -> None:
        cand = _candidate(
            capability="web_search",
            route_kind="cli",
            source="test",
            setup_required=True,
            setup_notes="Install the search tool",
        )
        resolver = CapabilityResolver([_FakeProvider([cand])])
        resolution = resolver.resolve(_req("web_search"), _ctx())
        assert resolution.setup_notes == ("Install the search tool",)
        assert resolution.selected_bindings[0].setup_status == "pending"

    def test_setup_not_required_excluded(self) -> None:
        cand = _candidate(
            capability="read",
            route_kind="builtin",
            source="core",
            setup_required=False,
            setup_notes="No setup needed",
        )
        resolver = CapabilityResolver([_FakeProvider([cand])])
        resolution = resolver.resolve(_req("read"), _ctx())
        assert resolution.setup_notes == ()
        assert resolution.selected_bindings[0].setup_status == "ready"

    def test_empty_setup_notes_excluded(self) -> None:
        """Candidates with setup_required=True but empty setup_notes are excluded."""
        cand = _candidate(
            capability="web_search",
            route_kind="cli",
            source="test",
            setup_required=True,
            setup_notes="",
        )
        resolver = CapabilityResolver([_FakeProvider([cand])])
        resolution = resolver.resolve(_req("web_search"), _ctx())
        assert resolution.setup_notes == ()

    def test_multiple_setup_notes_preserved_order(self) -> None:
        cand_a = _candidate(
            capability="web_search",
            route_kind="cli",
            source="test",
            setup_required=True,
            setup_notes="Install search",
        )
        cand_b = _candidate(
            capability="parse",
            route_kind="cli",
            source="test",
            setup_required=True,
            setup_notes="Install parser",
        )
        resolver = CapabilityResolver([_FakeProvider([cand_a, cand_b])])
        requirements = (
            CapabilityRequirement(capability="web_search"),
            CapabilityRequirement(capability="parse"),
        )
        resolution = resolver.resolve(requirements, _ctx())
        assert resolution.setup_notes == ("Install search", "Install parser")


# ---------------------------------------------------------------------------
# Test: arbitrary route_kind strings
# ---------------------------------------------------------------------------


class TestArbitraryRouteKind:
    def test_non_standard_route_kind_flows_through(self) -> None:
        cand = _candidate(
            capability="experimental",
            route_kind="future_xyz",
            source="test_lab",
            confidence=0.7,
            tool_names=("x_tool",),
        )
        resolver = CapabilityResolver([_FakeProvider([cand])])
        resolution = resolver.resolve(_req("experimental"), _ctx())
        assert len(resolution.selected_bindings) == 1
        assert resolution.selected_bindings[0].route_kind == "future_xyz"
        assert resolution.selected_bindings[0].tool_names == ("x_tool",)

    def test_route_kind_with_special_characters(self) -> None:
        cand = _candidate(
            capability="custom",
            route_kind="my-custom_kind!",
            source="plugin",
            confidence=0.5,
        )
        resolver = CapabilityResolver([_FakeProvider([cand])])
        resolution = resolver.resolve(_req("custom"), _ctx())
        assert resolution.selected_bindings[0].route_kind == "my-custom_kind!"


# ---------------------------------------------------------------------------
# Integration test: real providers together
# ---------------------------------------------------------------------------


class TestIntegrationRealProviders:
    def test_static_tool_selected_when_available(self) -> None:
        ctx = _ctx(available=("grep_search", "read_file"))
        resolver = CapabilityResolver([
            StaticToolProvider(),
            DynamicToolProvider(),
            InstalledMCPProvider(),
            GeneratedCodeFallbackProvider(),
        ])
        resolution = resolver.resolve(
            (
                CapabilityRequirement(capability="search_code"),
                CapabilityRequirement(capability="unknown_thing"),
            ),
            ctx,
        )
        bindings = {b.capability: b for b in resolution.selected_bindings}
        assert bindings["search_code"].route_kind == "static_tool"
        assert bindings["unknown_thing"].route_kind == "generated_code"
        # allowed_tools should include the static tools and the fallback tool name
        assert "grep_search" in resolution.allowed_tools
        assert "unknown_thing" in resolution.allowed_tools
        assert bindings["unknown_thing"].tool_names == ("unknown_thing",)

    def test_dynamic_tool_selected_when_static_unavailable(self) -> None:
        ctx = _ctx(dynamic=("my_dynamic_tool",))
        resolver = CapabilityResolver([
            StaticToolProvider(),
            DynamicToolProvider(),
            InstalledMCPProvider(),
            GeneratedCodeFallbackProvider(),
        ])
        resolution = resolver.resolve(_req("my_dynamic_tool"), ctx)
        assert resolution.selected_bindings[0].route_kind == "dynamic_tool"
        assert resolution.selected_bindings[0].source == "project_dynamic"

    def test_mcp_tool_selected_when_static_and_dynamic_unavailable(self) -> None:
        ctx = _ctx(mcp=("mcp_search",))
        resolver = CapabilityResolver([
            StaticToolProvider(),
            DynamicToolProvider(),
            InstalledMCPProvider(),
            GeneratedCodeFallbackProvider(),
        ])
        resolution = resolver.resolve(_req("mcp_search"), ctx)
        assert resolution.selected_bindings[0].route_kind == "mcp"
        assert resolution.selected_bindings[0].source == "installed_mcp_server"

    def test_static_tool_direct_name_match(self) -> None:
        """When capability is itself an available tool name, static provider matches it."""
        ctx = _ctx(available=("read_file", "write_file"))
        resolver = CapabilityResolver([StaticToolProvider(), GeneratedCodeFallbackProvider()])
        resolution = resolver.resolve(_req("read_file"), ctx)
        assert resolution.selected_bindings[0].route_kind == "static_tool"
        assert resolution.selected_bindings[0].tool_names == ("read_file",)

    def test_static_tool_mapped_capability(self) -> None:
        """When capability is a map key and a mapped tool is available."""
        ctx = _ctx(available=("grep_search",))
        resolver = CapabilityResolver([StaticToolProvider(), GeneratedCodeFallbackProvider()])
        resolution = resolver.resolve(_req("search_code"), ctx)
        assert resolution.selected_bindings[0].route_kind == "static_tool"
        assert "grep_search" in resolution.selected_bindings[0].tool_names
        assert "find_usages" in resolution.selected_bindings[0].tool_names

    def test_static_tool_map_not_matched_when_no_tool_available(self) -> None:
        """Map key exists but no mapped tool is available — static provider skips it."""
        ctx = _ctx(available=())  # nothing available
        resolver = CapabilityResolver([StaticToolProvider(), GeneratedCodeFallbackProvider()])
        resolution = resolver.resolve(_req("search_code"), ctx)
        # No static candidate, so fallback is selected.
        assert resolution.selected_bindings[0].route_kind == "generated_code"

    def test_allowed_tools_from_mixed_providers(self) -> None:
        ctx = _ctx(
            available=("read_file", "grep_search"),
            dynamic=("my_script",),
        )
        resolver = CapabilityResolver([
            StaticToolProvider(),
            DynamicToolProvider(),
            GeneratedCodeFallbackProvider(),
        ])
        resolution = resolver.resolve(
            (
                CapabilityRequirement(capability="read_file"),
                CapabilityRequirement(capability="my_script"),
            ),
            ctx,
        )
        # read_file -> static_tool -> tool_names=("read_file",)
        # my_script -> dynamic_tool -> tool_names=("my_script",)
        assert "read_file" in resolution.allowed_tools
        assert "my_script" in resolution.allowed_tools
        # allowed_tools is sorted
        assert resolution.allowed_tools == ("my_script", "read_file")
