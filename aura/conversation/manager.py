"""ConversationManager — runs the tool-loop and forwards events to a callback.

Lives on a worker thread (Qt bridge owns the QThread). The GUI never touches
this directly except through the bridge.

Cancellation: a threading.Event the GUI sets when Stop is clicked. We check
it between rounds and propagate it into client.stream() so the OpenAI iterator
short-circuits mid-chunk.

Roles: a manager instance is either a planner, a worker, or "single" (legacy
single-model chat). The role is implicit in the ToolRegistry's mode plus the
History's system prompt — the manager itself only branches when it sees a
`dispatch_to_worker` tool call: that path is intercepted and routed through
the supplied DispatchCallback rather than the registry.
"""
from __future__ import annotations

import json
import logging
import threading

_log = logging.getLogger(__name__)
from pathlib import Path
from typing import Any, Callable

from aura.client import (
    ApiError,
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    TerminalOutput,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
    Usage,
    WorkerDispatchRequested,
)
from aura.config import ModelId, ThinkingMode
from aura.context_gearbox.models import RuntimeRole
from aura.conversation.completion_guard import (
    assistant_message_text,
    is_repetitive_completion_final,
)
from aura.conversation.context_budget import resolve_model_budget
from aura.conversation.focused_action import (
    FOCUSED_ACTION_THINKING,
    OUTCOME_BLOCKER,
    OUTCOME_PROVIDER_CONTRACT_FAILURE,
    OUTCOME_WRITE,
    REPORT_BLOCKER,
    provider_contract_failure_message,
    should_enter_focused_action,
    tool_call_names,
)
from aura.conversation.workflow_state import WorkflowStatus
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_dispatch_gate import maybe_force_worker_dispatch
from aura.conversation.planner_stream_hygiene import PlannerStreamHygiene
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.stream_event_router import StreamEventRouter
from aura.conversation.task_router import TaskRoute
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.worker_finalization_gate import (
    handle_worker_candidate_finalization,
)
from aura.events import EventBus
from aura.lifecycle import LifecycleHooks
from aura.work_artifact.model import ValidationCommandSpec
from aura.conversation.worker_finish import (
    build_worker_unrecoverable_message,
)
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.research.policy import decide_research_policy

EventCallback = Callable[[Event], None]


def _stream_log_label(hook_name: str) -> str:
    """Return a short log label for the active model-generation hook."""
    if "planner" in hook_name:
        return "planner_stream"
    if "worker" in hook_name:
        return "worker_stream"
    return "production_stream"


def _log_context_round(budget, stats, tool_defs: list[dict[str, Any]] | None) -> None:
    """One line per model round describing where the context budget went.

    Deliberately a single log record, not a telemetry framework — enough to
    answer "why was this turn's evidence cut?" from a normal log file.
    """
    try:
        tool_schema_chars = len(json.dumps(tool_defs, ensure_ascii=False)) if tool_defs else 0
    except (TypeError, ValueError):
        tool_schema_chars = -1

    # Tool schemas ride outside the working-set budget — they are passed to the
    # provider separately and never pruned. Report them in the same unit as the
    # budget, plus the total, so the log answers "how big was the request?"
    # rather than only "how big was the part we budgeted?".
    tool_schema_tokens = max(tool_schema_chars, 0) // 4
    request_tokens = stats.tokens_after + tool_schema_tokens

    _log.info(
        "context_round model=%s provider=%s window=%d reserve=%d derived_budget=%d "
        "policy_cap=%s budget=%d capped_by_policy=%s budget_source=%s "
        "tokens_before=%d tokens_after=%d messages_before=%d messages_after=%d "
        "system_chars=%d tool_schema_chars=%d tool_schema_tokens=%d "
        "request_tokens=%d request_headroom=%d "
        "source_chars_generated=%d source_chars_retained=%d "
        "compacted_results=%d dropped_blocks=%d repaired=%d "
        "reasoning_chars_replayed=%d reasoning_chars_dropped=%d over_budget=%s",
        budget.model_id,
        budget.provider_id,
        budget.context_window_tokens,
        budget.output_reserve_tokens,
        budget.derived_working_set_tokens,
        "none" if budget.policy_cap_tokens is None else budget.policy_cap_tokens,
        budget.working_set_tokens,
        budget.capped_by_policy,
        "fallback" if budget.is_fallback else "catalog",
        stats.tokens_before,
        stats.tokens_after,
        stats.messages_before,
        stats.messages_after,
        stats.system_prompt_chars,
        tool_schema_chars,
        tool_schema_tokens,
        request_tokens,
        budget.context_window_tokens - budget.output_reserve_tokens - request_tokens,
        stats.source_result_chars_generated,
        stats.source_result_chars_retained,
        stats.compacted_results,
        stats.dropped_blocks,
        stats.repaired_messages,
        stats.reasoning_chars_replayed,
        stats.reasoning_chars_dropped,
        stats.over_budget,
    )


