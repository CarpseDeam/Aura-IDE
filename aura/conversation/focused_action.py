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
* discovery is over — :attr:`PreEditLoopGuard.focused` is true, because the
  guard saw a round gather nothing or saw the turn circling in an ``A, B, A, B``
  cycle.  There is no request, file, token, or time budget: a turn returning
  genuinely new evidence keeps surveying;
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
* a provider contract violation ends the turn with a provider-contract failure;
* **any other outcome** — a failed write, a stale patch, a rejected approval, an
  invalid blocker — marks the focused request spent *for that decision* and
  returns to the ordinary tool loop with the tool result in history, so the
  model can inspect, reread, correct, and act again.

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

from dataclasses import dataclass
from typing import Any

from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskRoute, route_bears_production_action

#: The control tools the focused action request adds to the mutation set.
#: ``report_blocker`` names why no edit is possible; ``report_already_satisfied``
#: records — as structured evidence, never as prose — that the requested state
#: already exists in the repository and no change is required.
REPORT_BLOCKER: str = "report_blocker"
REPORT_ALREADY_SATISFIED: str = "report_already_satisfied"

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
    "a write/edit tool or report_blocker — and the model returned prose with "
    "no tool call. No edit was made and nothing was retried. The conversation "
    "and its gathered evidence are intact; send again, or select a different "
    "model or provider."
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
    """Whether the provider returned prose instead of the required tool call."""


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

    ``guard.focused`` is the single authority on "discovery is over", and it is
    set only by evidence: a round that ran tools and gathered nothing, or a
    detected ``A, B, A, B`` cycle.  A turn whose rounds keep returning genuinely
    new evidence never reaches this gate, however many rounds that takes.

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
    if not guard.focused:
        return False
    if guard.recovery_open:
        # A failure the turn has not seen before just landed. Forcing a mutation
        # now would push the model straight back into the act the failure
        # already explained; the ordinary loop reads it first.
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


def focused_contract_ok(
    full_message: dict[str, Any] | None,
    exposed_tools: tuple[str, ...] | frozenset[str] | set[str] | list[str],
) -> bool:
    """Whether a focused response honours the exactly-one-tool-call contract.

    Validated against the *raw* ``tool_calls`` collection, not against the
    filtered names :func:`tool_call_names` returns.  That filter silently skips
    entries it cannot read, so a response carrying one valid call plus a
    malformed extra entry would otherwise present itself as a single clean call
    and execute — while the provider in fact asked for two acts, one of them
    unreadable.  Every structural requirement is checked here instead:

    * ``tool_calls`` is a list;
    * its raw length is exactly one — before any filtering;
    * the sole entry is a dictionary;
    * its ``function`` is a dictionary;
    * ``name`` is a non-empty string;
    * that name is in this round's exact exposed action set.

    Anything else is a provider-contract failure and executes nothing.
    """
    if not isinstance(full_message, dict):
        return False
    calls = full_message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        return False
    call = calls[0]
    if not isinstance(call, dict):
        return False
    function = call.get("function")
    if not isinstance(function, dict):
        return False
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return False
    return name in set(exposed_tools)


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
    """Return the honest ending for a provider that ignored the tool contract.

    Deliberately terminal and deliberately silent about retrying: a provider
    that will not honour ``tool_choice`` on one request is not more likely to
    honour it on the next, and quietly re-asking would spend the user's tokens
    hiding a provider defect.
    """
    content = PROVIDER_CONTRACT_FAILURE_MESSAGE
    return content, {"role": "assistant", "content": content}


__all__ = [
    "ACTION_FAILED_MESSAGE",
    "FOCUSED_ACTION_THINKING",
    "FocusedActionState",
    "OUTCOME_ACTION_FAILED",
    "OUTCOME_ALREADY_SATISFIED",
    "OUTCOME_BLOCKER",
    "OUTCOME_NOT_APPLIED",
    "OUTCOME_PROVIDER_CONTRACT_FAILURE",
    "OUTCOME_WRITE",
    "PROVIDER_CONTRACT_FAILURE_MESSAGE",
    "REPORT_ALREADY_SATISFIED",
    "REPORT_BLOCKER",
    "action_failed_message",
    "focused_contract_ok",
    "provider_contract_failure_message",
    "should_enter_focused_action",
    "tool_call_names",
]
