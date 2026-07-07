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
from aura.conversation.validation_ledger import WorkerValidationLedger
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
    planner_dispatch_gate_steered: bool = False

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
    worker_final_report_proof_nudge_sent: bool = False
    worker_flow_nudge_count: int = 0
    worker_flow_zero_work_recovery_count: int = 0
    worker_flow_thrash_recovery_count: int = 0
    worker_flow_last_steering: str = ""
    worker_flow_last_reason: str = ""
    worker_quality_nudge_sent: bool = False
    worker_quality_cleanup_attempted: bool = False
    critic_pass_attempted: bool = False
    last_quality_ok_fingerprint: str | None = None
    last_quality_findings: list[dict[str, Any]] = field(default_factory=list)
    worker_quality_enabled: bool = True
    stale_validation_notes: list[str] = field(default_factory=list)
    explicit_validation_passed_at_writes: int | None = None
    """Write-snapshot value when explicit validation last passed, or ``None``."""
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
    import_verification_required: set[str] = field(default_factory=set)

    # --- launch / dependency fingerprints (skip-optimisation) ---
    last_launch_ok_fingerprint: str | None = None
    last_dependent_ok_fingerprint: str | None = None
    last_structural_ok_fingerprint: str | None = None

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

    @property
    def worker_flow_nudge_sent(self) -> bool:
        return self.worker_flow_nudge_count > 0

    @worker_flow_nudge_sent.setter
    def worker_flow_nudge_sent(self, value: bool) -> None:
        self.worker_flow_nudge_count = max(1, self.worker_flow_nudge_count) if value else 0

    @property
    def worker_flow_zero_work_recovery_sent(self) -> bool:
        return self.worker_flow_zero_work_recovery_count > 0

    @worker_flow_zero_work_recovery_sent.setter
    def worker_flow_zero_work_recovery_sent(self, value: bool) -> None:
        self.worker_flow_zero_work_recovery_count = (
            max(1, self.worker_flow_zero_work_recovery_count) if value else 0
        )

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

    def mark_explicit_validation_passed(self) -> None:
        """Record that explicit validation passed at the current applied-write count."""
        self.explicit_validation_passed_at_writes = self.applied_write_count()

    @property
    def worker_explicit_validation_passed(self) -> bool:
        """``True`` when explicit validation passed and no applied writes since.

        Read-only: the backing field ``explicit_validation_passed_at_writes``
        is set by ``mark_explicit_validation_passed()``.  Any direct assignment
        ``state.worker_explicit_validation_passed = ...`` will raise
        ``AttributeError`` — use the method instead.
        """
        if self.explicit_validation_passed_at_writes is None:
            return False
        return self.explicit_validation_passed_at_writes == self.applied_write_count()
