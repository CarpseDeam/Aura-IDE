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

An internal *trajectory segment*.  When a turn spends a segment without
implementation progress, that is an **internal maintenance condition**, not an
outcome: the harness retires the superseded observation trajectory, rebuilds a
deterministic continuation capsule from facts it already owns, and starts a
fresh segment for the *same* real user turn — same model, same thinking mode,
same stable tool catalog, same task, same durable evidence.  No user message is
created, no re-routing happens, nothing is handed back to the user.

Rollovers are **not rationed**.  A turn may roll over as often as the work needs;
there is deliberately no "one rollover then return unfinished work" rule,
because an internal segment boundary is not a terminal outcome and returning
unfinished work would be exactly the user-visible failure this repair removes.
The only bound is the remote runaway ceiling below.

What a segment is spent on: repeated model requests
---------------------------------------------------

Not "three reads", not "eight rounds", not "ten files" — and deliberately *not*
the size of the tool results either.  The expensive, self-reinforcing part of a
wandering trajectory is the **repeated model sampling request**: every extra
round resends the whole growing outbound view plus the full tool schema and pays
for another round of model output and reasoning.  A hundred tiny reads are not
cheap because their payloads are small; they are a hundred progressively larger
requests.

So the segment is spent against ``request_tokens`` — the same authoritative
value :func:`ConversationManager.send` already computes immediately before each
request (compacted outbound view + serialised tool schema):

    segment_allowance = working_set_tokens
                        * RECENT_EVIDENCE_FRACTION  (0.25)
                        * SEGMENT_REQUEST_TURNOVERS (4)

    spend(segment)     = sum(request_tokens of every request in the segment
                             *after* the first)

The first request of a segment establishes the baseline and is free: opening a
segment is not wandering continuation, and charging it would make a rollover
able to trigger another rollover immediately.  Every later request in the
segment charges its full ``request_tokens``.  ``RECENT_EVIDENCE_FRACTION`` is the
share of the working set that :mod:`aura.conversation.api_view` replays as
verbatim recent observation, so the allowance is four complete resends of the
window the model can actually hold in front of it — after which the turn has
paid for its investigation four times over without once acting.  A bigger model
gets a proportionally bigger allowance, because the quantity being measured is
the model's own working set.

Observation-result tokens are still recorded, but as **telemetry only**: they
appear in the snapshot and the logs and no decision reads them.

Deliberately *not* implementation progress: reading another file, reading
another range, another search term, another symbol, technically-new evidence,
compacting or retiring old evidence, or producing more analysis prose.

Cross-segment resurvey
----------------------

Natural compaction legitimately makes an old result unavailable, and rereading
it is then recovering lost context — that is ``PreEditLoopGuard``'s residency
rule and it is untouched.  A *deliberate* rollover is different: Aura retired
that investigation precisely because it was not converging, so re-issuing the
identical broad survey it just retired is not new work.  This module keeps the
successful broad-observation fingerprints of superseded segments and refuses an
exact repeat of one whose target the capsule already names, through a single
explicit seam on the guard.  Failed calls, changed targets, edit recovery, and
genuinely narrower bounded reads are all still allowed.

The remote runaway ceiling
--------------------------

The pre-existing high emergency total (:data:`MAX_TOOL_CALLS_BY_MODE`) stays as
the total-runaway boundary, but its semantics are fixed: it is owned *here*, it
terminates the run directly as a harness/runtime failure, and it is never
reported as a task blocker, never asks the user to continue, and never produces
a state where every tool is rejected while prose endings are also refused.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aura.conversation.api_view import RECENT_EVIDENCE_FRACTION
from aura.conversation.context_budget import ModelBudget
from aura.conversation.pre_edit_loop_guard import is_narrow_read, read_fingerprint
from aura.conversation.task_router import TaskRoute, route_bears_production_action
from aura.conversation.tool_limits import MAX_TOOL_CALLS_BY_MODE

_log = logging.getLogger(__name__)

#: Complete resends of the model's resident recent-observation window that one
#: internal trajectory segment may pay for before the segment is considered
#: nonconvergent. See the module docstring for the derivation.
SEGMENT_REQUEST_TURNOVERS: int = 4

