"""ConversationManager — runs the tool-loop and forwards events to a callback.

Lives on a conversation thread (Qt bridge owns the QThread). The GUI never touches
this directly except through the bridge.

Cancellation: a threading.Event the GUI sets when Stop is clicked. We check
it between rounds and propagate it into client.stream() so the OpenAI iterator
short-circuits mid-chunk.

The loop itself lives in :mod:`aura.conversation.agent_loop` — a conventional
coding-agent loop and nothing more: send canonical history with the stable tool
catalog, the selected model, and the user-selected thinking mode; forward the
stream; append the complete assistant response; if it carries tool calls,
validate them structurally, execute the whole batch, append one truthful result
per call in order, and call the same model again; otherwise the turn is over.
There is no router, no classifier, no counter, no budget, and no continuation
Aura injects on the model's behalf.

This manager owns what makes a *turn* a turn for the root production agent: the
canonical History, the frozen Skills turn state, the per-turn tool catalog, and
the user-facing send.  It composes those, then drives its rounds through one
:class:`~aura.conversation.agent_loop.AgentLoop` whose backend is this module's
production stream.  Resolving that stream stays here, so the process-global
``PRODUCTION_STREAM_HOOK`` remains the root agent's business alone and the loop
below it never reaches for a registry.
"""
from __future__ import annotations

import threading
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
from aura.conversation.agent_loop import AgentLoop
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


class ExplicitSkillSelectionError(RuntimeError):
    """The frozen explicit installed-skill selection cannot be honored."""


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
        # The root production agent's rounds. Everything the loop needs is
        # handed to it here; it never reaches back for a registry, a global,
        # or Qt. The backend is this manager's own production stream, so the
        # process-global production hook stays this module's business.
        self._loop = AgentLoop(
            history=self._history,
            stream=self._production_stream,
            tool_round=self._tool_round_runner,
            label=_PRODUCTION_STREAM_LABEL,
            hook_name=PRODUCTION_STREAM_HOOK,
        )
        #: Blocker reason from the most recent turn's successful
        #: ``report_blocker``. Reset per send.
        self._last_turn_blocked_reason: str = ""
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
            explicit_install_ids=self._context_refresh.explicit_install_ids,
        )
        if pack.unresolved_explicit:
            details = "\n".join(
                f"- {item.reference or '<empty identity>'}: {item.reason}"
                for item in pack.unresolved_explicit
            )
            raise ExplicitSkillSelectionError(
                "Selected skills could not be activated:\n"
                f"{details}\n"
                "Restore or re-enable the affected skills, then Retry."
            )
        if not pack.candidates:
            return None
        return SkillTurnState(pack)

    @property
    def tools(self) -> ToolRegistry:
        return self._tools

    def _production_stream(self, **kwargs: Any):
        """The root agent's backend: whatever is registered on the production hook.

        Resolved per round through the module-level registry, so re-pointing
        the one production backend at another provider takes effect on the
        next round without the loop knowing a registry exists. A second agent
        does not register here — it is handed its own backend's ``stream``
        directly.
        """
        return model_streams.trigger(PRODUCTION_STREAM_HOOK, **kwargs)

    def set_workspace_root(self, root: Path) -> None:
        self._tool_runner.set_workspace_root(root)

    def reset_conversation_runtime(self) -> None:
        """Reset conversation-owned execution state without changing history."""
        self._tool_runner.reset()

    def close(self) -> None:
        """Close conversation-owned execution resources."""
        self._tool_runner.close()

    def configure_runtime_context(
        self,
        workspace_root: Path,
        *,
        model: str | None = None,
        task_kind: str | None = None,
        content: str | None = None,
        target_files: tuple[str, ...] = (),
        explicit_install_ids: tuple[str, ...] = (),
    ) -> None:
        """Store the production root and live terrain.

        This is the canonical configuration call for the production
        production path. Mid-turn context refreshes use this terrain so the
        turn's skills are not dropped mid-run.
        """
        self._context_refresh.configure(
            workspace_root,
            model=model,
            task_kind=task_kind,
            content=content,
            target_files=target_files,
            explicit_install_ids=explicit_install_ids,
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
    ) -> None:
        """Run the model -> tool -> model loop until the model stops calling tools.

        Caller appends the user message to history before invoking this.

        The turn is composed here — the frozen skill turn state, the per-turn
        tool catalog, and the production backend — and its rounds are run by
        :class:`~aura.conversation.agent_loop.AgentLoop`, which owns the shape
        below and nothing above it.

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
        # Clear the preceding turn's runtime state before attempting to freeze
        # this one. An invalid explicit selection must not inherit old skills.
        self._last_skill_turn = None
        self._tools.set_turn_skill_state(None)

        # Freeze this real user turn's skill candidates once, so load_skills
        # resolves against the same deterministic selection that produced the
        # initial skill index — never a recomputation per round.
        try:
            skill_turn = self._build_skill_turn_state()
        except ExplicitSkillSelectionError as exc:
            on_event(ApiError(status_code=None, message=str(exc)))
            return
        self._last_skill_turn = skill_turn
        self._tools.set_turn_skill_state(skill_turn)
        self._tool_round_runner.begin_turn()

        # The model-facing tool catalog is resolved exactly once per turn,
        # after the frozen skill turn state and begin_turn() are in place.
        # Every provider request and tool-round
        # preflight in this send reuses this same snapshot; capabilities
        # added, removed, connected, disconnected, or edited while this send
        # is running take effect on the next send, not midway through this
        # one.
        tool_defs = self._tools.tool_defs()

        self._loop.run(
            on_event=on_event,
            approval_cb=approval_cb,
            cancel_event=cancel_event,
            model=model,
            thinking=thinking,
            tool_defs=tool_defs,
            temperature=temperature,
            skill_turn=skill_turn,
            explicit_validation_commands=explicit_validation_commands,
        )


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
