from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aura.drones.capabilities import CapabilityBinding, CapabilityCandidate, CapabilityRequirement, CapabilityResolution

# ---------------------------------------------------------------------------
# CapabilityContext
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityContext:
    """Context describing what tools are available for capability resolution."""

    workspace_root: Path
    available_tool_names: tuple[str, ...] = ()
    dynamic_tool_names: tuple[str, ...] = ()
    mcp_tool_names: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# CapabilityProvider (duck-typed protocol)
# ---------------------------------------------------------------------------

# Providers implement find_candidates(self, requirements, context) -> tuple of
# CapabilityCandidate.  No base class required — duck typing is sufficient.


# ---------------------------------------------------------------------------
# CapabilityResolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CapabilityResolver:
    """Deterministic resolver that selects the best candidate per requirement."""

    _providers: tuple[object, ...]

    def __init__(self, providers: list[object]) -> None:
        object.__setattr__(self, "_providers", tuple(providers))

    def resolve(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> CapabilityResolution:
        # 1. Ask every provider for candidates.
        all_candidates: list[CapabilityCandidate] = []
        for provider in self._providers:
            all_candidates.extend(provider.find_candidates(requirements, context))

        # 2. Group candidates by capability string.
        by_capability: dict[str, list[CapabilityCandidate]] = {}
        for c in all_candidates:
            by_capability.setdefault(c.capability, []).append(c)

        # 3. Select best candidate per requirement.
        selected_candidates: list[CapabilityCandidate] = []
        for req in requirements:
            candidates = by_capability.get(req.capability, [])
            if not candidates:
                continue
            # Sort: confidence descending, route_kind, source, tool_names.
            candidates.sort(
                key=lambda c: (-c.confidence, c.route_kind, c.source, c.tool_names)
            )
            selected_candidates.append(candidates[0])

        # 4. Build bindings from selected candidates.
        selected_bindings: list[CapabilityBinding] = []
        for cand in selected_candidates:
            selected_bindings.append(
                CapabilityBinding(
                    capability=cand.capability,
                    route_kind=cand.route_kind,
                    source=cand.source,
                    tool_names=cand.tool_names,
                    setup_status="ready" if not cand.setup_required else "pending",
                    setup_notes=cand.setup_notes,
                    command=cand.install_command,
                    metadata={},
                )
            )

        # 5. Deduplicate and sort all tool names across bindings.
        seen: set[str] = set()
        allowed: list[str] = []
        for b in selected_bindings:
            for t in b.tool_names:
                if t not in seen:
                    seen.add(t)
                    allowed.append(t)
        allowed_tools = tuple(sorted(allowed))

        # 6. Aggregate non-empty setup notes from selected candidates.
        setup_notes = tuple(
            cand.setup_notes
            for cand in selected_candidates
            if cand.setup_required and cand.setup_notes
        )

        return CapabilityResolution(
            requirements=requirements,
            candidates=tuple(all_candidates),
            selected_bindings=tuple(selected_bindings),
            allowed_tools=allowed_tools,
            setup_notes=setup_notes,
        )


# ---------------------------------------------------------------------------
# StaticToolProvider
# ---------------------------------------------------------------------------


_STATIC_CAPABILITY_MAP: dict[str, tuple[str, ...]] = {
    "read_file": ("read_file", "read_files", "list_directory", "glob"),
    "search_code": ("grep_search", "find_usages", "search_codebase"),
    "git_operations": ("git_status", "git_diff", "git_log", "git_show"),
    "terminal": ("run_terminal_command", "run_diagnostic_command"),
    "write_file": ("write_file", "edit_file", "patch_file"),
}


class StaticToolProvider:
    """Matches requirements against statically-known tool capabilities."""

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        candidates: list[CapabilityCandidate] = []
        for req in requirements:
            capability = req.capability
            if capability in context.available_tool_names:
                candidates.append(
                    CapabilityCandidate(
                        capability=capability,
                        route_kind="static_tool",
                        source="aura_core",
                        confidence=1.0,
                        tool_names=(capability,),
                        setup_required=False,
                    )
                )
            elif capability in _STATIC_CAPABILITY_MAP:
                mapped = _STATIC_CAPABILITY_MAP[capability]
                if any(t in context.available_tool_names for t in mapped):
                    candidates.append(
                        CapabilityCandidate(
                            capability=capability,
                            route_kind="static_tool",
                            source="aura_core",
                            confidence=1.0,
                            tool_names=mapped,
                            setup_required=False,
                        )
                    )
        return tuple(candidates)


# ---------------------------------------------------------------------------
# DynamicToolProvider
# ---------------------------------------------------------------------------


class DynamicToolProvider:
    """Matches requirements against project dynamic tools."""

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        candidates: list[CapabilityCandidate] = []
        for req in requirements:
            if req.capability in context.dynamic_tool_names:
                candidates.append(
                    CapabilityCandidate(
                        capability=req.capability,
                        route_kind="dynamic_tool",
                        source="project_dynamic",
                        confidence=0.9,
                        tool_names=(req.capability,),
                        setup_required=False,
                    )
                )
        return tuple(candidates)


# ---------------------------------------------------------------------------
# InstalledMCPProvider
# ---------------------------------------------------------------------------


class InstalledMCPProvider:
    """Matches requirements against installed MCP server tools."""

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        candidates: list[CapabilityCandidate] = []
        for req in requirements:
            if req.capability in context.mcp_tool_names:
                candidates.append(
                    CapabilityCandidate(
                        capability=req.capability,
                        route_kind="mcp",
                        source="installed_mcp_server",
                        confidence=0.85,
                        tool_names=(req.capability,),
                        setup_required=False,
                    )
                )
        return tuple(candidates)


# ---------------------------------------------------------------------------
# GeneratedCodeFallbackProvider
# ---------------------------------------------------------------------------


class GeneratedCodeFallbackProvider:
    """Fallback that offers code generation for any capability."""

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        candidates: list[CapabilityCandidate] = []
        for req in requirements:
            candidates.append(
                CapabilityCandidate(
                    capability=req.capability,
                    route_kind="generated_code",
                    source="aura_codegen",
                    confidence=0.0,
                    tool_names=(req.capability,) if req.capability else (),
                    setup_required=True,
                    setup_notes=(
                        f"Aura can generate a dynamic tool/script for "
                        f"'{req.capability}' when needed."
                    ),
                )
            )
        return tuple(candidates)
