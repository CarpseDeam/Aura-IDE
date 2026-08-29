"""Tool registry facade for Aura conversation tools."""
from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from aura.code_intel.index import CodeIntelIndex
from aura.code_intel.inspection import CodeInspector
from aura.codebase_index.indexer import CodebaseIndex  # noqa: F401
from aura.codebase_index.tool import search_codebase as _search_codebase  # noqa: F401
from aura.conversation.plan_review import PlanReviewState, blocked_tool_payload
from aura.conversation.tools._code_intel_mixin import CodeIntelHandlersMixin
from aura.conversation.tools._diagnostic_mixin import DiagnosticHandlersMixin
from aura.conversation.tools._git_mixin import GitHandlersMixin
from aura.conversation.tools._godot_asset_preview_mixin import GodotAssetPreviewHandlersMixin
from aura.conversation.tools._godot_assets_mixin import GodotAssetHandlersMixin
from aura.conversation.tools._godot_editor_mixin import GodotEditorHandlersMixin
from aura.conversation.tools._godot_scene_mixin import GodotSceneHandlersMixin
from aura.conversation.tools._memory_mixin import MemoryHandlersMixin
from aura.conversation.tools._plan_review_mixin import PlanReviewHandlersMixin
from aura.conversation.tools._read_mixin import ReadHandlersMixin
from aura.conversation.tools._search_mixin import SearchHandlersMixin
from aura.conversation.tools._task_checklist_mixin import TaskChecklistHandlersMixin
from aura.conversation.tools._types import ApprovalCallback, ToolExecResult
from aura.conversation.tools._workspace_mixin import WorkspaceHandlersMixin
from aura.conversation.tools._write_mixin import WriteHandlersMixin
from aura.conversation.tools.backup import backup_existing  # noqa: F401
from aura.conversation.tools.catalog import ToolCatalog
from aura.conversation.tools.dynamic_registry import DynamicToolRegistry
from aura.conversation.tools.effects import (
    BUILTIN_TOOL_EFFECTS,
    DEFAULT_EXTENSIBLE_TOOL_EFFECT,
    ToolEffect,
)
from aura.conversation.tools.executor import ToolExecutor
from aura.conversation.tools.external_read import ExternalReadAccess, looks_absolute
from aura.conversation.tools.find_usages import find_usages  # noqa: F401
from aura.conversation.tools.fs_handler import FsReadHandler
from aura.conversation.tools.fs_write import (  # noqa: F401
    propose_patch_file,
    propose_write,
)
from aura.conversation.tools.git_handler import GitHandler
from aura.conversation.tools.grep import grep_files  # noqa: F401
from aura.conversation.tools.mcp_registry import MCPToolRegistry
from aura.conversation.tools.task_context import TaskContextHandlersMixin
from aura.paths import safe_is_relative_to, safe_relative_to

TOOL_HANDLERS: dict[str, Any] = {}


