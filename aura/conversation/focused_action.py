"""The focused action turn: serializing an already-reached decision into one act.

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

**A malformed response is a protocol lapse, not an impossible task.**  A focused
response that answers with prose, no calls, several calls, an unexposed tool, or
a malformed collection is discarded whole — nothing in it executes, nothing in
it is stored — but the *decision* it was supposed to serialize was never spent.
Killing the turn there forced the user to resend an entire task, complete with
all its repository discovery, because one response was shaped wrong.  So the
send loop instead classifies the violation
(:func:`diagnose_focused_contract`), attaches one compact request-local
correction naming exactly what was wrong, and reissues the *same* focused
request: same model, thinking off, same action catalog, same required-tool
flag, same frozen system prompt, same gathered evidence.  Nothing is appended to
canonical history and normal turns pay nothing for this.

That is not a retry allowance.  There is no attempt count and no cap.  What
makes a violation terminal is *evidence*: a structural fingerprint
(:attr:`FocusedContractDiagnosis.fingerprint`, built only from the violation
kind, the raw call count, and the readable tool names — never from prose,
reasoning, ids, or timing) that Aura has already corrected once and that the
provider produced again.  A repeat of the identical structural violation, after
being told precisely what was wrong, is proof the provider cannot honour this
protocol; a *different* violation is new evidence and gets its own correction.

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
ending.  The ``A, B, A, B`` cycle detector, a truthful blocker, a provider
contract violation, cancellation, and the catastrophic 300-call brake are the
other terminal paths, and none of them counts attempts.
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

#: The ordinary-catalog bookkeeping tool that ends discovery positively.  Not
#: part of the focused action surface: it is what *leads* to it.
COMMIT_IMPLEMENTATION_DECISION: str = "commit_implementation_decision"

#: Thinking mode used for the action-serialization request, always.
FOCUSED_ACTION_THINKING: str = "off"

#: Outcomes one focused action request can reach.
OUTCOME_WRITE: str = "write"
OUTCOME_BLOCKER: str = "blocker"
OUTCOME_ALREADY_SATISFIED: str = "already_satisfied"
OUTCOME_PROVIDER_CONTRACT_FAILURE: str = "provider_contract_failure"

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

PROVIDER_CONTRACT_FAILURE_MESSAGE = (
    "Provider contract failure: this request required exactly one tool call — "
    "a write/edit tool, report_blocker, or report_already_satisfied. The model "
    "broke that contract, so Aura discarded the response, executed nothing, "
    "and reissued the same focused request with an explicit correction naming "
    "the violation and the tools it was allowed to call. The model returned "
    "the same structural violation again, so it cannot honour this protocol. "
    "No edit was made. The conversation and its gathered evidence are intact; "
    "send again, or select a different model or provider."
)

# ── structural contract violations ─────────────────────────────────────────
#: The stream completed but carried no usable assistant message to validate.
VIOLATION_MISSING_MESSAGE: str = "missing_completed_message"
#: ``tool_calls`` was present but was not a list.
VIOLATION_TOOL_CALLS_NOT_A_LIST: str = "tool_calls_not_a_list"
#: ``tool_calls`` was absent, null, or empty — prose-only answers land here.
VIOLATION_NO_TOOL_CALLS: str = "no_tool_calls"
#: More than one call: the request authorized exactly one act.
VIOLATION_MULTIPLE_TOOL_CALLS: str = "multiple_tool_calls"
#: The sole entry was not a readable call object.
VIOLATION_MALFORMED_CALL: str = "malformed_sole_call"
#: The sole entry's ``function`` was missing or not an object.
VIOLATION_MALFORMED_FUNCTION: str = "malformed_function_object"
#: The tool name was absent, empty, or not a string.
VIOLATION_INVALID_TOOL_NAME: str = "invalid_tool_name"
#: A well-formed call naming a tool this request did not expose.
VIOLATION_UNEXPOSED_TOOL: str = "unexposed_tool"

#: Human-readable reason text, one per structural violation kind. Written for
#: the model that produced the violation, so each says what the response *did*,
#: not what an internal enum is called.
_VIOLATION_REASONS: dict[str, str] = {
    VIOLATION_MISSING_MESSAGE: (
        "it completed without a usable assistant message"
    ),
    VIOLATION_TOOL_CALLS_NOT_A_LIST: (
        "its tool_calls field was not a list of calls"
    ),
    VIOLATION_NO_TOOL_CALLS: (
        "it contained prose but no tool call at all"
    ),
    VIOLATION_MULTIPLE_TOOL_CALLS: (
        "it contained more than one tool call; this request authorizes exactly "
        "one act"
    ),
    VIOLATION_MALFORMED_CALL: (
        "its single tool call was not a readable call object"
    ),
    VIOLATION_MALFORMED_FUNCTION: (
        "its single tool call carried no readable function object"
    ),
    VIOLATION_INVALID_TOOL_NAME: (
        "its single tool call named no tool"
    ),
    VIOLATION_UNEXPOSED_TOOL: (
        "it called a tool this request does not expose"
    ),
}


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

    contract_violated: bool = False
    """Whether a violation ended the turn as a provider-contract failure.

    Set only on the terminal path — a repeat of a structural violation Aura had
    already corrected. A first violation that was repaired never sets it, so a
    recovered lapse cannot pollute the production receipt."""

    # ── focused protocol recovery (per focused decision, not a budget) ──

    pending_correction: str = ""
    """Request-local correction to attach to the *next* focused request.

    Lives here for exactly one request: the send loop appends it to that
    request's outbound message copy and clears it. It never enters the frozen
    system prompt, canonical history, or any other context source."""

    violation_fingerprints: set[str] = field(default_factory=set)
    """Structural violation fingerprints already corrected for this decision.

    Not a counter and not an allowance: membership is the whole question. A
    fingerprint absent here is new evidence and earns one explicit correction; a
    fingerprint already present means the provider repeated the identical
    structural violation *after* being told exactly what was wrong, which is the
    only evidence that ends the turn as a provider-contract failure."""

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


def tool_call_names(full_message: dict[str, Any] | None) -> list[str]:
    """Return the tool names in an assistant message, in call order.

    Reporting only: unreadable entries are skipped and a ``tool_calls`` field
    that is not a list yields nothing, so a malformed response can be described
    without crashing.  It cannot be *validated* from this — that is
    :func:`focused_contract_ok`, which reads the raw collection.
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


