"""Command context and exported handler functions for Companion routing."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from aura.companion.state import CompanionState
from aura.settings import AppSettings


@dataclass
class CommandContext:
    """Read-only context passed to companion command handlers.

    Fields:
        state: CompanionState — the mutable manager state (read/write).
        settings: AppSettings — the current settings snapshot.
        send_fn: Callable that accepts a raw envelope dict.
        bridge: The ConversationBridge, or None.
        project_store: A ProjectStore instance, or None.
        on_conversation_selected: Callback firing
            conversation_selected_by_companion signal (project_root, conversation_path).
    """

    state: CompanionState
    settings: AppSettings
    send_fn: Callable[[dict], None]
    bridge: Any = None
    project_store: Any = None
    on_conversation_selected: Callable | None = None


__all__ = ["CommandContext"]
