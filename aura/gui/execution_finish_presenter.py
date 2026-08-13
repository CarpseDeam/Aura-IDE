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

    The completion receipt is rendered into the workspace only. The assistant's answer is chat-owned and
    reaches the chat on its own.
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
        needs_followup: bool | None,
        status: str | None,
        metadata: dict,
    ) -> ExecutionFinishPresentation:
        outcome = classify_execution_finish(
            ok=ok,
            needs_followup=bool(needs_followup),
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
            if needs_followup is None:
                self._playground.execution_finished(ok, summary, status=status)
            else:
                self._playground.execution_finished(
                    ok,
                    summary,
                    needs_followup=bool(needs_followup),
                    status=status,
                )
        else:
            self._playground.set_execution_running(False)
        # Direct production execution has no SpecCard, and its completion
        # receipt is execution evidence: it stays in the workspace, rendered by
        # playground.execution_finished above. The chat transcript is untouched —
        # it already owns the assistant's answer, which streamed there directly.
        return ExecutionFinishPresentation(outcome=outcome)
