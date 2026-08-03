"""Worker finish presentation for chat and playground UI."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aura.gui.worker_finish_outcome import WorkerFinishOutcome, classify_worker_finish

if TYPE_CHECKING:
    from aura.conversation.workflow_state import WorkflowState
    from aura.gui.chat_view import ChatView
    from aura.gui.playground import AuraPlayground


@dataclass(frozen=True)
class WorkerFinishPresentation:
    outcome: WorkerFinishOutcome


class WorkerFinishPresenter:
    """Presents a completed execution run without owning dispatch sequencing.

    Tolerates direct production execution: there is no SpecCard and no Planner
    dispatch, so the completion receipt is rendered into the workspace only.
    The assistant's answer is chat-owned and reaches the chat on its own.
    """

    def __init__(self, chat: ChatView, playground: AuraPlayground) -> None:
        self._chat = chat
        self._playground = playground
        self._active_mismatch_card_id: str | None = None

    def resolve_active_mismatch(self) -> bool:
        if self._active_mismatch_card_id is None:
            return False
        self._chat.mark_mismatch_resolved(self._active_mismatch_card_id)
        self._active_mismatch_card_id = None
        return True

    def present(
        self,
        *,
        tool_call_id: str,
        ok: bool,
        summary: str,
        needs_followup: bool | None,
        status: str | None,
        metadata: dict,
        active_workflow: WorkflowState | None,
        spec_card,
    ) -> WorkerFinishPresentation:
        outcome = classify_worker_finish(
            ok=ok,
            needs_followup=bool(needs_followup),
            status=status,
            metadata=metadata,
        )

        if outcome.is_mismatch:
            kind, question = outcome.mismatch_display
            self._chat.add_mismatch_resolution_card(
                tool_call_id,
                kind,
                question,
            )
            self._active_mismatch_card_id = tool_call_id

        self._playground.stop_aura()
        # A direct production run (no SpecCard, no Planner dispatch) has exactly
        # one visible outcome: the receipt the backend already built from its
        # execution evidence. Rendering it only on success made every truthful
        # non-success terminal status — blocked, validation_failed,
        # harness_error, no_authoritative_change, cancelled — vanish,
        # leaving the run with no visible end at all. Rendering the receipt the
        # backend produced states the real outcome; it never invents one.
        # Dispatch flows keep their existing contract: a spec card owns their
        # non-success presentation, and a mismatch is answered by its own card.
        render_receipt = outcome.terminal_success or (
            spec_card is None and outcome.should_show_visible_summary
        )
        if render_receipt:
            if needs_followup is None:
                self._playground.worker_finished(ok, summary, status=status)
            else:
                self._playground.worker_finished(
                    ok,
                    summary,
                    needs_followup=bool(needs_followup),
                    status=status,
                )
        else:
            self._playground.set_worker_running(False)
        if outcome.is_mismatch:
            self._chat.begin_planner_resolution_aura()

        if spec_card and outcome.should_show_visible_summary:
            spec_card.worker_finished(
                ok,
                summary,
                status=status,
            )
        # Direct production execution has no SpecCard, and its completion
        # receipt is execution evidence: it stays in the workspace, rendered by
        # playground.worker_finished above. The chat transcript is untouched —
        # it already owns the assistant's answer, which streamed there directly.
        return WorkerFinishPresentation(outcome=outcome)

    @staticmethod
    def _worker_summary_goal(
        tool_call_id: str,
        spec_card,
        active_workflow: WorkflowState | None,
    ) -> str:
        if spec_card is not None and hasattr(spec_card, "current_spec"):
            try:
                goal, _files, _spec, _acceptance, _summary = spec_card.current_spec()
                if goal:
                    return str(goal)
            except Exception:
                logging.exception("Failed to read worker spec card goal")
        if active_workflow is not None and active_workflow.tool_call_id == tool_call_id:
            return active_workflow.task_title or "Worker task"
        return "Worker task"
