"""Tool catalog - builds the list of tool schemas for the current mode/read-only state."""

from __future__ import annotations

from typing import Any

from aura.conversation.tools._types import RegistryMode
from aura.conversation.tools.capability_groups import BULK_READ, CODE_INTEL, tool_names_for
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect
from aura.conversation.tools.schemas import (
    GIT_TOOL_DEFS,
    LOAD_SKILLS_TOOL_DEF,
    READ_REFERENCE_FILE_TOOL_DEF,
    READ_TOOL_DEFS,
    RECORD_IMPLEMENTATION_DECISION_TOOL_DEF,
    REPORT_ALREADY_SATISFIED_TOOL_DEF,
    REPORT_BLOCKER_TOOL_DEF,
    REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF,
    RUN_AND_WATCH_TOOL_DEF,
    TERMINAL_TOOL_DEF,
    WEB_SEARCH_TOOL_DEF,
    WORKER_TODO_TOOL_DEF,
    WORKSPACE_SNAPSHOT_TOOL_DEF,
    WRITE_TOOL_DEFS,
)
from aura.conversation.tools.schemas.code_intel import INSPECT_CODE_TOOL_DEF
from aura.conversation.tools.schemas.drones import RUN_READ_ONLY_DRONE_TOOL_DEF

# Read and search tools the production single-agent catalog no longer offers.
#
# Every one of these is reachable through a tool that remains: line windows via
# ``read_file``'s offset/limit, multi-file reads via several ``read_file`` calls
# in one round, directory listing via ``glob``, and symbol/structure lookup via
# ``grep_search``. Presenting all of them made the model choose an approach
# before it could choose an action.
#
# ``search_codebase`` is deliberately not in this set: it now returns ranked
# structural retrieval documents (bounded source regions with symbol/kind/
# parent metadata) rather than whole-file recall over what ``grep_search`` and
# ``glob`` already reach exactly, so it is a distinct capability and stays in
# the production catalog.
#
# The handlers stay registered, so a replayed historical tool call still runs.
SINGLE_SUPERSEDED_READ_TOOL_NAMES: frozenset[str] = tool_names_for(
    {BULK_READ, CODE_INTEL}
)

#: The git tools production SINGLE offers. ``git_status`` and ``git_diff`` are
#: the two an implementation turn genuinely needs — what changed, and exactly
#: how. History, branch, and stash inspection is ordinary shell work reachable
#: through ``run_terminal_command``, and a wrapper per git subcommand only
#: enlarged the surface the model has to choose from.
SINGLE_GIT_TOOL_NAMES: frozenset[str] = frozenset({"git_status", "git_diff"})

#: The production mutation tools — the write/edit tools the model acts through.
#: The same set the normal catalogs already register; used by the effect-model
#: tests to prove every mutation tool is explicitly classified.
MUTATION_TOOL_NAMES: frozenset[str] = frozenset({
    "write_file",
    "patch_file",
    "delete_file",
    "edit_godot_scene",
    "edit_godot_editor",
    "edit_godot_asset_preview",
    "install_godot_editor_bridge",
})


def _tool_name(tool_def: dict[str, Any]) -> str:
    fn = tool_def.get("function")
    return str(fn.get("name", "")) if isinstance(fn, dict) else ""


