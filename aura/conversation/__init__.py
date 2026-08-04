"""Conversation history and the tool-loop manager."""

from aura.conversation.history import History
from aura.conversation.worker_outcome import (
    WorkerOutcomeStatus,
    normalize_outcome_status,
)


def __getattr__(name: str):
    if name == "ConversationManager":
        from aura.conversation.manager import ConversationManager

        return ConversationManager
    raise AttributeError(name)


__all__ = [
    "History",
    "ConversationManager",
    "WorkerOutcomeStatus",
    "normalize_outcome_status",
]
