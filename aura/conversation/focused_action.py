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
* no failure recovery round is open (:attr:`PreEditLoopGuard.recovery_open`);
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

**Bounded recovery from a failed act.**  A focused mutation that failed or was
rejected must not kill the task merely because focused action was the thing
that was attempted — the tool result usually says exactly what to fix.  So the
outcome is fed back through the guard's existing failure ledger rather than
through a second retry manager:

* an applied mutation leaves focused action for the ordinary post-write
  validation path;
* a successful structured blocker ends the turn blocked;
* a provider contract violation ends the turn with a provider-contract failure;
* the **first distinct** mutation failure — distinct by
  :func:`~aura.conversation.pre_edit_loop_guard.failure_fingerprint`, so
  :attr:`PreEditLoopGuard.recovery_open` is true — un-spends the focused state
  once (:meth:`FocusedActionState.open_recovery`) and reopens exactly one
  ordinary round;
* that recovery round either advances the turn, in which case focused action is
  available again for the corrected act, or it advances nothing, in which case
  the turn ends truthfully as failed;
* the same failure repeated grants nothing, because the guard's fingerprint
  already knows it, and :attr:`FocusedActionState.recovery_used` makes the
  re-arm available exactly once per turn.

That is the whole recovery structure: one extra ordinary round, owned by state
that already existed, with no parallel state machine and no way to cycle
focus → failure → focus indefinitely.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskRoute, route_bears_production_action

#: The one control tool the focused action request adds to the mutation set.
REPORT_BLOCKER: str = "report_blocker"

#: Thinking mode used for the action-serialization request, always.
FOCUSED_ACTION_THINKING: str = "off"

#: Outcomes one focused action request can reach.  Every one is terminal for
#: the focused state — none of them schedules another focused request.
OUTCOME_WRITE: str = "write"
OUTCOME_BLOCKER: str = "blocker"
OUTCOME_PROVIDER_CONTRACT_FAILURE: str = "provider_contract_failure"

#: The focused request's act ran and left the workspace unchanged, and the one
#: recovery round this turn allows is unavailable or produced nothing.
OUTCOME_ACTION_FAILED: str = "action_failed"

#: The one bounded recovery round has been opened for a first distinct failure.
#: Not terminal — it is a note in the telemetry that the next ordinary round is
#: recovery, and that focused action is re-armed behind it.
OUTCOME_RECOVERY_OPENED: str = "recovery_opened"

ACTION_FAILED_MESSAGE = (
    "The edit did not land. The action failed or was rejected — the tool "
    "result above is the exact reason — and the one recovery round this turn "
    "allows either was already used or recovered nothing. Nothing was written "
    "and nothing was retried further. The conversation and its gathered "
    "evidence are intact; send again to try another approach."
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
    """Per-turn record of the focused action request and its bounded recovery.

    ``spent`` is the control structure: the request that runs consumes it, and
    nothing re-arms it except :meth:`open_recovery`, which may fire at most once
    per turn.  So a turn issues at most two focused requests — the act, and the
    corrected act after exactly one ordinary recovery round — and a failure can
    never cycle focus → failure → focus indefinitely.
    """

    spent: bool = False
    """Whether the currently available focused action request has been issued."""

    active: bool = False
    """Whether the request currently being built is the focused action request."""

    blocked: bool = False
    """Whether ``report_blocker`` ended the implementation attempt."""

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

    recovery_used: bool = False
    """Whether this turn's one focused-mutation recovery has been granted."""

    awaiting_recovery: bool = False
    """Whether the next ordinary round *is* the granted recovery round.

    Cleared by the send loop as soon as that round completes, which is also
    where the round is judged: a recovery round that advanced nothing ends the
    turn rather than handing back a re-armed focused request.
    """

    def open_recovery(self) -> bool:
        """Grant the one recovery round, if it has not been granted already.

        Returns whether it was granted.  The re-arm and the "only once" record
        are the same act, so the focused state can never be re-armed twice, and
        the second focused request — should recovery reach one — is final.
        """
        if self.recovery_used:
            return False
        self.recovery_used = True
        self.awaiting_recovery = True
        self.spent = False
        return True


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

    ``guard.recovery_open`` holds the transition back for exactly one round when
    a distinct failure has just occurred — the same ledger that grants the
    ordinary loop its reread grace, and the same one that grants a failed
    focused mutation its single recovery round.  It is never latched: the guard
    closes it when that one round ends, whatever the round produced.
    """
    if mode != "single":
        return False
    if not route_bears_production_action(route):
        return False
    if guard is None or guard.write_applied:
        return False
    if not guard.focused:
        return False
    if guard.recovery_open or state.awaiting_recovery:
        # A distinct failure just bought one ordinary round to correct it.
        # Forcing a mutation now would push the model straight back into the
        # act the failure already explained.
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
    """Return the honest ending for a focused act that changed nothing.

    Reached only when recovery cannot help: the failure repeats one the guard
    already fingerprinted, the one recovery round was already granted, or that
    granted round ended without progress or new evidence.  Falling back into the
    ordinary loop from any of those states is an unbounded failure loop by
    construction, because nothing left in the turn can change what happens next.

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
    "OUTCOME_BLOCKER",
    "OUTCOME_RECOVERY_OPENED",
    "OUTCOME_PROVIDER_CONTRACT_FAILURE",
    "OUTCOME_WRITE",
    "PROVIDER_CONTRACT_FAILURE_MESSAGE",
    "REPORT_BLOCKER",
    "action_failed_message",
    "focused_contract_ok",
    "provider_contract_failure_message",
    "should_enter_focused_action",
    "tool_call_names",
]
