"""GUI-thread ownership for live automatic-team chat cards."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, Slot

from aura.agents.team_compiler import CompiledAgentTeam
from aura.agents.workflow_runner import WorkflowRunResult

if TYPE_CHECKING:
    from aura.bridge.qt_bridge import ConversationBridge
    from aura.gui.cards.agent_team_card import AgentTeamCard
    from aura.gui.chat_view import ChatView

logger = logging.getLogger(__name__)


class AgentTeamChatController(QObject):
    """Projects bridge facts into session-local cards keyed by graph id."""

    def __init__(
        self,
        *,
        bridge: "ConversationBridge",
        chat: "ChatView",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._chat = chat
        self._cards: dict[str, AgentTeamCard] = {}
        bridge.agentTeamAccepted.connect(self._on_team_accepted)
        bridge.agentTeamStepChanged.connect(self._on_step_changed)
        bridge.agentTeamFinished.connect(self._on_team_finished)
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

    @Slot()
    def clear(self) -> None:
        """Forget cards removed by a transcript reset."""
        self._cards.clear()


__all__ = ["AgentTeamChatController"]
