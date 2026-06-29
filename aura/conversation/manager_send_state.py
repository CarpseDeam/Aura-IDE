"""Per-send loop state for ConversationManager.send().

Holds all the mutable variables that track progress, recovery, and validation
through one invocation of the model/tool loop.  Extracted so that send() starts
with a compact, readable state setup instead of a wall of local declarations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.conversation.edit_orchestrator import EditRetryLedger
from aura.conversation.tool_limits import ToolLimitState
from aura.conversation.worker_flow import WorkerFlowHarness
from aura.conversation.worker_stream_buffer import WorkerStreamBuffer


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

    # --- per-round state ---
    reject_all_for_turn: bool = False
    rounds_used: int = 0
    task_completion_context: bool = False
    final_messages_after_completion: int = 0
    last_completion_final_text: str = ""

    # --- worker-only objects (initialised in __post_init__) ---
    limits: ToolLimitState = field(init=False)
    stream_buffer: WorkerStreamBuffer | None = field(init=False)
    worker_flow: WorkerFlowHarness | None = field(init=False)

    # --- worker guard / quarantine ---
    candidate_final_message: dict[str, Any] | None = None
    worker_needs_final_report: bool = False
    worker_phase_boundary_info: dict[str, Any] | None = None
    worker_recovery_nudge_sent: bool = False
    worker_validation_nudge_sent: bool = False
    stale_validation_notes: list[str] = field(default_factory=list)

    # --- dispatch ---
    worker_redispatches: int = 0
    worker_dispatch_failures: dict[str, int] = field(default_factory=dict)

    # --- edit recovery ---
    edit_failed_shapes: set[str] = field(default_factory=set)
    edit_fallback_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    recovery_block_counts: dict[str, int] = field(default_factory=dict)
    line_range_reread_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    worker_file_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    patch_failed_cycles: dict[str, int] = field(default_factory=dict)
    patch_invalid_syntax_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    edit_retry_ledger: EditRetryLedger = field(default_factory=EditRetryLedger)
    write_attempts_by_path: dict[str, int] = field(default_factory=dict)
    worker_app_writes: set[str] = field(default_factory=set)

    # --- syntax / import validation ---
    syntax_repair_required: dict[str, dict[str, Any]] = field(default_factory=dict)
    syntax_validation_required: set[str] = field(default_factory=set)
    explicit_validation_failure_counts: dict[str, int] = field(default_factory=dict)
    import_verification_required: set[str] = field(default_factory=set)

    # --- launch / dependency fingerprints (skip-optimisation) ---
    last_launch_ok_fingerprint: str | None = None
    last_dependent_ok_fingerprint: str | None = None

    def __post_init__(self) -> None:
        self.limits = ToolLimitState(mode=self.mode)
        self.stream_buffer = None
        self.worker_flow = None
        if self.mode == "worker":
            self.stream_buffer = WorkerStreamBuffer()
            self.worker_flow = WorkerFlowHarness()

    def discard_worker_candidate_final(self) -> None:
        """Clear the quarantined final message and the stream buffer."""
        self.candidate_final_message = None
        if self.stream_buffer is not None:
            self.stream_buffer.discard()
