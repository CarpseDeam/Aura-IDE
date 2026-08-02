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
  turn is the ``implementation`` lane;
* :attr:`PreEditLoopGuard.focused` is true;
* no write has applied;
* no completion response is pending;
* no focused action request has already been spent this turn.

When all six hold, the next model request is an *action-serialization* request:
same model, same conversation, same gathered evidence, with ``thinking="off"``
for that one request, the fixed action tool surface (existing mutation tools
plus :data:`REPORT_BLOCKER`), and a provider-neutral requirement that the
response be exactly one tool call.  The user's selected thinking mode is never
changed — it is simply not the mode for this request — so the round after the
action runs on the selection again.

There are no token, character, time, round, retry, or tool-call limits here,
no classifier, no phase engine, and no watchdog.  The state is spent by the one
request it authorizes: whatever comes back — an applied write, a failed write,
a blocker, or a provider that broke the required-tool contract — the turn
leaves focused action and continues on the ordinary paths.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.task_router import TaskLane, TaskRoute

#: The one control tool the focused action request adds to the mutation set.
REPORT_BLOCKER: str = "report_blocker"

#: Thinking mode used for the action-serialization request, always.
FOCUSED_ACTION_THINKING: str = "off"

#: Outcomes one focused action request can reach.  Every one is terminal for
#: the focused state — none of them schedules another focused request.
OUTCOME_WRITE: str = "write"
OUTCOME_BLOCKER: str = "blocker"
OUTCOME_PROVIDER_CONTRACT_FAILURE: str = "provider_contract_failure"

PROVIDER_CONTRACT_FAILURE_MESSAGE = (
    "Provider contract failure: this request required exactly one tool call — "
    "a write/edit tool or report_blocker — and the model returned prose with "
    "no tool call. No edit was made and nothing was retried. The conversation "
    "and its gathered evidence are intact; send again, or select a different "
    "model or provider."
)


@dataclass
class FocusedActionState:
    """Per-turn record of the single focused action request.

    ``spent`` is the whole control structure: one turn authorizes at most one
    action-serialization request, and the request that runs consumes it.  A
    failed or rejected write therefore returns to the ordinary loop and its
    existing write-recovery path rather than looping back into another
    thinking-off request.
    """

    spent: bool = False
    """Whether this turn's one focused action request has already been issued."""

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
    not an estimate — there is nothing here to tune.
    """
    if mode != "single":
        return False
    if route is None or route.lane != TaskLane.implementation:
        return False
    if guard is None or not guard.focused or guard.write_applied:
        return False
    if task_completion_context:
        return False
    return not (state.spent or state.blocked)


def tool_call_names(full_message: dict[str, Any] | None) -> list[str]:
    """Return the tool names in an assistant message, in call order."""
    if not isinstance(full_message, dict):
        return []
    names: list[str] = []
    for call in full_message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if isinstance(function, dict):
            name = str(function.get("name") or "")
            if name:
                names.append(name)
    return names


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
    "FOCUSED_ACTION_THINKING",
    "FocusedActionState",
    "OUTCOME_BLOCKER",
    "OUTCOME_PROVIDER_CONTRACT_FAILURE",
    "OUTCOME_WRITE",
    "PROVIDER_CONTRACT_FAILURE_MESSAGE",
    "REPORT_BLOCKER",
    "provider_contract_failure_message",
    "should_enter_focused_action",
    "tool_call_names",
]