class ConversationManager:
    def __init__(
        self,
        history: History,
        tool_registry: ToolRegistry,
        lifecycle: LifecycleHooks | None = None,
        event_bus: EventBus | None = None,
    ) -> None:
        self._history = history
        self._tools = tool_registry
        self._lifecycle = lifecycle
        self._event_bus = event_bus
        self._tool_runner = ToolRunner(
            history=self._history,
            workspace_root=self._tools.workspace_root,
        )
        self._planner_refresh = PlannerRefreshState()
        self._tool_round_runner = ToolRoundRunner(
            history=self._history,
            tools=self._tools,
            tool_runner=self._tool_runner,
            planner_refresh=self._planner_refresh,
            lifecycle=self._lifecycle,
            event_bus=self._event_bus,
        )
    @property
    def history(self) -> History:
        return self._history

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def set_workspace_root(self, root: Path) -> None:
        self._tool_runner.set_workspace_root(root)

    def configure_runtime_context(
        self,
        base_prompt: str,
        workspace_root: Path,
        role: RuntimeRole | str = RuntimeRole.SINGLE,
        *,
        model: str | None = None,
        task_kind: str | None = None,
        content: str | None = None,
        target_files: tuple[str, ...] = (),
    ) -> None:
        """Role-neutral entry point: store the base prompt, root, role, terrain.

        This is the canonical configuration call for the production
        single-agent path.  Mid-turn context refreshes recompose against
        *role* and this turn's terrain, so nothing Planner-specific leaks into
        production execution and the turn's skills are not dropped mid-run.
        """
        self._planner_refresh.configure(
            base_prompt,
            workspace_root,
            role,
            model=model,
            task_kind=task_kind,
            content=content,
            target_files=target_files,
        )

    def configure_for_planner(self, base_prompt: str, workspace_root: Path) -> None:
        """Compatibility alias for the historical Planner path."""
        self.configure_runtime_context(
            base_prompt, workspace_root, RuntimeRole.PLANNER
        )

    def send(        self,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        dispatch_cb: DispatchCallback | None = None,
        workflow_state_cb: Callable[[str, str, str, WorkflowStatus], None] | None = None,
        worker_dispatch_request: WorkerDispatchRequest | None = None,
        dispatch_tool_call_id: str = "",
        loaded_target_files: list[str] | None = None,
        temperature: float = 0.7,
        max_tool_rounds: int | None = None,
        hook_name: str = PRODUCTION_STREAM_HOOK,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
        declared_run_command: str | None = None,
        task_route: TaskRoute | None = None,
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        `dispatch_cb` is required when the registry is in "planner" mode (the
        only mode that exposes the `dispatch_to_worker` tool). If the tool is
        called and `dispatch_cb` is None, the call returns an error result so
        the planner can recover rather than blocking forever.

        `task_route` is the deterministic ``TaskRoute`` the send layer already
        selected for this turn. It is read, never recomputed, and only the
        focused action turn consults it (see
        :mod:`aura.conversation.focused_action`).

        `hook_name` controls which hook to trigger for model generation.
        Normal production coding uses `generate_production_code` (the default).
        The historical Planner/Worker dispatch path uses
        `generate_planner_code` / `generate_worker_code`; those remain as
        unreachable compatibility scaffolding.
        """
        mode = getattr(self._tools, "mode", "single")
        state = _SendState(
            mode=mode,
            research_policy=decide_research_policy(_latest_user_text(self._history)),
            task_route=task_route,
        )
        state.focused_action.selected_thinking = str(thinking)
        if state.mode == "worker":
            state.loaded_target_files = list(loaded_target_files or [])
            if worker_dispatch_request is not None:
                state.dispatched_target_files = list(worker_dispatch_request.files)
                state.worker_artifact_id = str(worker_dispatch_request.artifact_id or "")
                state.worker_artifact_item_id = str(worker_dispatch_request.artifact_item_id or "")

        while True:
            if (
                state.mode in {"planner", "single"}
                and state.task_completion_context
                and state.final_messages_after_completion >= 1
            ):
                return

            state.rounds_used += 1
            if max_tool_rounds is not None and state.rounds_used > max_tool_rounds:
                on_event(ApiError(status_code=None, message=f"Exceeded max tool rounds ({max_tool_rounds})."))
                return

            state.limits.begin_model_round()
            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            full_message: dict[str, Any] | None = None

            # ── Focused action turn ──────────────────────────────────────
            # Discovery is already over by the loop's own deterministic
            # reckoning, so this one request serializes the decision into a
            # single act instead of opening another reasoning stream. The
            # user's thinking selection is untouched — it is simply not the
            # mode for this request, and the next round runs on it again.
            focused = state.focused_action
            focused.active = should_enter_focused_action(
                mode=state.mode,
                route=state.task_route,
                guard=state.pre_edit_guard,
                task_completion_context=state.task_completion_context,
                state=focused,
            )
            if focused.active:
                tool_defs = self._tools.focused_action_tool_defs()
                round_thinking: ThinkingMode = FOCUSED_ACTION_THINKING
                focused.exposed_tools = tuple(
                    str(t.get("function", {}).get("name", "")) for t in tool_defs
                )
                _log.info(
                    "focused_action_start route_lane=%s route_action=%s "
                    "selected_thinking=%s focused_action_thinking=%s "
                    "action_tools=%s",
                    getattr(getattr(state.task_route, "lane", None), "value", ""),
                    getattr(state.task_route, "action", ""),
                    focused.selected_thinking,
                    FOCUSED_ACTION_THINKING,
                    ",".join(focused.exposed_tools),
                )
            else:
                tool_defs = self._tools.tool_defs()
                round_thinking = thinking

            if state.stream_buffer is not None:
                state.stream_buffer.begin_round()
            if state.content_gate is not None:
                state.content_gate.begin_round()

            label = _stream_log_label(hook_name)
            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s focused_action=%s",
                label, model, round_thinking, hook_name, focused.active,
            )
            _first_event = True
            planner_hygiene = (
                PlannerStreamHygiene()
                if state.mode == "planner" and "planner" in hook_name
                else None
            )

            router = StreamEventRouter(
                planner_hygiene=planner_hygiene,
                on_event=on_event,
                mode=state.mode,
                stream_buffer=state.stream_buffer,
                content_gate=state.content_gate,
            )

            # The outbound view is compacted against *this* model's budget;
            # self._history.messages is left exact.
            budget = resolve_model_budget(model)
            api_view = self._history.build_api_payload(budget.working_set_tokens)
            _log_context_round(budget, api_view.stats, tool_defs)

            # ``require_tool_call`` is only passed when it is actually
            # required, so every other request reaches the backend with the
            # exact call signature it has always had.
            stream_kwargs: dict[str, Any] = {}
            if focused.active:
                stream_kwargs["require_tool_call"] = True

            for ev in model_streams.trigger(
                hook_name,
                messages=api_view.messages,
                tools=tool_defs,
                model=model,
                thinking=round_thinking,
                cancel_event=cancel_event,
                temperature=temperature,
                **stream_kwargs,
            ):
                if _first_event:
                    _log.info("%s_first_event model=%s", label, model)
                    _first_event = False

                result = router.process(ev)

                if result.full_message is not None:
                    full_message = result.full_message
                if result.api_error is not None:
                    _log.info("%s_api_error model=%s", label, model)
                    return

            _log.info("%s_done model=%s", label, model)

            # A stream that ended without a Done (cancel, truncated provider
            # response) still owes the user whatever prose it produced.  Rounds
            # that reached Done already resolved their buffer, so this is a
            # no-op for them.
            if state.content_gate is not None:
                state.content_gate.flush(on_event)

            if cancel_event.is_set():
                # If we have some content but no tool calls, we can keep it.
                # If it's empty or has orphaned tool calls, we must strip it.
                if full_message is not None:
                    # DeepSeek/OpenRouter specific: reasoning_content is NOT 'content' for the API.
                    # Standard APIs REQUIRE 'content' (string) or 'tool_calls' (list).
                    content = full_message.get("content")
                    reasoning = full_message.get("reasoning_content")

                    has_any_text = bool(content or reasoning)
                    if has_any_text:
                        full_message.pop("tool_calls", None)
                        # Normalize content to string so API doesn't reject it
                        if full_message.get("content") is None:
                            full_message["content"] = ""
                        self._history.append_assistant(full_message)
                    else:
                        self._cleanup_cancelled(on_event)
                else:
                    self._cleanup_cancelled(on_event)
                return

            if full_message is None:
                # Should not happen in normal stream completion
                return

            tool_calls = full_message.get("tool_calls") or []

            if focused.active:
                # The request is spent either way — nothing below re-enters
                # focused action, and nothing retries it. ``active`` stays set
                # until this round's tool results have been folded in; the top
                # of the next round recomputes it and finds ``spent``.
                focused.spent = True
                selected = tool_call_names(full_message)
                focused.selected_action = selected[0] if selected else ""
                if not tool_calls:
                    focused.contract_violated = True
                    focused.outcome = OUTCOME_PROVIDER_CONTRACT_FAILURE
                    _log.info(
                        "focused_action_outcome outcome=%s selected_action=%s "
                        "selected_thinking=%s focused_action_thinking=%s",
                        focused.outcome,
                        "<none>",
                        focused.selected_thinking,
                        FOCUSED_ACTION_THINKING,
                    )
                    self._history.append_assistant(full_message)
                    content, failure_message = provider_contract_failure_message()
                    self._history.append_assistant(failure_message)
                    on_event(ContentDelta(text=content))
                    on_event(
                        Done(finish_reason="stop", full_message=failure_message)
                    )
                    return
                focused.outcome = (
                    OUTCOME_BLOCKER
                    if REPORT_BLOCKER in selected
                    else OUTCOME_WRITE
                )

            if state.worker_flow is not None:
                state.worker_flow.observe_assistant_message(full_message)
            if (
                not tool_calls
                and state.mode in {"planner", "single"}
                and state.task_completion_context
            ):
                content_text = assistant_message_text(full_message)
                if state.final_messages_after_completion >= 1:
                    if is_repetitive_completion_final(
                        content_text,
                        state.last_completion_final_text,
                    ):
                        return
                    return
                self._history.append_assistant(full_message)
                state.final_messages_after_completion += 1
                state.last_completion_final_text = content_text
                return

            if not tool_calls:
                if state.mode == "planner":
                    dispatch_gate = maybe_force_worker_dispatch(
                        latest_user_text=_latest_user_text(self._history),
                        candidate_message=full_message,
                        planner_tool_calls_seen=state.limits.total_calls,
                        dispatch_calls_seen=state.limits.dispatch_calls,
                        already_steered=state.planner_dispatch_gate_steered,
                    )
                    if dispatch_gate.should_continue:
                        self._history.append_internal_user_text(
                            dispatch_gate.steering_message
                        )
                        state.planner_dispatch_gate_steered = True
                        continue
                if state.mode == "worker":
                    handle_worker_candidate_finalization(
                        state=state,
                        full_message=full_message,
                        history=self._history,
                        on_event=on_event,
                    )
                    return
                self._history.append_assistant(full_message)
                return

            self._history.append_assistant(full_message)
            if state.stream_buffer is not None:
                state.stream_buffer.discard()

            tool_round = self._tool_round_runner.run(
                tool_calls=tool_calls,
                state=state,
                on_event=on_event,
                approval_cb=approval_cb,
                cancel_event=cancel_event,
                dispatch_cb=dispatch_cb,
                workflow_state_cb=workflow_state_cb,
                cleanup_cancelled=self._cleanup_cancelled,
                explicit_validation_commands=explicit_validation_commands,
                declared_run_command=declared_run_command,
            )
            if focused.active:
                focused.active = False
                write_applied = bool(
                    state.pre_edit_guard is not None
                    and state.pre_edit_guard.write_applied
                )
                if focused.outcome == OUTCOME_BLOCKER:
                    # No mutation happened and none will: the attempt ends
                    # here and the turn owes exactly one factual final
                    # response, which the existing completion path produces.
                    focused.blocked = True
                    state.task_completion_context = True
                _log.info(
                    "focused_action_outcome outcome=%s selected_action=%s "
                    "write_applied=%s selected_thinking=%s "
                    "focused_action_thinking=%s",
                    focused.outcome,
                    focused.selected_action or "<none>",
                    write_applied,
                    focused.selected_thinking,
                    FOCUSED_ACTION_THINKING,
                )
            if state.pre_edit_guard is not None and not focused.blocked:
                # A reported blocker has already ended the implementation
                # attempt, so the guard's "make the change now" steering would
                # contradict the turn's own conclusion. Everything else is
                # unchanged.
                #
                # Internal steering only — appended as aura_internal so it never
                # redefines the real user-turn boundary.
                for steering in state.pre_edit_guard.take_internal_messages():
                    self._history.append_internal_user_text(steering)
            if tool_round.action == "return":
                return
            if tool_round.action == "continue":
                continue

    def _finish_worker_unrecoverable(
        self,
        on_event: EventCallback,
        *,
        failure_class: str,
        error: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        content, full_message = build_worker_unrecoverable_message(
            failure_class=failure_class,
            error=error,
            details=details,
        )
        self._history.append_assistant(full_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=full_message))

    def _cleanup_cancelled(self, on_event: EventCallback) -> None:
        """Call this when a turn is cancelled while waiting for model or tool.
        Ensure history doesn't contain an assistant message with pending tool calls
        that haven't been followed by tool result messages.
        """
        if not self._history.messages:
            on_event(ApiError(status_code=None, message="Cancelled."))
            return

        # We look for the MOST RECENT assistant message.
        # If it has tool calls that are missing results, we MUST clean it up.
        for i in range(len(self._history.messages) - 1, -1, -1):
            msg = self._history.messages[i]
            if msg.get("role") == "user":
                # If we hit a user message first, it means the turn was cancelled
                # before the assistant even started responding.
                break

            if msg.get("role") == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    call_ids = {tc["id"] for tc in tool_calls}
                    # Look at messages following this one.
                    for j in range(i + 1, len(self._history.messages)):
                        m = self._history.messages[j]
                        if m.get("role") == "tool":
                            call_ids.discard(m.get("tool_call_id"))

                    if call_ids:
                        # Incomplete! Truncate history back to BEFORE this assistant message.
                        # We find the user message that preceded it.
                        user_idx = -1
                        for k in range(i - 1, -1, -1):
                            if self._history.messages[k].get("role") == "user":
                                user_idx = k
                                break
                        if user_idx != -1:
                            self._history.truncate_after(user_idx + 1)
                        else:
                            self._history.truncate_after(i)
                elif not msg.get("content") and not msg.get("reasoning_content"):
                    # Empty assistant message — strip it.
                    self._history.truncate_after(i)
                break

        on_event(ApiError(status_code=None, message="Cancelled."))


def _latest_user_text(history: History) -> str:
    """The real user request driving this send.

    Aura's own steering messages are ``role="user"`` but carry
    ``aura_internal``; letting one stand in here would decide research policy
    and the planner dispatch gate from Aura's words rather than the user's.
    ``History`` owns that distinction.
    """
    return history.latest_real_user_text() or ""


__all__ = [
    "ConversationManager",
    "ApprovalCallback",
    "ApprovalDecision",
    "ApprovalRequest",
    "EventCallback",
    "Event",
    "ReasoningDelta",
    "ContentDelta",
    "ToolCallStart",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "Usage",
    "Done",
    "ApiError",
    "ToolResult",
    "WorkerDispatchRequested",
    "TerminalOutput",
    "DispatchCallback",
    "WorkerDispatchRequest",
    "WorkerDispatchResult",
]
