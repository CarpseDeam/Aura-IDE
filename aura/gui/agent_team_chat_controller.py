"""GUI-thread ownership for live automatic-team chat cards."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol

from PySide6.QtCore import QObject, Slot

from aura.agents.retention import AgentRetentionError, AgentRetentionResult
from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.workflow_runner import WorkflowRunResult

if TYPE_CHECKING:
    from aura.bridge.qt_bridge import ConversationBridge
    from aura.gui.cards.agent_team_card import AgentTeamCard
    from aura.gui.chat_view import ChatView

logger = logging.getLogger(__name__)


class _RetentionOwner(Protocol):
    def retain_generated_agent(
        self, team: CompiledAgentTeam, agent_id: str
    ) -> AgentRetentionResult: ...

    def retain_generated_team(
        self, team: CompiledAgentTeam
    ) -> AgentRetentionResult: ...


class AgentTeamChatController(QObject):
    """Projects bridge facts into session-local cards keyed by graph id."""

    def __init__(
        self,
        *,
        bridge: "ConversationBridge",
        chat: "ChatView",
        retention_owner: _RetentionOwner | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._chat = chat
        self._retention_owner = retention_owner
        self._cards: dict[str, AgentTeamCard] = {}
        self._teams: dict[str, CompiledAgentTeam] = {}
        bridge.agentTeamAccepted.connect(self._on_team_accepted)
        bridge.agentTeamStepChanged.connect(self._on_step_changed)
        bridge.agentTeamFinished.connect(self._on_team_finished)
        finished = getattr(bridge, "finished", None)
        if finished is not None:
            finished.connect(self._on_root_finished)
        chat.transientCardsCleared.connect(self.clear)

    @property
    def cards(self) -> tuple["AgentTeamCard", ...]:
        """Current conversation's cards, in acceptance order."""
        return tuple(self._cards.values())

    @Slot(object)
    def _on_team_accepted(self, team: object) -> None:
        if not isinstance(team, CompiledAgentTeam):
            logger.debug("Ignoring invalid automatic-team presentation payload")
            return
        try:
            card = self._chat.add_agent_team_card(team)
        except Exception:  # pragma: no cover - presentation stays non-fatal
            logger.exception("Failed to render automatic Agent team")
            return
        self._cards[team.plan.graph_id] = card
        self._teams[team.plan.graph_id] = team
        card.save_agent_requested.connect(
            lambda agent_id, graph_id=team.plan.graph_id: self._save_agent(
                graph_id, agent_id
            )
        )
        card.keep_team_requested.connect(
            lambda graph_id=team.plan.graph_id: self._keep_team(graph_id)
        )

    @Slot(str, str, str)
    def _on_step_changed(self, graph_id: str, node_id: str, state: str) -> None:
        card = self._cards.get(graph_id)
        if card is None:
            return
        try:
            card.update_occurrence(node_id, state)
        except RuntimeError:
            # The transcript may have reset while queued presentation events
            # were still draining. Execution remains wholly unaffected.
            self._cards.pop(graph_id, None)

    @Slot(object)
    def _on_team_finished(self, result: object) -> None:
        if not isinstance(result, WorkflowRunResult):
            return
        card = self._cards.get(result.graph_id)
        if card is None:
            return
        try:
            card.finish(result)
        except RuntimeError:
            self._cards.pop(result.graph_id, None)
            self._teams.pop(result.graph_id, None)

    @Slot()
    def _on_root_finished(self) -> None:
        if self._retention_owner is None:
            return
        for graph_id, card in tuple(self._cards.items()):
            try:
                card.settle_root_turn()
            except RuntimeError:
                self._cards.pop(graph_id, None)
                self._teams.pop(graph_id, None)

    def _save_agent(self, graph_id: str, agent_id: str) -> None:
        card, team = self._retention_target(graph_id)
        owner = self._retention_owner
        if card is None or team is None or owner is None:
            return
        if agent_id not in card.save_agent_ids:
            return
        try:
            owner.retain_generated_agent(team, agent_id)
        except AgentRetentionError as exc:
            card.show_retention_error(str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive GUI boundary
            logger.exception("Failed to save generated Agent")
            card.show_retention_error(f"Could not save this Agent: {exc}")
            return
        card.mark_agent_saved(agent_id)

    def _keep_team(self, graph_id: str) -> None:
        card, team = self._retention_target(graph_id)
        owner = self._retention_owner
        if card is None or team is None or owner is None or not card.can_keep_team:
            return
        try:
            owner.retain_generated_team(team)
        except AgentRetentionError as exc:
            card.show_retention_error(str(exc))
            return
        except Exception as exc:  # pragma: no cover - defensive GUI boundary
            logger.exception("Failed to keep generated team")
            card.show_retention_error(f"Could not keep this team: {exc}")
            return
        card.mark_team_kept()

    def _retention_target(
        self, graph_id: str
    ) -> tuple["AgentTeamCard | None", CompiledAgentTeam | None]:
        card = self._cards.get(graph_id)
        team = self._teams.get(graph_id)
        if card is None or team is None or card.compiled_team is not team:
            return None, None
        return card, team

    @Slot()
    def clear(self) -> None:
        """Forget cards removed by a transcript reset."""
        self._cards.clear()
        self._teams.clear()


__all__ = ["AgentTeamChatController"]
