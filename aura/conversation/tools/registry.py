"""Tool registry facade for Aura conversation tools."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from aura.code_intel.index import CodeIntelIndex
from aura.code_intel.inspection import CodeInspector
from aura.codebase_index.indexer import CodebaseIndex  # noqa: F401
from aura.codebase_index.tool import search_codebase as _search_codebase  # noqa: F401
from aura.conversation.tools._blocker_mixin import BlockerHandlersMixin
from aura.conversation.tools._code_intel_mixin import CodeIntelHandlersMixin
from aura.conversation.tools._decision_mixin import DecisionHandlersMixin
from aura.conversation.tools._diagnostic_mixin import DiagnosticHandlersMixin
from aura.conversation.tools._git_mixin import GitHandlersMixin
from aura.conversation.tools._godot_asset_preview_mixin import GodotAssetPreviewHandlersMixin
from aura.conversation.tools._godot_assets_mixin import GodotAssetHandlersMixin
from aura.conversation.tools._godot_editor_mixin import GodotEditorHandlersMixin
from aura.conversation.tools._godot_scene_mixin import GodotSceneHandlersMixin
from aura.conversation.tools._memory_mixin import MemoryHandlersMixin
from aura.conversation.tools._plan_review_mixin import PlanReviewHandlersMixin
from aura.conversation.tools._planner_mixin import PlannerHandlersMixin
from aura.conversation.tools._read_mixin import ReadHandlersMixin
from aura.conversation.tools._reference_mixin import ReferenceReadHandlersMixin
from aura.conversation.tools._search_mixin import SearchHandlersMixin
from aura.conversation.tools._types import (
    ApprovalCallback,
    RegistryMode,
    ToolExecResult,
)
from aura.conversation.tools._worker_todo_mixin import WorkerTodoHandlersMixin
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
from aura.conversation.tools.find_usages import find_usages  # noqa: F401
from aura.conversation.tools.fs_handler import FsReadHandler
from aura.conversation.tools.fs_write import (  # noqa: F401
    propose_patch_file,
    propose_write,
)
from aura.conversation.tools.git_handler import GitHandler
from aura.conversation.tools.grep import grep_files  # noqa: F401
from aura.conversation.tools.mcp_registry import MCPToolRegistry
from aura.conversation.tools.reference_root import ReferenceRootAccess
from aura.conversation.tools.task_context import TaskContextHandlersMixin
from aura.conversation.plan_review import PlanReviewState, blocked_tool_payload

TOOL_HANDLERS: dict[str, Any] = {}


class ToolRegistry(
    BlockerHandlersMixin,
    CodeIntelHandlersMixin,
    DecisionHandlersMixin,
    TaskContextHandlersMixin,
    ReadHandlersMixin,
    ReferenceReadHandlersMixin,
    SearchHandlersMixin,
    GitHandlersMixin,
    GodotAssetHandlersMixin,
    GodotAssetPreviewHandlersMixin,
    GodotEditorHandlersMixin,
    GodotSceneHandlersMixin,
    WriteHandlersMixin,
    WorkerTodoHandlersMixin,
    MemoryHandlersMixin,
    DiagnosticHandlersMixin,
    PlannerHandlersMixin,
    PlanReviewHandlersMixin,
):
    """Workspace-scoped tool dispatcher."""

    def __init__(
        self,
        workspace_root: Path,
        read_only: bool = False,
        mode: RegistryMode = "single",
    ) -> None:
        self._root = workspace_root.resolve()
        self._read_only = read_only
        self._mode: RegistryMode = mode
        self._codebase_index: CodebaseIndex | None = None
        self._reference_codebase_index: CodebaseIndex | None = None
        self._fs_handler = FsReadHandler(self._root, self._resolve_in_root)
        self._git_handler = GitHandler(self._root)
        self._code_intel_index = CodeIntelIndex(self._root)
        self._code_inspector = CodeInspector(self._root, self._code_intel_index)
        # The single turn-scoped external reference authorization. No
        # model-facing tool can attach or change it. See ReferenceRootAccess.
        self._reference_root = ReferenceRootAccess(self._root)
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
        # Whether the production catalog offers web research. Depends only on
        # whether the search backend is genuinely configured; it is re-resolved
        # once per real user turn and held for that turn's whole duration, so
        # the exposed catalog — and therefore the provider's cached request
        # prefix — never moves between rounds.
        self._web_search: bool = False
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
        self._reference_codebase_index = None
        self._fs_handler = FsReadHandler(self._root, self._resolve_in_root)
        self._git_handler = GitHandler(self._root)
        # Replace, not mutate: no CodeIntel fact from the old workspace's
        # index must remain reachable after the root changes.
        self._code_intel_index = CodeIntelIndex(self._root)
        self._code_inspector = CodeInspector(self._root, self._code_intel_index)
        # A Reference Folder authorized for the old workspace must never
        # remain reachable once the active workspace changes.
        self._reference_root.set_workspace_root(self._root)

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, value: bool) -> None:
        self._read_only = value

    @property
    def mode(self) -> RegistryMode:
        return self._mode

    def set_mode(self, mode: RegistryMode) -> None:
        self._mode = mode

    # ---- turn-scoped external reference ------------------------------------

    @property
    def reference_root_available(self) -> bool:
        """Whether this turn has an authorized external reference root."""
        return self._reference_root.is_available

    @property
    def reference_root_name(self) -> str | None:
        """Non-sensitive folder name used to label reference observations."""
        return self._reference_root.name

    def begin_reference_turn(self, candidate: Path | None) -> tuple[bool, str]:
        """Clear prior authorization and authorize one candidate for a turn."""
        self.clear_reference_authorization()
        if candidate is None:
            return True, "no external reference"
        ok, message = self._reference_root.attach(candidate)
        if not ok:
            # ``attach`` preserves a previous root by design; this boundary
            # must not, because authorization is strictly turn-scoped.
            self.clear_reference_authorization()
        return ok, message

    def clear_reference_authorization(self) -> None:
        """End the turn's external capability and release its live index."""
        self._reference_root.clear()
        self._reference_codebase_index = None

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
    def web_search_enabled(self) -> bool:
        """Whether this turn's production catalog offers ``web_search``."""
        return self._web_search

    def refresh_web_search_availability(self) -> None:
        """Re-resolve whether the web-search backend is actually usable.

        Availability is a fact about configuration — whether the search
        provider has a credential — and never a judgement about what the user
        asked for. Called once at the start of ``send()`` so a key added in
        Settings takes effect on the next turn while the catalog stays fixed
        for the duration of the turn in flight.
        """
        from aura.config import has_api_key
        from aura.research.native import SEARCH_PROVIDER_ID

        try:
            self._web_search = bool(has_api_key(SEARCH_PROVIDER_ID))
        except Exception:
            # Withhold rather than assert: a capability that cannot be
            # confirmed must not be offered as available.
            self._web_search = False

    @property
    def active_cancel_event(self) -> threading.Event | None:
        """The cancel event of the tool call currently executing, if any.

        Handlers that run long external work (web search) read this so they
        stop with the rest of the turn. It is the caller's event, relayed —
        not a second cancellation authority.
        """
        return self._cancel_event

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
        return self._catalog.build_tool_defs(
            mode=self._mode,
            read_only=self._read_only,
            dynamic_schemas=dynamic_schemas or None,
            mcp_schemas=mcp_schemas or None,
            web_search=self._web_search,
            plan_review=(not self._read_only) and self._plan_review.required,
            reference_available=self._reference_root.is_available,
        )

    def replayable_tool_defs(self) -> list[dict[str, Any]]:
        """Defs for tools the current catalog withholds but still executes.

        Preflight validates a call against the catalog the request exposed;
        these are the observation-only names that stay callable anyway (see
        :meth:`ToolCatalog.build_replayable_tool_defs`), supplied so a replayed
        historical call is schema-checked rather than rejected as unexposed.
        """
        return self._catalog.build_replayable_tool_defs(
            mode=self._mode,
            read_only=self._read_only,
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
        from aura.paths import safe_is_relative_to
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
            # dispatch (run_terminal_command/run_and_watch, which never
            # reaches this method) consults the same method before it runs.
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


TOOL_HANDLERS["load_skills"] = ToolRegistry._handle_load_skills
TOOL_HANDLERS["read_file"] = ToolRegistry._handle_read_file
TOOL_HANDLERS["read_reference_file"] = ToolRegistry._handle_read_reference_file
TOOL_HANDLERS["read_files"] = ToolRegistry._handle_read_files
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
TOOL_HANDLERS["write_file"] = ToolRegistry._handle_write_file
TOOL_HANDLERS["delete_file"] = ToolRegistry._handle_delete_file
TOOL_HANDLERS["patch_file"] = ToolRegistry._handle_patch_file
TOOL_HANDLERS["edit_godot_scene"] = ToolRegistry._handle_edit_godot_scene
TOOL_HANDLERS["inspect_godot_assets"] = ToolRegistry._handle_inspect_godot_assets
TOOL_HANDLERS["inspect_godot_asset_preview"] = ToolRegistry._handle_inspect_godot_asset_preview
TOOL_HANDLERS["capture_godot_asset_preview"] = ToolRegistry._handle_capture_godot_asset_preview
TOOL_HANDLERS["inspect_godot_editor"] = ToolRegistry._handle_inspect_godot_editor
TOOL_HANDLERS["inspect_godot_api"] = ToolRegistry._handle_inspect_godot_api
TOOL_HANDLERS["edit_godot_editor"] = ToolRegistry._handle_edit_godot_editor
TOOL_HANDLERS["edit_godot_asset_preview"] = ToolRegistry._handle_edit_godot_asset_preview
TOOL_HANDLERS["install_godot_editor_bridge"] = ToolRegistry._handle_install_godot_editor_bridge
TOOL_HANDLERS["update_worker_todo"] = ToolRegistry._handle_update_worker_todo
TOOL_HANDLERS["report_blocker"] = ToolRegistry._handle_report_blocker
TOOL_HANDLERS["report_already_satisfied"] = ToolRegistry._handle_report_already_satisfied
TOOL_HANDLERS["record_implementation_decision"] = (
    ToolRegistry._handle_record_implementation_decision
)

TOOL_HANDLERS["search_project_memory"] = ToolRegistry._handle_search_project_memory
TOOL_HANDLERS["save_to_project_memory"] = ToolRegistry._handle_save_to_project_memory
TOOL_HANDLERS["run_diagnostic_command"] = ToolRegistry._handle_run_diagnostic_command
TOOL_HANDLERS["get_workspace_snapshot"] = ToolRegistry._handle_get_workspace_snapshot
TOOL_HANDLERS["summon_drone"] = ToolRegistry._handle_summon_drone
TOOL_HANDLERS["web_search"] = ToolRegistry._handle_web_search
TOOL_HANDLERS["launch_read_only_drone"] = ToolRegistry._handle_launch_read_only_drone
TOOL_HANDLERS["run_read_only_drone"] = ToolRegistry._handle_run_read_only_drone
TOOL_HANDLERS["check_drone_run"] = ToolRegistry._handle_check_drone_run
TOOL_HANDLERS["register_drone_folder"] = ToolRegistry._handle_register_drone_folder
TOOL_HANDLERS["declare_ui_contract"] = ToolRegistry._handle_declare_ui_contract
TOOL_HANDLERS["code_intel_outline"] = ToolRegistry._handle_code_intel_outline
TOOL_HANDLERS["code_intel_references"] = ToolRegistry._handle_code_intel_references
TOOL_HANDLERS["code_intel_dependents"] = ToolRegistry._handle_code_intel_dependents
TOOL_HANDLERS["code_intel_audit"] = ToolRegistry._handle_code_intel_audit
TOOL_HANDLERS["inspect_code"] = ToolRegistry._handle_inspect_code
TOOL_HANDLERS["review_implementation_plan"] = ToolRegistry._handle_review_implementation_plan
