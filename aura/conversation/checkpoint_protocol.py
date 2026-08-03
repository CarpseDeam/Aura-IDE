"""Provider-tolerant normalization for the two production SINGLE checkpoints.

Aura issues two narrowed requests inside a production implementation turn: the
**decision checkpoint** (commit the decision, name the unresolved question, or
report a genuine blocker) and the **focused action** request (carry the decision
out).  Both are protocol requests — they exist to move the turn from one state
to the next — and both used to be validated as an all-or-nothing contract:
exactly one well-formed call naming an exposed tool, or the response was
discarded and, on a repeat, the whole coding task dead-stopped as a provider
contract failure.

That was the wrong owner for the problem.  A model that answers with a sentence
of prose *and* the correct tool call has not failed the task; it has formatted
the answer differently than Aura asked.  A model that emits two perfectly valid
``patch_file`` calls has not failed the task either — that is an ordinary
mutation batch the whole-batch preflight already knows how to run.  Ending a
turn there discarded every byte of repository evidence it had gathered and made
the user resend the entire request, because of the shape of a response envelope.

So the harness normalizes instead of adjudicating.  This module answers one
question — *what, if anything, in this response can safely be executed?* — and
its answer is only ever "execute this" or "ask again":

* prose plus one valid allowed call → the prose is discarded, the call runs;
* one valid allowed call → it runs;
* several valid calls that form a safe ordinary mutation batch → they run
  through the existing whole-batch preflight, which is the authority on whether
  a batch is admissible;
* several *control* calls that contradict each other → nothing runs, each call
  receives a truthful paired rejection result, and the same checkpoint is
  reissued with a compact correction;
* anything unknown, malformed, or unexposed mixed in → nothing from that batch
  runs, every call receives a paired structured rejection, and the same
  checkpoint is reissued;
* zero valid calls → nothing to pair, so nothing is appended, and the same
  checkpoint is reissued with a compact correction.

Nothing here is terminal.  Repeating a structural violation is not proof that
the task is impossible, only that this request shape is not working with this
provider, so the send loop's answer to a repeat is to fall back to the ordinary
production request with all gathered evidence and the decision state intact —
never ``STATUS_PROVIDER_CONTRACT_FAILURE``, and never a completed task.

``tool_choice="required"`` is a request, not a guarantee.  Normalization here is
authoritative regardless of what any provider claims to enforce.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: Nothing in the response may execute; reissue the same checkpoint.
ACTION_REISSUE: str = "reissue"
#: The response yielded usable calls; execute exactly those.
ACTION_EXECUTE: str = "execute"

# ── structural classifications ─────────────────────────────────────────────
#: The response carried no readable tool call at all — prose-only answers, a
#: missing assistant message, a non-list ``tool_calls``, or an empty list.
KIND_NO_USABLE_CALL: str = "no_usable_call"
#: Every call was readable and allowed, but they are control calls that cannot
#: all be true at once (commit *and* continue, blocker *and* commit, ...).
KIND_CONFLICTING_CONTROL: str = "conflicting_control_calls"
#: At least one call was unknown, malformed, or not exposed by this request.
KIND_INVALID_CALL_IN_BATCH: str = "invalid_call_in_batch"

_KIND_REASONS: dict[str, str] = {
    KIND_NO_USABLE_CALL: (
        "it contained no usable tool call — prose alone cannot answer this "
        "request"
    ),
    KIND_CONFLICTING_CONTROL: (
        "it contained several control calls that contradict each other; "
        "exactly one of them can be true"
    ),
    KIND_INVALID_CALL_IN_BATCH: (
        "it contained a call that was malformed or names a tool this request "
        "does not expose"
    ),
}


@dataclass(frozen=True)
class CheckpointRejection:
    """One truthful paired result for a call that was not executed."""

    tool_call_id: str
    name: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class CheckpointNormalization:
    """What the send loop should do with one checkpoint response.

    Structural only.  It never reads assistant prose for intent, never scores
    the response, and never decides that a task is over — the two actions are
    "execute these calls" and "ask the same question again".
    """

    action: str
    kind: str = ""
    calls: tuple[dict[str, Any], ...] = ()
    """The calls to execute, in call order. Empty unless ``action`` executes."""

    rejections: tuple[CheckpointRejection, ...] = ()
    """Paired results to append, one per call, when nothing executed.

    Empty when the response carried no pairable call — appending an assistant
    message whose calls cannot all receive a result would corrupt the
    transcript, so in that case the response is discarded whole instead.
    """

    names: tuple[str, ...] = ()
    """Readable tool names in call order — reporting and fingerprinting only."""

    call_count: int = -1
    """Raw ``tool_calls`` length before any filtering; ``-1`` when unreadable."""

    @property
    def ok(self) -> bool:
        return self.action == ACTION_EXECUTE

    @property
    def fingerprint(self) -> str:
        """Stable identity of *this shape* of malformed response.

        Structural facts only — the classification, the raw call count, and the
        readable tool names.  Prose, reasoning, generated call ids, and timings
        are excluded: they differ on every response, so fingerprinting them
        would make every repeat look novel and no correction would ever be
        recognised as having already been tried.
        """
        if self.ok:
            return ""
        return "|".join([self.kind, str(self.call_count), ",".join(self.names)])

    @property
    def reason(self) -> str:
        """One clause describing what the response did wrong."""
        return _KIND_REASONS.get(self.kind, "it broke the response contract")


def _readable_call(call: Any) -> tuple[str, str] | None:
    """Return ``(call_id, name)`` for a structurally readable call, else None."""
    if not isinstance(call, dict):
        return None
    call_id = call.get("id")
    if not isinstance(call_id, str) or not call_id:
        return None
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    return call_id, name


def _readable_name(call: Any) -> str:
    """Return the tool name of a call for reporting, or ``""``."""
    readable = _readable_call(call)
    return readable[1] if readable else ""


def normalize_checkpoint_response(
    full_message: dict[str, Any] | None,
    *,
    exposed_tools: tuple[str, ...] | frozenset[str] | set[str] | list[str],
    control_tools: tuple[str, ...] | frozenset[str] | set[str] | list[str],
) -> CheckpointNormalization:
    """Decide what may safely execute from one checkpoint response.

    ``exposed_tools`` is the exact catalog this request offered; a name outside
    it is not callable however well-formed the call is.  ``control_tools`` names
    the subset that decides *what happens next* rather than *what changes* —
    those are mutually exclusive by nature, so more than one of them is a
    contradiction, while several ordinary mutation calls are simply a batch.
    """
    exposed = {str(name) for name in exposed_tools}
    control = {str(name) for name in control_tools}

    if not isinstance(full_message, dict):
        return CheckpointNormalization(
            action=ACTION_REISSUE, kind=KIND_NO_USABLE_CALL
        )

    raw = full_message.get("tool_calls")
    if not isinstance(raw, list) or not raw:
        return CheckpointNormalization(
            action=ACTION_REISSUE,
            kind=KIND_NO_USABLE_CALL,
            call_count=len(raw) if isinstance(raw, list) else -1,
        )

    names = tuple(name for name in (_readable_name(c) for c in raw) if name)
    valid: list[dict[str, Any]] = []
    invalid: list[Any] = []
    for call in raw:
        readable = _readable_call(call)
        if readable is not None and readable[1] in exposed:
            valid.append(call)
        else:
            invalid.append(call)

    if invalid:
        return _reissue_with_rejections(
            raw,
            kind=KIND_INVALID_CALL_IN_BATCH,
            names=names,
            offenders={id(call) for call in invalid},
            exposed=exposed,
        )

    if len(valid) == 1:
        # Prose alongside the call is discarded by the caller; the call runs.
        return CheckpointNormalization(
            action=ACTION_EXECUTE,
            calls=tuple(valid),
            names=names,
            call_count=len(raw),
        )

    control_calls = [c for c in valid if _readable_name(c) in control]
    if control_calls:
        # Two answers to a one-answer question, or an answer bundled with an
        # act. Executing any part of it would pick one on the model's behalf.
        return _reissue_with_rejections(
            raw,
            kind=KIND_CONFLICTING_CONTROL,
            names=names,
            offenders={id(call) for call in control_calls},
            exposed=exposed,
        )

    # Several ordinary non-control calls: a normal batch. The existing
    # whole-batch preflight decides whether it is admissible, exactly as it does
    # for any other round — this module does not second-guess it.
    return CheckpointNormalization(
        action=ACTION_EXECUTE,
        calls=tuple(valid),
        names=names,
        call_count=len(raw),
    )


def _reissue_with_rejections(
    raw: list[Any],
    *,
    kind: str,
    names: tuple[str, ...],
    offenders: set[int],
    exposed: set[str],
) -> CheckpointNormalization:
    """Build a reissue that pairs one truthful result to every call.

    Pairing is only attempted when *every* entry carries a usable id: a
    synthetic result needs one to pair back, and a transcript with an
    unpaired call in it poisons every later request.  When even one entry
    cannot be paired the response is discarded whole and the checkpoint is
    reissued with no results appended — which is exactly as safe and loses
    nothing, because nothing executed either way.
    """
    readable = [_readable_call(call) for call in raw]
    if any(entry is None for entry in readable):
        return CheckpointNormalization(
            action=ACTION_REISSUE,
            kind=kind,
            names=names,
            call_count=len(raw),
        )

    rejections: list[CheckpointRejection] = []
    offender_names = sorted({
        _readable_name(call) for call in raw if id(call) in offenders
    })
    for call, entry in zip(raw, readable):
        assert entry is not None  # guarded above
        call_id, name = entry
        if id(call) in offenders:
            payload = {
                "ok": False,
                "recoverable": True,
                "failure_class": "checkpoint_call_rejected",
                "reason": kind,
                "tool": name,
                "call_rejected": True,
                "applied": False,
                "mutation": False,
                "message": (
                    f"Nothing in this response executed. Call {call_id} "
                    f"({name}) was rejected: "
                    f"{_KIND_REASONS.get(kind, 'it broke the response contract')}. "
                    "The repository evidence gathered so far is unchanged and "
                    "still applies. Answer the same request again with exactly "
                    "one call."
                ),
            }
        else:
            payload = {
                "ok": False,
                "recoverable": True,
                "failure_class": "checkpoint_batch_rejected",
                "reason": "checkpoint_batch_rejected_before_execution",
                "tool": name,
                "batch_rejected": True,
                "applied": False,
                "mutation": False,
                "rejected_sibling_tools": offender_names,
                "message": (
                    f"Nothing in this response executed. Call {call_id} "
                    f"({name}) was valid, but a sibling call in the same "
                    "response was rejected "
                    f"({', '.join(offender_names) or 'unknown'}), so the whole "
                    "response was rejected rather than running part of it. The "
                    "repository evidence gathered so far is unchanged. Re-issue "
                    "the single call you actually mean."
                ),
            }
        rejections.append(
            CheckpointRejection(tool_call_id=call_id, name=name, payload=payload)
        )

    return CheckpointNormalization(
        action=ACTION_REISSUE,
        kind=kind,
        rejections=tuple(rejections),
        names=names,
        call_count=len(raw),
    )


def checkpoint_correction(
    normalization: CheckpointNormalization,
    exposed_tools: tuple[str, ...] | list[str],
    *,
    allow_batch: bool,
    edit_attempted: bool = False,
) -> str:
    """Return the one request-local correction for a reissued checkpoint.

    Request-local by construction: the caller appends it to a single outbound
    message copy.  It never enters the frozen system prompt, never enters
    canonical history, and a request with no violation carries none of it.

    It states only facts the send loop already knows — that nothing executed,
    what was structurally wrong, that the gathered evidence is untouched, and
    which tools are callable.
    """
    tools = ", ".join(str(name) for name in exposed_tools if name)
    shape = (
        "one tool call, or several calls of the same editing tools when the "
        "change genuinely spans them"
        if allow_batch
        else "exactly one tool call"
    )
    # Reaching for an editing tool at the decision checkpoint is not confusion —
    # it is the model saying the decision is made. Say so, and point at the one
    # call that turns that into the edit, so the round converges instead of
    # repeating.
    made_up_mind = (
        "\nYou reached for an editing tool, which means you already know what "
        "to change. Call commit_implementation_decision naming those exact "
        "target files and the change; the request straight after it exposes the "
        "editing tools and nothing else, and you make the edit there."
        if edit_attempted
        else ""
    )
    return (
        "Nothing in your previous response was executed, because "
        f"{normalization.reason}. Everything you have gathered in this turn is "
        "unchanged and still applies — you are not being asked to investigate "
        "anything again.\n"
        f"This request accepts {shape}. Allowed tools: {tools}."
        f"{made_up_mind}\n"
        "Prose cannot accompany the call and cannot replace it. Answer with "
        "the call."
    )


__all__ = [
    "ACTION_EXECUTE",
    "ACTION_REISSUE",
    "KIND_CONFLICTING_CONTROL",
    "KIND_INVALID_CALL_IN_BATCH",
    "KIND_NO_USABLE_CALL",
    "CheckpointNormalization",
    "CheckpointRejection",
    "checkpoint_correction",
    "normalize_checkpoint_response",
]
