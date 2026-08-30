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
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QMessageBox, QWidget

from aura.agents.identity import is_valid_agent_id
from aura.agents.local_state import (
    DEFAULT_PERMISSION,
    AgentLocalState,
    AgentLocalStateError,
    AgentPermission,
)
from aura.agents.models import AgentDefinition, AgentScope, AgentThinking, ModelTarget
from aura.agents.roster import EMPTY_AGENT_ROSTER, AgentTurnRoster, resolve_agent_turn_roster
from aura.agents.store import AgentStore, AgentStoreError, AgentSummary
from aura.gui.agents_page import (
    AgentDetail,
    AgentDraft,
    AgentRow,
    AgentsPage,
    ProviderChoices,
)

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

    def __init__(
        self,
        window: MainWindow,
        parent: QObject | None = None,
        *,
        workspace_root: Path | None = None,
        store_factory: Callable[[Path], AgentStore] | None = None,
        state_factory: Callable[[Path], AgentLocalState] | None = None,
        parent_widget: QWidget | None = None,
        choices: ProviderChoices | None = None,
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
        self._choices = choices
        self._agents_page: AgentsPage | None = None
        self._summaries: dict[str, AgentSummary] = {}
        self._execution_active = False

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
        if self._agents_page is not None:
            self._agents_page.set_rows(())
            if self._agents_page.isVisible():
                self.refresh()

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
            page = AgentsPage(self._parent_widget, choices=self._choices)
            page.visibility_changed.connect(lambda _visible: self.sync_agents_tab_checked())
            page.current_row_changed.connect(self._on_current_row_changed)
            page.create_requested.connect(self._on_create_requested)
            page.save_requested.connect(self._on_save_requested)
            page.delete_requested.connect(self._on_delete_requested)
            page.availability_changed.connect(self._on_availability_changed)
            page.permission_changed.connect(self._on_permission_changed)
            page.set_mutations_enabled(not self._execution_active)
            self._agents_page = page
        return self._agents_page

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

    # ---- rendering ---------------------------------------------------------

    def refresh(self) -> None:
        """Rebuild the roster and the editor from disk."""
        page = self._agents_page
        if page is None:
            return
        rows = self._build_rows()
        page.set_mutations_enabled(not self._execution_active)
        page.set_rows(rows)
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
        rows: list[AgentRow] = []
        for summary in summaries:
            definition = summary.definition
            addressable = is_valid_agent_id(summary.agent_id)
            rows.append(
                AgentRow(
                    agent_id=summary.agent_id,
                    scope=summary.scope.value,
                    name=summary.name,
                    description=summary.description,
                    target_label=definition.target_label if definition else "",
                    thinking_label=definition.thinking.label if definition else "",
                    # A definition that did not load is never offered to Aura,
                    # whatever the local roster still remembers about it.
                    available=(
                        summary.valid and addressable and summary.agent_id in available
                    ),
                    permission=(
                        state.permission(summary.agent_id)
                        if addressable
                        else DEFAULT_PERMISSION
                    ),
                    valid=summary.valid,
                    errors=summary.errors,
                )
            )
        return tuple(rows)

    def _on_current_row_changed(self, source_key: str) -> None:
        page = self._agents_page
        if page is None:
            return
        page.set_detail(self._detail_for(source_key) if source_key else None)

    def _detail_for(self, source_key: str) -> AgentDetail | None:
        summary = self._summaries.get(source_key)
        state = self._state()
        if summary is None or state is None:
            return None
        definition = summary.definition
        agent_id = summary.agent_id
        addressable = is_valid_agent_id(agent_id)
        return AgentDetail(
            agent_id=summary.agent_id,
            scope=summary.scope.value,
            name=summary.name,
            description=summary.description,
            instructions=definition.instructions if definition else "",
            provider=definition.target.provider if definition else "",
            model=definition.target.model if definition else "",
            thinking=definition.thinking if definition else AgentThinking.INHERIT,
            permission=state.permission(agent_id) if addressable else DEFAULT_PERMISSION,
            available=state.is_available(agent_id) if addressable else False,
            valid=summary.valid,
            errors=summary.errors,
        )

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
                    target=ModelTarget.explicit(draft.provider, draft.model)
                    if draft.provider or draft.model
                    else ModelTarget.inherited(),
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
