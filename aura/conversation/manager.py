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
from aura.conversation.dispatch import (
    DispatchCallback,
    WorkerDispatchRequest,
    WorkerDispatchResult,
)
from aura.conversation.focused_action import (
    FOCUSED_ACTION_THINKING,
    OUTCOME_ACTION_FAILED,
    OUTCOME_ALREADY_SATISFIED,
    OUTCOME_BLOCKER,
    OUTCOME_NOT_APPLIED,
    OUTCOME_PROVIDER_CONTRACT_FAILURE,
    OUTCOME_WRITE,
    REPORT_ALREADY_SATISFIED,
    REPORT_BLOCKER,
    FocusedActionState,
    FocusedContractDiagnosis,
    action_failed_message,
    diagnose_focused_contract,
    focused_correction_instruction,
    provider_contract_failure_message,
    should_enter_focused_action,
    tool_call_names,
)
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.planner_dispatch_gate import maybe_force_worker_dispatch
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.planner_stream_hygiene import PlannerStreamHygiene
from aura.conversation.stream_event_router import StreamEventRouter
from aura.conversation.task_router import (
    TaskRoute,
    classify_user_request,
    route_bears_production_action,
)
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
from aura.conversation.worker_finish import (
    build_worker_unrecoverable_message,
)
from aura.conversation.workflow_state import WorkflowStatus
from aura.events import EventBus
from aura.lifecycle import LifecycleHooks
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.research.policy import decide_research_policy
from aura.skills.turn_state import SkillTurnState
from aura.work_artifact.model import ValidationCommandSpec

EventCallback = Callable[[Event], None]


def _stream_log_label(hook_name: str) -> str:
    """Return a short log label for the active model-generation hook."""
    if "planner" in hook_name:
        return "planner_stream"
    if "worker" in hook_name:
        return "worker_stream"
    return "production_stream"


def _blocker_reason_from_call(full_message: dict[str, Any]) -> str:
    """Return the blocker text named by this turn's ``report_blocker`` call.

    The focused action request serializes exactly one tool call; when it is
    ``report_blocker`` its ``blocker`` argument is the reason the implementation
    attempt ended. Carried to the completion receipt so a blocked turn reports
    as blocked, never as completed.
    """
    for call in full_message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict) or function.get("name") != REPORT_BLOCKER:
            continue
        try:
            args = json.loads(function.get("arguments") or "{}")
        except (TypeError, ValueError):
            return ""
        reason = args.get("blocker")
        return str(reason).strip() if isinstance(reason, str) else ""
    return ""


#: Structured payload paired to a tool call that was cancelled before Aura
#: received its authoritative result. Deliberately fail-closed: never
#: ``applied``, never successful, and it explicitly refuses to claim the tool
#: made no workspace changes — the operation's effects are simply unknown.
_CANCELLATION_TOOL_RESULT_TEMPLATE: dict[str, Any] = {
    "ok": False,
    "cancelled": True,
    "recoverable": False,
    "failure_class": "cancelled",
    "execution_status": "interrupted_before_authoritative_result",
    "message": (
        "Cancelled before Aura received an authoritative result. Do not "
        "infer that the operation completed or that a mutation applied."
    ),
}


def _synthetic_cancellation_result(tool_name: str) -> str:
    """Return the JSON tool-result payload for one cancelled tool call.

    The structured payload exists only to restore the transcript's tool-call
    pairing.  It must never be read as a successful execution, a validation
    pass, or an applied mutation: ``ok`` is false, ``applied`` is absent, and
    the message states that nothing about the operation's effect may be
    inferred.
    """
    payload = dict(_CANCELLATION_TOOL_RESULT_TEMPLATE)
    payload["tool"] = tool_name or "<unknown>"
    return json.dumps(payload, ensure_ascii=False)


def _assistant_tool_call_name(call: Any) -> str:
    """Return the tool name for one assistant tool-call entry, or ``""``.

    Repair/reporting only: a malformed entry yields the empty string so the
    caller can decide whether safe pairing is even possible.
    """
    if not isinstance(call, dict):
        return ""
    function = call.get("function")
    if not isinstance(function, dict):
        return ""
    name = function.get("name")
    return name if isinstance(name, str) else ""


