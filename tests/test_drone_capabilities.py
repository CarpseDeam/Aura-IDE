"""Tests for aura.drones.capabilities — capability data model."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from aura.drones.badges import compute_capability_badges
from aura.drones.capabilities import (
    CapabilityBinding,
    CapabilityCandidate,
    CapabilityRequirement,
    CapabilityResolution,
)
from aura.drones.definition import DroneDefinition

# ---------------------------------------------------------------------------
# CapabilityRequirement
# ---------------------------------------------------------------------------


class TestCapabilityRequirementConstruction:
    def test_default_construction(self) -> None:
        req = CapabilityRequirement(capability="web_search")
        assert req.capability == "web_search"
        assert req.purpose == ""
        assert req.notes == ""

    def test_full_construction(self) -> None:
        req = CapabilityRequirement(
            capability="web_search",
            purpose="Find documentation",
            notes="Needs internet access",
        )
        assert req.capability == "web_search"
        assert req.purpose == "Find documentation"
        assert req.notes == "Needs internet access"

    def test_frozen(self) -> None:
        req = CapabilityRequirement(capability="web_search")
        with pytest.raises(FrozenInstanceError):
            req.capability = "file_read"  # type: ignore[misc]


class TestCapabilityRequirementRoundtrip:
    def test_to_dict(self) -> None:
        req = CapabilityRequirement(
            capability="web_search",
            purpose="Find docs",
            notes="Needs internet",
        )
        d = req.to_dict()
        assert d == {
            "capability": "web_search",
            "purpose": "Find docs",
            "notes": "Needs internet",
        }

    def test_roundtrip(self) -> None:
        original = CapabilityRequirement(
            capability="web_search",
            purpose="Find docs",
            notes="Needs internet",
        )
        restored = CapabilityRequirement.from_dict(original.to_dict())
        assert restored == original

    def test_empty_defaults(self) -> None:
        restored = CapabilityRequirement.from_dict({})
        assert restored.capability == ""
        assert restored.purpose == ""
        assert restored.notes == ""

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "extra": "ignored",
        }
        restored = CapabilityRequirement.from_dict(data)
        assert restored.capability == "web_search"
        assert restored.purpose == ""
        assert restored.notes == ""


# ---------------------------------------------------------------------------
# CapabilityCandidate
# ---------------------------------------------------------------------------


class TestCapabilityCandidateConstruction:
    def test_minimal_construction(self) -> None:
        c = CapabilityCandidate(
            capability="web_search",
            route_kind="api",
            source="openai",
        )
        assert c.capability == "web_search"
        assert c.route_kind == "api"
        assert c.source == "openai"
        assert c.confidence == 0.0
        assert c.setup_required is False
        assert c.setup_notes == ""
        assert c.tool_names == ()
        assert c.install_command == ""
        assert c.docs_url == ""

    def test_full_construction(self) -> None:
        c = CapabilityCandidate(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            confidence=0.85,
            setup_required=True,
            setup_notes="Install duckduckgo_search",
            tool_names=("duckduckgo_search",),
            install_command="pip install duckduckgo_search",
            docs_url="https://pypi.org/project/duckduckgo-search/",
        )
        assert c.capability == "web_search"
        assert c.route_kind == "cli"
        assert c.source == "duckduckgo"
        assert c.confidence == 0.85
        assert c.setup_required is True
        assert c.setup_notes == "Install duckduckgo_search"
        assert c.tool_names == ("duckduckgo_search",)
        assert c.install_command == "pip install duckduckgo_search"
        assert c.docs_url == "https://pypi.org/project/duckduckgo-search/"

    def test_frozen(self) -> None:
        c = CapabilityCandidate(
            capability="web_search",
            route_kind="api",
            source="test",
        )
        with pytest.raises(FrozenInstanceError):
            c.confidence = 1.0  # type: ignore[misc]


class TestCapabilityCandidateRoundtrip:
    def test_to_dict(self) -> None:
        c = CapabilityCandidate(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            confidence=0.85,
            setup_required=True,
            setup_notes="Install the package",
            tool_names=("duckduckgo_search", "selenium"),
            install_command="pip install duckduckgo_search",
            docs_url="https://example.com",
        )
        d = c.to_dict()
        assert d["capability"] == "web_search"
        assert d["route_kind"] == "cli"
        assert d["source"] == "duckduckgo"
        assert d["confidence"] == 0.85
        assert d["setup_required"] is True
        assert d["setup_notes"] == "Install the package"
        assert d["tool_names"] == ["duckduckgo_search", "selenium"]
        assert d["install_command"] == "pip install duckduckgo_search"
        assert d["docs_url"] == "https://example.com"

    def test_roundtrip(self) -> None:
        original = CapabilityCandidate(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            confidence=0.85,
            setup_required=True,
            setup_notes="Install the package",
            tool_names=("duckduckgo_search",),
            install_command="pip install duckduckgo_search",
            docs_url="https://example.com",
        )
        restored = CapabilityCandidate.from_dict(original.to_dict())
        assert restored == original

    def test_minimal_roundtrip(self) -> None:
        original = CapabilityCandidate(
            capability="file_read",
            route_kind="builtin",
            source="core",
        )
        restored = CapabilityCandidate.from_dict(original.to_dict())
        assert restored == original

    def test_list_to_tuple_conversion(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "route_kind": "cli",
            "source": "test",
            "tool_names": ["tool_a", "tool_b"],
        }
        restored = CapabilityCandidate.from_dict(data)
        assert isinstance(restored.tool_names, tuple)
        assert restored.tool_names == ("tool_a", "tool_b")

    def test_empty_defaults(self) -> None:
        restored = CapabilityCandidate.from_dict({})
        assert restored.capability == ""
        assert restored.route_kind == ""
        assert restored.source == ""
        assert restored.confidence == 0.0
        assert restored.setup_required is False
        assert restored.setup_notes == ""
        assert restored.tool_names == ()
        assert restored.install_command == ""
        assert restored.docs_url == ""

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "route_kind": "api",
            "source": "test",
            "extra_field": "ignored",
        }
        restored = CapabilityCandidate.from_dict(data)
        assert restored.capability == "web_search"
        assert restored.route_kind == "api"
        assert restored.source == "test"


# ---------------------------------------------------------------------------
# CapabilityBinding
# ---------------------------------------------------------------------------


class TestCapabilityBindingConstruction:
    def test_minimal_construction(self) -> None:
        b = CapabilityBinding(
            capability="web_search",
            route_kind="api",
            source="openai",
        )
        assert b.capability == "web_search"
        assert b.route_kind == "api"
        assert b.source == "openai"
        assert b.tool_names == ()
        assert b.setup_status == "pending"
        assert b.setup_notes == ""
        assert b.command == ""
        assert b.metadata == {}

    def test_full_construction(self) -> None:
        b = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            tool_names=("ddg_search",),
            setup_status="installed",
            setup_notes="v2.0.1",
            command="ddg_search --query",
            metadata={"version": "2.0.1"},
        )
        assert b.capability == "web_search"
        assert b.route_kind == "cli"
        assert b.source == "duckduckgo"
        assert b.tool_names == ("ddg_search",)
        assert b.setup_status == "installed"
        assert b.setup_notes == "v2.0.1"
        assert b.command == "ddg_search --query"
        assert b.metadata == {"version": "2.0.1"}

    def test_frozen(self) -> None:
        b = CapabilityBinding(
            capability="web_search",
            route_kind="api",
            source="test",
        )
        with pytest.raises(FrozenInstanceError):
            b.setup_status = "installed"  # type: ignore[misc]


class TestCapabilityBindingRoundtrip:
    def test_to_dict(self) -> None:
        b = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            tool_names=("ddg_search",),
            setup_status="installed",
            setup_notes="v2.0.1",
            command="ddg_search --query",
            metadata={"version": "2.0.1", "priority": 1},
        )
        d = b.to_dict()
        assert d["capability"] == "web_search"
        assert d["route_kind"] == "cli"
        assert d["source"] == "duckduckgo"
        assert d["tool_names"] == ["ddg_search"]
        assert d["setup_status"] == "installed"
        assert d["setup_notes"] == "v2.0.1"
        assert d["command"] == "ddg_search --query"
        assert d["metadata"] == {"version": "2.0.1", "priority": 1}

    def test_roundtrip(self) -> None:
        original = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            tool_names=("ddg_search",),
            setup_status="installed",
            setup_notes="v2.0.1",
            command="ddg_search --query",
            metadata={"version": "2.0.1"},
        )
        restored = CapabilityBinding.from_dict(original.to_dict())
        assert restored == original

    def test_minimal_roundtrip(self) -> None:
        original = CapabilityBinding(
            capability="file_read",
            route_kind="builtin",
            source="core",
        )
        restored = CapabilityBinding.from_dict(original.to_dict())
        assert restored == original

    def test_list_to_tuple_conversion(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "route_kind": "cli",
            "source": "test",
            "tool_names": ["tool_a", "tool_b"],
        }
        restored = CapabilityBinding.from_dict(data)
        assert isinstance(restored.tool_names, tuple)
        assert restored.tool_names == ("tool_a", "tool_b")

    def test_empty_defaults(self) -> None:
        restored = CapabilityBinding.from_dict({})
        assert restored.capability == ""
        assert restored.route_kind == ""
        assert restored.source == ""
        assert restored.tool_names == ()
        assert restored.setup_status == "pending"
        assert restored.setup_notes == ""
        assert restored.command == ""
        assert restored.metadata == {}

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "route_kind": "api",
            "source": "test",
            "bogus": "ignored",
        }
        restored = CapabilityBinding.from_dict(data)
        assert restored.capability == "web_search"

    def test_metadata_roundtrip(self) -> None:
        original = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            metadata={"installed": True, "version": "2.0.1", "count": 3},
        )
        restored = CapabilityBinding.from_dict(original.to_dict())
        assert restored.metadata == {"installed": True, "version": "2.0.1", "count": 3}

    def test_metadata_default_empty_dict(self) -> None:
        data: dict[str, object] = {
            "capability": "web_search",
            "route_kind": "api",
            "source": "test",
        }
        restored = CapabilityBinding.from_dict(data)
        assert restored.metadata == {}


# ---------------------------------------------------------------------------
# CapabilityResolution
# ---------------------------------------------------------------------------


class TestCapabilityResolutionConstruction:
    def test_minimal_construction(self) -> None:
        res = CapabilityResolution(
            requirements=(),
            candidates=(),
            selected_bindings=(),
            allowed_tools=(),
            setup_notes=(),
        )
        assert res.requirements == ()
        assert res.candidates == ()
        assert res.selected_bindings == ()
        assert res.allowed_tools == ()
        assert res.setup_notes == ()

    def test_full_construction(self) -> None:
        req = CapabilityRequirement(capability="web_search", purpose="Find docs")
        cand = CapabilityCandidate(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
        )
        bind = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            tool_names=("ddg_search",),
        )
        res = CapabilityResolution(
            requirements=(req,),
            candidates=(cand,),
            selected_bindings=(bind,),
            allowed_tools=("ddg_search",),
            setup_notes=("Install duckduckgo_search",),
        )
        assert res.requirements == (req,)
        assert res.candidates == (cand,)
        assert res.selected_bindings == (bind,)
        assert res.allowed_tools == ("ddg_search",)
        assert res.setup_notes == ("Install duckduckgo_search",)

    def test_frozen(self) -> None:
        res = CapabilityResolution(
            requirements=(),
            candidates=(),
            selected_bindings=(),
            allowed_tools=(),
            setup_notes=(),
        )
        with pytest.raises(FrozenInstanceError):
            res.allowed_tools = ("something",)  # type: ignore[misc]


class TestCapabilityResolutionRoundtrip:
    def test_empty_roundtrip(self) -> None:
        original = CapabilityResolution(
            requirements=(),
            candidates=(),
            selected_bindings=(),
            allowed_tools=(),
            setup_notes=(),
        )
        restored = CapabilityResolution.from_dict(original.to_dict())
        assert restored == original

    def test_nested_roundtrip(self) -> None:
        req = CapabilityRequirement(
            capability="web_search",
            purpose="Find documentation",
            notes="Needs internet",
        )
        cand = CapabilityCandidate(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            confidence=0.9,
            setup_required=True,
            setup_notes="pip install duckduckgo_search",
            tool_names=("duckduckgo_search",),
            install_command="pip install duckduckgo_search",
            docs_url="https://pypi.org/project/duckduckgo-search/",
        )
        bind = CapabilityBinding(
            capability="web_search",
            route_kind="cli",
            source="duckduckgo",
            tool_names=("duckduckgo_search",),
            setup_status="pending",
            setup_notes="Will install via pip",
            command="",
            metadata={"source_version": "unknown"},
        )

        original = CapabilityResolution(
            requirements=(req,),
            candidates=(cand,),
            selected_bindings=(bind,),
            allowed_tools=("duckduckgo_search",),
            setup_notes=("pip install duckduckgo_search",),
        )

        d = original.to_dict()
        restored = CapabilityResolution.from_dict(d)

        # Structural equality — each nested object should be equal
        assert restored == original
        assert restored.requirements == (req,)
        assert restored.candidates == (cand,)
        assert restored.selected_bindings == (bind,)
        assert restored.allowed_tools == ("duckduckgo_search",)
        assert restored.setup_notes == ("pip install duckduckgo_search",)

    def test_multiple_nested_roundtrip(self) -> None:
        req1 = CapabilityRequirement(capability="web_search", purpose="Search web")
        req2 = CapabilityRequirement(capability="file_read", purpose="Read files")
        cand1 = CapabilityCandidate(
            capability="web_search",
            route_kind="api",
            source="tavily",
        )
        cand2 = CapabilityCandidate(
            capability="file_read",
            route_kind="builtin",
            source="core",
        )
        bind1 = CapabilityBinding(
            capability="web_search",
            route_kind="api",
            source="tavily",
        )
        bind2 = CapabilityBinding(
            capability="file_read",
            route_kind="builtin",
            source="core",
        )

        original = CapabilityResolution(
            requirements=(req1, req2),
            candidates=(cand1, cand2),
            selected_bindings=(bind1, bind2),
            allowed_tools=("web_search", "file_read"),
            setup_notes=("No setup needed for file_read",),
        )
        restored = CapabilityResolution.from_dict(original.to_dict())
        assert restored == original

    def test_list_to_tuple_conversion(self) -> None:
        data: dict[str, object] = {
            "requirements": [],
            "candidates": [],
            "selected_bindings": [],
            "allowed_tools": ["tool_a", "tool_b"],
            "setup_notes": ["note_a", "note_b"],
        }
        restored = CapabilityResolution.from_dict(data)
        assert isinstance(restored.allowed_tools, tuple)
        assert isinstance(restored.setup_notes, tuple)
        assert restored.allowed_tools == ("tool_a", "tool_b")
        assert restored.setup_notes == ("note_a", "note_b")

    def test_empty_defaults(self) -> None:
        restored = CapabilityResolution.from_dict({})
        assert restored.requirements == ()
        assert restored.candidates == ()
        assert restored.selected_bindings == ()
        assert restored.allowed_tools == ()
        assert restored.setup_notes == ()

    def test_unknown_keys_ignored(self) -> None:
        data: dict[str, object] = {
            "requirements": [],
            "candidates": [],
            "selected_bindings": [],
            "allowed_tools": [],
            "setup_notes": [],
            "extra_key": "ignored",
        }
        restored = CapabilityResolution.from_dict(data)
        assert restored.requirements == ()
        assert restored.allowed_tools == ()


# ---------------------------------------------------------------------------
# compute_capability_badges
# ---------------------------------------------------------------------------


class TestComputeCapabilityBadges:
    """Tests for aura.drones.badges.compute_capability_badges."""

    def test_empty_bindings_returns_empty(self) -> None:
        drone = DroneDefinition(
            id="test",
            name="Test",
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="",
            capability_bindings=(),
        )
        assert compute_capability_badges(drone) == []
    def test_pending_setup_returns_needs_setup(self) -> None:
        bindings = (
            CapabilityBinding(
                capability="web_search",
                route_kind="api",
                source="tavily",
                setup_status="pending",
            ),
        )
        drone = DroneDefinition(
            id="test",
            name="Test",
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="",
            capability_bindings=bindings,
        )
        assert "Needs setup" in compute_capability_badges(drone)

    def test_generated_code_returns_generated_tool(self) -> None:
        bindings = (
            CapabilityBinding(
                capability="code_gen",
                route_kind="generated_code",
                source="aura",
                setup_status="ready",
            ),
        )
        drone = DroneDefinition(
            id="test",
            name="Test",
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="",
            capability_bindings=bindings,
        )
        badges = compute_capability_badges(drone)
        assert "Generated tool" in badges
        assert "Needs setup" not in badges

    def test_mcp_route_returns_uses_mcp(self) -> None:
        bindings = (
            CapabilityBinding(
                capability="mcp_tool",
                route_kind="installed_mcp",
                source="mcp",
                setup_status="ready",
            ),
        )
        drone = DroneDefinition(
            id="test",
            name="Test",
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="",
            capability_bindings=bindings,
        )
        assert "Uses MCP" in compute_capability_badges(drone)

    def test_multiple_bindings_and_test_produces_all_badges(self) -> None:
        bindings = (
            CapabilityBinding(
                capability="web_search",
                route_kind="mcp",
                source="mcp",
                setup_status="ready",
            ),
            CapabilityBinding(
                capability="code_gen",
                route_kind="generated_code",
                source="aura",
                setup_status="pending",
            ),
            CapabilityBinding(
                capability="browser",
                route_kind="browser_existing_session",
                source="browser",
                setup_status="ready",
            ),
        )
        drone = DroneDefinition(
            id="test",
            name="Test",
            description="",
            instructions="",
            write_policy="read_only",
            allowed_tools=(),
            output_contract="",
            capability_bindings=bindings,
        )
        badges = compute_capability_badges(drone)
        assert badges == ["Needs setup", "Uses MCP", "Generated tool"]