"""Per-send loop state for ConversationManager.send().

Holds all the mutable variables that track progress, recovery, and validation
through one invocation of the model/tool loop.  Extracted so that send() starts
with a compact, readable state setup instead of a wall of local declarations.

This module also owns :func:`implementation_action_pending` — the per-turn fact
that a production SINGLE turn bears a production action it has not yet
performed.  It is a *fact about the turn*, not a budget: nothing here counts
requests, files, tokens, or elapsed time, and nothing here ends the loop.  A
production turn ends only on a truthful terminal outcome — never on a count of
how much discovery it did.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from aura.conversation.edit_orchestrator import EditRetryLedger
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.single_content_gate import SingleContentGate
from aura.conversation.task_router import TaskRoute, route_bears_production_action
from aura.conversation.tool_limits import ToolLimitState
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.validation_ledger import WorkerValidationLedger
from aura.conversation.worker_flow import WorkerFlowHarness
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer
from aura.skills.turn_state import SkillTurnState


def implementation_action_pending(
    *,
    mode: str,
    route: TaskRoute | None,
    guard: PreEditLoopGuard | None,
    read_only: bool,
) -> bool:
    """Whether this turn owes a production action it has not yet performed.

    Pure over state the send loop already owns: production ``single`` mode, a
    route that bears a production action, no applied write yet, and a registry
    that is not read-only.  A read-only registry exposes no mutation tools, so
    there is no action owed at all.

    This is a *fact*, not a ceiling.  It answers "is the edit still outstanding"
    — which is what
    :func:`~aura.conversation.completion_guard.tool_result_completes_action`
    needs to refuse to call a probe the completed action of a turn that has not
    written anything.  It never bounds how much discovery the turn may do.

    "Bears a production action" is
    :func:`~aura.conversation.task_router.route_bears_production_action`, the one
    shared predicate — never a lane comparison written out again here.  A hybrid
    ``research`` / ``research_then_worker`` turn is a coding turn that needed
    facts first, and reading the lane alone silently exempted every one of them.
    """
    if mode != "single" or read_only:
        return False
    if not route_bears_production_action(route):
        return False
    return guard is not None and not guard.write_applied


@dataclass
class _SendState:
    """Per-call mutable state for ConversationManager.send().

    Bundles all the loop-tracking, recovery, and validation variables so the
    method's preamble is compact and the state access points are explicit
    (``state.field``) rather than scattered across 30+ bare-name assignments.
    """

    # --- initialisation inputs ---
    mode: str
    """``\"worker\"``, ``\"planner\"``, or ``\"single\"`` — determines which
    objects and branches are active."""

    research_policy: Any
    """Result of ``decide_research_policy()`` for this turn."""

    task_route: Any = None
    """The deterministic ``TaskRoute`` selected for this turn, when the caller
    supplied one. The focused action turn reads its lane; nothing here
    reclassifies the request."""

    tool_effect: Callable[[str], ToolEffect] | None = None
    """The live registry's authoritative tool-effect classifier, wired in by
    the send loop so the pre-edit guard never re-derives intent from a tool's
    name. ``None`` falls back to the built-in table plus the registry's
    observation default."""

    read_only: bool = False
    """Whether this turn's registry exposes no mutation tools, read once from
    the registry by the send loop. Carried here so the deep call sites that need
    it — the terminal round handlers among them — read one fact off the state
    rather than each re-reaching for the registry."""

    # --- per-round state ---
    reject_all_for_turn: bool = False
    rounds_used: int = 0
    task_completion_context: bool = False
    final_messages_after_completion: int = 0
    last_completion_final_text: str = ""
    planner_dispatch_gate_steered: bool = False

    # --- worker-only objects (initialised in __post_init__) ---
    limits: ToolLimitState = field(init=False)
    stream_buffer: WorkerStreamBuffer | None = field(init=False)
    worker_flow: WorkerFlowHarness | None = field(init=False)

    # --- single-mode production objects (initialised in __post_init__) ---
    content_gate: SingleContentGate | None = field(init=False)
    """Holds each round's ContentDelta until ``Done`` says who owns it."""

    pre_edit_guard: PreEditLoopGuard | None = field(init=False)
    """Deterministic duplicate-observation guard for the production turn."""

    # --- worker recovery state ---
    worker_flow_last_steering: str = ""
    worker_flow_last_reason: str = ""
    stale_validation_notes: list[str] = field(default_factory=list)
    validation_ledger: WorkerValidationLedger = field(
        default_factory=WorkerValidationLedger
    )

    # --- frozen per-turn skill candidates ---
    skill_turn: SkillTurnState | None = None
    """The frozen candidate index + activation ledger for this real user turn.

    Composed once when the turn begins (never recomputed per model/tool round)
    from the same deterministic terrain that produced the initial skill index,
    so ``load_skills`` resolves against that snapshot and nothing else. ``None``
    means this turn exposed no candidates."""

    # --- dispatch ---
    planner_dispatch_attempts: int = 0
    planner_visible_dispatch_tool_call_id: str = ""
    seen_internal_constraints: set[str] = field(default_factory=set)

    # --- edit recovery ---
    edit_failed_shapes: set[str] = field(default_factory=set)
    edit_fallback_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_block_counts: dict[str, int] = field(default_factory=dict)
    line_range_reread_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker_file_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    loaded_target_files: list[str] = field(default_factory=list)
    dispatched_target_files: list[str] = field(default_factory=list)
    worker_artifact_id: str = ""
    worker_artifact_item_id: str = ""
    patch_failed_cycles: dict[str, int] = field(default_factory=dict)
    patch_invalid_syntax_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    edit_retry_ledger: EditRetryLedger = field(default_factory=EditRetryLedger)
    write_attempts_by_path: dict[str, int] = field(default_factory=dict)
    worker_app_writes: set[str] = field(default_factory=set)

    # --- syntax / import validation ---
    syntax_repair_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    syntax_validation_required: set[str] = field(default_factory=set)
    explicit_validation_fingerprints: dict[str, str] = field(default_factory=dict)
    explicit_validation_edit_snapshot: int = 0

    def __post_init__(self) -> None:
        self.limits = ToolLimitState(mode=self.mode)
        self.stream_buffer = None
        self.worker_flow = None
        self.content_gate = None
        self.pre_edit_guard = None
        if self.mode == "worker":
            self.stream_buffer = WorkerStreamBuffer()
            self.worker_flow = WorkerFlowHarness()
        elif self.mode == "single":
            self.content_gate = SingleContentGate()
            if self.tool_effect is not None:
                self.pre_edit_guard = PreEditLoopGuard(
                    effect_lookup=self.tool_effect
                )
            else:
                self.pre_edit_guard = PreEditLoopGuard()

    # ── Outstanding production action ───────────────────────────────────

    def implementation_action_pending(self) -> bool:
        """Whether this turn still owes the production action it was routed for."""
        return implementation_action_pending(
            mode=self.mode,
            route=self.task_route,
            guard=self.pre_edit_guard,
            read_only=self.read_only,
        )

    def probes_complete_action(self) -> bool:
        """Whether a successful probe may mark this turn's action complete.

        A ``git_status``, ``run_diagnostic_command``, or terminal call is a
        *probe*: it inspects, it does not act.  On most turns treating a
        successful probe as the completed action is right — "show me the git
        status" is a turn whose whole point is the probe.  On an implementation
        turn before its first applied write it is simply false: the action is the
        edit, the edit has not happened, and nothing was completed.

        Believing it there had a concrete cost.  ``task_completion_context``
        would let one successful ``git_status`` before the first write end the
        turn as if the edit had happened, which is why an implementation turn
        before its first applied write answers ``False``.
        """
        return not self.implementation_action_pending()

    # ── Write-count helpers (honest signals from WorkerFlowHarness) ──

    def applied_write_count(self) -> int:
        """Return applied writes (``write_actions``), or 0 outside worker mode."""
        if self.worker_flow is not None:
            return self.worker_flow.state.write_actions
        return 0

    def write_attempt_count(self) -> int:
        """Return attempted writes (``write_intents``), or 0 outside worker mode."""
        if self.worker_flow is not None:
            return self.worker_flow.state.write_intents
        return 0

