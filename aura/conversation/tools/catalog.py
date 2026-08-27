"""Tool catalog for Aura's production and read-only capability surfaces."""

from __future__ import annotations

from typing import Any

from aura.conversation.tools.capability_groups import GODOT, tool_names_for
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect
from aura.conversation.tools.schemas import (
    APPLY_PATCH_TOOL_DEF,
    GIT_TOOL_DEFS,
    LOAD_SKILLS_TOOL_DEF,
    READ_SKILL_RESOURCE_TOOL_DEF,
    READ_TOOL_DEFS,
    REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF,
    TASK_CHECKLIST_TOOL_DEF,
    TERMINAL_TOOL_DEF,
)

# Godot remains implemented and registered, but is not part of the ordinary
# model-facing catalogs.
GODOT_TOOL_NAMES: frozenset[str] = tool_names_for({GODOT})

#: The single production read tool. Line windows via offset/limit already
#: cover the rest of what a bulk-read wrapper would add.
PRODUCTION_READ_TOOL_NAMES: frozenset[str] = frozenset({"read_file"})

#: The single production repository-search tool. Discovery and bulk
#: inspection are ordinary ``shell`` work (``rg --files``, ``Get-ChildItem``,
#: git) rather than a second dedicated tool.
PRODUCTION_SEARCH_TOOL_NAMES: frozenset[str] = frozenset({"grep_search"})

#: The compact observation surface Read-Only Mode offers in addition to the
#: production read/search tools. Unrestricted ``shell`` cannot be offered in a
#: mode whose whole promise is that nothing it offers can mutate.
READ_ONLY_EXTRA_TOOL_NAMES: frozenset[str] = frozenset({"glob"})

#: The production mutation tool — the one edit tool the model acts through.
#: Used by the effect-model tests to prove every mutation tool is explicitly
#: classified.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset({
    "apply_patch",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
})


def _tool_name(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function")
    return str(fn.get("name", "")) if isinstance(fn, dict) else ""


class ToolCatalog:
    """Builds the available tool schemas for current capability facts."""

    def build_tool_defs(
        self,
        *,
        read_only: bool,
        dynamic_schemas: list[dict[str, Any]] | None = None,
        mcp_schemas: list[dict[str, Any]] | None = None,
        plan_review: bool = False,
        skills_active: bool = False,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for the production catalog.

        A normal production turn presents exactly five built-in capabilities
        — ``read_file``, ``grep_search``, ``apply_patch``, ``shell``, and
        ``update_task_checklist`` — plus whichever optional capabilities are
        actually active for this turn. Every optional capability is resolved
        once per real user turn and held for that turn's whole duration, so
        the exposed catalog — and therefore the provider's cached request
        prefix — never moves between rounds.

        ``plan_review`` adds ``review_implementation_plan`` when Plan Review is required for this
        turn (never in read-only mode: nothing read-only can mutate, so there
        is nothing for a plan to gate). ``skills_active`` adds ``load_skills``
        and ``read_skill_resource`` only when this turn's frozen skill index
        actually has candidates.

        Reading an explicitly authorized external location is not a separate
        capability and never changes this catalog: ``read_file`` and
        ``grep_search`` accept such a path directly.

        Provider-hosted web search is likewise not in this catalog, in either
        mode. It is a server tool inside the selected provider's own request,
        owned by ``aura.providers.native_search``; it never becomes a client
        function and never reaches ToolRunner. That is why a Read Only turn
        can still be grounded on the web while this catalog stays exactly the
        existing local read/git surface — no ``apply_patch``, no ``shell``, no
        lifecycle or dynamic mutation tools, and no Aura ``web_search`` proxy.
        """
        if read_only:
            tools: list[dict[str, Any]] = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool)
                in (PRODUCTION_READ_TOOL_NAMES | PRODUCTION_SEARCH_TOOL_NAMES | READ_ONLY_EXTRA_TOOL_NAMES)
            ] + list(GIT_TOOL_DEFS)
        else:
            # Production: one continuous model owns the whole request. The
            # catalog is the minimum a normal implementation turn needs — one
            # obvious route for reading known content, one for repository
            # search, one for filesystem edits, one for commands, and one
            # simple visible TODO — and it is stable: the same set on every
            # active request of the turn.
            production_read_tools = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool) in (PRODUCTION_READ_TOOL_NAMES | PRODUCTION_SEARCH_TOOL_NAMES)
            ]
            tools = (
                production_read_tools
                + [dict(TASK_CHECKLIST_TOOL_DEF)]
                + [dict(APPLY_PATCH_TOOL_DEF)]
                + [dict(TERMINAL_TOOL_DEF)]
            )
            if plan_review:
                tools.append(dict(REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF))

        if skills_active:
            tools.append(dict(LOAD_SKILLS_TOOL_DEF))
            tools.append(dict(READ_SKILL_RESOURCE_TOOL_DEF))

        if not read_only and dynamic_schemas:
            tools.extend(dynamic_schemas)

        if mcp_schemas:
            tools.extend(mcp_schemas)

        return tools

    def effect_for(self, name: str) -> ToolEffect:
        """Explicit effect classification for a built-in tool name.

        Built-ins must be classified: an unclassified built-in is a catalog
        bug and raises instead of silently defaulting to a guess.
        """
        try:
            return BUILTIN_TOOL_EFFECTS[name]
        except KeyError:
            raise KeyError(
                f"built-in tool '{name}' has no explicit effect classification; "
                "add it to BUILTIN_TOOL_EFFECTS in "
                "aura/conversation/tools/effects.py"
            ) from None
