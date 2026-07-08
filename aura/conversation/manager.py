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
from aura.conversation.completion_guard import (
    assistant_message_text,
    is_repetitive_completion_final,
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
from aura.model_streams import model_streams
from aura.research.policy import decide_research_policy

EventCallback = Callable[[Event], None]

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
        self._preexisting_failures: list[dict[str, str | list[str]]] = []

    @property
    def history(self) -> History:
        return self._history

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def set_workspace_root(self, root: Path) -> None:
        self._tool_runner.set_workspace_root(root)

    def configure_for_planner(self, base_prompt: str, workspace_root: Path) -> None:
        """Store the base system prompt template and workspace root for mid-turn refresh."""
        self._planner_refresh.configure(base_prompt, workspace_root)

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
        hook_name: str = 'generate_planner_code',
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
        declared_run_command: str | None = None,
        baseline_validation_fingerprints: dict[str, list[str]] | None = None,
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        `dispatch_cb` is required when the registry is in "planner" mode (the
        only mode that exposes the `dispatch_to_worker` tool). If the tool is
        called and `dispatch_cb` is None, the call returns an error result so
        the planner can recover rather than blocking forever.

        `hook_name` controls which hook to trigger for model generation.
        The planner uses `generate_planner_code`; workers use `generate_worker_code`.
        """
        mode = getattr(self._tools, "mode", "single")
        state = _SendState(
            mode=mode,
            research_policy=decide_research_policy(_latest_user_text(self._history)),
        )
        if state.mode == "worker":
            state.loaded_target_files = list(loaded_target_files or [])
            if worker_dispatch_request is not None:
                state.dispatched_target_files = list(worker_dispatch_request.files)
                state.worker_artifact_id = str(worker_dispatch_request.artifact_id or "")
                state.worker_artifact_item_id = str(worker_dispatch_request.artifact_item_id or "")
            if baseline_validation_fingerprints is not None:
                state.baseline_validation_fingerprints = dict(baseline_validation_fingerprints)

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
            tool_defs = [] if state.worker_needs_final_report else self._tools.tool_defs()
            if state.stream_buffer is not None:
                state.stream_buffer.begin_round()

            label = "planner_stream" if "planner" in hook_name else "worker_stream"
            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s",
                label, model, thinking, hook_name,
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
            )

            for ev in model_streams.trigger(
                hook_name,
                messages=self._history.for_api(),
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
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
                    finalization_action = handle_worker_candidate_finalization(
                        state=state,
                        full_message=full_message,
                        history=self._history,
                        workspace_root=self._tools.workspace_root,
                        on_event=on_event,
                        finish_worker_recoverable_followup=(
                            self._finish_worker_unrecoverable
                        ),
                        declared_run_command=declared_run_command,
                        explicit_validation_commands=explicit_validation_commands,
                    )
                    if finalization_action == "continue":
                        continue
                    self._preexisting_failures = list(state.preexisting_validation_failures)
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
    for message in reversed(history.messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
            return "\n".join(part for part in parts if part)
    return ""


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