@dataclass(frozen=True)
class FocusedContractDiagnosis:
    """What a focused response did, structurally — and nothing else.

    Deliberately narrow.  It answers "did this honour the contract, and if not,
    in exactly what way?" so the send loop can both refuse to execute the
    response *and* tell the model precisely what to fix.  Everything it carries
    is structural; none of it is provider prose.
    """

    ok: bool
    """Whether the response may execute. False means execute nothing at all."""

    kind: str
    """The ``VIOLATION_*`` constant, or ``""`` when the contract held."""

    call_count: int
    """Raw ``tool_calls`` length before any filtering; ``-1`` when unreadable."""

    names: tuple[str, ...]
    """Readable tool names in call order — reporting only, never a verdict."""

    @property
    def fingerprint(self) -> str:
        """Stable identity of *this shape* of violation.

        Built from structural facts only: the violation kind, the raw call
        count, and the readable tool names.  The exposed action surface is not
        part of it because it is fixed for the focused decision the fingerprint
        is scoped to — two violations compared here always faced the same
        catalog.  Prose, reasoning, generated call ids, timestamps, and token
        counts are deliberately excluded: they differ on every response, and
        fingerprinting them would make every repeat look novel and every
        violation infinitely recoverable.
        """
        if self.ok:
            return ""
        parts = [self.kind, str(self.call_count), ",".join(self.names)]
        return "|".join(parts)

    @property
    def reason(self) -> str:
        """One clause describing what the response did wrong."""
        return _VIOLATION_REASONS.get(self.kind, "it broke the response contract")


def diagnose_focused_contract(
    full_message: dict[str, Any] | None,
    exposed_tools: tuple[str, ...] | frozenset[str] | set[str] | list[str],
) -> FocusedContractDiagnosis:
    """Classify a focused response against the exactly-one-tool-call contract.

    Validated against the *raw* ``tool_calls`` collection, not against the
    filtered names :func:`tool_call_names` returns.  That filter silently skips
    entries it cannot read, so a response carrying one valid call plus a
    malformed extra entry would otherwise present itself as a single clean call
    and execute — while the provider in fact asked for two acts, one of them
    unreadable.  Every structural requirement is checked here instead:

    * there is a completed assistant message at all;
    * ``tool_calls`` is a list;
    * its raw length is exactly one — before any filtering;
    * the sole entry is a dictionary;
    * its ``function`` is a dictionary;
    * ``name`` is a non-empty string;
    * that name is in this round's exact exposed action set.

    Anything else is a contract violation and executes nothing.  Classification
    is what changed here, not strictness: every response this rejects is exactly
    the set the boolean check rejected.
    """
    def fail(kind: str, *, count: int = -1, names: tuple[str, ...] = ()):
        return FocusedContractDiagnosis(
            ok=False, kind=kind, call_count=count, names=names
        )

    if not isinstance(full_message, dict):
        return fail(VIOLATION_MISSING_MESSAGE)

    names = tuple(tool_call_names(full_message))
    calls = full_message.get("tool_calls")
    if calls is None:
        return fail(VIOLATION_NO_TOOL_CALLS, count=0)
    if not isinstance(calls, list):
        return fail(VIOLATION_TOOL_CALLS_NOT_A_LIST)
    if not calls:
        return fail(VIOLATION_NO_TOOL_CALLS, count=0)
    if len(calls) != 1:
        return fail(
            VIOLATION_MULTIPLE_TOOL_CALLS, count=len(calls), names=names
        )

    call = calls[0]
    if not isinstance(call, dict):
        return fail(VIOLATION_MALFORMED_CALL, count=1)
    function = call.get("function")
    if not isinstance(function, dict):
        return fail(VIOLATION_MALFORMED_FUNCTION, count=1)
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return fail(VIOLATION_INVALID_TOOL_NAME, count=1)
    if name not in set(exposed_tools):
        return fail(VIOLATION_UNEXPOSED_TOOL, count=1, names=(name,))

    return FocusedContractDiagnosis(ok=True, kind="", call_count=1, names=names)


