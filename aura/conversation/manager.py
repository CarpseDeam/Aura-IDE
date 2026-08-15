"""ConversationManager — runs the tool-loop and forwards events to a callback.

Lives on a conversation thread (Qt bridge owns the QThread). The GUI never touches
this directly except through the bridge.

Cancellation: a threading.Event the GUI sets when Stop is clicked. We check
it between rounds and propagate it into client.stream() so the OpenAI iterator
short-circuits mid-chunk.

The loop is a conventional coding-agent loop and nothing more: send canonical
history with the stable tool catalog, the selected model, and the user-selected
thinking mode; forward the stream; append the complete assistant response; if it
carries tool calls, validate them structurally, execute the whole batch, append
one truthful result per call in order, and call the same model again; otherwise
the turn is over. There is no router, no classifier, no counter, no budget, and
no continuation Aura injects on the model's behalf.
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
)
from aura.config import ModelId, ThinkingMode
from aura.conversation._report_tools import REPORT_BLOCKER
from aura.conversation.context_refresh import ContextRefreshState
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import (
    ApprovalCallback,
    ApprovalDecision,
    ApprovalRequest,
)
from aura.conversation.tools.registry import ToolRegistry
from aura.conversation.validation_orchestrator import ValidationCommandSpec
from aura.events import EventBus
from aura.model_streams import PRODUCTION_STREAM_HOOK, model_streams
from aura.skills.turn_state import SkillTurnState

EventCallback = Callable[[Event], None]

_PRODUCTION_STREAM_LABEL = "production_stream"


def _blocker_reason_from_call(full_message: dict[str, Any]) -> str:
    """Return the blocker text named by this turn's ``report_blocker`` call.

    Bookkeeping for the completion receipt only: a turn that reported a blocker
    is summarized as blocked rather than completed. It does not end the turn.
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