#: Historical name for :data:`SEGMENT_REQUEST_TURNOVERS`, kept so existing
#: imports keep resolving. The quantity is the same; what it is spent against
#: changed from result bytes to repeated request cost.
OBSERVATION_SEGMENT_TURNOVERS: int = SEGMENT_REQUEST_TURNOVERS

#: Floor for the segment allowance, so a pathologically small resolved budget
#: cannot make the second request of a segment trip the boundary.
MIN_SEGMENT_ALLOWANCE_TOKENS: int = 2_048

#: Historical name for :data:`MIN_SEGMENT_ALLOWANCE_TOKENS`.
MIN_OBSERVATION_SEGMENT_TOKENS: int = MIN_SEGMENT_ALLOWANCE_TOKENS

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

#: Rejection reason for re-issuing a broad survey that a superseded internal
#: segment already ran successfully.
SUPERSEDED_SEGMENT_READ_REASON: str = "superseded_segment_resurvey"

_SUPERSEDED_SEGMENT_READ_MESSAGE = (
    "You already ran this exact broad survey earlier in this turn, in an "
    "internal segment that was retired because it was not converging. Its "
    "findings are retained in the continuation capsule and the evidence ledger "
    "above, so running it again returns the same survey and restarts the same "
    "investigation. Act on the evidence you already have — apply the edit, or "
    "narrow to a concrete unresolved edit surface (a bounded range of one "
    "specific file). Rereads after a failed call, after a write changed the "
    "file, or while an edit-recovery step is pending are not affected by this."
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

    Pulls the path-, symbol-, and query-shaped arguments Aura's observation
    tools actually use, so a rollover capsule can state what has already been
    surveyed without replaying any of the evidence — and so the cross-segment
    resurvey rule can tell whether a repeated call's target is one the capsule
    already names.
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
class EditRecoveryEvent:
    """One authoritative edit-recovery requirement created by a failed mutation.

    A failed write is implementation movement only when the runtime actually
    created or advanced concrete recovery state, and such an event must be able
    to name all three of these.  A rejected approval, a malformed call, an
    internal harness exception, an unavailable tool, or a batch rejection
    creates none of them and therefore produces no event.
    """

    #: The affected workspace path, normalised with forward slashes.
    path: str
    #: The concrete recovery requirement the runtime recorded, named after the
    #: recovery owner that recorded it (``line_range_reread``,
    #: ``edit_fallback``, ``syntax_repair``, ``patch_invalid_syntax``,
    #: ``syntax_validation``).
    requirement: str
    #: The authoritative failure class from the write tool's own result that
    #: created the requirement.
    failure_class: str

    def describe(self) -> str:
        return f"{self.path} — {self.requirement} (from {self.failure_class})"


@dataclass(frozen=True)
class RoundFacts:
    """Authoritative facts from one completed tool round.

    Every field is derived from results the round already had to compute; the
    controller invents nothing and re-reads no history.
    """

    #: Estimated tokens of *newly accepted* observation results, in the same
    #: unit the context ladder uses (``len // 4``). **Telemetry only** — no
    #: decision reads it; the segment is spent against repeated request cost.
    observation_tokens: int = 0
    #: What those observations looked at, for the capsule.
    observation_targets: tuple[str, ...] = ()
    #: Read fingerprints of successful *broad* observation calls this round —
    #: the input to the cross-segment resurvey rule. Narrow bounded reads are
    #: deliberately excluded: narrowing is how the model is meant to converge.
    broad_observation_fingerprints: tuple[str, ...] = ()
    #: Paths whose write result explicitly proved the change applied.
    applied_write_paths: tuple[str, ...] = ()
    #: Paths a write or stale-file notice changed this round. Reading them again
    #: returns different bytes, so any retained fingerprint touching them is
    #: forgotten by both this owner and the duplicate guard.
    stale_paths: tuple[str, ...] = ()
    #: Authoritative edit-recovery requirements created this round.
    edit_recovery_events: tuple[EditRecoveryEvent, ...] = ()
    #: A command/terminal call actually executed this round.
    command_executed: bool = False
    #: A structured ``report_blocker`` succeeded.
    blocker_succeeded: bool = False
    #: A structured ``report_already_satisfied`` succeeded.
    already_satisfied_succeeded: bool = False

    @property
    def edit_recovery_paths(self) -> tuple[str, ...]:
        """Paths of the recovery requirements created this round."""
        return tuple(event.path for event in self.edit_recovery_events)

    def bears_implementation_progress(self) -> bool:
        """Whether this round moved the implementation forward.

        The exact definition, and the whole of it: an applied mutation, a
        mutation attempt whose authoritative result *created concrete
        edit-recovery state* in one of the runtime's recovery owners, a
        successful structured already-satisfied report, or a successful
        structured blocker.

        A write that merely failed is not enough.  An approval the user
        rejected, a malformed or unexposed call, an internal harness exception,
        a batch rejection, and a limit rejection all leave the implementation
        exactly where it was, and none of them may reset the segment.
        """
        return bool(
            self.applied_write_paths
            or self.edit_recovery_events
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
    #: Request-spend allowance for one internal segment, derived from the active
    #: model's working-set budget. See the module docstring.
    segment_allowance_tokens: int = MIN_SEGMENT_ALLOWANCE_TOKENS
    #: The resolved budget the allowance came from, for logging.
    budget_working_set_tokens: int = 0

    # ---- per-segment accounting ------------------------------------------
    segment_index: int = 1
    rollovers: int = 0
    #: Model requests issued in the current segment, including the free first.
    segment_requests: int = 0
    #: ``request_tokens`` of the segment's first request. Establishes the
    #: baseline; never charged.
    segment_baseline_request_tokens: int = 0
    #: Charged ``request_tokens`` of every *later* request in the segment.
    segment_request_spend_tokens: int = 0
    #: Observation-result tokens since the last progress event. Telemetry only.
    observation_tokens_since_progress: int = 0
    prose_nonconvergence_in_segment: int = 0

    # ---- durable turn state (survives every rollover) --------------------
    accepted_tool_calls: int = 0
    implementation_progress_events: int = 0
    applied_write_paths: list[str] = field(default_factory=list)
    edit_recovery_events: list[EditRecoveryEvent] = field(default_factory=list)
    observed_targets: list[str] = field(default_factory=list)
    commands_run: int = 0
    structured_terminal: str = ""
    terminated_for_runaway: bool = False
    #: Successful broad-observation fingerprints issued in the *current*
    #: segment. Promoted to :attr:`retired_observation_fingerprints` on rollover.
    segment_observation_fingerprints: set[str] = field(default_factory=set)
    #: Successful broad-observation fingerprints from segments this turn has
    #: deliberately retired — the input to the cross-segment resurvey rule.
    retired_observation_fingerprints: set[str] = field(default_factory=set)
    #: How many resurveys of a retired segment were refused. Telemetry.
    superseded_resurveys_blocked: int = 0

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
            MIN_SEGMENT_ALLOWANCE_TOKENS,
            int(
                budget.working_set_tokens
                * RECENT_EVIDENCE_FRACTION
                * SEGMENT_REQUEST_TURNOVERS
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
        for fingerprint in facts.broad_observation_fingerprints:
            self.segment_observation_fingerprints.add(fingerprint)
        for path in facts.applied_write_paths:
            if path not in self.applied_write_paths:
                self.applied_write_paths.append(path)
        for event in facts.edit_recovery_events:
            if event not in self.edit_recovery_events:
                self.edit_recovery_events.append(event)
        if facts.command_executed:
            self.commands_run += 1
        if facts.blocker_succeeded:
            self.structured_terminal = "blocker"
        elif facts.already_satisfied_succeeded:
            self.structured_terminal = "already_satisfied"

        # A write or a stale-file notice changed these bytes: every retained
        # fingerprint that touched them is forgotten, exactly as the duplicate
        # guard forgets its own. Rereading a file you just changed is never a
        # resurvey.
        self.note_stale_paths(facts.stale_paths)

        # Telemetry: the segment is not spent on this.
        self.observation_tokens_since_progress += max(0, facts.observation_tokens)

        if facts.bears_implementation_progress():
            # Implementation movement resets pre-mutation trajectory accounting:
            # an applied write, or a write attempt whose authoritative failure
            # created concrete edit-recovery state, ends the segment that
            # preceded it and buys the turn a fresh request-spend allowance.
            self.implementation_progress_events += 1
            self.segment_requests = 0
            self.segment_baseline_request_tokens = 0
            self.segment_request_spend_tokens = 0
            self.observation_tokens_since_progress = 0
            self.prose_nonconvergence_in_segment = 0

    def note_stale_paths(self, paths: Any) -> None:
        """Forget retained observation fingerprints touching changed paths."""
        normalized = [
            str(path).replace("\\", "/").strip()
            for path in (paths or ())
            if str(path).strip()
        ]
        if not normalized:
            return
        for retained in (
            self.segment_observation_fingerprints,
            self.retired_observation_fingerprints,
        ):
            for fingerprint in list(retained):
                probe = fingerprint.replace("\\\\", "/").replace("\\", "/")
                if any(path in probe for path in normalized):
                    retained.discard(fingerprint)

    def note_accepted_tool_calls(self, total: int) -> None:
        """Record the turn's total accepted tool usage for the runaway ceiling."""
        self.accepted_tool_calls = max(self.accepted_tool_calls, int(total))

    def note_prose_nonconvergence(self) -> None:
        """A prose-only response arrived while the implementation action is owed."""
        self.prose_nonconvergence_in_segment += 1

    # ---- decisions --------------------------------------------------------

    def charge_model_request(self, request_tokens: int) -> TrajectoryDecision:
        """Decide, and charge, the model request that is about to be issued.

        This is the primary convergence boundary.  It runs *after* the outbound
        view for the next request has been built — so ``request_tokens`` is the
        real, authoritative cost of that request — and *before* the request is
        sent, so a segment that has stopped converging is never paid for again.

        The charge is committed only when the answer is
        :attr:`TrajectoryDecision.CONTINUE`.  A rollover therefore leaves the
        counters untouched, the caller rebuilds the view behind the new segment
        boundary, and the rebuilt request arrives here as that segment's free
        first request — which is also why this can never loop.
        """
        if self.accepted_tool_calls >= SINGLE_RUNAWAY_TOOL_CALL_CEILING:
            self.terminated_for_runaway = True
            return TrajectoryDecision.TERMINAL_HARNESS_FAILURE

        cost = max(0, int(request_tokens))

        if self.segment_requests == 0:
            # Opening a segment is not wandering continuation.
            self.segment_requests = 1
            self.segment_baseline_request_tokens = cost
            return TrajectoryDecision.CONTINUE

        if not self.engaged or self.structured_terminal:
            # Read-only turns, answer-only routes, and turns that already
            # reached a truthful terminal outcome are metered for the runaway
            # ceiling only. They are never pushed toward a mutation.
            self.segment_requests += 1
            self.segment_request_spend_tokens += cost
            return TrajectoryDecision.CONTINUE

        if self.prose_nonconvergence_in_segment > PROSE_NONCONVERGENCE_PER_SEGMENT:
            return TrajectoryDecision.INTERNAL_ROLLOVER

        if self.segment_request_spend_tokens + cost > self.segment_allowance_tokens:
            return TrajectoryDecision.INTERNAL_ROLLOVER

        self.segment_requests += 1
        self.segment_request_spend_tokens += cost
        return TrajectoryDecision.CONTINUE

    def decide(self) -> TrajectoryDecision:
        """Return what the send loop should do at a boundary with no request cost.

        Used at the top of the loop and on the prose-only path, where no
        outbound view has been built yet.  It answers the two questions that do
        not need a request size: the runaway ceiling, and whether Aura's own
        in-segment steering has stopped working.  Request spend is decided by
        :meth:`charge_model_request`.
        """
        if self.accepted_tool_calls >= SINGLE_RUNAWAY_TOOL_CALL_CEILING:
            self.terminated_for_runaway = True
            return TrajectoryDecision.TERMINAL_HARNESS_FAILURE
        if not self.engaged or self.structured_terminal:
            return TrajectoryDecision.CONTINUE
        if self.prose_nonconvergence_in_segment > PROSE_NONCONVERGENCE_PER_SEGMENT:
            return TrajectoryDecision.INTERNAL_ROLLOVER
        return TrajectoryDecision.CONTINUE

    def begin_new_segment(self) -> None:
        """Open a fresh internal segment for the same real user turn.

        Durable state — the request, the route, observed targets, applied
        writes, edit-recovery requirements, command count, structured terminal
        state, and total accepted tool usage — is deliberately *not* reset: it
        is what the capsule is rebuilt from and what the next segment continues
        with.  The segment's successful broad surveys are promoted to the
        retired set, which is what stops the next segment re-running the very
        investigation this boundary just retired.
        """
        self.rollovers += 1
        self.segment_index += 1
        self.retired_observation_fingerprints |= self.segment_observation_fingerprints
        self.segment_observation_fingerprints = set()
        self.segment_requests = 0
        self.segment_baseline_request_tokens = 0
        self.segment_request_spend_tokens = 0
        self.observation_tokens_since_progress = 0
        self.prose_nonconvergence_in_segment = 0

    # ---- cross-segment resurvey rule --------------------------------------

    def cross_segment_rejection(self, name: str, args: Any) -> dict[str, Any] | None:
        """Refuse an exact broad survey a superseded segment already ran.

        The one cross-segment rule, and the whole of it.  It is wired into
        :class:`~aura.conversation.pre_edit_loop_guard.PreEditLoopGuard` through
        a single explicit seam so the guard stays the narrow exact-duplicate
        gate and gains no workflow state; every condition below is checked here,
        by the owner that knows what a segment is.

        ``None`` means the call may run.  It is allowed whenever:

        * this turn has not deliberately rolled anything over;
        * the call is a genuinely narrower bounded read (:func:`is_narrow_read`)
          — narrowing to a concrete edit surface is exactly the convergence
          being asked for;
        * the prior call was not an identical, *successful*, broad observation
          (failures were never retained, so recovering from one is free);
        * a write or a stale-file notice has since changed the target, which
          drops the fingerprint in :meth:`note_stale_paths`;
        * the target is not one the capsule and ledger actually carry.

        Pending edit recovery is handled one level up: the guard returns before
        this seam whenever ``recovery_pending`` is set, so a recovery step that
        needs fresh bytes is never refused here.
        """
        if not self.engaged or self.rollovers <= 0:
            return None
        if is_narrow_read(name, args):
            return None
        fingerprint = read_fingerprint(name, args)
        if fingerprint not in self.retired_observation_fingerprints:
            return None
        targets = observation_targets(name, args)
        if targets and not any(target in self.observed_targets for target in targets):
            return None
        self.superseded_resurveys_blocked += 1
        return {
            "ok": False,
            "loop_guard": True,
            "recoverable": True,
            "reason": SUPERSEDED_SEGMENT_READ_REASON,
            "tool": name,
            "retired_segments": self.rollovers,
            "message": _SUPERSEDED_SEGMENT_READ_MESSAGE,
        }

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
            "the same tools. One bounded source surface from the retired "
            "segment is still above you — act on it rather than re-reading it.",
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

        if self.edit_recovery_events:
            lines.append("")
            lines.append(
                "Write attempts that failed and created a concrete recovery "
                "requirement (satisfy the requirement before re-proposing):"
            )
            for event in self.edit_recovery_events:
                lines.append(f"  - {event.describe()}")

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
            "segment_requests": self.segment_requests,
            "segment_baseline_request_tokens": self.segment_baseline_request_tokens,
            "segment_request_spend_tokens": self.segment_request_spend_tokens,
            "observation_tokens_since_progress": self.observation_tokens_since_progress,
            "prose_nonconvergence_in_segment": self.prose_nonconvergence_in_segment,
            "implementation_progress_events": self.implementation_progress_events,
            "accepted_tool_calls": self.accepted_tool_calls,
            "runaway_ceiling": SINGLE_RUNAWAY_TOOL_CALL_CEILING,
            "applied_writes": len(self.applied_write_paths),
            "edit_recovery_requirements": len(self.edit_recovery_events),
            "observed_targets": len(self.observed_targets),
            "retired_survey_fingerprints": len(self.retired_observation_fingerprints),
            "superseded_resurveys_blocked": self.superseded_resurveys_blocked,
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
    "MIN_SEGMENT_ALLOWANCE_TOKENS",
    "OBSERVATION_SEGMENT_TURNOVERS",
    "PROSE_NONCONVERGENCE_PER_SEGMENT",
    "RUNAWAY_FAILURE_CLASS",
    "RUNAWAY_FAILURE_MESSAGE",
    "SEGMENT_REQUEST_TURNOVERS",
    "SINGLE_RUNAWAY_TOOL_CALL_CEILING",
    "SUPERSEDED_SEGMENT_READ_REASON",
    "EditRecoveryEvent",
    "RoundFacts",
    "SingleTrajectoryController",
    "TrajectoryDecision",
    "observation_targets",
]
