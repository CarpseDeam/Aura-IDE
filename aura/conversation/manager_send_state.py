"""Per-send loop state for ConversationManager.send().

Holds all the mutable variables that track progress, recovery, and validation
through one invocation of the model/tool loop.  Extracted so that send() starts
with a compact, readable state setup instead of a wall of local declarations.

This module also owns the **implementation discovery stage** — the per-turn
authority that bounds how many *ordinary* model requests a production SINGLE
implementation turn may spend before the first applied write.  See
:class:`ImplementationStage`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from aura.conversation.edit_orchestrator import EditRetryLedger
from aura.conversation.focused_action import FocusedActionState
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.single_content_gate import SingleContentGate
from aura.conversation.task_router import TaskLane, TaskRoute
from aura.conversation.tool_limits import ToolLimitState
from aura.conversation.tools.effects import ToolEffect
from aura.conversation.validation_ledger import WorkerValidationLedger
from aura.conversation.worker_flow import WorkerFlowHarness
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer


class ImplementationStage(str, Enum):
    """How many ordinary discovery requests an implementation turn has left.

    :class:`~aura.conversation.pre_edit_loop_guard.PreEditLoopGuard` bounds
    *repetition* — the same read twice, a round that gathered nothing.  It
    deliberately never bounds *novelty*: a turn whose every call returns
    genuinely new evidence is, by the guard's own rule, always still making
    progress, so ``guard.focused`` never fires and the send loop keeps opening
    another ordinary reasoning stream.  That is the production loop this stage
    closes: endless technically-novel discovery.

    The stage is a second, independent authority over the same transition.  An
    implementation turn gets exactly **two ordinary discovery hops** before the
    first applied write:

    * :attr:`SURVEY` — the first ordinary request.  Ordinary thinking level,
      ordinary tool catalog.  It may edit immediately, or batch as many reads,
      searches, Git inspections, diagnostics, commands, and TODO calls as it
      likes.
    * :attr:`FINAL_EVIDENCE` — one more ordinary request, carrying the ephemeral
      protocol instruction (see
      :data:`~aura.conversation.focused_action.FINAL_EVIDENCE_INSTRUCTION`).
      Act now, or batch every remaining exact read in this one response.
    * :attr:`EXHAUSTED` — ordinary discovery is over.  The next request is the
      existing focused action request.

    **What consumes a stage.**  Exactly one thing: a structurally valid tool-call
    round in which at least one tool *actually executed*.  Whether those tools
    succeeded is irrelevant — a stage is a discovery *hop*, and a hop that ran
    and failed still spent the model's opportunity to look.  Nothing that failed
    to execute consumes anything: a preflight / schema / exposure / limit /
    guard rejection rejects the whole batch before execution, and an API error
    or cancellation never reaches the tool round at all.

    Existing failure recovery still does its job *inside* the remaining stages —
    it justifies rereads and corrected commands — but it can no longer add an
    ordinary stage or postpone the focused transition.  After two executed
    non-writing rounds the next request is action-only even if both rounds
    failed; ``report_blocker`` is the truthful answer when the evidence gathered
    is genuinely insufficient.

    **The accepted tradeoff.**  Unlimited batching *within* a hop, but only two
    sequential hops.  A task that genuinely needs a third sequential discovery
    hop — where the file to edit is only identifiable after reading something
    found in the second hop — will reach focused action under-informed and
    produce a visible structured blocker.  That is the intended outcome: a
    blocker the user can see and answer beats a turn that circles silently
    forever.
    """

    SURVEY = "survey"
    FINAL_EVIDENCE = "final_evidence"
    EXHAUSTED = "exhausted"


def implementation_staging_applies(
    *,
    mode: str,
    route: TaskRoute | None,
    guard: PreEditLoopGuard | None,
    read_only: bool,
) -> bool:
    """Whether the discovery stage governs this turn at this moment.

    Pure over state the send loop already owns, and deliberately the same four
    facts the protocol is specified against: production ``single`` mode, the
    deterministic ``implementation`` lane, no applied write yet, and a registry
    that is not read-only.  A read-only registry exposes no mutation tools, so
    there is no action to serialize and nothing to bound; those turns keep their
    existing unbounded-discovery behaviour exactly.
    """
    if mode != "single" or read_only:
        return False
    if route is None or route.lane != TaskLane.implementation:
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
    """Deterministic repeat-read / stagnant-discovery guard before the first write."""

    focused_action: FocusedActionState = field(default_factory=FocusedActionState)
    """The one action-serialization request this turn may spend."""

    implementation_stage: ImplementationStage = ImplementationStage.SURVEY
    """How many ordinary discovery requests this implementation turn has left.

    Advanced only by :meth:`consume_implementation_stage`, only when a tool
    round actually executed.  Meaningless outside a production SINGLE
    implementation turn — :func:`implementation_staging_applies` gates every
    read of it.
    """

    # --- worker recovery state ---
    worker_flow_last_steering: str = ""
    worker_flow_last_reason: str = ""
    stale_validation_notes: list[str] = field(default_factory=list)
    validation_ledger: WorkerValidationLedger = field(
        default_factory=WorkerValidationLedger
    )


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

    # ── Implementation discovery stage ──────────────────────────────────

    def implementation_staging_active(self, *, read_only: bool) -> bool:
        """Whether the discovery stage governs this turn right now."""
        return implementation_staging_applies(
            mode=self.mode,
            route=self.task_route,
            guard=self.pre_edit_guard,
            read_only=read_only,
        )

    def implementation_stage_exhausted(self, *, read_only: bool) -> bool:
        """Whether ordinary discovery is spent and the next request must act.

        One half of the focused transition authority:
        ``focused = stalled_round OR implementation_stage_exhausted``.  The
        guard's own stall rule remains untouched and still fires on its own
        terms; this is the independent bound on novelty.
        """
        return (
            self.implementation_stage is ImplementationStage.EXHAUSTED
            and self.implementation_staging_active(read_only=read_only)
        )

    def awaiting_final_evidence_request(self, *, read_only: bool) -> bool:
        """Whether the next ordinary request is the last one, and must say so."""
        return (
            self.implementation_stage is ImplementationStage.FINAL_EVIDENCE
            and self.implementation_staging_active(read_only=read_only)
        )

    def consume_implementation_stage(self, *, executed: bool, read_only: bool) -> bool:
        """Spend one ordinary discovery hop; return whether the stage moved.

        Called once per tool round, after that round's results have been folded
        into history, so the stage advances on a completed hop rather than on an
        intention to take one.

        ``executed`` must be the honest answer to "did at least one tool in this
        round actually run", *not* "did the round succeed".  A round whose tools
        ran and failed consumes its stage: the model still spent a discovery hop,
        and existing failure recovery gets to use what remains rather than
        minting a fresh one.  A batch rejected before execution — preflight,
        schema, exposure, limit, or loop-guard — executed nothing and so consumes
        nothing, and neither does an API error or a cancellation, which never
        reach this point.

        An applied write leaves the protocol entirely:
        :func:`implementation_staging_applies` goes false the moment
        ``guard.write_applied`` is set, so the stage stops being consulted and
        every existing post-write path runs unchanged.
        """
        if not executed:
            return False
        if not self.implementation_staging_active(read_only=read_only):
            return False
        if self.implementation_stage is ImplementationStage.SURVEY:
            self.implementation_stage = ImplementationStage.FINAL_EVIDENCE
            return True
        if self.implementation_stage is ImplementationStage.FINAL_EVIDENCE:
            self.implementation_stage = ImplementationStage.EXHAUSTED
            return True
        return False

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

