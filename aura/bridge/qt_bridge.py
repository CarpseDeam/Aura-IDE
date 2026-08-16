"""Bridge between the production ConversationManager thread and Qt's GUI thread.

Normal Aura coding is one continuous production run:

    user request
      → selected production provider and model
      → one ConversationManager over the original conversation
      → inspection → live TODO → edits → terminal → diagnosis → repair
      → validation rerun → one factual completion receipt

- send() spawns a QThread that runs ConversationManager.send against the
  `generate_production_code` hook.
- Every event is projected into the workspace through
  ``ProductionExecutionSession`` under one stable run id.
- The approval callback is bridged via QMetaObject.invokeMethod with
  Qt.BlockingQueuedConnection — the conversation thread blocks until the user clicks
  in the modal dialog on the main thread.

There is exactly one backend, one provider, one system prompt, one model, one
thinking choice, and one ``PRODUCTION_STREAM_HOOK``.
"""
from __future__ import annotations

import copy
import logging
import threading
from pathlib import Path

from PySide6.QtCore import (
    QObject,
    QThread,
    Signal,
    Slot,
)

from aura.backends import (
    APIAgentBackend,
)
from aura.bridge.approval_proxy import _ApprovalProxy
from aura.bridge.plan_review_proxy import PlanReviewProxy
from aura.bridge.production_execution import ProductionExecutionSession
from aura.bridge.read_only_routing import emit_read_only_facts
from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
)
from aura.config import (
    ModelId,
    ProviderId,
    ThinkingMode,
)
from aura.context_gearbox.runtime import (
    PRODUCTION_SYSTEM_PROMPT,
    compose_system_prompt,
    context_gearbox_metadata,
    diagnose_custom_prompt,
    format_custom_prompt_diagnostics,
    format_prompt_composition,
)
from aura.conversation import (
    ConversationManager,
    History,
)
from aura.conversation.tools import (
    ToolRegistry,
)
from aura.model_streams import (
    PRODUCTION_STREAM_HOOK,
    model_streams,
)
from aura.windows_mcp import WindowsComputerUseManager

_log = logging.getLogger(__name__)


