"""The production SINGLE request protocol: discovery, checkpoint, action.

This module owns the whole alternation, and it is the *only* owner: there is no
second readiness manager, no phase engine, and no competing state machine.  One
:class:`FocusedActionState` per turn answers, before every model request, which
of three shapes that request has:

.. code-block:: text

    observation round ─▶ decision checkpoint ─▶ observation round
                                  │                    │
                                  └──▶ committed decision ──▶ focused action

**Ordinary observation round.**  The full discovery catalog.  The turn reads,
searches, and inspects as deeply as the work genuinely needs.

**Decision checkpoint.**  After every completed pre-write observation round, the
next request is not another discovery request — it is the checkpoint, and it
exposes only ``commit_implementation_decision``,
``continue_implementation_discovery``, and (when the task is externally blocked)
``report_blocker``.  No read, search, terminal, diagnostic, mutation, web, Git,
Godot inspection, TODO, MCP, or drone tool appears in it.

That is the structural fix for a model wandering forever.  Committing the
decision was previously *voluntary*: the prompt asked for it, and a model that
simply never called it kept chaining unrelated reads until something else ran
out.  Now the harness asks the question, and the only way to earn another
observation round is to name the exact unresolved implementation question and
the repository evidence that would answer it.  Answering costs one call and the
round is granted; there is still no request, file, token, or time budget
anywhere in this, and a turn with genuinely open questions may alternate as many
times as it needs.  What it cannot do is silently keep looking.

**Focused action.**  A committed decision — or the guard's anti-loop stall
fallback — hands the next request to the mutation surface, which is what the
rest of this module has always described.

``PreEditLoopGuard`` can already conclude, deterministically, that a production
turn has finished discovery — it has issued its one focus instruction and no
write has applied.  Without this module the send loop's answer to that is
another ordinary reasoning stream, and the model is free to spend a whole
response reconsidering an edit it has already scoped.

The focused action turn is the fixed protocol for that state.  It is *not* a
second opinion about how hard the task is, and it is not an effort decision.
Every input is state the loop already keeps:

* the mode is production ``single``;
* the deterministic :class:`~aura.conversation.task_router.TaskRoute` for this
  turn bears a production action —
  :func:`~aura.conversation.task_router.route_bears_production_action`, which is
  the ``implementation`` lane *or* a hybrid ``research`` /
  ``research_then_worker`` route, the same predicate the discovery stage uses;
* discovery is over, by one of two facts.  Either the agent committed an
  implementation decision — ``commit_implementation_decision`` succeeded, so it
  can name the authoritative owner, the seams, the target files, and the change
  (:attr:`FocusedActionState.decision_committed`) — or
  :attr:`PreEditLoopGuard.focused` is true because the guard saw a round gather
  nothing or saw the turn circling in an ``A, B, A, B`` cycle.  The first is a
  positive readiness signal and the second an anti-loop fallback; there is no
  request, file, token, or time budget in either, and a turn that commits no
  decision and keeps returning genuinely new evidence keeps surveying;
* no distinct failure is currently open (:attr:`PreEditLoopGuard.recovery_open`);
* no write has applied;
* no completion response is pending;
* no focused action request is currently available to spend.

When all of these hold, the next model request is an *action-serialization*
request:
same model, same conversation, same gathered evidence, with ``thinking="off"``
for that one request, the fixed action tool surface (existing mutation tools
plus :data:`REPORT_BLOCKER`), and a provider-neutral requirement that the
response be exactly one tool call.  The user's selected thinking mode is never
changed — it is simply not the mode for this request — so the round after the
action runs on the selection again.

There are no token, character, time, round, or tool-call limits here, no
classifier, no phase engine, and no watchdog.  The state is spent by the one
request it authorizes.

**A failed act is evidence, not completion.**  A focused mutation that failed or
was rejected must not kill the task — the tool result usually says exactly what
to fix, and the turn was asked to complete a change, not to take one swing at
it.  So the outcome is fed back through the guard's existing evidence and
failure ledgers rather than through a retry manager:

* an applied mutation leaves focused action for the ordinary post-write
  validation path;
* a successful structured blocker ends the turn blocked;
* a provider contract violation is *repaired*, not fatal — see below;
* **any other outcome** — a failed write, a stale patch, a rejected approval, an
  invalid blocker — marks the focused request spent *for that decision* and
  returns to the ordinary tool loop with the tool result in history, so the
  model can inspect, reread, correct, and act again.

**A malformed response is a formatting disagreement, not an impossible task.**
Response shape is normalized, never adjudicated — see
:mod:`aura.conversation.checkpoint_protocol`, which both checkpoints share.
Prose alongside a valid call loses the prose and keeps the call; several valid
mutation calls are an ordinary batch and go through the existing whole-batch
preflight; contradictory control calls and malformed or unexposed calls execute
nothing, receive truthful paired rejection results, and the *same* request is
reissued with one compact correction.

Reissuing costs the turn nothing it had: the decision was never spent, the
gathered evidence is untouched, and normal turns build no correction at all.  If
the provider keeps producing an unusable shape, the send loop falls back to the
ordinary production request with the decision capsule and unresolved state
preserved.  It never ends the coding task, and there is no
``STATUS_PROVIDER_CONTRACT_FAILURE`` for tool-call shape — a turn that dead-stops
on an envelope while holding intact evidence and an unmade edit is simply a bug.
``tool_choice="required"`` is a request to the provider, never a guarantee;
manager-side normalization stays authoritative.

Terminal stops are reserved for user cancellation, a successful structured
``report_blocker``, a successful ``report_already_satisfied``, an unrecoverable
provider/API transport failure, the catastrophic 300-call brake, and a
successfully completed task.

Whether the turn is advancing or repeating is then decided by the guard alone,
exactly as it is during discovery.  While the ordinary loop keeps producing new
evidence, successful commands, applied mutations, or genuinely *different*
failures, the turn continues; when the ordinary loop develops a corrected
decision — a round that actually advanced — the focused request becomes
available again, however many times that happens.  There is no allowance, no
recovery counter, and no cap on how many corrected decisions a turn may reach.

The turn ends without a write only on evidence: a round that neither advanced
nor produced a failure the turn had not already seen (see
:func:`~aura.conversation.pre_edit_loop_guard.failure_fingerprint`) means
nothing left in the turn can change what happens next, and that is the honest
ending.  The ``A, B, A, B`` cycle detector, a truthful blocker, cancellation,
and the catastrophic 300-call brake are the other terminal paths, and none of
them counts attempts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskRoute, route_bears_production_action

#: The control tools the focused action request adds to the mutation set.
#: ``report_blocker`` names why no edit is possible; ``report_already_satisfied``
#: records — as structured evidence, never as prose — that the requested state
#: already exists in the repository and no change is required.
REPORT_BLOCKER: str = "report_blocker"
REPORT_ALREADY_SATISFIED: str = "report_already_satisfied"

#: The checkpoint tool that ends discovery positively.  Not part of the focused
#: action surface: it is what *leads* to it.
COMMIT_IMPLEMENTATION_DECISION: str = "commit_implementation_decision"

#: The checkpoint tool that buys exactly one more observation round, and only
#: against a named unresolved implementation question.
CONTINUE_IMPLEMENTATION_DISCOVERY: str = "continue_implementation_discovery"

#: The control tools of the decision checkpoint — the ones that decide which
#: request comes next rather than changing anything.  Mutually exclusive by
#: nature: more than one of them in a response is a contradiction, not a batch.
DECISION_CHECKPOINT_CONTROL_TOOLS: frozenset[str] = frozenset({
    COMMIT_IMPLEMENTATION_DECISION,
    CONTINUE_IMPLEMENTATION_DISCOVERY,
    REPORT_BLOCKER,
})

#: The control tools of the focused action request.  The mutation tools around
#: them are *not* control: several of those are an ordinary batch.
FOCUSED_CONTROL_TOOLS: frozenset[str] = frozenset({
    REPORT_BLOCKER,
    REPORT_ALREADY_SATISFIED,
})

#: Thinking mode used for the action-serialization request, always.
FOCUSED_ACTION_THINKING: str = "off"

#: Thinking mode used for the decision checkpoint request, always.  Both
#: narrowed protocol requests pin ``tool_choice="required"``, and DeepSeek
#: rejects that combination outright while thinking is enabled
#: (``400: Thinking mode does not support this tool_choice``).  The requirement
#: is the load-bearing half — the checkpoint is answered with a control call or
#: it is reissued — so thinking is what gives way.  Request-local only: the
#: user's saved selection is untouched and the next ordinary round uses it.
DECISION_CHECKPOINT_THINKING: str = "off"

#: Outcomes one focused action request can reach.
OUTCOME_WRITE: str = "write"
OUTCOME_BLOCKER: str = "blocker"
OUTCOME_ALREADY_SATISFIED: str = "already_satisfied"

#: The focused act ran and left the workspace unchanged.  **Not terminal**: the
#: tool result is in history, the request is spent for that one decision, and
#: the turn returns to the ordinary loop to inspect, correct, and act again.
OUTCOME_NOT_APPLIED: str = "not_applied"

#: The turn ran out of evidence: a round neither advanced nor produced a failure
#: the turn had not already seen.  Terminal, and reached from the ordinary loop —
#: never from an attempt count.
OUTCOME_ACTION_FAILED: str = "action_failed"

ACTION_FAILED_MESSAGE = (
    "The change did not land. The last round neither made progress nor "
    "produced anything the turn had not already seen — the tool results above "
    "are the exact reasons — so continuing would repeat the same failure "
    "knowing nothing new. Nothing was written. The conversation and its "
    "gathered evidence are intact; send again to try another approach."
)


@dataclass
class FocusedActionState:
    """Per-turn record of the *current* focused action request.

    Deliberately not a budget.  It tracks the request in flight, whether a
    blocker ended the attempt, which action the model chose, and whether the
    provider honoured the contract — nothing here is a lifetime allowance, and
    there is no count of how many focused requests a turn has issued.

    ``spent`` is scoped to one decision: the request that runs consumes it so a
    single decision cannot be re-issued unchanged, and the send loop clears it
    once the ordinary loop has actually advanced — which is a corrected
    decision, not a retry.  How often that may happen is unbounded; what stops
    the turn is the guard finding no new evidence, never arithmetic.
    """

    spent: bool = False
    """Whether the focused request for the current decision has been issued."""

    active: bool = False
    """Whether the request currently being built is the focused action request."""

    checkpoint_active: bool = False
    """Whether the request currently being built is the decision checkpoint."""

    discovery_round_open: bool = True
    """Whether an ordinary observation round is currently authorized.

    True at the start of the turn — the first production request performs its
    first observation round normally, exactly as it always did — and again after
    every successful ``continue_implementation_discovery``.  It is closed by the
    completion of an ordinary pre-write observation round, which is what hands
    the next request to the checkpoint.

    Not a budget and not a count: it holds one bit, and the only way to reopen
    it is to name an unresolved implementation question."""

    unresolved_question: str = ""
    """The most recent named unresolved implementation question, for logs."""

    protocol_fallback: bool = False
    """Whether the next request must be the ordinary production request.

    Set when a provider has produced an unusable checkpoint response shape
    twice.  The turn is *not* over: the decision capsule, the gathered evidence,
    and the unresolved state are all intact, and the ordinary request is simply
    a shape the provider has already demonstrated it can answer.  Consumed by
    the one request it redirects."""

    blocked: bool = False
    """Whether ``report_blocker`` ended the implementation attempt."""

    already_satisfied: bool = False
    """Whether ``report_already_satisfied`` ended the implementation attempt.

    True only when the structured tool result succeeded: the model inspected
    authoritative repository evidence and recorded, explicitly, that the
    requested state already exists.  Never inferred from the absence of a
    write and never from assistant prose."""

    selected_thinking: str = ""
    """The user-selected thinking mode, restored for every other request."""

    exposed_tools: tuple[str, ...] = ()
    """The action tool names exposed by the focused request, for telemetry."""

    selected_action: str = ""
    """The tool the model actually chose, once the round has streamed."""

    outcome: str = ""
    """Terminal outcome of the focused request, once known."""

    # ── protocol recovery (per checkpoint, not a budget) ────────────────

    pending_correction: str = ""
    """Request-local correction to attach to the *next* focused request.

    Lives here for exactly one request: the send loop appends it to that
    request's outbound message copy and clears it. It never enters the frozen
    system prompt, canonical history, or any other context source."""

    violation_fingerprints: set[str] = field(default_factory=set)
    """Structural response shapes already corrected at this checkpoint.

    Not a counter and not an allowance: membership is the whole question. A
    fingerprint absent here is new evidence and earns one explicit correction; a
    fingerprint already present means the provider repeated the identical
    unusable shape *after* being told exactly what was wrong, which is the
    evidence that this request shape is not working — and the send loop's answer
    to that is the ordinary production request, never the end of the task."""

    last_violation: str = ""
    """Kind of the most recent structural violation, for logs and telemetry."""

    # ── committed implementation decision (positive readiness) ──────────

    decision_committed: bool = False
    """Whether a ``commit_implementation_decision`` call succeeded and has not
    yet been spent.

    The *positive* route into focused action, and deliberately a different
    concept from the guard's stall: a stalled round is evidence the turn has
    stopped moving, while this is the agent stating that it knows the owner,
    the seams, the target files, and the change. Both hand the next request to
    the same protocol, and neither is a count of anything."""

    decision_id: str = ""
    """Content identity of the committed decision, for logs and telemetry.

    A hash of the normalized packet, never an ordinal: a corrected decision has
    a different identity, and nothing branches on how many a turn produced."""

    def continue_discovery(self, question: str) -> None:
        """Grant exactly one more ordinary observation round.

        Called only from a *successful* structured
        ``continue_implementation_discovery`` result — never from the tool name
        alone and never from assistant prose.  The round it grants is spent by
        the observation round that follows, after which the checkpoint returns.
        """
        self.discovery_round_open = True
        self.unresolved_question = str(question or "")

    def close_discovery_round(self) -> None:
        """Spend the authorized observation round.

        Called when an ordinary pre-write round that actually observed
        something has completed.  The next request is then the checkpoint.
        """
        self.discovery_round_open = False

    def commit_decision(self, decision_id: str) -> None:
        """Record that this turn has committed an implementation decision.

        Called only from a *successful* structured tool result — never from the
        tool name alone and never from assistant prose. Re-committing simply
        replaces the identity: the newest decision is the one the next focused
        request will serialize.
        """
        self.decision_committed = True
        self.decision_id = str(decision_id or "")

    def consume_decision(self) -> None:
        """Spend the committed decision.

        A decision authorizes exactly the one focused act that serializes it.
        Once a valid focused response has arrived — a write, a blocker, an
        already-satisfied report, or an act that failed to apply — the decision
        is spent, so an old decision can never authorize an unrelated later
        mutation. A turn that wants to act again after a failed act commits a
        corrected decision, or the guard's stall fallback takes over; neither
        path counts attempts.
        """
        self.decision_committed = False
        self.decision_id = ""
        # A spent decision returns the turn to the ordinary loop, and the round
        # that follows is an ordinary one: an act that failed to apply must be
        # readable and correctable, not answered by a checkpoint asking whether
        # a decision already made is made.
        self.discovery_round_open = True

    def clear_protocol_recovery(self) -> None:
        """Forget this decision's protocol-recovery state.

        Called when the decision genuinely moves on — a valid focused response
        arrived, or the ordinary loop advanced and re-armed the focused request
        for a corrected decision. The next decision starts with a clean slate,
        so a violation corrected earlier in the turn can never be mistaken for a
        repeat later on.
        """
        self.pending_correction = ""
        self.violation_fingerprints.clear()
        self.last_violation = ""


def should_enter_focused_action(
    *,
    mode: str,
    route: TaskRoute | None,
    guard: PreEditLoopGuard | None,
    task_completion_context: bool,
    state: FocusedActionState,
) -> bool:
    """Return whether the next request must be the focused action request.

    Pure over state the send loop already owns.  Every condition is a fact,
    not an estimate — there is nothing here to tune, and nothing here counts.

    Discovery ends by one of exactly two routes, and this function is where
    they meet:

    * ``state.decision_committed`` — the *positive* route.  The agent called
      ``commit_implementation_decision`` and the structured result succeeded, so
      it has named the authoritative owner, the seams, the target files, and the
      change.  Nothing further can be learned that would change the next act, so
      the next request serializes it.  This is why a turn that never stalls, and
      never cycles, no longer gets to keep surveying adjacent systems forever.
    * ``guard.focused`` — the *negative* route, unchanged and still the
      fallback for an agent that commits no decision and actually begins
      circling.  It is set only by evidence: a round that ran tools and gathered
      nothing, or a detected ``A, B, A, B`` cycle.

    They are kept separate on purpose.  A committed decision is a readiness
    signal; a stalled round is an anti-loop signal.  Neither is a budget, and a
    turn whose rounds keep returning genuinely new evidence still reaches this
    gate only by one of those two facts.

    ``guard.recovery_open`` holds the transition back while a failure the turn
    has not seen before is unresolved — the same ledger that grants the ordinary
    loop its reread grace.  It is never latched: the guard closes it when the
    round after the failure ends, whatever that round produced.

    ``state.spent`` is per-decision, not per-turn.  The send loop clears it
    whenever an ordinary round actually advances the turn, so a corrected
    decision always gets its focused request and nothing limits how many
    corrected decisions a turn may reach.
    """
    if mode != "single":
        return False
    if not route_bears_production_action(route):
        return False
    if guard is None or guard.write_applied:
        return False
    if state.protocol_fallback:
        # The provider has twice produced an unusable response to a narrowed
        # request. Nothing about the decision is discarded — it is simply asked
        # for again in the request shape the provider has already demonstrated
        # it can answer.
        return False
    if not (state.decision_committed or guard.focused):
        return False
    if guard.recovery_open:
        # A failure the turn has not seen before just landed. Forcing a mutation
        # now would push the model straight back into the act the failure
        # already explained; the ordinary loop reads it first. This holds for a
        # committed decision too — the decision is not discarded, only deferred
        # by the one round recovery is open, because a decision made before a
        # brand-new failure has not yet accounted for it.
        return False
    if task_completion_context:
        # A pending completion response outranks the focused request: the turn
        # already acted and owes prose, not another mutation.
        return False
    return not (state.spent or state.blocked)


def should_enter_decision_checkpoint(
    *,
    mode: str,
    route: TaskRoute | None,
    guard: PreEditLoopGuard | None,
    task_completion_context: bool,
    read_only: bool,
    state: FocusedActionState,
) -> bool:
    """Return whether the next request must be the decision checkpoint.

    Pure over state the send loop already owns, and evaluated *after*
    :func:`should_enter_focused_action` — a committed decision or the guard's
    stall fallback outranks the checkpoint, because both already answer the
    question the checkpoint asks.

    The one positive condition is that the authorized observation round has been
    spent: an ordinary pre-write round ran, observed something, and ended.  Every
    other condition is a reason *not* to ask:

    * a read-only registry owes no implementation at all;
    * a write has applied, so the pre-write protocol is over;
    * ``guard.recovery_open`` — a failure the turn has not seen before just
      landed, and the ordinary loop reads it first.  Asking a model to commit or
      justify while a fresh failure is unexplained would force it to answer with
      the wrong information;
    * a completion response is pending;
    * the attempt already ended in a blocker or an already-satisfied report;
    * ``protocol_fallback`` — the provider has twice failed to produce a usable
      checkpoint response, so the next request is the ordinary one instead.

    Nothing here counts requests, files, tokens, or elapsed time.
    """
    if mode != "single" or read_only:
        return False
    if not route_bears_production_action(route):
        return False
    if guard is None or guard.write_applied:
        return False
    if state.protocol_fallback:
        return False
    if state.decision_committed or guard.focused:
        return False
    if state.blocked or state.already_satisfied:
        return False
    if guard.recovery_open:
        return False
    if task_completion_context:
        return False
    return not state.discovery_round_open


def tool_call_names(full_message: dict[str, Any] | None) -> list[str]:
    """Return the tool names in an assistant message, in call order.

    Reporting only: unreadable entries are skipped and a ``tool_calls`` field
    that is not a list yields nothing, so a malformed response can be described
    without crashing.  It cannot be *validated* from this — that is
    :func:`~aura.conversation.checkpoint_protocol.normalize_checkpoint_response`,
    which reads the raw collection.
    """
    if not isinstance(full_message, dict):
        return []
    calls = full_message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    names: list[str] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
            if name:
                names.append(name)
    return names


def action_failed_message() -> tuple[str, dict[str, Any]]:
    """Return the honest ending for a turn that ran out of evidence.

    Reached only from the one evidence-based judgement the send loop makes after
    a focused act has already run without applying: the round that just ended
    neither advanced the turn (no applied mutation, no successful command, no
    new evidence) nor produced a failure the guard had not already
    fingerprinted.  Continuing from *that* state is an unbounded failure loop by
    construction, because nothing left in the turn can change what happens next.

    It is not reached because a write failed, a patch was stale, approval was
    rejected, a command failed, or a corrected attempt failed differently — each
    of those returns to the ordinary loop with its result in history.

    Narrow by construction: a focused act whose write *applied* makes
    ``guard.write_applied`` true and never reaches here, so the successful path
    keeps its existing validation and final-response behaviour untouched.
    """
    content = ACTION_FAILED_MESSAGE
    return content, {"role": "assistant", "content": content}


__all__ = [
    "ACTION_FAILED_MESSAGE",
    "COMMIT_IMPLEMENTATION_DECISION",
    "CONTINUE_IMPLEMENTATION_DISCOVERY",
    "DECISION_CHECKPOINT_CONTROL_TOOLS",
    "DECISION_CHECKPOINT_THINKING",
    "FOCUSED_ACTION_THINKING",
    "FOCUSED_CONTROL_TOOLS",
    "FocusedActionState",
    "OUTCOME_ACTION_FAILED",
    "OUTCOME_ALREADY_SATISFIED",
    "OUTCOME_BLOCKER",
    "OUTCOME_NOT_APPLIED",
    "OUTCOME_WRITE",
    "REPORT_ALREADY_SATISFIED",
    "REPORT_BLOCKER",
    "action_failed_message",
    "should_enter_decision_checkpoint",
    "should_enter_focused_action",
    "tool_call_names",
]