def _repairable_tool_call_block(tool_calls: Any) -> bool:
    """Return whether every entry in a tool-call list can receive a paired result.

    A block is repairable when each entry is a dict carrying a usable ``id`` —
    the only key a synthetic tool result needs to pair back.  A non-dict entry,
    a missing id, or an empty id means no synthetic result could safely pair,
    so the newest block is removed instead of fabricating unpaired results.
    """
    if not isinstance(tool_calls, list):
        return False
    for call in tool_calls:
        if not isinstance(call, dict):
            return False
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id:
            return False
    return True


def _log_context_round(
    budget,
    stats,
    tool_defs: list[dict[str, Any]] | None,
    *,
    request_growth_tokens: int | None = None,
) -> None:
    """One line per model round describing where the context budget went.

    Deliberately a single log record, not a telemetry framework — enough to
    answer "why was this turn's evidence cut?" from a normal log file. The
    lifecycle fields prove the request plateaued: how many completed blocks
    retired into the evidence ledger, how many characters the ledger and the
    active chain retain, and how the request grew from the preceding round.
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
    growth = (
        "na"
        if request_growth_tokens is None
        else str(request_growth_tokens)
    )

    _log.info(
        "context_round model=%s provider=%s window=%d reserve=%d derived_budget=%d "
        "policy_cap=%s budget=%d capped_by_policy=%s budget_source=%s "
        "tokens_before=%d tokens_after=%d messages_before=%d messages_after=%d "
        "system_chars=%d tool_schema_chars=%d tool_schema_tokens=%d "
        "request_tokens=%d request_growth_tokens=%s request_headroom=%d "
        "system_fingerprint=%s "
        "retired_blocks=%d ledger_entries=%d ledger_chars_retained=%d "
        "ledger_dropped_entries=%d ledger_budget_tokens=%d "
        "active_chain_chars_retained=%d recent_evidence_tokens=%d "
        "bounded_replays=%d "
        "source_chars_generated=%d source_chars_retained=%d "
        "compacted_results=%d dropped_blocks=%d repaired=%d "
        "reasoning_chars_replayed=%d reasoning_chars_dropped=%d "
        "boundary_messages_inserted=%d over_budget=%s",
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
        growth,
        budget.context_window_tokens - budget.output_reserve_tokens - request_tokens,
        stats.system_prompt_fingerprint,
        stats.retired_blocks,
        stats.ledger_entries,
        stats.ledger_chars_retained,
        stats.ledger_dropped_entries,
        stats.ledger_budget_tokens,
        stats.active_chain_chars_retained,
        stats.recent_evidence_tokens,
        stats.bounded_replays,
        stats.source_result_chars_generated,
        stats.source_result_chars_retained,
        stats.compacted_results,
        stats.dropped_blocks,
        stats.repaired_messages,
        stats.reasoning_chars_replayed,
        stats.reasoning_chars_dropped,
        stats.boundary_messages_inserted,
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
        self._planner_refresh = PlannerRefreshState(
            capabilities_provider=getattr(
                self._tools, "active_capabilities", None
            ),
        )
        self._tool_round_runner = ToolRoundRunner(
            history=self._history,
            tools=self._tools,
            tool_runner=self._tool_runner,
            planner_refresh=self._planner_refresh,
            lifecycle=self._lifecycle,
            event_bus=self._event_bus,
        )
        #: Terminal blocker reason from the most recent focused action turn
        #: that ended in a successful ``report_blocker``, for completion
        #: receipts. Reset at the start of every send.
        self._last_turn_blocked_reason: str = ""
        #: Whether the most recent turn ended in a focused provider-contract
        #: failure (the required-tool call was not honoured). Completion
        #: receipts report it as its own terminal status. Reset per send.
        self._last_turn_provider_contract_failure: bool = False
        #: Whether the most recent turn ended in a successful structured
        #: ``report_already_satisfied`` outcome — the requested state already
        #: exists. Completion receipts read this as explicit evidence, never
        #: inferred from the absence of a write. Reset per send.
        self._last_turn_already_satisfied: bool = False
        #: Whether the most recent turn was routed for a production action.
        #: Read once from the turn's authoritative route at the start of send;
        #: completion receipts hold such turns to the truthful-outcome contract.
        self._last_turn_bears_production_action: bool = False
        #: The most recent send's frozen skill turn state (or None when the
        #: send exposed no candidates). Kept so the bridge can surface the
        #: activation ledger after the turn completes.
        self._last_skill_turn: SkillTurnState | None = None

    @property
    def history(self) -> History:
        return self._history

    @property
    def last_turn_blocked_reason(self) -> str:
        """Blocker text from the last turn's successful ``report_blocker``."""
        return self._last_turn_blocked_reason

    @property
    def last_turn_provider_contract_failure(self) -> bool:
        """Whether the last turn ended in a focused provider-contract failure."""
        return self._last_turn_provider_contract_failure

    @property
    def last_turn_already_satisfied(self) -> bool:
        """Whether the last turn ended in structured already-satisfied evidence."""
        return self._last_turn_already_satisfied

    @property
    def last_turn_bears_production_action(self) -> bool:
        """Whether the last turn was routed for a production action."""
        return self._last_turn_bears_production_action

    def skill_activation_log(self) -> list[dict]:
        """Structured skill activation ledger of the last completed send.

        Empty when the last send exposed no candidates. Inspection-only — never
        injected into the provider prompt.
        """
        if self._last_skill_turn is None:
            return []
        return self._last_skill_turn.activation_log()

    def _build_skill_turn_state(self) -> SkillTurnState | None:
        """Compose and freeze this real user turn's skill candidates.

        Runs once at the start of ``send()`` — never after each model/tool
        round — from the same deterministic terrain that produced the initial
        skill index (configured via ``configure_runtime_context``).  Because
        selection is deterministic, a retried request reconstructs the same
        frozen index from the same terrain and repository state.  ``None`` when
        no workspace/terrain is configured (the send exposed no candidates).
        """
        root = self._planner_refresh.workspace_root
        if root is None:
            return None
        from aura.skills.text import build_skill_pack

        pack = build_skill_pack(
            root,
            model=self._planner_refresh.model,
            task_kind=self._planner_refresh.task_kind,
            target_files=self._planner_refresh.target_files,
            content=self._planner_refresh.content,
        )
        if not pack.candidates:
            return None
        return SkillTurnState(pack)

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
        latest_user_text = _latest_user_text(self._history)
        if task_route is None and mode == "single":
            # A production send with no route is not an unbounded turn. The
            # discovery ceiling reads the route, so a caller that omits one
            # would silently switch the ceiling off for the whole turn — the
            # bound must not depend on a caller remembering to pass an argument.
            # Resolving it here is the same deterministic classification the
            # send layer already performs, over the same real user message, so
            # a route that *was* passed is still never recomputed.
            task_route = classify_user_request(latest_user_text)
            _log.info(
                "task_route_resolved_defensively lane=%s action=%s",
                task_route.lane.value,
                task_route.action,
            )
        state = _SendState(
            mode=mode,
            research_policy=decide_research_policy(latest_user_text),
            task_route=task_route,
            tool_effect=self._tools.tool_effect,
            read_only=bool(getattr(self._tools, "read_only", False)),
        )
        state.focused_action.selected_thinking = str(thinking)
        # Freeze this real user turn's skill candidates once, so load_skills
        # resolves against the same deterministic selection that produced the
        # initial skill index — never a recomputation per round.
        state.skill_turn = self._build_skill_turn_state()
        self._last_skill_turn = state.skill_turn
        self._last_turn_blocked_reason = ""
        self._last_turn_provider_contract_failure = False
        self._last_turn_already_satisfied = False
        # The authoritative route (the caller's, or the defensive resolution
        # above) decides whether this turn bore a production action. A
        # read-only registry exposes no mutation tools, so such turns never owe
        # an act and keep their ordinary completion behaviour. Read once and
        # carried to the completion receipt, which holds production-action
        # turns to the truthful-outcome contract.
        self._last_turn_bears_production_action = (
            not state.read_only
            and route_bears_production_action(state.task_route)
        )
        if state.mode == "worker":
            state.loaded_target_files = list(loaded_target_files or [])
            if worker_dispatch_request is not None:
                state.dispatched_target_files = list(worker_dispatch_request.files)
                state.worker_artifact_id = str(worker_dispatch_request.artifact_id or "")
                state.worker_artifact_item_id = str(worker_dispatch_request.artifact_item_id or "")

        prev_request_tokens: int | None = None

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
            if focused.active and state.read_only:
                # A read-only registry exposes no mutation tools, so there is
                # nothing for a focused action request to act on. Do not enter
                # focused mutation mode under a read-only registry.
                focused.active = False
            if focused.active:
                tool_defs = self._tools.focused_action_tool_defs()
                round_thinking: ThinkingMode = FOCUSED_ACTION_THINKING
                focused.exposed_tools = tuple(
                    str(t.get("function", {}).get("name", "")) for t in tool_defs
                )
                _log.info(
                    "focused_action_start route_lane=%s route_action=%s "
                    "entered_via=%s decision_id=%s "
                    "selected_thinking=%s focused_action_thinking=%s "
                    "action_tools=%s",
                    getattr(getattr(state.task_route, "lane", None), "value", ""),
                    getattr(state.task_route, "action", ""),
                    "committed_decision" if focused.decision_committed else "stall",
                    focused.decision_id or "<none>",
                    focused.selected_thinking,
                    FOCUSED_ACTION_THINKING,
                    ",".join(focused.exposed_tools),
                )
            elif focused.blocked or focused.already_satisfied:
                # A successful blocker or already-satisfied report has already
                # ended the implementation attempt: the turn owes exactly one
                # factual final response and nothing else, so the request that
                # produces it exposes no tool catalog. The final cannot mutate,
                # search, or re-open planning.
                tool_defs = []
                round_thinking = thinking
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
                # A focused action request must answer with exactly one tool
                # call. Prose is a provider-contract violation: hold it so it is
                # discarded, never streamed to chat.
                discard_prose_final=focused.active,
                # Whether the response honours that contract is only knowable
                # once the full message has been inspected below, so the
                # provider's Done is held until then: a violating response must
                # not project a Done of its own and then receive the factual
                # contract-failure Done as well.
                defer_done=focused.active,
            )

            # The outbound view is compacted against *this* model's budget;
            # self._history.messages is left exact.
            budget = resolve_model_budget(model)
            api_view = self._history.build_api_payload(
                budget.working_set_tokens,
                effect_for=self._tools.declared_effect,
            )
            try:
                schema_chars = len(json.dumps(tool_defs, ensure_ascii=False)) if tool_defs else 0
            except (TypeError, ValueError):
                schema_chars = 0
            request_tokens = api_view.stats.tokens_after + max(schema_chars, 0) // 4
            growth = (
                None
                if prev_request_tokens is None
                else request_tokens - prev_request_tokens
            )
            prev_request_tokens = request_tokens
            _log_context_round(
                budget,
                api_view.stats,
                tool_defs,
                request_growth_tokens=growth,
            )

            request_messages = api_view.messages

            # ── Focused protocol correction ──────────────────────────────
            # A discarded focused response leaves exactly one compact
            # instruction naming what was structurally wrong with it. It is
            # attached to *this outbound copy only*: canonical history is
            # untouched, the frozen system prompt is untouched (its fingerprint
            # is logged above and does not move), and it is consumed by the one
            # request it corrects. A turn with no violation never builds one, so
            # ordinary turns pay nothing — not a request, not a token.
            if focused.active and focused.pending_correction:
                request_messages = [
                    *request_messages,
                    {"role": "user", "content": focused.pending_correction},
                ]
                focused.pending_correction = ""
                _log.info(
                    "focused_action_correction_issued violation=%s",
                    focused.last_violation or "<none>",
                )

            # ``require_tool_call`` is only passed when it is actually
            # required, so every other request reaches the backend with the
            # exact call signature it has always had.
            stream_kwargs: dict[str, Any] = {}
            if focused.active:
                stream_kwargs["require_tool_call"] = True

            for ev in model_streams.trigger(
                hook_name,
                messages=request_messages,
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
                    # An API error is not a contract verdict either: anything
                    # the stream had already completed keeps its terminal event.
                    router.release_deferred_done()
                    return

            _log.info("%s_done model=%s", label, model)

            # A stream that ended without a Done (cancel, truncated provider
            # response) still owes the user whatever prose it produced.  Rounds
            # that reached Done already resolved their buffer, so this is a
            # no-op for them.
            if state.content_gate is not None:
                state.content_gate.flush(on_event)

            if cancel_event.is_set():
                # Cancellation is not a contract verdict: whatever the stream
                # already completed keeps its terminal event, exactly as it did
                # before the focused Done was deferred.
                router.release_deferred_done()
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

            if full_message is None and focused.active:
                # A focused stream that completed with nothing to validate is
                # still a contract violation, not a silent exit: returning here
                # dead-stopped a turn whose evidence was entirely intact.
                if self._repair_focused_contract(
                    focused=focused,
                    diagnosis=diagnose_focused_contract(
                        None, focused.exposed_tools
                    ),
                    router=router,
                    state=state,
                    on_event=on_event,
                ):
                    continue
                return

            if full_message is None:
                # Should not happen in normal stream completion. There is no
                # message to validate, so nothing can be held back on its
                # behalf: release any deferred terminal event.
                router.release_deferred_done()
                return

            tool_calls = full_message.get("tool_calls") or []

            if focused.active:
                # The focused provider contract, checked against the raw
                # ``tool_calls`` collection: exactly one entry, well-formed,
                # naming a tool in this round's exposed action set. Zero calls,
                # multiple calls, a malformed entry alongside a valid one, or an
                # unknown/unexposed name are all contract violations — none of
                # them execute anything.
                diagnosis = diagnose_focused_contract(
                    full_message, focused.exposed_tools
                )
                if not diagnosis.ok:
                    # A malformed response never spent the decision, so the
                    # request is deliberately *not* marked spent here: the
                    # repair reissues the identical focused request rather than
                    # sending the model back through repository discovery to fix
                    # its own formatting.
                    if self._repair_focused_contract(
                        focused=focused,
                        diagnosis=diagnosis,
                        router=router,
                        state=state,
                        on_event=on_event,
                    ):
                        continue
                    return

                # Valid: the decision is now genuinely spent, and whatever
                # protocol repair this decision needed is over. ``active`` stays
                # set until this round's tool results have been folded in; the
                # top of the next round recomputes it and finds ``spent``.
                focused.spent = True
                focused.clear_protocol_recovery()
                # A committed implementation decision authorizes exactly the one
                # act that serializes it — the write, the blocker, the
                # already-satisfied report, or an act that failed to apply. It
                # is spent here so it can never authorize an unrelated later
                # mutation; acting again means committing a corrected decision,
                # or the guard's stall fallback taking over.
                focused.consume_decision()
                selected = tool_call_names(full_message)
                focused.selected_action = selected[0] if selected else ""
                if REPORT_ALREADY_SATISFIED in selected:
                    focused.outcome = OUTCOME_ALREADY_SATISFIED
                else:
                    focused.outcome = (
                        OUTCOME_BLOCKER
                        if REPORT_BLOCKER in selected
                        else OUTCOME_WRITE
                    )
                # The response is valid: the provider's own Done is forwarded
                # now, exactly once, before the single tool runs.
                router.release_deferred_done()

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
                tool_defs=tool_defs,
            )
            guard = state.pre_edit_guard
            write_applied = bool(guard is not None and guard.write_applied)
            if focused.active:
                focused.active = False
                if focused.outcome == OUTCOME_BLOCKER:
                    # Blocked completion is terminal only when the matching
                    # tool result actually succeeded and its structured payload
                    # says the blocker was reported. An invalid blocker call is
                    # an ordinary failed tool result and must not end the
                    # attempt as blocked.
                    if tool_round.blocker_succeeded:
                        # No mutation happened and none will: the attempt ends
                        # here and the turn owes exactly one factual final
                        # response, which the existing completion path produces
                        # against a request that exposes no tool catalog.
                        focused.blocked = True
                        state.task_completion_context = True
                        self._last_turn_blocked_reason = _blocker_reason_from_call(
                            full_message
                        )
                if focused.outcome == OUTCOME_ALREADY_SATISFIED:
                    # Already-satisfied completion is terminal only when the
                    # matching tool result actually succeeded and its structured
                    # payload recorded it. An invalid call is an ordinary failed
                    # tool result and must not end the attempt as satisfied —
                    # it returns to the ordinary loop like any other failed act.
                    if tool_round.already_satisfied_succeeded:
                        # The requested state already exists: no mutation
                        # happened and none is required. The attempt ends here
                        # and the turn owes exactly one factual final response,
                        # against a request that exposes no tool catalog.
                        focused.already_satisfied = True
                        state.task_completion_context = True
                        self._last_turn_already_satisfied = True
                if (
                    not focused.blocked
                    and not focused.already_satisfied
                    and not write_applied
                    and not cancel_event.is_set()
                    and state.implementation_action_pending()
                ):
                    # The act ran and changed nothing — a failed write, a stale
                    # patch, a rejected approval, an invalid blocker, or an
                    # invalid already-satisfied report. That is evidence, not a
                    # completed task: the tool result is in history and says
                    # what to fix. The request is spent for *this* decision
                    # only, and the turn goes back to the ordinary loop to
                    # inspect, correct, and act again. Nothing here counts
                    # attempts; the evidence judgement below is the only thing
                    # that can end the turn.
                    focused.outcome = OUTCOME_NOT_APPLIED
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

            # ── Is the turn still advancing? ─────────────────────────────
            # Asked only once a focused act has already run without applying,
            # and answered only from the guard's existing ledgers. A round that
            # advanced the turn is a corrected decision: re-arm the focused
            # request, however many times that happens. A round that produced a
            # failure the turn had not seen before changes the diagnosis, so the
            # ordinary loop keeps it. A round that did neither means nothing
            # left in this turn can change what happens next — that, and only
            # that, is the honest ending.
            if (
                focused.spent
                and guard is not None
                and not focused.blocked
                and not focused.already_satisfied
                and not write_applied
                and not cancel_event.is_set()
                and tool_round.action != "return"
                and state.implementation_action_pending()
            ):
                if guard.last_round_advanced:
                    focused.spent = False
                    # A corrected decision is a new focused decision: whatever
                    # protocol repair the previous one needed does not carry
                    # into it, so a violation corrected earlier in the turn can
                    # never be mistaken for a repeat here.
                    focused.clear_protocol_recovery()
                    _log.info(
                        "focused_action_rearmed reason=round_advanced "
                        "selected_action=%s",
                        focused.selected_action or "<none>",
                    )
                elif not guard.recovery_open:
                    focused.outcome = OUTCOME_ACTION_FAILED
                    _log.info(
                        "focused_action_outcome outcome=%s selected_action=%s "
                        "advanced=False new_failure=False repeated_failures=%s",
                        focused.outcome,
                        focused.selected_action or "<none>",
                        guard.repeated_failures,
                    )
                    content, failure_message = action_failed_message()
                    self._history.append_assistant(failure_message)
                    on_event(ContentDelta(text=content))
                    on_event(
                        Done(finish_reason="stop", full_message=failure_message)
                    )
                    return
            if tool_round.action == "return":
                return
            if tool_round.action == "continue":
                continue

    def _repair_focused_contract(
        self,
        *,
        focused: FocusedActionState,
        diagnosis: FocusedContractDiagnosis,
        router: StreamEventRouter,
        state: _SendState,
        on_event: EventCallback,
    ) -> bool:
        """Discard a contract-violating focused response and decide what follows.

        Returns ``True`` when the send loop should reissue the focused request
        with an explicit correction, ``False`` when the turn ends as a
        provider-contract failure (this method has already emitted it).

        The discard is identical either way, and it is total: the buffered
        prose, the reasoning, every tool call, and the provider's own deferred
        ``Done`` are dropped, nothing is executed, nothing is salvaged from a
        mixed batch, and the violating assistant message never enters canonical
        history.  Only *terminality* differs — validation is exactly as strict
        as it was.

        Which one it is comes from evidence, not arithmetic.  A structural
        violation this decision has not seen before is a recoverable
        formatting lapse: it earns one explicit correction naming precisely what
        was wrong, and the same focused request goes out again over the same
        gathered evidence.  The identical structural violation *after* that
        correction is proof the provider cannot honour the protocol, and only
        then does the turn end.  There is no attempt count anywhere in here.
        """
        router.discard_deferred_done()
        if state.content_gate is not None:
            state.content_gate.discard_buffer()
        focused.selected_action = ""
        focused.last_violation = diagnosis.kind

        fingerprint = diagnosis.fingerprint
        if fingerprint not in focused.violation_fingerprints:
            focused.violation_fingerprints.add(fingerprint)
            focused.pending_correction = focused_correction_instruction(
                diagnosis, focused.exposed_tools
            )
            _log.info(
                "focused_action_contract_repair violation=%s calls=%d names=%s "
                "action=reissue_focused_request",
                diagnosis.kind,
                diagnosis.call_count,
                ",".join(diagnosis.names) or "<none>",
            )
            return True

        focused.contract_violated = True
        focused.outcome = OUTCOME_PROVIDER_CONTRACT_FAILURE
        self._last_turn_provider_contract_failure = True
        _log.info(
            "focused_action_outcome outcome=%s violation=%s calls=%d "
            "corrected=True repeated=True selected_thinking=%s "
            "focused_action_thinking=%s",
            focused.outcome,
            diagnosis.kind,
            diagnosis.call_count,
            focused.selected_thinking,
            FOCUSED_ACTION_THINKING,
        )
        # The turn owes exactly one factual failure response, with exactly one
        # Done — the one emitted here.
        content, failure_message = provider_contract_failure_message()
        self._history.append_assistant(failure_message)
        on_event(ContentDelta(text=content))
        on_event(Done(finish_reason="stop", full_message=failure_message))
        return False

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
        """Repair the current real-user turn after a cancellation.

        Cancellation can interrupt an assistant tool-call block mid-batch:
        history then ends at an assistant message whose calls have no results.
        The old behaviour rewound the whole turn back to the preceding user
        message, silently erasing completed reads, applied writes, terminal
        commands, and validation evidence from the same turn even though the
        workspace was already modified.

        This repairs instead of rewinding.  Every completed assistant/tool-
        result block is preserved byte-for-byte; only the newest incomplete
        assistant tool-call block is touched, and only by appending one
        structured synthetic cancellation result for each call whose
        authoritative result never arrived, in call order, so the provider's
        tool-call pairing stays valid.  Synthetic results are fail-closed —
        never ``applied``, never successful — and exist only to restore that
        pairing.  Repair is idempotent: a second call finds every call already
        paired and changes nothing.

        A newest block too malformed to pair safely removes only that newest
        malformed assistant/result block; the real-user turn is never rewound.
        """
        if not self._history.messages:
            on_event(ApiError(status_code=None, message="Cancelled."))
            return

        messages = self._history.messages
        user_idx = self._history.latest_real_user_index()
        start = (user_idx + 1) if user_idx is not None else 0

        for i in range(len(messages) - 1, start - 1, -1):
            msg = messages[i]
            if msg.get("role") != "assistant":
                continue

            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                # Newest assistant block of the turn, without tool calls. A
                # block that carried only reasoning or prose is preserved —
                # partial-stream output survives cancellation — while an empty
                # block (no content, reasoning, or calls) is stripped exactly
                # as before.
                if not msg.get("content") and not msg.get("reasoning_content"):
                    self._history.truncate_after(i)
                break

            if not _repairable_tool_call_block(tool_calls):
                # A malformed block no synthetic result could pair to. Remove
                # only this newest malformed assistant/result block; never the
                # whole real-user turn.
                self._history.truncate_after(i)
                break

            # Authoritative results that already arrived before the cancel are
            # kept verbatim. Collect them so an existing result is never
            # replaced by a synthetic one.
            present: set[str] = set()
            for j in range(i + 1, len(messages)):
                m = messages[j]
                if m.get("role") == "tool":
                    present.add(m.get("tool_call_id"))

            missing = [
                call for call in tool_calls if call.get("id") not in present
            ]
            if not missing:
                break  # every call is already paired; nothing to repair

            for call in tool_calls:
                if call.get("id") in present:
                    continue
                self._history.append_tool_result(
                    call["id"],
                    _synthetic_cancellation_result(
                        _assistant_tool_call_name(call)
                    ),
                )
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
