"""The production SINGLE trajectory lifecycle owner.

``ConversationManager`` owns the model/tool loop.  This module owns exactly one
thing the loop could not answer for itself: *is this real user turn still making
implementation progress, or has it turned into an unbounded investigation?*

The defect this exists to close
------------------------------

Production SINGLE deliberately has no round ceiling — a turn ends on an outcome,
not on arithmetic.  But "no ceiling" left one real gap.  ``PreEditLoopGuard``
rejects only an *exact* repeat whose original result is still resident, so a
different file, a different range, a different search term, or a genuinely new
piece of evidence always passed.  A prose-only response could not end the turn
while the implementation action was outstanding, so it was answered with
``_UNPROVEN_CONTINUATION`` and the model was called again — into the same
expanding chain.  The only backstop left was the ~300-call emergency guard,
which rejected further tools as ordinary recoverable tool results while prose
endings were still refused: every tool blocked, every ending blocked, nothing to
do.  The user saw a very long turn that never wrote anything.

What replaces it
----------------

An internal *trajectory segment*.  When a turn spends a segment observing
without implementation progress, that is an **internal maintenance condition**,
not an outcome: the harness retires the superseded observation trajectory,
rebuilds a deterministic continuation capsule from facts it already owns, and
starts a fresh segment for the *same* real user turn — same model, same thinking
mode, same stable tool catalog, same task, same durable evidence.  No user
message is created, no re-routing happens, nothing is handed back to the user.

Rollovers are **not rationed**.  A turn may roll over as often as the work needs;
there is deliberately no "one rollover then return unfinished work" rule,
because an internal segment boundary is not a terminal outcome and returning
unfinished work would be exactly the user-visible failure this repair removes.
The only bound is the remote runaway ceiling below.

The remote runaway ceiling
--------------------------

The pre-existing high emergency total (:data:`MAX_TOOL_CALLS_BY_MODE`) stays as
the total-runaway boundary, but its semantics are fixed: it is owned *here*, it
terminates the run directly as a harness/runtime failure, and it is never
reported as a task blocker, never asks the user to continue, and never produces
a state where every tool is rejected while prose endings are also refused.

Observation accounting
----------------------

Not "three reads", not "eight rounds", not "ten files".  The segment boundary is
a **token budget derived from the active model's working-set budget**, spent
against the actual size of newly accepted observation results:

    segment_allowance = working_set_tokens
                        * RECENT_EVIDENCE_FRACTION     (0.25)
                        * OBSERVATION_SEGMENT_TURNOVERS (4)

``RECENT_EVIDENCE_FRACTION`` is the share of the working set that
:mod:`aura.conversation.api_view` replays as *verbatim recent observation* — in
other words, how much raw looking the model can actually hold in front of it at
once.  The allowance is therefore ``OBSERVATION_SEGMENT_TURNOVERS`` complete
refills of that window: by the time it is spent, the turn has pushed four full
windows of observation evidence through the model and displaced it four times
over without once acting.  That is not a task that needs more reading; it is a
trajectory that has become dominated by observation, and a bigger model gets a
proportionally bigger allowance because the quantity being measured is the
model's own working set.

Deliberately *not* implementation progress: reading another file, reading
another range, another search term, another symbol, technically-new evidence,
compacting or retiring old evidence, or producing more analysis prose.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aura.conversation.api_view import RECENT_EVIDENCE_FRACTION
from aura.conversation.context_budget import ModelBudget
from aura.conversation.task_router import TaskRoute, route_bears_production_action
from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE

_log = logging.getLogger(__name__)

#: Complete refills of the model's resident recent-observation window that one
#: internal trajectory segment may spend before the segment is considered
#: dominated by observation. See the module docstring for the derivation.
OBSERVATION_SEGMENT_TURNOVERS: int = 4

#: Floor for the segment allowance, so a pathologically small resolved budget
#: cannot make the very first read trip the boundary.
MIN_OBSERVATION_SEGMENT_TOKENS: int = 2_048

#: Prose-only endings with an outstanding implementation action that one segment
#: absorbs before the segment is treated as nonconvergent.  The first gets the
#: cheap in-segment correction (``_UNPROVEN_CONTINUATION``); a second one in the
#: *same* segment proves the correction did not work, which the contract lists
#: as an internal maintenance condition to recover from — so it rolls over
#: rather than steering into the same chain again.  This is a convergence rule
#: for Aura's own steering, never an observation budget.
PROSE_NONCONVERGENCE_PER_SEGMENT: int = 1

#: The remote total-runaway boundary. Same number as the pre-existing emergency
#: guard — one source of truth — but classified and terminated here.
SINGLE_RUNAWAY_TOOL_CALL_CEILING: int = MAX_TOOL_CALLS_BY_MODE["single"]

#: Failure classification for the runaway boundary. Deliberately not ``blocked``:
#: nothing external stopped the work, so it must never reach the user as a task
#: blocker or a request to continue.
RUNAWAY_FAILURE_CLASS: str = "single_trajectory_runaway"

RUNAWAY_FAILURE_MESSAGE: str = (
    "Aura stopped this turn: the production run passed its total runaway "
    "protection limit without reaching an outcome. This is a harness failure, "
    "not a blocker in your task — every completed read, applied write, command "
    "result, and validation result from this turn is preserved above."
)


class TrajectoryDecision(str, Enum):
    """What the lifecycle owner wants the send loop to do next."""

    CONTINUE = "continue"
    """Stay in the current internal segment and issue the next request."""

    INTERNAL_ROLLOVER = "internal_rollover"
    """Retire the superseded trajectory and start a fresh internal segment for
    the same real user turn. Not a terminal outcome and not user-visible."""

    TERMINAL_HARNESS_FAILURE = "terminal_harness_failure"
    """Remote runaway boundary reached: end the run as a harness failure."""


def observation_targets(name: str, args: Any) -> tuple[str, ...]:
    """What one observation call looked at, for the continuation capsule.

    Reporting only — nothing branches on it.  Pulls the path-, symbol-, and
    query-shaped arguments Aura's observation tools actually use, so a rollover
    capsule can state what has already been surveyed without replaying any of
    the evidence.
    """
    if not isinstance(args, dict):
        return ()
    found: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in found:
            found.append(text.replace("\\", "/"))

    for key in ("path", "file", "file_path", "directory", "dir"):
        if key in args:
            add(args[key])
    for key in ("paths", "files"):
        value = args.get(key)
        if isinstance(value, (list, tuple)):
            for item in value:
                add(item)
    for key in ("symbol", "name", "query", "pattern", "search", "term"):
        if key in args:
            add(f"{name}:{args[key]}")
    return tuple(found)


@dataclass(frozen=True)
class RoundFacts:
    """Authoritative facts from one completed tool round.

    Every field is derived from results the round already had to compute; the
    controller invents nothing and re-reads no history.
    """

    #: Estimated tokens of *newly accepted* observation results, in the same
    #: unit the context ladder uses (``len // 4``). Rejected, duplicate-gated,
    #: and batch-rejected calls contribute nothing: they returned no evidence.
    observation_tokens: int = 0
    #: What those observations looked at, for the capsule.
    observation_targets: tuple[str, ...] = ()
    #: Paths whose write result explicitly proved the change applied.
    applied_write_paths: tuple[str, ...] = ()
    #: Paths of mutation attempts whose authoritative result failed or was
    #: explicitly not applied — a concrete edit-recovery state. This is
    #: implementation movement, not more observation.
    edit_recovery_paths: tuple[str, ...] = ()
    #: A command/terminal call actually executed this round.
    command_executed: bool = False
    #: A structured ``report_blocker`` succeeded.
    blocker_succeeded: bool = False
    #: A structured ``report_already_satisfied`` succeeded.
    already_satisfied_succeeded: bool = False

    def bears_implementation_progress(self) -> bool:
        """Whether this round moved the implementation forward.

        The exact definition, and the whole of it: an applied mutation, a
        mutation attempt whose authoritative result created concrete
        edit-recovery state, a successful structured already-satisfied report,
        or a successful structured blocker.
        """
        return bool(
            self.applied_write_paths
            or self.edit_recovery_paths
            or self.blocker_succeeded
            or self.already_satisfied_succeeded
        )


@dataclass
class SingleTrajectoryController:
    """Trajectory progress, internal rollover, and runaway classification.

    Owns *only* those three things.  It does not run the model, execute tools,
    compact context, judge completion truth, or schedule work: the send loop,
    the tool round, :mod:`aura.conversation.api_view`, the completion contract,
    and the display-only worker TODO all keep their existing roles unchanged.
    """

    #: The real user request driving this turn — replayed verbatim into every
    #: capsule so the original request stays authoritative across segments.
    user_request: str = ""
    #: The turn's authoritative route. Read, never recomputed, never re-derived
    #: from Aura's own capsule text.
    route: TaskRoute | None = None
    #: Whether pre-mutation trajectory accounting applies at all. False for
    #: read-only registries and for routes whose production action is itself
    #: observation, command execution, or prose — those turns are never pushed
    #: toward a mutation, and only the runaway ceiling still applies to them.
    engaged: bool = False
    #: Token allowance for one internal segment, derived from the active model's
    #: working-set budget. See the module docstring.
    segment_allowance_tokens: int = MIN_OBSERVATION_SEGMENT_TOKENS
    #: The resolved budget the allowance came from, for logging.
    budget_working_set_tokens: int = 0

    # ---- per-segment accounting ------------------------------------------
    segment_index: int = 1
    rollovers: int = 0
    observation_tokens_since_progress: int = 0
    prose_nonconvergence_in_segment: int = 0

    # ---- durable turn state (survives every rollover) --------------------
    accepted_tool_calls: int = 0
    implementation_progress_events: int = 0
    applied_write_paths: list[str] = field(default_factory=list)
    edit_recovery_paths: list[str] = field(default_factory=list)
    observed_targets: list[str] = field(default_factory=list)
    commands_run: int = 0
    structured_terminal: str = ""
    terminated_for_runaway: bool = False

    # ---- construction -----------------------------------------------------

    @classmethod
    def for_turn(
        cls,
        *,
        mode: str,
        read_only: bool,
        route: TaskRoute | None,
        user_request: str,
        budget: ModelBudget,
    ) -> "SingleTrajectoryController":
        """Build the lifecycle owner for one production SINGLE user turn."""
        engaged = (
            mode == "single"
            and not read_only
            and route_bears_production_action(route)
        )
        allowance = max(
            MIN_OBSERVATION_SEGMENT_TOKENS,
            int(
                budget.working_set_tokens
                * RECENT_EVIDENCE_FRACTION
                * OBSERVATION_SEGMENT_TURNOVERS
            ),
        )
        return cls(
            user_request=user_request,
            route=route,
            engaged=engaged,
            segment_allowance_tokens=allowance,
            budget_working_set_tokens=budget.working_set_tokens,
        )

    # ---- accounting -------------------------------------------------------

    def note_tool_round(self, facts: RoundFacts) -> None:
        """Fold one completed tool round's authoritative facts into the segment."""
        for target in facts.observation_targets:
            if target not in self.observed_targets:
                self.observed_targets.append(target)
        for path in facts.applied_write_paths:
            if path not in self.applied_write_paths:
                self.applied_write_paths.append(path)
        for path in facts.edit_recovery_paths:
            if path not in self.edit_recovery_paths:
                self.edit_recovery_paths.append(path)
        if facts.command_executed:
            self.commands_run += 1
        if facts.blocker_succeeded:
            self.structured_terminal = "blocker"
        elif facts.already_satisfied_succeeded:
            self.structured_terminal = "already_satisfied"

        if facts.bears_implementation_progress():
            # Implementation movement resets pre-mutation trajectory accounting:
            # an applied write, or a write attempt that entered concrete edit
            # recovery, ends the observation segment that preceded it.
            self.implementation_progress_events += 1
            self.observation_tokens_since_progress = 0
            self.prose_nonconvergence_in_segment = 0
            return

        self.observation_tokens_since_progress += max(0, facts.observation_tokens)

    def note_accepted_tool_calls(self, total: int) -> None:
        """Record the turn's total accepted tool usage for the runaway ceiling."""
        self.accepted_tool_calls = max(self.accepted_tool_calls, int(total))

    def note_prose_nonconvergence(self) -> None:
        """A prose-only response arrived while the implementation action is owed."""
        self.prose_nonconvergence_in_segment += 1

    # ---- decisions --------------------------------------------------------

    def decide(self) -> TrajectoryDecision:
        """Return what the send loop should do at this boundary.

        The runaway ceiling is checked first and applies to every production
        SINGLE turn, engaged or not: it is a harness protection, not a
        pre-mutation policy.
        """
        if self.accepted_tool_calls >= SINGLE_RUNAWAY_TOOL_CALL_CEILING:
            self.terminated_for_runaway = True
            return TrajectoryDecision.TERMINAL_HARNESS_FAILURE
        if not self.engaged:
            return TrajectoryDecision.CONTINUE
        if self.structured_terminal:
            # A structured blocker or already-satisfied result already
            # succeeded; the turn owes its one final response, not a rollover.
            return TrajectoryDecision.CONTINUE
        if self.observation_tokens_since_progress > self.segment_allowance_tokens:
            return TrajectoryDecision.INTERNAL_ROLLOVER
        if self.prose_nonconvergence_in_segment > PROSE_NONCONVERGENCE_PER_SEGMENT:
            return TrajectoryDecision.INTERNAL_ROLLOVER
        return TrajectoryDecision.CONTINUE

    def begin_new_segment(self) -> None:
        """Open a fresh internal segment for the same real user turn.

        Durable state — the request, the route, observed targets, applied
        writes, edit-recovery paths, command count, structured terminal state,
        and total accepted tool usage — is deliberately *not* reset: it is what
        the capsule is rebuilt from and what the next segment continues with.
        """
        self.rollovers += 1
        self.segment_index += 1
        self.observation_tokens_since_progress = 0
        self.prose_nonconvergence_in_segment = 0

    # ---- the internal continuation capsule --------------------------------

    def capsule_text(self) -> str:
        """Deterministic internal continuation capsule for the next segment.

        Built only from facts Aura already owns.  It is appended as an internal
        message carrying
        :data:`~aura.conversation.api_view.TRAJECTORY_ROLLOVER_MARKER`, so it is
        structurally distinguishable from a real user message: task routing does
        not run again from Aura's own text, research policy does not change, the
        user transcript stays truthful, and the original request below remains
        the authoritative one.
        """
        route_line = "unrouted"
        if self.route is not None:
            route_line = f"{self.route.lane.value} / {self.route.action}"

        lines: list[str] = [
            "[Aura internal trajectory rollover — not a user message, not a new "
            "request, and not a task outcome.]",
            "",
            "This turn's investigation consumed an internal trajectory segment "
            "without implementation progress, so the superseded observation "
            "detail above has been retired into the evidence ledger and a fresh "
            "internal segment was started. Nothing about the task changed. "
            "Continue the same original request below, in this same turn, with "
            "the same tools.",
            "",
            f"Original user request (authoritative): {self.user_request.strip()}",
            f"Selected task route: {route_line}",
            f"Internal segment: {self.segment_index + 1} (rollovers so far: "
            f"{self.rollovers + 1})",
        ]

        if self.engaged and not self.structured_terminal:
            lines.append(
                "Outstanding implementation action: the requested change has "
                "not been applied. Act on the evidence you already have — "
                "apply the edit, or call report_blocker / "
                "report_already_satisfied with real evidence."
            )

        if self.observed_targets:
            shown = self.observed_targets[:40]
            lines.append("")
            lines.append(
                "Already observed this turn (do not re-survey these to "
                "rediscover the same facts; retired detail is summarised in the "
                "evidence ledger above):"
            )
            for target in shown:
                lines.append(f"  - {target}")
            if len(self.observed_targets) > len(shown):
                lines.append(f"  - … and {len(self.observed_targets) - len(shown)} more")

        if self.applied_write_paths:
            lines.append("")
            lines.append("Writes already applied this turn (still applied):")
            for path in self.applied_write_paths:
                lines.append(f"  - {path}")

        if self.edit_recovery_paths:
            lines.append("")
            lines.append(
                "Write attempts that failed and require edit recovery "
                "(re-read the current bytes before re-proposing):"
            )
            for path in self.edit_recovery_paths:
                lines.append(f"  - {path}")

        if self.commands_run:
            lines.append("")
            lines.append(
                f"Commands/validation runs executed this turn: {self.commands_run} "
                "(their results are above or in the evidence ledger)."
            )

        if self.structured_terminal:
            lines.append("")
            lines.append(
                f"Structured terminal state already recorded: {self.structured_terminal}."
            )

        return "\n".join(lines)

    # ---- observability ----------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        """Structured state for logs and receipts."""
        return {
            "engaged": self.engaged,
            "segment_index": self.segment_index,
            "rollovers": self.rollovers,
            "segment_allowance_tokens": self.segment_allowance_tokens,
            "budget_working_set_tokens": self.budget_working_set_tokens,
            "observation_tokens_since_progress": self.observation_tokens_since_progress,
            "prose_nonconvergence_in_segment": self.prose_nonconvergence_in_segment,
            "implementation_progress_events": self.implementation_progress_events,
            "accepted_tool_calls": self.accepted_tool_calls,
            "runaway_ceiling": SINGLE_RUNAWAY_TOOL_CALL_CEILING,
            "applied_writes": len(self.applied_write_paths),
            "edit_recovery_paths": len(self.edit_recovery_paths),
            "observed_targets": len(self.observed_targets),
            "commands_run": self.commands_run,
            "structured_terminal": self.structured_terminal,
            "terminated_for_runaway": self.terminated_for_runaway,
        }

    def log_snapshot(self, event: str) -> None:
        _log.info(
            "single_trajectory %s %s",
            event,
            json.dumps(self.snapshot(), sort_keys=True, ensure_ascii=False),
        )


__all__ = [
    "MIN_OBSERVATION_SEGMENT_TOKENS",
    "OBSERVATION_SEGMENT_TURNOVERS",
    "PROSE_NONCONVERGENCE_PER_SEGMENT",
    "RUNAWAY_FAILURE_CLASS",
    "RUNAWAY_FAILURE_MESSAGE",
    "SINGLE_RUNAWAY_TOOL_CALL_CEILING",
    "RoundFacts",
    "SingleTrajectoryController",
    "TrajectoryDecision",
    "observation_targets",
]
