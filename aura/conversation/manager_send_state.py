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
        if self.mode == "worker":
            self.stream_buffer = WorkerStreamBuffer()
            self.worker_flow = WorkerFlowHarness()

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