class _ConversationRunner(QObject):
    """Runs the production conversation loop on its dedicated Qt thread."""

    reasoningDelta = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(int, str, str)  # index, id, name
    toolCallArgs = Signal(int, str)  # index, fragment
    toolCallEnd = Signal(int)
    apiError = Signal(int, str)
    streamDone = Signal(str, dict)
    toolResultEmitted = Signal(str, str, bool, str, dict)
    terminalOutput = Signal(str, str)  # (tool_call_id, text)
    agentProcessStarted = Signal(str, str, str)  # process_id, label, command
    agentProcessOutput = Signal(str, str)  # process_id, text
    agentProcessFinished = Signal(str, object)  # process_id, exit_code
    # The single authoritative accounting path for provider Usage on a
    # Read Only collaborative turn. Normal turns account usage through the
    # production session's relay; a Read Only turn routes the same Usage facts
    # here so conversation telemetry still receives them exactly once without
    # projecting any production worker activity.
    usage = Signal(str, str, int, int, int, int)  # tool_id, model, prompt, comp, hit, miss
    finished = Signal()

    def __init__(
        self,
        manager: ConversationManager,
        approval_proxy: "_ApprovalProxy",
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        temperature: float = 0.7,
        workspace_root: Path | None = None,
        production_session: "ProductionExecutionSession | None" = None,
        read_only_turn: bool = False,
    ) -> None:
        super().__init__()
        self._manager = manager
        self._approval_proxy = approval_proxy
        self._cancel = cancel_event
        self._model = model
        self._thinking = thinking
        self._temperature = temperature
        self._workspace_root = workspace_root
        self._production_session = production_session
        self._read_only_turn = read_only_turn
        self._blocked_reason: str = ""
        self._provider_contract_failure: bool = False
        self._already_satisfied: bool = False

    @Slot()
    def run(self) -> None:
        try:
            self._manager.send(
                on_event=self._on_event,
                approval_cb=self._approval_proxy.request_approval,
                cancel_event=self._cancel,
                model=self._model,
                thinking=self._thinking,
                temperature=self._temperature,
            )
            self._blocked_reason = self._manager.last_turn_blocked_reason
            self._already_satisfied = self._manager.last_turn_already_satisfied
        except Exception as exc:
            from aura.config import redact_secrets
            self.apiError.emit(-1, redact_secrets(f"{type(exc).__name__}: {exc}"))
        finally:
            if self._cancel.is_set():
                self._manager.history.pop_if_empty_assistant_message()
            self.finished.emit()

    def _on_event(self, ev: Event) -> None:
        if self._read_only_turn:
            # Read Only collaborative turn: assistant reasoning, prose, and
            # read/search/research tool calls all belong in the chat
            # experience. Nothing is projected into the workspace, and the
            # right-side worker presentation stays idle. Usage is a
            # conversation fact and still reaches telemetry exactly once via
            # the single accounting signal (see read_only_routing).
            emit_read_only_facts(self, ev, str(self._model))
            return

        session = self._production_session
        if session is not None:
            # Production *execution* projects into the workspace; the
            # assistant's conversation prose stays chat-owned and travels the
            # canonical contentDelta path (see _emit_chat_facts).
            session.handle_event(ev)
            self._emit_chat_facts(ev)
            return

    def _emit_chat_facts(self, ev: Event) -> None:
        """Emit the event set the chat and persistence layers own.

        Assistant prose is chat-owned and must reach ChatView through exactly
        one signal path, so ``ContentDelta`` surfaces here and is *not* also
        projected into the workspace Execution Log.  Reasoning, tool calls, usage
        accounting, and every other execution fact stay workspace-owned: usage
        reaches the GUI accumulator once, through the execution ledger's
        ``executionUsage`` path.
        """
        if isinstance(ev, ContentDelta):
            self.contentDelta.emit(ev.text)
        elif isinstance(ev, Done):
            if ev.full_message:
                self.streamDone.emit(ev.finish_reason or "", ev.full_message)
        elif isinstance(ev, ApiError):
            from aura.config import redact_secrets
            self.apiError.emit(
                ev.status_code if ev.status_code is not None else -1,
                redact_secrets(ev.message)
            )


