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
    """Presents a completed Worker run without owning dispatch sequencing."""

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
                is_internal=outcome.is_internal,
            )
            self._active_mismatch_card_id = tool_call_id

        self._playground.stop_aura()
        if outcome.should_show_visible_summary:
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
        self._playground.finish_todo_list(
            tool_call_id,
            ok=ok,
            needs_followup=bool(needs_followup),
        )
        if outcome.is_mismatch:
            self._chat.begin_planner_resolution_aura()

        if spec_card and outcome.should_show_visible_summary:
            spec_card.worker_finished(
                ok,
                summary,
                status=status,
                is_internal=outcome.is_internal,
            )
        goal = self._worker_summary_goal(tool_call_id, spec_card, active_workflow)
        if outcome.should_show_visible_summary:
            self._chat.add_worker_summary(
                tool_call_id,
                goal,
                ok,
                summary,
                needs_followup=bool(needs_followup),
                status=status,
                is_internal=outcome.is_internal,
            )
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
