"""Deterministic pre-edit loop guard for the SINGLE production runtime.

Two mechanical signals, both derived from state the send loop already keeps:

1. **Exact read fingerprints.** Before the first applied write, the same
   read-only tool call with the same arguments is rejected the second time it
   is issued.  The result is already in the conversation above; repeating it
   only feeds the model its own transcript again.
2. **Consecutive read-only rounds.** After
   :data:`MAX_READ_ONLY_ROUNDS_BEFORE_STEER` rounds that ran tools but produced
   no write and no terminal command, one concise internal steering message is
   injected — once per turn — telling the agent to use the evidence it has and
   implement.

A reread is legitimate, and is allowed, when the previous round had a tool
failure, when a stale-file notice invalidated that path, or while edit-recovery
state is pending.  Failures buy exactly one round of grace; stale notices clear
only the fingerprints for the paths they name.

There is deliberately no semantic classification of model output, no
planner/worker workflow, and no phase state machine here.  The 300-call
emergency brake in :mod:`aura.conversation.tool_limits` stays the final runaway
guard; this guard is the ordinary nudge that fires long before it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS

#: Reads whose exact repetition before the first edit carries no new evidence.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_files",
    "read_file_range",
    "read_file_outline",
    "list_directory",
    "glob",
    "grep_search",
    "find_usages",
    "search_codebase",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
})

#: Tools that count as forward progress and reset the read-only round counter.
PROGRESS_TOOLS: frozenset[str] = frozenset(WRITE_TOOLS | TERMINAL_TOOLS)

#: Rounds of tools-without-progress tolerated before one steering message.
MAX_READ_ONLY_ROUNDS_BEFORE_STEER: int = 4

DUPLICATE_READ_REASON = "duplicate_read_before_first_edit"
READ_ONLY_STALL_REASON = "read_only_rounds_before_first_edit"

_DUPLICATE_READ_MESSAGE = (
    "You already ran this exact call earlier in this turn and its result is "
    "still in the conversation above. Reading it again returns the same bytes "
    "and adds no evidence. Use what you already have and make the edit. "
    "Rereads after a failed tool call, a stale-file notice, or a pending "
    "edit-recovery step are allowed and are not blocked by this guard."
)

_STEERING_MESSAGE = (
    "Loop guard: {rounds} consecutive rounds ran read-only tools with no edit "
    "applied and no command run. You have the evidence you need. Do not "
    "re-read, restate the plan, or re-derive the decision — make the change "
    "now with write_file or patch_file, then validate it. If something "
    "genuinely blocks the edit, name that blocker in one sentence and stop."
)


def read_fingerprint(name: str, args: Any) -> str:
    """Return a stable identity for one read-only call and its arguments."""
    try:
        rendered = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    return f"{name}:{rendered}"


@dataclass
class PreEditLoopGuard:
    """Track read repetition and read-only rounds before the first write."""

    seen_reads: dict[str, int] = field(default_factory=dict)
    consecutive_read_only_rounds: int = 0
    write_applied: bool = False
    steered: bool = False
    blocked_calls: int = 0

    # Grace budget in rounds; >0 means a reread is currently justified.
    _grace_rounds: int = 0
    _round_had_tools: bool = False
    _round_made_progress: bool = False

    # ---- round lifecycle -------------------------------------------------

    def begin_round(self) -> None:
        self._round_had_tools = False
        self._round_made_progress = False

    def end_round(self) -> None:
        if self._grace_rounds > 0:
            self._grace_rounds -= 1
        if self._round_made_progress:
            self.consecutive_read_only_rounds = 0
        elif self._round_had_tools:
            self.consecutive_read_only_rounds += 1

    # ---- pre-execution gate ----------------------------------------------

    def check(
        self,
        name: str,
        args: Any,
        *,
        recovery_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Return a rejection payload for an unjustified exact repeat read.

        ``None`` means the call may run.  The guard is dormant once any write
        has applied: rereading to verify your own edit is normal work.
        """
        if self.write_applied or name not in READ_ONLY_TOOLS:
            return None
        if self._grace_rounds > 0 or recovery_pending:
            return None
        fingerprint = read_fingerprint(name, args)
        previous = self.seen_reads.get(fingerprint, 0)
        if previous < 1:
            return None
        self.blocked_calls += 1
        return {
            "ok": False,
            "loop_guard": True,
            "recoverable": True,
            "reason": DUPLICATE_READ_REASON,
            "tool": name,
            "previous_calls": previous,
            "message": _DUPLICATE_READ_MESSAGE,
        }

    def record(self, name: str, args: Any) -> None:
        """Record one accepted tool call for this round."""
        self._round_had_tools = True
        if name in READ_ONLY_TOOLS:
            fingerprint = read_fingerprint(name, args)
            self.seen_reads[fingerprint] = self.seen_reads.get(fingerprint, 0) + 1
        if name in PROGRESS_TOOLS:
            self._round_made_progress = True

    # ---- evidence that justifies a reread --------------------------------

    def observe_result(self, name: str, ok: bool, payload: Any = None) -> None:
        """Fold one tool result into the guard's state."""
        if not ok:
            self.note_failure()
            return
        if name in WRITE_TOOLS and _payload_applied(payload):
            self.write_applied = True
            self.consecutive_read_only_rounds = 0

    def note_failure(self) -> None:
        """A tool failed: the next round may reread whatever it needs."""
        self._grace_rounds = 2

    def note_stale_paths(self, paths: list[str] | tuple[str, ...]) -> None:
        """A stale-file notice landed: forget the reads that touched *paths*."""
        normalized = [
            str(path).replace("\\", "/").strip()
            for path in paths
            if str(path).strip()
        ]
        if not normalized:
            return
        for fingerprint in list(self.seen_reads):
            probe = fingerprint.replace("\\\\", "/").replace("\\", "/")
            if any(path and path in probe for path in normalized):
                del self.seen_reads[fingerprint]

    # ---- steering --------------------------------------------------------

    def take_steering_message(self) -> str:
        """Return the one steering message when it is due, else ``""``."""
        if self.write_applied or self.steered:
            return ""
        if self.consecutive_read_only_rounds < MAX_READ_ONLY_ROUNDS_BEFORE_STEER:
            return ""
        self.steered = True
        return _STEERING_MESSAGE.format(rounds=self.consecutive_read_only_rounds)


def _payload_applied(payload: Any) -> bool:
    """Return whether a write tool's result claims the change actually landed."""
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return True
    if not isinstance(data, dict):
        return True
    if "applied" in data:
        return bool(data["applied"])
    return True


__all__ = [
    "DUPLICATE_READ_REASON",
    "MAX_READ_ONLY_ROUNDS_BEFORE_STEER",
    "PROGRESS_TOOLS",
    "PreEditLoopGuard",
    "READ_ONLY_STALL_REASON",
    "READ_ONLY_TOOLS",
    "read_fingerprint",
]