class ToolRegistry(
    CodeIntelHandlersMixin,
    TaskContextHandlersMixin,
    ReadHandlersMixin,
    SearchHandlersMixin,
    GitHandlersMixin,
    GodotAssetHandlersMixin,
    GodotAssetPreviewHandlersMixin,
    GodotEditorHandlersMixin,
    GodotSceneHandlersMixin,
    WriteHandlersMixin,
    TaskChecklistHandlersMixin,
    MemoryHandlersMixin,
    DiagnosticHandlersMixin,
    WorkspaceHandlersMixin,
    PlanReviewHandlersMixin,
):
    """Workspace-scoped tool dispatcher."""

    def __init__(
        self,
        workspace_root: Path,
        read_only: bool = False,
    ) -> None:
        self._root = workspace_root.resolve()
        self._read_only = read_only
        self._codebase_index: CodebaseIndex | None = None
        # The turn-scoped external read allowlist. No model-facing tool can
        # add to or change it; see ExternalReadAccess. It is created before
        # the reader below, which consults it for absolute paths.
        self._external_read = ExternalReadAccess(self._root)
        self._fs_handler = FsReadHandler(
            self._root, self._resolve_readable, self._is_external_target
        )
        self._git_handler = GitHandler(self._root)
        self._code_intel_index = CodeIntelIndex(self._root)
        self._code_inspector = CodeInspector(self._root, self._code_intel_index)
        self._catalog = ToolCatalog()
        self._dynamic_tools = DynamicToolRegistry(self._root)
        self._mcp_tools = MCPToolRegistry()
        # Each extensible registry refuses a name the other already owns. They
        # are pointed at each other here — the one place that holds both — so
        # neither has to import the other and the check stays live rather than
        # snapshotted.
        self._mcp_tools.reserved_names = self._dynamic_tool_names
        self._dynamic_tools.reserved_names = self._mcp_tools.registered_names
        # The turn's cancel event, supplied per execute() call by the tool
        # round. The registry never creates one — it only relays the caller's.
        self._cancel_event: threading.Event | None = None
        # The frozen per-turn skill candidates, supplied per execute() call by
        # the tool round. Only ``load_skills`` reads it; ``None`` means this
        # turn exposed no candidates and every activation request fails
        # truthfully.
        self._skill_turn_state: Any = None
        # The same frozen per-turn skill state, held for the duration of the
        # turn (not cleared between execute() calls) so ``tool_defs()`` can
        # decide whether ``load_skills`` belongs in the catalog this turn.
        # Set once per real user turn via ``set_turn_skill_state``.
        self._turn_skill_state: Any = None
        # Plan Review — required/approved state for the active turn, and the
        # GUI-thread proxy that pauses the tool loop for human review. The
        # state always exists (required defaults to False); the proxy is
        # wired in by the caller that owns a GUI (see
        # ``set_plan_review_proxy``) and is None in a headless registry.
        self._plan_review: PlanReviewState = PlanReviewState()
        self._plan_review_proxy: Any = None
        self._executor = ToolExecutor(
            owner=self,
            dynamic_tools=self._dynamic_tools,
            mcp_tools=self._mcp_tools,
        )

    @property
    def workspace_root(self) -> Path:
        return self._root

    def set_workspace_root(self, root: Path | None) -> None:
        if root is None:
            return
        self._root = root.resolve()
        self._dynamic_tools.set_workspace_root(self._root)
        self._codebase_index = None
        # External access authorized while the old workspace was active must
        # never remain reachable once the active workspace changes.
        self._external_read.set_workspace_root(self._root)
        self._fs_handler = FsReadHandler(
            self._root, self._resolve_readable, self._is_external_target
        )
        self._git_handler = GitHandler(self._root)
        # Replace, not mutate: no CodeIntel fact from the old workspace's
        # index must remain reachable after the root changes.
        self._code_intel_index = CodeIntelIndex(self._root)
        self._code_inspector = CodeInspector(self._root, self._code_intel_index)

    def _refresh_code_intel_paths(self, paths: str | Iterable[str]) -> None:
        """Target-refresh the canonical CodeIntel index for known mutations."""
        raw_paths = (paths,) if isinstance(paths, str) else paths
        changed_files: list[str] = []
        seen: set[str] = set()
        for raw_path in raw_paths:
            if not isinstance(raw_path, str):
                continue
            try:
                target = self._resolve_in_root(raw_path)
            except ValueError:
                continue
            rel_path = safe_relative_to(target, self._root).as_posix()
            if rel_path in seen:
                continue
            seen.add(rel_path)
            changed_files.append(rel_path)

        if changed_files:
            self._code_intel_index.refresh(changed_files=changed_files)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, value: bool) -> None:
        self._read_only = value

    # ---- turn-scoped external read access ----------------------------------

    @property
    def external_read(self) -> ExternalReadAccess:
        """This turn's external read allowlist — the one such authority."""
        return self._external_read

    @property
    def external_read_available(self) -> bool:
        """Whether this turn authorized any external read location."""
        return self._external_read.is_available

    @property
    def external_read_names(self) -> tuple[str, ...]:
        """Non-sensitive display names for the authorized locations."""
        return self._external_read.display_names

    def begin_external_read_turn(self, paths: Iterable[Path] | None) -> tuple[Path, ...]:
        """Replace the allowlist with the paths this turn's user text named."""
        return self._external_read.authorize(paths or ())

    def clear_external_read_authorization(self) -> None:
        """End the turn's external read capability."""
        self._external_read.clear()

    @property
    def plan_review(self) -> PlanReviewState:
        """This turn's Plan Review required/approved state."""
        return self._plan_review

    def set_plan_review_proxy(self, proxy: Any | None) -> None:
        """Wire (or clear) the GUI-thread Plan Review synchronization proxy.

        ``None`` (the default) means no GUI is connected: the
        ``review_implementation_plan`` handler then fails closed instead of
        blocking forever.
        """
        self._plan_review_proxy = proxy

    def get_plan_review_proxy(self) -> Any | None:
        return self._plan_review_proxy

    @property
    def active_cancel_event(self) -> threading.Event | None:
        """The cancel event of the tool call currently executing, if any.

        Handlers that run long external work read this so they stop with the
        rest of the turn. It is the caller's event, relayed — not a second
        cancellation authority.
        """
        return self._cancel_event

    def set_turn_skill_state(self, state: Any | None) -> None:
        """Freeze this real user turn's skill candidates for catalog purposes.

        Called once per turn, before the first ``tool_defs()`` of that turn,
        so ``load_skills`` joins the catalog only when the turn actually has
        candidates — and stays in that state for every round of the turn.
        """
        self._turn_skill_state = state

    def tool_defs(self) -> list[dict[str, Any]]:
        dynamic_schemas = self._dynamic_tools.schemas() if not self._read_only else []
        # Read-only mode exposes only the MCP tools resolved as observations.
        # An unannotated or consequential server tool has no place in a surface
        # whose whole promise is that nothing it offers can change anything.
        mcp_schemas = (
            self._mcp_tools.observation_schemas
            if self._read_only
            else self._mcp_tools.schemas
        )
        skills_active = bool(
            self._turn_skill_state is not None
            and not getattr(self._turn_skill_state, "is_empty", True)
        )
        return self._catalog.build_tool_defs(
            read_only=self._read_only,
            dynamic_schemas=dynamic_schemas or None,
            mcp_schemas=mcp_schemas or None,
            plan_review=(not self._read_only) and self._plan_review.required,
            skills_active=skills_active,
        )

    def tool_effect(self, name: str) -> ToolEffect:
        """Authoritative runtime effect lookup for any exposed tool.

        Built-in tools carry an explicit classification in the model; the
        catalog enumeration test proves every exposed built-in is classified.
        Everything else is extensible surface the runtime cannot inspect, so it
        fails safe: an MCP or dynamic tool resolves to its declared effect when
        it declared one and to the consequential
        :data:`DEFAULT_EXTENSIBLE_TOOL_EFFECT` otherwise, and a name this
        runtime does not recognise at all resolves the same way.  Nothing here
        is ever assumed to be an observation.
        """
        if name in BUILTIN_TOOL_EFFECTS:
            return BUILTIN_TOOL_EFFECTS[name]
        mcp_effect = self._mcp_tools.resolved_effect(name)
        if mcp_effect is not None:
            return mcp_effect
        dynamic_effect = self._dynamic_tools.resolved_effect(name)
        if dynamic_effect is not None:
            return dynamic_effect
        return DEFAULT_EXTENSIBLE_TOOL_EFFECT

    def declared_effect(self, name: str) -> ToolEffect | None:
        """Declared effect of a *known* tool, or None when nothing declares one.

        Built-in tools are always classified.  Extensible tools contribute only
        their explicit declaration (``x-aura-effect`` / ``AURA_TOOL_EFFECT``):
        an undeclared dynamic or MCP tool resolves to None here, never to the
        consequential default.  Runtime policy keeps the fail-safe default in
        :meth:`tool_effect`; a caller that decides *retirement* policy uses
        this so unknown or missing effect metadata is preserved, never treated
        as an observation.
        """
        if name in BUILTIN_TOOL_EFFECTS:
            return BUILTIN_TOOL_EFFECTS[name]
        mcp_effect = self._mcp_tools.effect(name)
        if mcp_effect is not None:
            return mcp_effect
        return self._dynamic_tools.effect(name)

    def _dynamic_tool_names(self) -> frozenset[str]:
        """Names the workspace's dynamic tool scripts currently claim."""
        return frozenset(self._dynamic_tools.scan())

    def active_capabilities(self) -> frozenset[str]:
        """Capability ids the connected extensible surface contributes now.

        Read at prompt-composition time so capability-scoped context exists
        exactly while the tools it describes do.
        """
        return self._mcp_tools.capabilities()

    def connect_mcp_server(
        self,
        server_command: str,
        *,
        tool_filter: Any | None = None,
        capability: str | None = None,
    ) -> int:
        """Connect a server, optionally narrowed to a reviewed tool surface.

        ``tool_filter`` and ``capability`` are forwarded unchanged; see
        :meth:`MCPToolRegistry.connect_server`.
        """
        return self._mcp_tools.connect_server(
            server_command, tool_filter=tool_filter, capability=capability
        )

    def disconnect_mcp_server(self, server_command: str) -> int:
        """Remove a server's tools from this registry and close its client.

        Returns the number of tools removed. Registration is instance-owned,
        so this affects only this registry — another registry in the process
        keeps whatever it connected itself.
        """
        return self._mcp_tools.disconnect_server(server_command)

    def set_restore_point_manager(
        self, mgr: Any | None,
    ) -> None:
        """Set an optional RestorePointManager for pre-write capture.

        When set, the write tool layer calls ``mgr.capture_path(rel_path)``
        before every file mutation so that open restore-point sessions can
        record a baseline.  Pass ``None`` to clear.
        """
        self._restore_point_manager = mgr

    def get_restore_point_manager(self) -> Any | None:
        """Return the current RestorePointManager, or None."""
        return getattr(self, "_restore_point_manager", None)

    def _is_external_target(self, target: Path) -> bool:
        """Whether an already-resolved read target lies outside the workspace."""
        return not safe_is_relative_to(target, self._root)

    def _resolve_readable(self, raw: str) -> Path:
        """Resolve a read target: workspace-relative, or authorized absolute.

        Anything that is not a real absolute path goes to the unchanged
        workspace jail, so relative paths — and the rooted-without-drive forms
        the jail has always folded back into the workspace — behave exactly as
        before. A genuine absolute path inside the active workspace is an
        ordinary workspace read; one outside it is readable only if this turn's
        external allowlist authorizes it, and that allowlist is the sole
        authority for reading outside the workspace. Nothing here is reachable
        from a write, command, or Git handler: those resolve through
        ``_resolve_in_root``, which never consults the allowlist.
        """
        text = "" if raw is None else str(raw).strip()
        if not looks_absolute(text) or not Path(text).is_absolute():
            return self._resolve_in_root(raw)
        if ".." in Path(text).parts:
            raise ValueError("'..' is not allowed in tool paths")
        try:
            resolved = Path(text).expanduser().resolve()
        except (OSError, ValueError):
            resolved = None
        if resolved is not None and safe_is_relative_to(resolved, self._root):
            return resolved
        return self._external_read.resolve(text)

    def _resolve_in_root(self, raw: str) -> Path:
        if raw is None:
            raise ValueError("path is required")
        s = str(raw).strip()
        if s == "":
            raise ValueError("path must not be empty")
        s = s.lstrip("/\\")
        if ".." in Path(s).parts:
            raise ValueError("'..' is not allowed in tool paths")
        candidate = (self._root / s).resolve() if not Path(s).is_absolute() else Path(s).resolve()
        if not safe_is_relative_to(candidate, self._root):
            raise ValueError(f"path '{raw}' escapes workspace root")
        return candidate

    def execute(
        self,
        name: str,
        args: dict[str, Any],
        approval_cb: ApprovalCallback,
        reject_all: bool = False,
        cancel_event: threading.Event | None = None,
        skill_turn_state: Any | None = None,
    ) -> ToolExecResult:
        self._cancel_event = cancel_event
        self._skill_turn_state = skill_turn_state
        try:
            # Plan Review's runtime guarantee: while required and not yet
            # approved for this turn, a MUTATION- or COMMAND-effect call is
            # refused before it reaches any handler — the authoritative
            # effect lookup covers built-in, MCP, and dynamic tools alike, so
            # there is no second handwritten mutation/command-name list to
            # keep in sync. ``PlanReviewState.blocks`` is the single
            # authoritative policy; the tool round's terminal-special-cased
            # dispatch (``shell``, which never reaches this method) consults
            # the same method before it runs.
            if self._plan_review.blocks(self.tool_effect(name)):
                return ToolExecResult(ok=False, payload=blocked_tool_payload())
            return self._executor.execute(name, args, approval_cb, reject_all)
        finally:
            self._cancel_event = None
            self._skill_turn_state = None

    def _handle_load_skills(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        """Serve full bodies of frozen candidate skills for the current turn.

        Read-only and turn-scoped: ids resolve only against the frozen
        candidate index the send loop composed when the real user turn began.
        With no frozen index (``None``) every id is rejected truthfully.  This
        never touches the workspace and never grants filesystem access.
        """
        from aura.skills.turn_state import SkillTurnState, load_skills_result

        state: SkillTurnState | None = self._skill_turn_state
        raw_ids = args.get("skill_ids") or []
        skill_ids = list(raw_ids) if isinstance(raw_ids, list) else []
        if state is None or state.is_empty:
            payload = {
                "ok": True,
                "tool": "load_skills",
                "activated_count": 0,
                "rejected_count": len(skill_ids),
                "skills": [],
                "rejected": [
                    {
                        "skill_id": str(skill_id),
                        "status": "not_exposed_for_turn",
                        "reason": "no skills were selected for this turn",
                    }
                    for skill_id in skill_ids
                ],
            }
            return ToolExecResult(ok=True, payload=payload)
        return ToolExecResult(ok=True, payload=load_skills_result(state, skill_ids))

    def _handle_read_skill_resource(
        self, args: dict[str, Any], approval_cb: ApprovalCallback, reject_all: bool
    ) -> ToolExecResult:
        """Read one supporting resource file of a skill activated this turn.

        Resolution happens entirely against the frozen per-turn snapshot: the
        skill must be exposed in this turn's candidate index *and* already
        activated via ``load_skills``, and only that skill's own canonical
        source directory is ever reachable.
        """
        from aura.skills.turn_state import SkillTurnState, read_skill_resource_result

        state: SkillTurnState | None = self._skill_turn_state
        skill_id = str(args.get("skill_id") or "")
        path = str(args.get("path") or "")
        if state is None or state.is_empty:
            return ToolExecResult(
                ok=False,
                payload={
                    "ok": False,
                    "tool": "read_skill_resource",
                    "error": "no skills were selected for this turn",
                },
            )
        payload = read_skill_resource_result(state, skill_id, path)
        return ToolExecResult(ok=bool(payload.get("ok")), payload=payload)


TOOL_HANDLERS["load_skills"] = ToolRegistry._handle_load_skills
TOOL_HANDLERS["read_skill_resource"] = ToolRegistry._handle_read_skill_resource
TOOL_HANDLERS["read_file"] = ToolRegistry._handle_read_file
TOOL_HANDLERS["read_file_range"] = ToolRegistry._handle_read_file_range
TOOL_HANDLERS["read_task_context"] = ToolRegistry._handle_read_task_context
TOOL_HANDLERS["list_directory"] = ToolRegistry._handle_list_directory
TOOL_HANDLERS["glob"] = ToolRegistry._handle_glob
TOOL_HANDLERS["grep_search"] = ToolRegistry._handle_grep_search
TOOL_HANDLERS["read_file_outline"] = ToolRegistry._handle_read_file_outline
TOOL_HANDLERS["find_usages"] = ToolRegistry._handle_find_usages
TOOL_HANDLERS["search_codebase"] = ToolRegistry._handle_search_codebase
TOOL_HANDLERS["git_status"] = ToolRegistry._handle_git_status
TOOL_HANDLERS["git_diff"] = ToolRegistry._handle_git_diff
TOOL_HANDLERS["git_log"] = ToolRegistry._handle_git_log
TOOL_HANDLERS["git_show"] = ToolRegistry._handle_git_show
TOOL_HANDLERS["git_log_file"] = ToolRegistry._handle_git_log_file
TOOL_HANDLERS["git_branch_list"] = ToolRegistry._handle_git_branch_list
TOOL_HANDLERS["git_stash_list"] = ToolRegistry._handle_git_stash_list
TOOL_HANDLERS["git_stash_show"] = ToolRegistry._handle_git_stash_show
TOOL_HANDLERS["apply_patch"] = ToolRegistry._handle_apply_patch
TOOL_HANDLERS["edit_godot_scene"] = ToolRegistry._handle_edit_godot_scene
TOOL_HANDLERS["inspect_godot_assets"] = ToolRegistry._handle_inspect_godot_assets
TOOL_HANDLERS["inspect_godot_asset_preview"] = ToolRegistry._handle_inspect_godot_asset_preview
TOOL_HANDLERS["capture_godot_asset_preview"] = ToolRegistry._handle_capture_godot_asset_preview
TOOL_HANDLERS["inspect_godot_editor"] = ToolRegistry._handle_inspect_godot_editor
TOOL_HANDLERS["inspect_godot_api"] = ToolRegistry._handle_inspect_godot_api
TOOL_HANDLERS["edit_godot_editor"] = ToolRegistry._handle_edit_godot_editor
TOOL_HANDLERS["edit_godot_asset_preview"] = ToolRegistry._handle_edit_godot_asset_preview
TOOL_HANDLERS["install_godot_editor_bridge"] = ToolRegistry._handle_install_godot_editor_bridge
TOOL_HANDLERS["update_task_checklist"] = ToolRegistry._handle_update_task_checklist

TOOL_HANDLERS["search_project_memory"] = ToolRegistry._handle_search_project_memory
TOOL_HANDLERS["save_to_project_memory"] = ToolRegistry._handle_save_to_project_memory
TOOL_HANDLERS["run_diagnostic_command"] = ToolRegistry._handle_run_diagnostic_command
TOOL_HANDLERS["get_workspace_snapshot"] = ToolRegistry._handle_get_workspace_snapshot
TOOL_HANDLERS["code_intel_outline"] = ToolRegistry._handle_code_intel_outline
TOOL_HANDLERS["code_intel_references"] = ToolRegistry._handle_code_intel_references
TOOL_HANDLERS["code_intel_dependents"] = ToolRegistry._handle_code_intel_dependents
TOOL_HANDLERS["code_intel_audit"] = ToolRegistry._handle_code_intel_audit
TOOL_HANDLERS["inspect_code"] = ToolRegistry._handle_inspect_code
TOOL_HANDLERS["review_implementation_plan"] = ToolRegistry._handle_review_implementation_plan
