"""Execution finish presentation for chat and playground UI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aura.gui.execution_finish_outcome import ExecutionFinishOutcome, classify_execution_finish

if TYPE_CHECKING:
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


@dataclass(frozen=True)
class ExecutionFinishPresentation:
    outcome: ExecutionFinishOutcome


class ExecutionFinishPresenter:
    """Presents a completed production execution run.

    The workspace status chip always gets the truthful terminal status. A
    plain success (ok) leaves chat untouched — the assistant's answer is
    chat-owned and already reached chat on its own. A genuine non-success
    outcome (validation failure, harness error, rejected approval, ...) is
    durable execution evidence that used to live only in the now-removed
    Execution Log, so it is also surfaced in chat.
    """

    def __init__(self, chat: ChatView, playground: AuraPlayground) -> None:
        self._chat = chat
        self._playground = playground

    def present(
        self,
        *,
        tool_call_id: str,
        ok: bool,
        summary: str,
        status: str | None,
        metadata: dict,
    ) -> ExecutionFinishPresentation:
        outcome = classify_execution_finish(
            ok=ok,
            status=status,
            metadata=metadata,
        )

        self._playground.stop_aura()
        # A direct production run has exactly one visible outcome: the receipt
        # the backend already built from its execution evidence. Rendering it
        # only on success made every truthful non-success terminal status —
        # blocked, validation_failed, harness_error, cancelled — vanish,
        # leaving the run with no visible end at all.
        # Rendering the receipt the backend produced states the real outcome;
        # it never invents one.
        if outcome.should_show_visible_summary:
            self._playground.execution_finished(ok, summary, status=status)
        else:
            self._playground.set_execution_running(False)
        if not outcome.terminal_success:
            self._chat.add_error(outcome.status_label, summary)
        return ExecutionFinishPresentation(outcome=outcome)