class ToolCatalog:
    """Builds the available tool schemas for the current mode/read-only state."""

    def build_tool_defs(
        self,
        *,
        mode: RegistryMode,
        read_only: bool,
        dynamic_schemas: list[dict[str, Any]] | None = None,
        mcp_schemas: list[dict[str, Any]] | None = None,
        web_search: bool = False,
        plan_review: bool = False,
        reference_available: bool = False,
    ) -> list[dict[str, Any]]:
        """Build tool definitions for the production single-agent catalog.

        ``web_search`` adds the research tool to the production single-agent
        catalog.  It reflects only whether the search backend is configured,
        is resolved once per real user turn, and is held for the whole turn, so
        the catalog the model sees never moves between rounds.

        ``plan_review`` adds ``review_implementation_plan`` when Plan Review
        is enabled for the active real user turn (frozen once at turn start,
        same as ``web_search``). Never offered in read-only mode: nothing
        read-only can mutate, so there is nothing for a plan to gate.

        ``reference_available`` adds ``read_reference_file`` whenever the
        current turn has an authorized external reference — in both
        production and read-only mode, since its effect is observation-only.
        With no reference authorized the catalog is unchanged from before this
        capability existed.
        """
        if read_only:
            tools: list[dict[str, Any]] = (
                list(READ_TOOL_DEFS)
                + [dict(LOAD_SKILLS_TOOL_DEF)]
                + list(GIT_TOOL_DEFS)
            )
        else:
            # Production single-agent mode: one continuous model owns the whole
            # request.  The catalog is the minimum a normal implementation turn
            # needs — read/glob/grep, TODO, the write and Godot edit tools, the
            # terminal, git status/diff, skills, and the two structured exit
            # tools — and it is stable: the same set on every active request of
            # the turn.  ``web_search`` joins it whenever the search backend is
            # configured, and then for the whole turn.
            #
            # Deliberately absent: diagnostic and snapshot wrappers, extra git
            # subcommand wrappers, and drone tools.  Every one of them is either
            # reachable through ``run_terminal_command`` or unrelated to normal
            # implementation, and each one made the model pick an approach
            # before it could pick an action.
            single_read_tools = [
                tool for tool in READ_TOOL_DEFS
                if _tool_name(tool) not in SINGLE_SUPERSEDED_READ_TOOL_NAMES
            ]
            single_git_tools = [
                tool for tool in GIT_TOOL_DEFS
                if _tool_name(tool) in SINGLE_GIT_TOOL_NAMES
            ]
            tools = (
                single_read_tools
                + [dict(INSPECT_CODE_TOOL_DEF)]
                + [dict(WORKER_TODO_TOOL_DEF)]
                + [dict(RECORD_IMPLEMENTATION_DECISION_TOOL_DEF)]
                + list(WRITE_TOOL_DEFS)
                + [dict(REPORT_BLOCKER_TOOL_DEF)]
                + [dict(REPORT_ALREADY_SATISFIED_TOOL_DEF)]
                + [dict(TERMINAL_TOOL_DEF)]
                + [dict(RUN_AND_WATCH_TOOL_DEF)]
                + single_git_tools
                + [dict(LOAD_SKILLS_TOOL_DEF)]
            )
            if web_search:
                tools.append(dict(WEB_SEARCH_TOOL_DEF))
            if plan_review:
                tools.append(dict(REVIEW_IMPLEMENTATION_PLAN_TOOL_DEF))

        if reference_available:
            tools.append(dict(READ_REFERENCE_FILE_TOOL_DEF))

        if not read_only and dynamic_schemas:
            tools.extend(dynamic_schemas)

        if mcp_schemas:
            tools.extend(mcp_schemas)

        return tools

    def build_replayable_tool_defs(
        self,
        *,
        mode: RegistryMode,
        read_only: bool,
    ) -> list[dict[str, Any]]:
        """Defs for tools this mode withholds but still accepts when called.

        A mode narrows its catalog to shape *choice*, not to revoke capability:
        the single-agent catalog drops the superseded read, search, git-history
        and snapshot tools because offering all of them made the model pick an
        approach before it could pick an action, but their handlers stay
        registered so a replayed historical tool call still runs.  Preflight
        rejects any name outside the exposed catalog, so those
        withheld-but-callable names need their schemas from here or the replay
        is rejected instead of executed.

        Only observations qualify.  Nothing that can change the workspace is
        callable from outside the catalog that offered it, which is what keeps
        ``read_only`` and Planner mode's refusal to edit meaningful rather than
        advisory.
        """
        if read_only or mode != "single":
            return []
        exposed = {
            _tool_name(tool)
            for tool in self.build_tool_defs(
                mode=mode, read_only=read_only, web_search=True
            )
        }
        candidates = (
            list(READ_TOOL_DEFS)
            + list(GIT_TOOL_DEFS)
            + [WORKSPACE_SNAPSHOT_TOOL_DEF, RUN_READ_ONLY_DRONE_TOOL_DEF]
        )
        return [
            tool for tool in candidates
            if _tool_name(tool) not in exposed
            and BUILTIN_TOOL_EFFECTS.get(_tool_name(tool)) is ToolEffect.OBSERVATION
        ]

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
