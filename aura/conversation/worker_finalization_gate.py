"""Worker candidate finalization after a no-tool-call response.

No harness-enforced post-Worker validation.  The Worker's own
validation (run during its tool loop) is sufficient.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Literal

from aura.client import Event
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState

_log = logging.getLogger(__name__)

EventCallback = Callable[[Event], None]
WorkerFinalizationAction = Literal["continue", "finished", "none"]


def handle_worker_candidate_finalization(
    *,
    state: _SendState,
    full_message: dict,
    history: History,
    on_event: EventCallback,
    **kwargs,
) -> WorkerFinalizationAction:
    """Release candidate final message after Worker's no-tool-call response.

    No harness-enforced post-Worker validation.  The Worker's own
    validation (run during its tool loop) is sufficient.
    """
    if state.stream_buffer is not None:
        state.stream_buffer.flush(on_event)
    history.append_assistant(full_message)
    return "finished"