class ConversationBridge(QObject):
    """Public Qt-facing facade for one running conversation."""

    reasoningDelta = Signal(str)
    contentDelta = Signal(str)
    toolCallStart = Signal(str, str)  # tool_call_id, name
    toolCallArgs = Signal(str, str)
    toolCallEnd = Signal(str)
    apiError = Signal(int, str)
    streamDone = Signal(str, dict)
    toolResult = Signal(str, str, bool, str, dict)
    diffApplied = Signal(str, str, str, str, bool)
    diffDecided = Signal(str, str, str, str, str, bool)
    started = Signal()
    finished = Signal()

    # Workspace projection signals for the active production execution.
    executionStarted = Signal(str)
    executionFinished = Signal(str, bool, str, bool, str)
    executionCancelled = Signal(str)
    executionReasoningDelta = Signal(str, str)
    executionContentDelta = Signal(str, str)
    executionToolCallStart = Signal(str, str, str)
    executionToolCallArgs = Signal(str, str, str)
    executionToolCallEnd = Signal(str, str)
    executionToolResult = Signal(str, str, str, bool, str, dict)
    executionDiffDecided = Signal(str, str, str, str, str, str, bool)
    executionFileEditLifecycle = Signal(str, str, str, str, list, str)
    executionTerminalCommandStarted = Signal(str, str, str, str)
    executionApiError = Signal(str, int, str)
    executionUsage = Signal(str, str, int, int, int, int)
    executionActivityUpdated = Signal(str, list)  # Activity entries (append-only execution heartbeat)
    taskChecklistUpdated = Signal(str, list)  # Full Task Checklist snapshot
    executionTerminalOutput = Signal(str, str, str)  # parent_tool_id, execution_tool_id, text
    executionAgentProcessStarted = Signal(str, str, str, str)
    executionAgentProcessOutput = Signal(str, str, str)
    executionAgentProcessFinished = Signal(str, str, object)

    # Terminal output
    terminalOutput = Signal(str, str)  # tool_call_id, text
    agentProcessStarted = Signal(str, str, str)
    agentProcessOutput = Signal(str, str)
    agentProcessFinished = Signal(str, object)

    def __init__(
        self,
        parent_widget,
        provider: ProviderId = "deepseek",
    ) -> None:
        super().__init__()
        self._provider = provider

        # The one active production backend. Normal coding always runs here.
        self._production_backend = APIAgentBackend(provider=provider)
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        model_streams.register(PRODUCTION_STREAM_HOOK, self._production_backend.stream)

        self._history = History()
        self._registry = ToolRegistry(workspace_root=_dummy_root())
        self._manager = ConversationManager(self._history, self._registry)
        # Owned here because this is where the production ToolRegistry lives —
        # the manager drives that registry's MCP seam and adds no table of its
        # own. Constructing it starts nothing: with the setting off it never
        # touches the network, the disk, or a subprocess.
        self._windows_computer_use = WindowsComputerUseManager(self._registry)
        self._parent_widget = parent_widget
        self._approval_proxy = _ApprovalProxy(parent_widget)
        self._plan_review_proxy = PlanReviewProxy(parent=self)
        self._registry.set_plan_review_proxy(self._plan_review_proxy)
        # Toolbar-controlled: whether the *next* real user turn requires Plan
        # Review. A toggle change mid-turn never mutates the turn already
        # frozen in ToolRegistry.plan_review (see send()).
        self._review_plan_before_changes: bool = False

        # Production execution session — owns the run identity, the single
        # authoritative execution ledger, and workspace projection.
        self._production_session = ProductionExecutionSession(
            approval_proxy=self._approval_proxy,
            parent=self,
        )

        self._cancel: threading.Event = threading.Event()
        self._thread: QThread | None = None
        self._conversation_runner: _ConversationRunner | None = None
        self._index_to_id: dict[int, str] = {}
        self._index_to_name: dict[int, str] = {}
        self._last_proposed_tool_call_id: str | None = None
        self._active_model: str = ""

        self._temperature: float = 0.7
        self._single_system_prompt: str = ""
        self._tier1_context: str = ""
        self._context_gearbox_metadata: dict = {}
        self._custom_prompt_diagnostics = diagnose_custom_prompt("")
        self._pre_execution_sha: str | None = None
        # Skill-selection terrain no longer carries a task kind: Aura does not
        # classify the request. Kept as a None-valued argument so the skill
        # pack simply applies no task-kind filter.
        self._turn_task_kind: str | None = None
        self._turn_content: str = ""
        self._turn_target_files: tuple[str, ...] = ()
        # The frozen Read Only collaborative-turn intent for the active turn,
        # set at the start of send(). A toolbar toggle during an active response
        # never mutates it; it applies to the next turn.
        self._turn_read_only: bool = False
        # Keep the toolbar request separate from the active-turn capability.
        # ConversationBridge owns when the request is copied into ToolRegistry;
        # ToolRegistry remains the authority used by every model/tool round.
        self._requested_read_only: bool = self._registry.read_only
        self._turn_active: bool = False

        # Re-emit production session signals on the same bridge signals so the
        # polished workspace projection binds once and stays role-neutral.
        session = self._production_session
        session.executionStarted.connect(self.executionStarted)
        session.executionFinished.connect(self.executionFinished)
        session.executionCancelled.connect(self.executionCancelled)
        session.executionReasoningDelta.connect(self.executionReasoningDelta)
        session.executionContentDelta.connect(self.executionContentDelta)
        session.executionToolCallStart.connect(self.executionToolCallStart)
        session.executionToolCallArgs.connect(self.executionToolCallArgs)
        session.executionToolCallEnd.connect(self.executionToolCallEnd)
        session.executionToolResult.connect(self.executionToolResult)
        session.executionDiffDecided.connect(self.executionDiffDecided)
        session.executionFileEditLifecycle.connect(self.executionFileEditLifecycle)
        session.executionTerminalCommandStarted.connect(self.executionTerminalCommandStarted)
        session.executionApiError.connect(self.executionApiError)
        session.executionUsage.connect(self.executionUsage)
        session.executionActivityUpdated.connect(self.executionActivityUpdated)
        session.taskChecklistUpdated.connect(self.taskChecklistUpdated)
        session.executionTerminalOutput.connect(self.executionTerminalOutput)
        session.executionAgentProcessStarted.connect(self.executionAgentProcessStarted)
        session.executionAgentProcessOutput.connect(self.executionAgentProcessOutput)
        session.executionAgentProcessFinished.connect(self.executionAgentProcessFinished)

    # ---- config -----------------------------------------------------------

    @property
    def history(self) -> History:
        return self._history

    @property
    def registry(self) -> ToolRegistry:
        return self._registry

    @property
    def plan_review_proxy(self) -> PlanReviewProxy:
        """The GUI-thread synchronization proxy for the active Plan Review."""
        return self._plan_review_proxy

    @property
    def production_session(self) -> ProductionExecutionSession:
        """The active production execution owner (run identity + ledger)."""
        return self._production_session

    @property
    def production_run_id(self) -> str:
        return self._production_session.run_id

    def execution_result_metadata(self, run_id: str) -> dict:
        """Role-neutral result metadata accessor for the active execution."""
        return self._production_session.result_metadata(run_id)

    def context_gearbox_metadata(self) -> dict:
        return copy.deepcopy(self._context_gearbox_metadata)

    def set_workspace_root(self, root) -> None:
        self._cancel.set()
        if root is None:
            self._manager.reset_conversation_runtime()
            self._tier1_context = ""
            self._context_gearbox_metadata = {}
            return
        self._manager.set_workspace_root(root)
        self._registry.set_workspace_root(root)
        self.refresh_tier1_context()

    def set_read_only(self, value: bool) -> None:
        """Set the toolbar mode for the next real user turn.

        During a turn this only records the requested mode. The registry must
        keep the frozen turn's capability policy until the bridge finishes.
        """
        self._requested_read_only = bool(value)
        if not self._turn_active:
            self._registry.set_read_only(self._requested_read_only)

    @property
    def requested_read_only(self) -> bool:
        """The toolbar mode selected for the next real user turn."""
        return self._requested_read_only

    @property
    def active_turn_read_only(self) -> bool:
        """Whether the currently active bridge turn is frozen Read Only."""
        return self._turn_active and self._turn_read_only

    # ---- turn-scoped external reference ------------------------------------

    def authorize_reference_root(self, candidate: Path | None) -> tuple[bool, str]:
        """Authorize one user-derived reference candidate for the next turn."""
        return self._registry.begin_reference_turn(candidate)

    def clear_reference_authorization(self) -> None:
        """End the active turn's external read-only capability."""
        self._registry.clear_reference_authorization()

    def set_system_prompt(self, prompt: str) -> None:
        """Store the custom production system prompt and reapply composition."""
        self._single_system_prompt = prompt or ""
        composed = self._compose_prompt(self._single_system_prompt)
        self._history.set_system(composed.system_prompt)

    def refresh_tier1_context(self, force_repo_map: bool = False) -> None:
        """Refresh workspace context and reapply the active system prompt."""
        composed = self._compose_prompt(
            self._single_system_prompt,
            force_repo_map=force_repo_map,
        )
        self._history.set_system(composed.system_prompt)

    def refresh_production_prompt(self) -> None:
        """Recompose Aura's one production prompt."""
        composed = self._compose_prompt(self._single_system_prompt)
        self._history.set_system(composed.system_prompt)

    def set_temperature(self, temperature: float) -> None:
        self._temperature = temperature

    @property
    def windows_computer_use(self) -> WindowsComputerUseManager:
        """The Windows Computer Use lifecycle, for the settings page."""
        return self._windows_computer_use

    def apply_windows_computer_use(self, settings) -> None:
        """Bring the Windows MCP connection in line with *settings*.

        Returns immediately; connecting happens on the manager's execution.
        """
        self._windows_computer_use.apply_settings(settings)

    def shutdown_windows_computer_use(self) -> None:
        """Disconnect the Windows MCP server and close its process."""
        self._windows_computer_use.shutdown()

    def shutdown(self) -> None:
        """Close the conversation shell and all bridge-owned execution state."""
        self._cancel.set()
        self._approval_proxy.cancel_active_dialog()
        self._plan_review_proxy.cancel_active()
        self._manager.close()
        self._production_session.clear()

    def set_auto_approve(self, enabled: bool) -> None:
        self._approval_proxy.set_approve_all_session(enabled)

    def set_review_plan_before_changes(self, enabled: bool) -> None:
        """Set whether the *next* real user turn requires Plan Review.

        Takes effect at the start of the next ``send()`` — never mutates a
        turn already in flight.
        """
        self._review_plan_before_changes = bool(enabled)

    def _compose_prompt(
        self,
        custom_prompt: str,
        *,
        force_repo_map: bool = False,
    ):
        """The one place a production system prompt is built and cached."""
        composed = compose_system_prompt(
            custom_prompt,
            self._registry.workspace_root,
            force=force_repo_map,
            model=self._active_model or None,
            task_kind=self._turn_task_kind,
            target_files=self._turn_target_files,
            content=self._turn_content or None,
            active_capabilities=self._registry.active_capabilities(),
            read_only=self._turn_read_only,
        )
        self._context_gearbox_metadata = context_gearbox_metadata(
            composed.ledger, workspace_root=self._registry.workspace_root,
        )
        self._custom_prompt_diagnostics = diagnose_custom_prompt(custom_prompt)
        _log.info(
            "prompt_composed %s",
            format_custom_prompt_diagnostics(
                self._custom_prompt_diagnostics,
                effective_prompt_chars=len(composed.system_prompt),
            ),
        )
        _log.info("%s", format_prompt_composition(composed))
        self._tier1_context = composed.context_text
        return composed

    def set_production_provider(self, provider: ProviderId) -> None:
        """Point the one production backend at *provider*.

        This is the canonical provider entry point for normal coding.
        """
        self._provider = provider
        self._production_backend = APIAgentBackend(provider=provider)
        model_streams.unregister(PRODUCTION_STREAM_HOOK)
        model_streams.register(PRODUCTION_STREAM_HOOK, self._production_backend.stream)

    def set_provider(self, provider: ProviderId) -> None:
        """Update the production provider."""
        self.set_production_provider(provider)

    def check_backend_auth(self, backend_name: str) -> bool:
        """Check if the named backend is authenticated.

        Args:
            backend_name: 'default_api'

        Returns:
            True if the backend is authenticated, False otherwise.
        """
        return True  # 'default_api' is always authenticated


    def reset_history(self) -> None:
        self._cancel.set()
        self._manager.reset_conversation_runtime()
        self._history.messages.clear()
        self._index_to_id.clear()
        self._index_to_name.clear()
        self._production_session.clear()
        # We do NOT reset _approve_all_session here, as it is managed by the
        # persistent toolbar toggle.

    def is_running(self) -> bool:
        thread = self._thread
        if thread is None:
            return False
        try:
            return thread.isRunning()
        except RuntimeError:
            self._thread = None
            self._conversation_runner = None
            return False

    def get_pre_execution_snapshot(self) -> str | None:
        return self._pre_execution_sha

    def clear_pre_execution_snapshot(self) -> None:
        self._pre_execution_sha = None

    # ---- send / cancel ----------------------------------------------------

    def send(
        self,
        model: ModelId,
        thinking: ThinkingMode,
    ) -> None:
        """Run one production turn over the existing conversation.

        The manager already owns the persisted ``History``, so the model
        receives the actual conversation and the user's latest original
        request — never a SpecCard or a generated implementation capsule.
        """
        if self.is_running():
            return
        # Freeze the requested toolbar mode before composing the prompt or
        # starting the worker. The registry remains at this value for the
        # entire model/tool loop, including every later round.
        self._turn_read_only = self._requested_read_only
        self._registry.set_read_only(self._turn_read_only)
        self._turn_active = True
        # Freeze Plan Review's required/approved state for this turn now, so
        # a toolbar toggle flipped while the turn is running cannot mutate
        # the tool catalog or gate it already exposed.
        self._registry.plan_review.begin_turn(required=self._review_plan_before_changes)
        # The active model is terrain for skill selection, so it must be known
        # before the turn's system prompt is composed.
        self._active_model = str(model)
        self._prepare_turn_context()
        # Capture pre-run snapshot for reliable /undo.
        if self._registry.workspace_root is not None:
            from aura.git_ops import snapshot
            self._pre_execution_sha = snapshot(self._registry.workspace_root)
        else:
            self._pre_execution_sha = None
        self._cancel = threading.Event()
        self._index_to_id.clear()
        self._index_to_name.clear()
        if self._registry.workspace_root is not None:
            base_prompt = (
                self._single_system_prompt
                if self._single_system_prompt
                else PRODUCTION_SYSTEM_PROMPT
            )
            self._manager.configure_runtime_context(
                base_prompt=base_prompt,
                workspace_root=self._registry.workspace_root,
                model=self._active_model or None,
                task_kind=self._turn_task_kind,
                content=self._turn_content or None,
                target_files=self._turn_target_files,
            )

        # A Read Only collaborative turn stays conversation-first: no
        # production workspace session is begun, so the right-side worker
        # presentation (start, activity, live TODO, completion receipt) stays
        # visually idle. Normal turns begin one stable execution identity.
        if not self._turn_read_only:
            self._production_session.begin(model=str(model))

        self._thread = QThread()
        self._conversation_runner = _ConversationRunner(
            manager=self._manager,
            approval_proxy=self._approval_proxy,
            cancel_event=self._cancel,
            model=model,
            thinking=thinking,
            temperature=self._temperature,
            workspace_root=self._registry.workspace_root,
            production_session=self._production_session,
            read_only_turn=self._turn_read_only,
        )
        self._conversation_runner.moveToThread(self._thread)

        self._conversation_runner.reasoningDelta.connect(self.reasoningDelta)
        self._conversation_runner.contentDelta.connect(self.contentDelta)
        self._conversation_runner.toolCallStart.connect(self._on_tool_call_start)
        self._conversation_runner.toolCallArgs.connect(self._on_tool_call_args)
        self._conversation_runner.toolCallEnd.connect(self._on_tool_call_end)
        self._conversation_runner.apiError.connect(self.apiError)
        self._conversation_runner.streamDone.connect(self.streamDone)
        self._conversation_runner.toolResultEmitted.connect(self._on_tool_result)
        self._conversation_runner.terminalOutput.connect(self.terminalOutput)
        self._conversation_runner.agentProcessStarted.connect(self.agentProcessStarted)
        self._conversation_runner.agentProcessOutput.connect(self.agentProcessOutput)
        self._conversation_runner.agentProcessFinished.connect(self.agentProcessFinished)
        # The single authoritative usage accounting path. Normal turns reach
        # it through the production session's relay; a Read Only turn routes
        # the same Usage facts here so conversation telemetry counts them
        # exactly once without projecting production worker activity.
        self._conversation_runner.usage.connect(self.executionUsage)
        self._conversation_runner.finished.connect(self._on_finished)

        self._thread.started.connect(self._conversation_runner.run)
        self.started.emit()
        self._thread.start()



    def request_cancel(self) -> None:
        self._cancel.set()
        self._production_session.note_cancelled()
        self._approval_proxy.cancel_active_dialog()
        self._plan_review_proxy.cancel_active()

    # ---- private slots ----------------------------------------------------

    @Slot(int, str, str)
    def _on_tool_call_start(self, index: int, tool_id: str, name: str) -> None:
        self._index_to_id[index] = tool_id
        self._index_to_name[index] = name
        self._last_proposed_tool_call_id = tool_id
        self.toolCallStart.emit(tool_id, name)

    @Slot(int, str)
    def _on_tool_call_args(self, index: int, fragment: str) -> None:
        tool_id = self._index_to_id.get(index, "")
        if tool_id:
            self.toolCallArgs.emit(tool_id, fragment)

    @Slot(int)
    def _on_tool_call_end(self, index: int) -> None:
        tool_id = self._index_to_id.get(index, "")
        if tool_id:
            self.toolCallEnd.emit(tool_id)

    @Slot(str, str, bool, str, dict)
    def _on_tool_result(
        self, tool_id: str, name: str, ok: bool, result: str, extras: dict
    ) -> None:
        approval = extras.get("approval")
        if approval:
            ev = self._approval_proxy.consume_last_event()
            if ev is not None:
                # One emit per changed file — truthful for a multi-file
                # patch_file transaction; identical to before for the
                # ordinary single-file case.
                changes = ev.get("changes") or [ev]
                for change in changes:
                    self.diffDecided.emit(
                        tool_id,
                        str(approval),
                        str(change["rel_path"]),
                        str(change["old_content"]),
                        str(change["new_content"]),
                        bool(change["is_new_file"]),
                    )
        self.toolResult.emit(tool_id, name, ok, result, extras)

    @Slot()
    def _on_finished(self) -> None:
        thread = self._thread
        runner = self._conversation_runner
        self._thread = None
        self._conversation_runner = None

        # Exactly one completion receipt per production turn, built from the
        # run's structured execution evidence, then back to idle. A successful
        # ``report_blocker`` names the reason so the receipt reports the turn as
        # blocked, never as completed; a provider-contract failure is reported
        # as its own terminal status rather than as a completed turn.
        # Structured ``report_already_satisfied`` evidence and the turn's
        # production-action route are carried so the completion contract can
        # report truthfully.
        blocked_reason = runner._blocked_reason if runner is not None else ""
        provider_contract_failure = (
            runner._provider_contract_failure if runner is not None else False
        )
        already_satisfied = runner._already_satisfied if runner is not None else False
        # A Read Only collaborative turn never began a production session, so
        # there is no execution receipt to build — the turn is presented
        # conversationally and returns straight to idle.
        if not self._turn_read_only:
            try:
                self._production_session.finish(
                    blocked_reason=blocked_reason,
                    provider_contract_failure=provider_contract_failure,
                    already_satisfied=already_satisfied,
                )
            except Exception:
                _log.exception("Failed to build production completion receipt")

        # Surface the turn's skill activation ledger on the same metadata the
        # Context Gearbox exposes, so activations that happened *during* the
        # turn join the composition-time candidate/guard/skipped records.
        try:
            activations = self._manager.skill_activation_log()
            if activations:
                metadata = dict(self._context_gearbox_metadata or {})
                metadata["skill_activations"] = list(activations)
                self._context_gearbox_metadata = metadata
        except Exception:
            _log.exception("Failed to surface skill activation ledger")

        try:
            if runner is not None:
                runner.deleteLater()
            if thread is not None:
                thread.quit()
                thread.wait(2000)
                thread.deleteLater()
        except Exception:
            _log.exception("Failed to clean up production conversation thread")
        finally:
            # Reference authorization is a production-turn capability, never
            # a session setting. Clear the root and its dedicated index before
            # the bridge announces that the turn is finished. Apply the latest
            # toolbar request only after the active turn has released the
            # registry, so it is ready for the next send without changing any
            # remaining round of this one.
            self.clear_reference_authorization()
            self._registry.set_read_only(self._requested_read_only)
            self._turn_active = False
            self.finished.emit()

    def _prepare_turn_context(self) -> None:
        """Recompose the system prompt against this turn's live terrain.

        Runs on every turn: skill selection uses the current user message,
        task kind, active model, and any known target files, never a cached
        workspace-startup composition.
        """
        self._turn_content = _latest_user_text(self._history)
        if self._registry.workspace_root is None:
            return
        composed = self._compose_prompt(self._single_system_prompt)
        self._history.set_system(composed.system_prompt)
        _log.info(
            "context_gearbox_turn_summary %s",
            self._context_gearbox_metadata.get("summary", {}).get("display", ""),
        )

    def set_turn_target_files(self, target_files: list[str] | tuple[str, ...]) -> None:
        """Declare the files this turn is known to target.

        The send layer calls this before every send with the paths the user
        actually named — and with an empty tuple when they named none, so the
        previous turn's scope does not carry over.
        """
        self._turn_target_files = tuple(str(path) for path in target_files if str(path).strip())


def _dummy_root():
    return Path.home()


def _latest_user_text(history: History) -> str:
    """This turn's content terrain: the real user request.

    Aura's own ``aura_internal`` steering is ``role="user"`` too, and letting a
    nudge stand in as terrain would drive skill selection off Aura's words
    rather than the user's. ``History`` owns that distinction.
    """
    return history.latest_real_user_text() or ""