def focused_contract_ok(
    full_message: dict[str, Any] | None,
    exposed_tools: tuple[str, ...] | frozenset[str] | set[str] | list[str],
) -> bool:
    """Whether a focused response honours the exactly-one-tool-call contract.

    The boolean face of :func:`diagnose_focused_contract`, kept for callers that
    only need the verdict.  The send loop reads the diagnosis, because a bare
    "no" cannot tell the model what to fix.
    """
    return diagnose_focused_contract(full_message, exposed_tools).ok


def focused_correction_instruction(
    diagnosis: FocusedContractDiagnosis,
    exposed_tools: tuple[str, ...] | list[str],
) -> str:
    """Return the one request-local correction for a discarded focused response.

    Request-local by construction: the caller appends it to a single outbound
    message copy.  It never mutates the frozen system prompt, is never appended
    to canonical history, and is not a new context source — so a turn with no
    violation carries none of this and costs exactly what it did before.

    It states only facts the send loop already knows: the previous response was
    discarded, why it was structurally invalid, that nothing executed, and what
    a valid response is.
    """
    tools = ", ".join(str(name) for name in exposed_tools if name)
    return (
        "Your previous response was discarded and no action was executed, "
        f"because {diagnosis.reason}. The repository evidence gathered so far "
        "is unchanged and still applies.\n"
        "This request requires exactly one tool call and nothing else. "
        f"Allowed tools: {tools}.\n"
        "Do not answer with prose, a plan, or an explanation — prose cannot "
        "accompany the call and cannot replace it. Respond with exactly one "
        "call to one of the tools above."
    )


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


def provider_contract_failure_message() -> tuple[str, dict[str, Any]]:
    """Return the honest ending for a provider that cannot honour the contract.

    Reached only on evidence, never on a first lapse: Aura discarded the first
    violating response, told the model exactly what was structurally wrong, and
    reissued the identical focused request — and the model produced the same
    structural violation again.  The wording says so, because a message
    claiming nothing was retried when a correction *was* issued would misreport
    what the turn did.
    """
    content = PROVIDER_CONTRACT_FAILURE_MESSAGE
    return content, {"role": "assistant", "content": content}


__all__ = [
    "ACTION_FAILED_MESSAGE",
    "COMMIT_IMPLEMENTATION_DECISION",
    "FOCUSED_ACTION_THINKING",
    "FocusedActionState",
    "FocusedContractDiagnosis",
    "OUTCOME_ACTION_FAILED",
    "OUTCOME_ALREADY_SATISFIED",
    "OUTCOME_BLOCKER",
    "OUTCOME_NOT_APPLIED",
    "OUTCOME_PROVIDER_CONTRACT_FAILURE",
    "OUTCOME_WRITE",
    "PROVIDER_CONTRACT_FAILURE_MESSAGE",
    "REPORT_ALREADY_SATISFIED",
    "REPORT_BLOCKER",
    "VIOLATION_INVALID_TOOL_NAME",
    "VIOLATION_MALFORMED_CALL",
    "VIOLATION_MALFORMED_FUNCTION",
    "VIOLATION_MISSING_MESSAGE",
    "VIOLATION_MULTIPLE_TOOL_CALLS",
    "VIOLATION_NO_TOOL_CALLS",
    "VIOLATION_TOOL_CALLS_NOT_A_LIST",
    "VIOLATION_UNEXPOSED_TOOL",
    "action_failed_message",
    "diagnose_focused_contract",
    "focused_contract_ok",
    "focused_correction_instruction",
    "provider_contract_failure_message",
    "should_enter_focused_action",
    "tool_call_names",
]
