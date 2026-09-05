"""Agents responsibility cluster for MainWindow.

This controller is the GUI's only owner of agent storage. Everything the
Agents page shows comes from :class:`aura.agents.store.AgentStore` (the
definitions, project and personal) and
:class:`aura.agents.local_state.AgentLocalState` (this user's private roster
and permission grants for this workspace). The page renders what it is
given and emits what the user asked for; nothing there touches a file.

The two kinds of change stay separate on the way back down, too. A Save
writes a definition. Making an agent available, or changing what it may do,
writes only local state — so a definition that arrives with a project is
inactive and read-only until the person sitting in front of it says
otherwise, and nothing they decide is ever written back into the project.

This controller also owns the workflow session, and therefore the Agents
switch in the main toolbar. The session lives here rather than in the Agents
window because the switch must be answerable — and honest — before that window
has ever been opened. The switch is the sole conversation gate; workflow
selection remains editor state, and freezing every runnable workflow for a
submitted turn happens here too.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox, QWidget

from aura.agents.graph_local_state import WorkflowLocalState, WorkflowLocalStateError
from aura.agents.graph_session import WorkflowSession
from aura.agents.graph_store import AgentGraphStore
from aura.agents.identity import is_valid_agent_id
from aura.agents.local_state import (
    DEFAULT_PERMISSION,
    AgentLocalState,
    AgentLocalStateError,
    AgentPermission,
)
from aura.agents.models import AgentDefinition, AgentScope
from aura.agents.retention import (
    AgentRetentionError,
    AgentRetentionResult,
    AgentTeamRetention,
)
from aura.agents.roster import EMPTY_AGENT_ROSTER, AgentTurnRoster, resolve_agent_turn_roster
from aura.agents.store import AgentStore, AgentStoreError, AgentSummary
from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.turn_context import (
    AgentModelTarget,
    AgentModelTargets,
    AgentTurnContext,
    AgentWorkflowCatalog,
)
from aura.agents.workflow_plan import WorkflowRunPlan, freeze_workflow_plan
from aura.config import has_usable_provider_configuration
from aura.gui.agents_page import (
    AgentDetail,
    AgentDraft,
    AgentRow,
    AgentsPage,
    ModelChoices,
    catalog_choices,
)
from aura.gui.agents_presenter import agent_detail, agent_row
from aura.gui.main_window_agents_graphs import AgentsGraphController

if TYPE_CHECKING:
    from aura.gui.main_window import MainWindow

logger = logging.getLogger(__name__)

#: What a brand-new agent starts as. Every field is editable immediately in
#: the page, so creation never needs a separate dialog.
NEW_AGENT_NAME = "New agent"
NEW_AGENT_DESCRIPTION = "Describe what this agent is for."
NEW_AGENT_INSTRUCTIONS = "Describe how this agent should work."


class MainWindowAgentsController(QObject):
    """Owns the Agents page, its storage, and this user's local decisions."""

    #: ``(enabled, available)`` for the toolbar's Agents switch, emitted
    #: whenever either could have changed.
    workflow_gate_changed = Signal(bool, bool)
    workflows_changed = Signal()
    author_with_aura_requested = Signal()

    def __init__(
        self,
        window: MainWindow,
        parent: QObject | None = None,
        *,
        workspace_root: Path | None = None,
        store_factory: Callable[[Path], AgentStore] | None = None,
        state_factory: Callable[[Path], AgentLocalState] | None = None,
        graph_store_factory: Callable[[Path], AgentGraphStore] | None = None,
        workflow_state_factory: Callable[[Path], WorkflowLocalState] | None = None,
        parent_widget: QWidget | None = None,
        choices: ModelChoices | None = None,
        model_context: Callable[[], tuple[str, str, str]] | None = None,
        workflow_runner: Callable[[], object] | None = None,
    ) -> None:
        super().__init__(parent)
        self._window = window
        self._parent_widget = parent_widget if parent_widget is not None else (
            window if isinstance(window, QWidget) else None
        )
        root = workspace_root if workspace_root is not None else getattr(window, "_workspace_root", None)
        self._workspace_root = Path(root) if root is not None else None
        self._store_factory = store_factory or AgentStore
        self._state_factory = state_factory or AgentLocalState
        self._graph_store_factory = graph_store_factory or self._default_graph_store
        self._workflow_state_factory = workflow_state_factory or WorkflowLocalState
        self._choices = choices
        self._model_context = model_context
        self._workflow_runner = workflow_runner
        self._agents_page: AgentsPage | None = None
        self._graphs: AgentsGraphController | None = None
        self._summaries: dict[str, AgentSummary] = {}
        self._execution_active = False
        # The session is owned here, not by the Agents window, so the toolbar
        # gate is answerable whether or not that window has ever been built.
        self._workflow_session = WorkflowSession(
            self._workspace_root,
            store_factory=self._graph_store_factory,
            state_factory=self._workflow_state_factory,
        )

    # ---- lifecycle ---------------------------------------------------------

    @property
    def agents_page(self) -> AgentsPage | None:
        return self._agents_page

    def is_open(self) -> bool:
        return bool(self._agents_page and self._agents_page.is_open())

    def hide_page(self) -> None:
        if self._agents_page is not None:
            self._agents_page.hide()

    def set_workspace_root(self, root: Path | None) -> None:
        """Rebind to a new workspace, discarding the previous one's roster.

        Project definitions belong to the workspace they live in, and the
        local roster and grants are per workspace as well, so nothing from
        the old one survives the switch.
        """
        self._workspace_root = Path(root) if root is not None else None
        self._summaries = {}
        self._workflow_session.rebind(self._workspace_root)
        if self._graphs is not None:
            self._graphs.set_workspace_root(self._workspace_root)
        if self._agents_page is not None:
            self._agents_page.set_rows(())
            if self._agents_page.isVisible():
                self.refresh()
        self.refresh_workflow_gate()

    def available_agent_ids(self) -> tuple[str, ...]:
        """The ordered ids this user has made available to Aura, here.

        Read straight from local state rather than from whatever the page last
        rendered, so the answer is right whether or not the Agents page has
        ever been opened. An id whose definition no longer loads is left out:
        an agent Aura cannot read is not an agent it can be given.
        """
        state = self._state()
        store = self._store()
        if state is None or store is None:
            return ()
        try:
            available = state.available_ids()
        except Exception:
            logger.debug("agents: could not read the roster", exc_info=True)
            return ()
        if not available:
            return ()
        try:
            valid = {
                summary.agent_id
                for summary in store.list_summaries()
                if summary.valid
            }
        except Exception:
            logger.debug("agents: could not validate the roster", exc_info=True)
            return ()
        return tuple(agent_id for agent_id in available if agent_id in valid)

    def capture_agent_turn_roster(self) -> AgentTurnRoster:
        """Produce the full immutable roster for one submitted turn."""
        state = self._state()
        store = self._store()
        if state is None or store is None:
            return EMPTY_AGENT_ROSTER
        try:
            return resolve_agent_turn_roster(
                state.available_ids(),
                definitions=store,
                permissions=state,
            )
        except Exception:
            logger.debug("agents: could not freeze the submitted roster", exc_info=True)
            return EMPTY_AGENT_ROSTER

    # ---- the toolbar's Agents switch ---------------------------------------

    @property
    def workflow_session(self) -> WorkflowSession:
        """The one session owning workflow discovery and editor selection."""
        return self._workflow_session

    def workflow_gate(self) -> tuple[bool, bool]:
        """``(enabled, available)`` for the switch, read from disk.

        A bound workspace can always generate an Agent, so the switch does not
        depend on an Agent definition or Workflow being present or runnable.
        """
        if not self._workflow_session.bound:
            return False, False
        state = self._workflow_session.state()
        if state is None:
            return False, False
        try:
            enabled = state.is_enabled()
        except Exception:
            logger.debug("agents: could not read the workflow gate", exc_info=True)
            enabled = False
        return enabled, True

    def refresh_workflow_gate(self) -> None:
        """Tell whoever draws the switch what it should be showing."""
        enabled, available = self.workflow_gate()
        self.workflow_gate_changed.emit(enabled, available)

    def set_workflow_enabled(self, enabled: bool) -> None:
        """Record the user's answer to the switch, privately, and re-read it."""
        state = self._workflow_session.state()
        if state is None:
            self.refresh_workflow_gate()
            return
        try:
            state.set_enabled(bool(enabled))
        except WorkflowLocalStateError as exc:
            self._show_error("Agents", str(exc))
        self.refresh_workflow_gate()

    def capture_agent_turn_context(
        self, *, model: str, thinking: str
    ) -> AgentTurnContext:
        """Freeze the complete Agent capability authorized for one root turn."""
        enabled, _available = self.workflow_gate()
        if not enabled:
            return AgentTurnContext.off()

        provider, _model, _thinking = self._current_model_context()
        return AgentTurnContext.enabled(
            roster=self.capture_agent_turn_roster(),
            workflows=self._freeze_saved_workflows(provider, model, thinking),
            model_targets=self._automatic_model_targets(),
            root_provider=provider,
            root_model=model,
            root_thinking=thinking,
        )

    def _freeze_saved_workflows(
        self, provider: str, model: str, thinking: str
    ) -> AgentWorkflowCatalog:
        """Freeze each saved Workflow independently, excluding only failures."""
        graph_store = self._workflow_session.store()
        if graph_store is None:
            return AgentWorkflowCatalog()
        try:
            summaries = graph_store.list_summaries()
        except Exception:
            logger.debug("agents: could not list saved workflows", exc_info=True)
            return AgentWorkflowCatalog()

        plans: list[WorkflowRunPlan] = []
        for summary in summaries:
            if summary.graph is None:
                continue
            try:
                plan, errors = self._freeze_graph(
                    summary.graph, provider, model, thinking
                )
            except Exception:
                logger.debug(
                    "agents: could not freeze workflow %s",
                    summary.graph_id,
                    exc_info=True,
                )
                continue
            if plan is not None:
                plans.append(plan)
            elif errors:
                logger.info(
                    "agents: excluding unrunnable workflow %s: %s",
                    summary.graph_id,
                    "; ".join(errors),
                )
        return AgentWorkflowCatalog.freeze(plans)

    def _automatic_model_targets(self) -> AgentModelTargets:
        """Freeze usable provider/model rows behind stable tool-facing keys."""
        targets: list[AgentModelTarget] = []
        seen: set[str] = set()
        for choice in self.model_choices().targets:
            provider = str(choice.provider or "").strip()
            model = str(choice.model or "").strip()
            if not provider or not model:
                continue
            try:
                usable = has_usable_provider_configuration(provider)
            except Exception:
                usable = False
            if not usable:
                continue
            key = f"{provider}:{model}"
            if key in seen:
                continue
            seen.add(key)
            targets.append(
                AgentModelTarget(
                    key=key,
                    provider=provider,
                    model=model,
                    label=str(choice.label or ""),
                )
            )
        return AgentModelTargets.freeze(targets)

    def freeze_open_workflow(self) -> tuple[WorkflowRunPlan | None, tuple[str, ...]]:
        """Freeze the open workflow for a manual Run, gate or no gate.

        Run is the authoring gesture: it runs what is in front of the person
        who asked for it, so it deliberately never consults the switch.
        """
        provider, model, thinking = self._current_model_context()
        return self._freeze_plan(provider, model, thinking)

    def _freeze_plan(
        self, provider: str, model: str, thinking: str
    ) -> tuple[WorkflowRunPlan | None, tuple[str, ...]]:
        return self._freeze_graph(
            self._workflow_session.graph, provider, model, thinking
        )

    def _freeze_graph(
        self,
        graph,
        provider: str,
        model: str,
        thinking: str,
    ) -> tuple[WorkflowRunPlan | None, tuple[str, ...]]:
        store = self._store()
        state = self._state()
        if store is None or state is None:
            return None, ("no workspace is open",)
        return freeze_workflow_plan(
            graph,
            definitions=store,
            permissions=state,
            agent_scopes=self._agent_scopes(),
            provider=provider,
            model=model,
            thinking=thinking,
        )

    def _current_model_context(self) -> tuple[str, str, str]:
        """Aura's own provider, model, and thinking, as the window has them."""
        if self._model_context is not None:
            try:
                provider, model, thinking = self._model_context()
                return str(provider or ""), str(model or ""), str(thinking or "off")
            except Exception:
                logger.debug("agents: could not read Aura's model context", exc_info=True)
        window = self._window
        settings = getattr(window, "_settings", None)
        provider = str(getattr(settings, "provider", "") or "")
        model = ""
        thinking = "off"
        for attribute, target in (("current_model", "model"), ("current_thinking", "thinking")):
            getter = getattr(window, attribute, None)
            if callable(getter):
                try:
                    value = str(getter() or "")
                except Exception:
                    value = ""
                if target == "model":
                    model = value
                elif value:
                    thinking = value
        return provider, model, thinking

    def model_choices(self) -> ModelChoices:
        """The editor's provider-qualified model targets and Aura baseline."""
        if self._choices is not None:
            return self._choices
        provider, model, _thinking = self._current_model_context()
        return catalog_choices(provider, model)

    def refresh_model_choices(self) -> None:
        """Re-list targets after Aura's target or a provider catalog moved."""
        if self._agents_page is not None and self._choices is None:
            self._agents_page.set_model_choices(self.model_choices())

    def set_execution_active(self, active: bool) -> None:
        """Keep the page browsable during a turn, but freeze every change."""
        self._execution_active = bool(active)
        if self._agents_page is not None:
            self._agents_page.set_mutations_enabled(not self._execution_active)

    # ---- opening -----------------------------------------------------------

    def on_agents_requested(self) -> None:
        self.open_or_toggle_agents_page()

    def open_or_toggle_agents_page(self) -> None:
        page = self._ensure_page()
        if page.isVisible():
            page.hide()
        else:
            self.refresh()
            page.show()
            page.raise_()
            page.activateWindow()
        self.sync_agents_tab_checked()

    def sync_agents_tab_checked(self) -> None:
        rail = getattr(self._window, "_edge_rail", None)
        if rail is None:
            return
        tab = rail.agents_tab
        if tab is not None:
            tab.setChecked(self.is_open())

    def _ensure_page(self) -> AgentsPage:
        if self._agents_page is None:
            page = AgentsPage(self._parent_widget, choices=self.model_choices())
            page.visibility_changed.connect(lambda _visible: self.sync_agents_tab_checked())
            page.current_row_changed.connect(self._on_current_row_changed)
            page.create_requested.connect(self._on_create_requested)
            page.author_with_aura_requested.connect(self.author_with_aura_requested)
            page.save_requested.connect(self._on_save_requested)
            page.delete_requested.connect(self._on_delete_requested)
            page.availability_changed.connect(self._on_availability_changed)
            page.permission_changed.connect(self._on_permission_changed)
            page.set_mutations_enabled(not self._execution_active)
            self._agents_page = page
            self._graphs = AgentsGraphController(
                page,
                session=self._workflow_session,
                agent_summaries=self.agent_summaries,
                mutations_allowed=self._mutations_allowed,
                workflow_runner=self._workflow_runner,
                run_plan=self.freeze_open_workflow,
                parent_widget=self._parent_widget,
                parent=self,
            )
            self._graphs.gate_changed.connect(self.refresh_workflow_gate)
            self._graphs.gate_changed.connect(self.workflows_changed)
        return self._agents_page

    @property
    def graphs(self) -> AgentsGraphController | None:
        """The workflow half of the page, once it has been built."""
        return self._graphs

    # ---- storage access ----------------------------------------------------

    def _store(self) -> AgentStore | None:
        if self._workspace_root is None:
            return None
        try:
            return self._store_factory(self._workspace_root)
        except Exception:
            logger.debug("agents: could not bind AgentStore", exc_info=True)
            return None

    def _state(self) -> AgentLocalState | None:
        if self._workspace_root is None:
            return None
        try:
            return self._state_factory(self._workspace_root)
        except Exception:
            logger.debug("agents: could not bind AgentLocalState", exc_info=True)
            return None

    def agent_summaries(self) -> tuple[AgentSummary, ...]:
        """Every discovered definition, for whoever needs to resolve an id.

        Workflows reference agents but never own them, so the graph
        controller reads the library through this and never binds its own
        :class:`~aura.agents.store.AgentStore`.
        """
        store = self._store()
        if store is None:
            return ()
        try:
            return store.list_summaries()
        except Exception:
            logger.debug("agents: could not read the library", exc_info=True)
            return ()

    def capture_explicit_workflow_context(self, workflow_id: str, *, model: str, thinking: str):
        """One deliberate card Run, with the same independence as manual Run."""
        store = self._workflow_session.store()
        graph = store.get(workflow_id) if store is not None else None
        provider, _, _ = self._current_model_context()
        plan, errors = self._freeze_graph(graph, provider, model, thinking)
        if plan is None:
            raise AgentRetentionError("This Workflow cannot run: " + "; ".join(errors))
        return AgentTurnContext.enabled(
            workflows=(plan,), root_provider=provider, root_model=model,
            root_thinking=thinking, explicit_workflow_id=workflow_id,
        )

    def capture_workflow_authoring(self):
        """Freeze authoring model choices independently of the execution gate."""
        from aura.agents.workflow_authoring import WorkflowAuthoring
        agents, workflows, state = self._store(), self._workflow_session.store(), self._state()
        if agents is None or workflows is None or state is None:
            return None
        return WorkflowAuthoring(
            agents=agents, workflows=workflows, local_state=state,
            edits=self._workflow_session.edits, model_targets=self._automatic_model_targets(),
        )

    def open_workflow(self, workflow_id: str) -> None:
        """Open the exact saved graph for editing, without changing the Agent gate."""
        store = self._workflow_session.store()
        if store is None or store.get(workflow_id) is None:
            raise AgentRetentionError("That saved Workflow is no longer available.")
        page = self._ensure_page()
        self._workflow_session.open(workflow_id)
        self.refresh()
        page.show()
        page.raise_()
        page.activateWindow()
        self.sync_agents_tab_checked()

    def retain_generated_agent(
        self, team: CompiledAgentTeam, agent_id: str
    ) -> AgentRetentionResult:
        """Save one exact generated definition and add it to the direct roster."""
        result = self._retention().save_agent(team, agent_id)
        self.refresh()
        return result

    def retain_generated_team(
        self, team: CompiledAgentTeam
    ) -> AgentRetentionResult:
        """Keep one exact compiled graph as a personal saved Workflow."""
        result = self._retention().keep_team(team)
        self.refresh()
        return result

    def _retention(self) -> AgentTeamRetention:
        agents = self._store()
        workflows = self._workflow_session.store()
        state = self._state()
        if agents is None or workflows is None or state is None:
            raise AgentRetentionError(
                "This Agent run cannot be saved because its workspace is no longer open."
            )
        return AgentTeamRetention(
            agents=agents,
            workflows=workflows,
            local_state=state,
        )

    def _agent_scopes(self) -> dict[str, AgentScope]:
        return {
            summary.agent_id: summary.scope
            for summary in self.agent_summaries()
            if summary.valid
        }

    def _default_graph_store(self, root: Path) -> AgentGraphStore:
        """The production workflow store, taught which agents exist and where.

        Passing the scope index in is what lets the store refuse to write a
        project workflow that points at a personal agent.
        """
        return AgentGraphStore(root, agent_scopes=self._agent_scopes)

    # ---- rendering ---------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the roster and the editor from disk."""
        page = self._agents_page
        if page is None:
            return
        rows = self._build_rows()
        page.set_mutations_enabled(not self._execution_active)
        if self._choices is None:
            page.set_model_choices(self.model_choices())
        page.set_rows(rows)
        if self._graphs is not None:
            self._graphs.refresh()
        else:
            self.refresh_workflow_gate()
        self._on_current_row_changed(page.current_source_key())

    def _build_rows(self) -> tuple[AgentRow, ...]:
        store = self._store()
        state = self._state()
        if store is None or state is None:
            self._summaries = {}
            return ()
        try:
            summaries = store.list_summaries()
        except Exception:
            logger.debug("agents: list_summaries failed", exc_info=True)
            self._summaries = {}
            return ()

        self._summaries = {
            _source_key(summary.scope.value, summary.agent_id): summary
            for summary in summaries
        }
        available = set(state.available_ids())
        return tuple(
            agent_row(
                summary,
                available=summary.agent_id in available,
                permission=self._permission(state, summary.agent_id),
            )
            for summary in summaries
        )

    def _on_current_row_changed(self, source_key: str) -> None:
        page = self._agents_page
        if page is None:
            return
        detail = self._detail_for(source_key) if source_key else None
        page.set_detail(detail)
        if self._graphs is not None:
            self._graphs.on_library_selection(detail.agent_id if detail else "")

    def _detail_for(self, source_key: str) -> AgentDetail | None:
        summary = self._summaries.get(source_key)
        state = self._state()
        if summary is None or state is None:
            return None
        return agent_detail(
            summary,
            available=self._is_available(state, summary.agent_id),
            permission=self._permission(state, summary.agent_id),
        )

    @staticmethod
    def _permission(state: AgentLocalState, agent_id: str) -> AgentPermission:
        """What one agent may do here, defaulting for an unaddressable id."""
        if not is_valid_agent_id(agent_id):
            return DEFAULT_PERMISSION
        return state.permission(agent_id)

    @staticmethod
    def _is_available(state: AgentLocalState, agent_id: str) -> bool:
        return bool(is_valid_agent_id(agent_id) and state.is_available(agent_id))

    # ---- definition changes ------------------------------------------------

    def _on_create_requested(self, scope_key: str) -> None:
        store = self._store()
        if store is None or not self._mutations_allowed():
            return
        try:
            scope = AgentScope(scope_key)
        except ValueError:
            return
        try:
            definition = store.create(
                scope,
                name=NEW_AGENT_NAME,
                description=NEW_AGENT_DESCRIPTION,
                instructions=NEW_AGENT_INSTRUCTIONS,
            )
        except AgentStoreError as exc:
            self._show_error("Agents", str(exc))
            return
        self.refresh()
        if self._agents_page is not None:
            self._agents_page.select_agent(definition.agent_id, definition.scope.value)

    def _on_save_requested(self, draft: object) -> None:
        if not isinstance(draft, AgentDraft) or not self._mutations_allowed():
            return
        store = self._store()
        summary = self._summaries.get(_source_key(draft.scope, draft.agent_id))
        if store is None or summary is None:
            return
        try:
            store.update(
                AgentDefinition(
                    agent_id=summary.agent_id,
                    scope=summary.scope,
                    name=draft.name,
                    description=draft.description,
                    instructions=draft.instructions,
                    provider=draft.provider,
                    model=draft.model,
                    thinking=draft.thinking,
                )
            )
        except AgentStoreError as exc:
            self._show_error("Agents", str(exc))
            return
        self.refresh()

    def _on_delete_requested(self, scope_key: str, agent_id: str) -> None:
        store = self._store()
        state = self._state()
        summary = self._summaries.get(_source_key(scope_key, agent_id))
        if store is None or state is None or summary is None or not self._mutations_allowed():
            return
        if not self._confirm_delete(summary.name):
            return
        try:
            store.delete(summary.scope, agent_id)
        except AgentStoreError as exc:
            self._show_error("Agents", str(exc))
            return
        # A deleted agent keeps no authority: the local decisions about it go
        # with it, so an id that is later reused starts read-only and inactive.
        if not any(row.agent_id == agent_id for row in store.list_summaries()):
            try:
                state.forget(agent_id)
            except AgentLocalStateError as exc:
                # The definition is already gone, so the stale id cannot become
                # available. Surface the persistence failure instead of pretending
                # every part of the operation succeeded.
                self._show_error("Agents", str(exc))
        self.refresh()

    # ---- local decisions ---------------------------------------------------

    def _on_availability_changed(self, agent_id: str, available: bool) -> None:
        state = self._state()
        if state is None or not self._mutations_allowed():
            return
        summaries = [
            summary
            for summary in self._summaries.values()
            if summary.agent_id == agent_id and summary.valid
        ]
        if len(summaries) != 1:
            return
        try:
            state.set_available(agent_id, bool(available))
        except AgentLocalStateError as exc:
            self._show_error("Agents", str(exc))
            self._apply_local_state(agent_id, state)
            return
        self._apply_local_state(agent_id, state)

    def _on_permission_changed(self, agent_id: str, permission_value: str) -> None:
        state = self._state()
        permission = AgentPermission.parse(permission_value)
        if state is None or permission is None or not self._mutations_allowed():
            return
        summaries = [
            summary
            for summary in self._summaries.values()
            if summary.agent_id == agent_id and summary.valid
        ]
        if len(summaries) != 1:
            return
        try:
            state.set_permission(agent_id, permission)
        except AgentLocalStateError as exc:
            self._show_error("Agents", str(exc))
            self._apply_local_state(agent_id, state)
            return
        self._apply_local_state(agent_id, state)

    def _apply_local_state(self, agent_id: str, state: AgentLocalState) -> None:
        """Show one row's new local decision, read back from what was stored.

        Deliberately not a full refresh: both decisions arrive from inside a
        widget's own signal, and rebuilding the list there would destroy the
        widget mid-emit.
        """
        page = self._agents_page
        if page is None:
            return
        page.apply_local_state(
            agent_id,
            available=state.is_available(agent_id),
            permission=state.permission(agent_id),
        )

    def _mutations_allowed(self) -> bool:
        """A second refusal behind the page's own disabled controls.

        The page already shuts every mutation off for the length of a turn
        and says so; this is the guard that makes that true even if a signal
        arrives anyway.
        """
        return not self._execution_active

    # ---- dialogs -----------------------------------------------------------

    def _confirm_delete(self, name: str) -> bool:
        parent = self._parent_widget
        if parent is None:
            return True
        reply = QMessageBox.question(
            parent,
            "Delete agent",
            f"Delete “{name}”? Its definition file is removed from disk.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes

    def _show_error(self, title: str, message: str) -> None:
        parent = self._parent_widget
        if parent is None:
            logger.warning("%s: %s", title, message)
            return
        QMessageBox.warning(parent, title, message)


def _source_key(scope: str, agent_id: str) -> str:
    return f"{scope}:{agent_id}"