class ConversationManager:
    def __init__(
        self,
        history: History,
        tool_registry: ToolRegistry,
        event_bus: EventBus | None = None,
    ) -> None:
        self._history = history
        self._tools = tool_registry
        self._event_bus = event_bus
        self._tool_runner = ToolRunner(
            history=self._history,
            workspace_root=self._tools.workspace_root,
        )
        self._context_refresh = ContextRefreshState(
            capabilities_provider=getattr(
                self._tools, "active_capabilities", None
            ),
        )
        self._tool_round_runner = ToolRoundRunner(
            history=self._history,
            tools=self._tools,
            tool_runner=self._tool_runner,
            event_bus=self._event_bus,
        )
        #: Blocker reason from the most recent turn's successful
        #: ``report_blocker``, for completion receipts. Reset per send.
        self._last_turn_blocked_reason: str = ""
        #: Whether the most recent turn recorded a successful structured
        #: ``report_already_satisfied``. Receipt bookkeeping only — never
        #: inferred from the absence of a write. Reset per send.
        self._last_turn_already_satisfied: bool = False
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
    def last_turn_already_satisfied(self) -> bool:
        """Whether the last turn recorded structured already-satisfied evidence."""
        return self._last_turn_already_satisfied

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
        root = self._context_refresh.workspace_root
        if root is None:
            return None
        from aura.skills.text import build_skill_pack

        pack = build_skill_pack(
            root,
            model=self._context_refresh.model,
            task_kind=self._context_refresh.task_kind,
            target_files=self._context_refresh.target_files,
            content=self._context_refresh.content,
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
        *,
        model: str | None = None,
        task_kind: str | None = None,
        content: str | None = None,
        target_files: tuple[str, ...] = (),
    ) -> None:
        """Store the production base prompt, root, and live terrain.

        This is the canonical configuration call for the production
        production path. Mid-turn context refreshes use this terrain so the
        turn's skills are not dropped mid-run.
        """
        self._context_refresh.configure(
            base_prompt,
            workspace_root,

            model=model,
            task_kind=task_kind,
            content=content,
            target_files=target_files,
        )

    def send(
        self,
        on_event: EventCallback,
        approval_cb: ApprovalCallback,
        cancel_event: threading.Event,
        model: ModelId,
        thinking: ThinkingMode,
        temperature: float = 0.7,
        explicit_validation_commands: list[ValidationCommandSpec] | None = None,
        declared_run_command: str | None = None,
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        One shape, every round:

        1. Send canonical history with the stable tool catalog
           (:meth:`ToolRegistry.tool_defs`), the caller-selected ``model``, and
           the caller-selected ``thinking`` mode.
        2. Forward the provider stream to ``on_event`` as it arrives.
        3. Append the complete assistant response exactly as received.
        4. If it carries tool calls, validate them structurally, execute the
           whole batch, append exactly one truthful result per call in original
           call order, and call the same model again.
        5. If it carries no tool calls, the turn is finished.
        6. Cancellation or provider failure stops the loop without claiming the
           work succeeded.

        Nothing else ends a turn. There is no round, tool, token, or time
        ceiling; no injected continuation; no required-tool round; no rejection
        of a repeated read; and no classification of the request deciding
        whether the model is allowed to stop.
        """
        self._last_turn_blocked_reason = ""
        self._last_turn_already_satisfied = False
        # Web research is offered when the search backend is genuinely
        # configured — never because Aura read the user's sentence. Resolved
        # once per turn so the catalog, and the provider's cached request
        # prefix, stay identical across the turn's rounds.
        self._tools.refresh_web_search_availability()
        # Freeze this real user turn's skill candidates once, so load_skills
        # resolves against the same deterministic selection that produced the
        # initial skill index — never a recomputation per round.
        skill_turn = self._build_skill_turn_state()
        self._last_skill_turn = skill_turn
        self._tool_round_runner.begin_turn()

        while True:
            if cancel_event.is_set():
                self._cleanup_cancelled(on_event)
                return

            full_message: dict[str, Any] | None = None

            # The one request shape: the same stable catalog, the user's model,
            # and the user's thinking mode, on every round of the turn.
            tool_defs = self._tools.tool_defs()

            _log.info(
                "%s_start model=%s thinking=%s hook_name=%s",
                _PRODUCTION_STREAM_LABEL, model, thinking, PRODUCTION_STREAM_HOOK,
            )
            _first_event = True

            # Pass a deep-copied canonical history snapshot. Nothing is
            # compacted, pruned, or rewritten here, so the round's own plan and
            # reasoning remain durable for the UI and replay inspection. The
            # client/protocol layer owns the provider-specific wire projection.
            request_messages = self._history.for_api()

            for ev in model_streams.trigger(
                PRODUCTION_STREAM_HOOK,
                messages=request_messages,
                tools=tool_defs,
                model=model,
                thinking=thinking,
                cancel_event=cancel_event,
                temperature=temperature,
            ):
                if _first_event:
                    _log.info(
                        "%s_first_event model=%s", _PRODUCTION_STREAM_LABEL, model
                    )
                    _first_event = False

                # Every event is forwarded as produced, including prose emitted
                # before tool calls. Nothing is buffered, withheld, or blanked.
                on_event(ev)

                if isinstance(ev, Done):
                    full_message = ev.full_message
                elif isinstance(ev, ApiError):
                    _log.info(
                        "%s_api_error model=%s", _PRODUCTION_STREAM_LABEL, model
                    )
                    # A provider failure stops the turn. Nothing already
                    # completed is retracted and no success is claimed.
                    return

            _log.info("%s_done model=%s", _PRODUCTION_STREAM_LABEL, model)

            if cancel_event.is_set():
                # Cancellation is not a verdict: whatever the stream already
                # completed keeps its terminal event.
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
                # The stream ended without a Done. There is no assistant
                # response to append and nothing to execute.
                return

            # The complete assistant response, exactly as received.
            self._history.append_assistant(full_message)

            tool_calls = full_message.get("tool_calls") or []
            if not tool_calls:
                return

            tool_round = self._tool_round_runner.run(
                tool_calls=tool_calls,
                skill_turn=skill_turn,
                on_event=on_event,
                approval_cb=approval_cb,
                cancel_event=cancel_event,
                cleanup_cancelled=self._cleanup_cancelled,
                explicit_validation_commands=explicit_validation_commands,
                declared_run_command=declared_run_command,
                tool_defs=tool_defs,
            )

            # ── Passive receipt bookkeeping ──────────────────────────────
            # A successful ``report_blocker`` names why the attempt ended; a
            # successful ``report_already_satisfied`` records that the requested
            # state already existed. Both are ordinary optional tools: they are
            # summarized in the completion receipt and neither ends the turn,
            # forces a finalization round, or changes the next request.
            if tool_round.blocker_succeeded:
                self._last_turn_blocked_reason = _blocker_reason_from_call(
                    full_message
                )
            if tool_round.already_satisfied_succeeded:
                self._last_turn_already_satisfied = True

            if tool_round.cancelled:
                return

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
    "TerminalOutput",
]
