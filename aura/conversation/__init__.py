"""Conversation history and the tool-loop manager."""

from aura.conversation.execution_outcome import (
    ExecutionOutcomeStatus,
    normalize_outcome_status,
)
from aura.conversation.history import History


def __getattr__(name: str):
    if name == "ConversationManager":
        from aura.conversation.manager import ConversationManager

        return ConversationManager
    raise AttributeError(name)


__all__ = [
    "History",
    "ConversationManager",
    "ExecutionOutcomeStatus",
    "normalize_outcome_status",
]
