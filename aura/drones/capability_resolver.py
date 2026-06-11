from __future__ import annotations

import json
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
                    command=cand.command,
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
# MCPDiscoveryProvider
# ---------------------------------------------------------------------------


class MCPDiscoveryProvider:
    """Discovers MCP tool servers from a workspace-local catalog file.

    Reads ``.aura/capabilities/mcp_catalog.json`` if present.
    Matches capabilities by case-insensitive comparison.
    """

    CATALOG_RELATIVE = Path(".aura") / "capabilities" / "mcp_catalog.json"

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        catalog = self._load_catalog(context.workspace_root)
        if not catalog:
            return ()

        matches: list[CapabilityCandidate] = []
        for req in requirements:
            req_cap = req.capability.strip().lower()

            for item in catalog:
                if not isinstance(item, dict):
                    continue
                item_cap = str(item.get("capability", "")).strip().lower()
                if not item_cap or item_cap != req_cap:
                    continue

                # Parse tool_names — must be a list of non-empty strings
                tool_names_raw = item.get("tool_names", [])
                tool_names: tuple[str, ...] = ()
                if isinstance(tool_names_raw, list):
                    tool_names = tuple(
                        str(t) for t in tool_names_raw
                        if isinstance(t, str) and t.strip()
                    )

                candidate = CapabilityCandidate(
                    capability=req.capability,
                    route_kind="mcp",
                    source=str(item.get("source", "mcp catalog")),
                    confidence=0.5,
                    setup_required=bool(item.get("setup_required", True)),
                    setup_notes=str(item.get("setup_notes", "")),
                    tool_names=tool_names,
                    command=str(item.get("command", "")),
                    install_command=str(item.get("install_command", "")),
                    docs_url=str(item.get("docs_url", "")),
                )
                matches.append(candidate)

        return tuple(matches)

    def _load_catalog(self, workspace_root: Path) -> list:
        """Load catalog entries. Returns empty list if file does not exist."""
        catalog_path = workspace_root / self.CATALOG_RELATIVE
        if not catalog_path.is_file():
            return []
        with open(catalog_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        return []


# ---------------------------------------------------------------------------
# AppRouteProvider
# ---------------------------------------------------------------------------


class AppRouteProvider:
    """Suggests browser/app-driving routes for relevant capability requirements.

    Returns candidates with route_kind like ``browser_existing_session``,
    ``chrome_devtools``, ``windows_ui_automation``, or ``browser_extension_bridge``.
    No browser or app automation is actually performed.
    """

    _BROWSER_KEYWORDS: tuple[str, ...] = (
        "browser", "website", "web app", "web page", "web", "chrome",
        "firefox", "edge", "safari", "read a page", "monitor a page",
        "scrape", "crawl", "extension", "bridge",
    )
    _APP_KEYWORDS: tuple[str, ...] = (
        "local app", "desktop app", "desktop", "open an app", "open app",
        "app", "window",
    )

    def find_candidates(
        self,
        requirements: tuple[CapabilityRequirement, ...],
        context: CapabilityContext,
    ) -> tuple[CapabilityCandidate, ...]:
        candidates: list[CapabilityCandidate] = []
        for requirement in requirements:
            cap_lower = requirement.capability.lower().strip()

            is_browser = any(k in cap_lower for k in self._BROWSER_KEYWORDS)
            is_app = any(k in cap_lower for k in self._APP_KEYWORDS)

            if not is_browser and not is_app:
                continue

            if is_app:
                route_kind = "windows_ui_automation"
                setup_notes = (
                    "Requires Windows UI Automation or platform-specific app-driving "
                    "tool (e.g., pywinauto, AppleScript, xdotool). Set up the appropriate "
                    "accessibility bridge for the target OS."
                )
            elif "extension" in cap_lower or "bridge" in cap_lower:
                route_kind = "browser_extension_bridge"
                setup_notes = (
                    "Requires a browser extension bridge to be installed and connected. "
                    "Configure the extension to allow automation access."
                )
            elif "monitor" in cap_lower or "watch" in cap_lower or "devtools" in cap_lower:
                route_kind = "chrome_devtools"
                setup_notes = (
                    "Requires Chrome DevTools Protocol (CDP) connection to a running "
                    "browser instance. Start Chrome with --remote-debugging-port and "
                    "configure the connection."
                )
            else:
                route_kind = "browser_existing_session"
                setup_notes = (
                    "Requires a browser automation session. Connect to an existing "
                    "browser session via Playwright, Puppeteer, or Selenium WebDriver."
                )

            candidate = CapabilityCandidate(
                capability=requirement.capability,
                route_kind=route_kind,
                source="app route heuristic",
                confidence=0.4,
                setup_required=True,
                setup_notes=setup_notes,
                tool_names=(),
                install_command="",
                docs_url="",
            )
            candidates.append(candidate)

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
                    tool_names=(),
                    setup_required=True,
                    setup_notes=(
                        f"A Worker must create a real dynamic tool under .aura/tools "
                        f"for capability '{req.capability}' first. Then save the actual "
                        f"generated tool name in capability_bindings and allowed_tools."
                    ),
                )
            )
        return tuple(candidates)
