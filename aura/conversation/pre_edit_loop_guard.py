"""Deterministic duplicate-observation guard for the SINGLE production runtime.

This is the production loop's *only* pre-execution gate, and it is deliberately
narrow.  It exists to stop the one mechanical waste a conventional coding-agent
loop cannot tolerate — re-reading the exact same thing whose result is still in
front of the model — without pretending to judge how much looking is enough.

The rule: the same read-only tool call with the same arguments is rejected the
second time it is issued — *while its first result is still materially in the
model's request*.  That qualifier is the
whole rule.  The rejection's justification is "the result is already in the
conversation above", and compaction can make that false: a read retired into an
evidence receipt, truncated to a floor, bounded on replay, folded into a
summary, or dropped from the working set is no longer answerable at the range
and detail it originally had.  Rejecting the reread anyway would tell the model
to use something it could no longer see, which is the one instruction it cannot
follow.

So the guard does not decide this from canonical history, which always remains
exact.  It reads :class:`~aura.conversation.api_view.ResultResidency` — the
authoritative record of what survived byte-identically into the request that is
actually being sent — and rejects only when the original result is still there.
There is no second compaction system and no prediction of what compaction might
do: the answer comes from the view the ladder already built.

**Failures are truth, and truth is rereadable.**  A failed tool result opens a
one-round reread grace: after a failed read, a failed write, a rejected patch,
or a failed validation, the model is expected to look again, and the duplicate
gate does not stand in its way.  The grace is exactly one round — a failure in
a later round renews it, and a round without one spends it.

**Post-edit verification is not a loop.**  An applied write retires every read
fingerprint that touched the written paths (see :meth:`note_stale_paths`, driven
by the tool round's post-write notices), so re-reading a file you just changed
returns different bytes and is never refused.  What stays refused after a write
is what stayed refused before one: issuing the identical call whose identical
result is still sitting in the request.

There is deliberately **no counter of discovery calls, files, tokens, model
rounds, or elapsed time** anywhere in production SINGLE, and no "how many files
should this task need" budget.  Distinct searches, different files, different
ranges, different arguments, changed results, failure recovery, and post-edit
verification are not loops and are never rejected.  A turn that keeps returning
genuinely new evidence may keep surveying across as many ordinary requests as
the work needs, and a rejection here is a recoverable tool result — it tells the
model to use the evidence it already has and choose a different next action, and
it never ends the turn.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS
from aura.conversation.tools.effects import (
    BUILTIN_TOOL_EFFECTS,
    DEFAULT_EXTENSIBLE_TOOL_EFFECT,
    ToolEffect,
)

#: Built-in names whose effect is observation.  Historical name set kept for
#: tests and back-compat: the runtime classifies every call through the
#: registry's authoritative tool-effect metadata
#: (:meth:`ToolRegistry.tool_effect`), which also covers Git, Godot, dynamic,
#: MCP, drone, workspace, and web tools.  Nothing here decides behaviour.
READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_files",
    "read_file_range",
    "read_file_outline",
    "read_task_context",
    "list_directory",
    "glob",
    "grep_search",
    "find_usages",
    "search_codebase",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
})

#: Historical narrow read tool names, kept for transcript replay.  The
#: canonical narrow read is ``read_file`` with actual bounded ``offset`` and
#: ``limit``; see :func:`is_narrow_read`.
NARROW_READ_TOOLS: frozenset[str] = frozenset({
    "read_file_range",
    "read_file_outline",
})

#: Historical read-only-minus-narrow set.  Superseded by
#: :meth:`PreEditLoopGuard._is_observation`, which uses the effect metadata;
#: kept for tests and back-compat.
DISCOVERY_TOOLS: frozenset[str] = frozenset(READ_ONLY_TOOLS - NARROW_READ_TOOLS)

#: Historical progress sets.  Superseded by the effect classification in
#: :meth:`PreEditLoopGuard.observe_result`; kept for tests and back-compat.
DIAGNOSTIC_TOOLS: frozenset[str] = frozenset({"run_diagnostic_command"})
COMMAND_TOOLS: frozenset[str] = frozenset(TERMINAL_TOOLS | DIAGNOSTIC_TOOLS)
PROGRESS_TOOLS: frozenset[str] = frozenset(WRITE_TOOLS | COMMAND_TOOLS)


def is_narrow_read(name: str, args: Any) -> bool:
    """Return whether a call pulls a bounded slice of one known file.

    ``read_file`` with actual bounded ``offset`` and ``limit`` is the canonical
    narrow read; the historical range/outline tools stay narrow for transcript
    replay.  An unbounded ``read_file`` — and every other observation call — is
    broad observation.
    """
    if name in NARROW_READ_TOOLS:
        return True
    if name != "read_file" or not isinstance(args, dict):
        return False
    offset = args.get("offset")
    limit = args.get("limit")
    return (
        isinstance(offset, int)
        and not isinstance(offset, bool)
        and isinstance(limit, int)
        and not isinstance(limit, bool)
        and offset >= 1
        and limit >= 1
    )


def _default_effect_lookup(name: str) -> ToolEffect:
    """Classify without a live registry: the built-in table, else fail safe.

    Matches the registry's own resolution, including its fail-safe default for
    extensible and unrecognised tools — an unclassified name is treated as
    consequential, never as a free observation whose repeats can be gated.
    """
    return BUILTIN_TOOL_EFFECTS.get(name, DEFAULT_EXTENSIBLE_TOOL_EFFECT)


#: Result-payload keys that describe call mechanics, not evidence content.
#: Stripped from evidence fingerprints so that two calls returning effectively
#: identical results (differing only in engine, search stats, echoed args, or
#: derived summaries) are recognised as the same evidence.  Kept for the
#: evidence ledger; nothing here decides the duplicate gate.
_NON_EVIDENCE_KEYS: frozenset[str] = frozenset({
    "ok",
    "error",
    "engine",
    "searched_files",
    "skipped_files",
    "skipped_details",
    "summary",
    "regex_mode",
    "auto_regex_retry",
    "include_pattern",
    "regex_hint",
    "pattern",
})

#: Payload keys that identify *which* failure this is.  A failure is "the same
#: shape" when the tool, the command, the failure class, the exit code and the
#: first line of the error match.  Kept for the failure ledger; nothing here
#: decides the duplicate gate.
_FAILURE_IDENTITY_KEYS: tuple[str, ...] = (
    "requested_command",
    "command",
    "failure_class",
    "reason",
    "exit_code",
    "path",
)

DUPLICATE_READ_REASON = "duplicate_read_before_first_edit"

_DUPLICATE_READ_MESSAGE = (
    "You already ran this exact call earlier in this turn and its result is "
    "still in the conversation above. Reading it again returns the same bytes "
    "and adds no evidence. Use what you already have and make the edit. "
    "Rereads after a failed tool call, a stale-file notice, or a pending "
    "edit-recovery step are allowed and are not blocked by this guard, and so "
    "is rereading something whose earlier result is no longer in front of you."
)


def read_fingerprint(name: str, args: Any) -> str:
    """Return a stable identity for one read-only call and its arguments."""
    try:
        rendered = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        rendered = repr(args)
    return f"{name}:{rendered}"


def evidence_fingerprint(name: str, payload: Any) -> str | None:
    """Return a normalized identity for the evidence a successful result carries.

    The fingerprint is derived from the *result payload*, never from the call
    arguments, so re-issuing a read with cosmetic argument changes cannot
    launder the same evidence as new.  Bookkeeping keys (search stats, engine
    names, echoed arguments, derived summaries) are stripped; the content —
    file bytes, selected ranges, match lists, directory listings — is the
    fingerprint.  ``None`` is returned when there is no payload to count.

    Reporting only: the send loop and the duplicate gate never branch on it.
    """
    if payload is None or payload == "":
        return None
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            data = None
    if isinstance(data, dict):
        normalized = {
            key: value
            for key, value in data.items()
            if key not in _NON_EVIDENCE_KEYS
        }
        rendered = json.dumps(
            normalized, sort_keys=True, ensure_ascii=False, default=str
        )
    elif data is None:
        rendered = str(payload)
    else:
        rendered = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return f"{name}:{rendered}"


def failure_fingerprint(name: str, payload: Any) -> str:
    """Return a stable identity for the *shape* of one failed tool result.

    Derived from the result, like every other signal here: the tool, the
    command it was asked to run, the failure class, the exit code and the first
    line of the error.  Reporting only: nothing here decides the duplicate
    gate.
    """
    data = _decode_payload(payload)
    if not isinstance(data, dict):
        rendered = str(payload).strip() if payload not in (None, "") else ""
        return f"{name}:{_condense(rendered)}"
    parts = [name]
    for key in _FAILURE_IDENTITY_KEYS:
        if key in data:
            parts.append(f"{key}={_condense(str(data[key]))}")
    for key in ("error", "stderr", "message"):
        text = data.get(key)
        if isinstance(text, str) and text.strip():
            parts.append(f"{key}={_condense(text.strip().splitlines()[0])}")
            break
    return "|".join(parts)


def _condense(text: str) -> str:
    """Lowercase, collapse whitespace, and bound the length of *text*."""
    return " ".join(text.lower().split())[:200]


@dataclass
class PreEditLoopGuard:
    """Track read repetition and whether an applied mutation has landed.

    A generic loop-safety gate, not a workflow state machine: ``focused``,
    stall detection, and cycle detection do not exist here.  What the guard
    answers is exactly two things —

    * has an applied mutation landed yet (``write_applied``), which the
      completion contract reads to know whether the turn still owes its edit;
    * is this exact observation call a wasteful repeat of a result still in the
      request (``check``).

    A distinct failed tool result opens one round of reread grace so the model
    can look again at whatever the failure contradicts.
    """

    #: Authoritative effect classifier — the live registry's ``tool_effect``
    #: when one is wired in, else the built-in table plus the observation
    #: default.  The guard never re-derives intent from a tool's name.
    effect_lookup: Callable[[str], ToolEffect] = field(
        default=_default_effect_lookup, repr=False, compare=False
    )

    #: The one integration seam for a decision the guard cannot make itself.
    #:
    #: The guard's rule is residency: "is that exact result still in front of
    #: the model".  A *deliberate* internal trajectory rollover breaks that
    #: rule's premise — the result left the request because Aura retired a
    #: non-converging investigation on purpose, not because natural compaction
    #: needed the room — and only
    #: :class:`~aura.conversation.single_trajectory.SingleTrajectoryController`
    #: knows a segment boundary happened.  So the owner supplies a callable that
    #: returns a rejection payload, or ``None``, and the guard simply asks it
    #: for calls its own rule already cleared.  Nothing about segments,
    #: rollovers, workflow phases, or progress is stored or decided here.
    cross_segment_check: Callable[[str, Any], dict[str, Any] | None] | None = field(
        default=None, repr=False, compare=False
    )

    seen_reads: dict[str, int] = field(default_factory=dict)
    seen_evidence: set[str] = field(default_factory=set)
    write_applied: bool = False
    blocked_calls: int = 0
    #: How many rereads were allowed because the original result had left the
    #: outbound view. Telemetry only; nothing branches on it.
    rereads_allowed_after_compaction: int = 0

    #: Tool-call ids per read fingerprint, oldest first. The link between "this
    #: exact call was made before" and "is that call's result still in the
    #: request", which is what residency is keyed on.
    read_call_ids: dict[str, list[str]] = field(default_factory=dict)
    #: Ids whose original result survived byte-identically into the outbound
    #: view built for the request now being sent.
    _resident_call_ids: frozenset[str] = frozenset()
    #: Whether any outbound view has been reported yet. Before the first one the
    #: guard has no residency evidence, so it keeps its historical behaviour
    #: rather than guessing that a result has gone.
    _residency_known: bool = False

    #: Failure shapes already seen this turn. Reporting only.
    seen_failures: set[str] = field(default_factory=set)
    #: How many times a failure this turn had already been seen. Reporting only.
    repeated_failures: int = 0

    #: Whether the round *after* the current one may reread freely because a
    #: tool failed.  Set by any failed result, spent by the next round boundary,
    #: renewed by another failure.
    _reread_grace: bool = False
    #: The snapshot of the grace at the start of the current round — the round
    #: that follows a failure is the one that gets to reread.
    _reread_grace_active: bool = False

    # ---- outbound API-view residency -------------------------------------

    def note_api_view_residency(self, resident_call_ids: Any) -> None:
        """Record which results survived into the request about to be sent.

        Called by the send loop with
        :attr:`~aura.conversation.api_view.ResultResidency.resident_call_ids`
        from the view it just built, every round.  This is the guard's only
        source of truth about what the model can still see, and it is a report
        of what compaction *did*, never a prediction of what it might do.
        """
        self._resident_call_ids = frozenset(
            str(call_id) for call_id in (resident_call_ids or ()) if call_id
        )
        self._residency_known = True

    def is_rereadable(self, fingerprint: str) -> bool:
        """Whether this exact observation may be issued again.

        True when the turn has recorded call ids for the fingerprint and *none*
        of them is still resident in the outbound view — the result has been
        retired into a receipt, truncated, replay-bounded, summarised, or
        dropped, so the model genuinely cannot answer from it any more.

        Fail-closed everywhere else.  Before any view has been reported, and for
        a fingerprint whose calls were recorded without ids, the guard has no
        evidence that anything left the request, so the duplicate stays
        rejectable exactly as it was.
        """
        if not self._residency_known:
            return False
        call_ids = self.read_call_ids.get(fingerprint) or []
        if not call_ids:
            return False
        return not any(call_id in self._resident_call_ids for call_id in call_ids)

    # ---- effect classification -------------------------------------------

    def _effect(self, name: str) -> ToolEffect:
        return self.effect_lookup(name)

    def _is_observation(self, name: str) -> bool:
        """Whether the registry classifies *name* as a read-only inspection."""
        return self._effect(name) is ToolEffect.OBSERVATION

    def _is_mutation(self, name: str) -> bool:
        """Whether the registry classifies *name* as a workspace/file edit."""
        return self._effect(name) is ToolEffect.MUTATION

    # ---- round lifecycle -------------------------------------------------

    def begin_round(self) -> None:
        """Open the reread grace for the round that follows a failure.

        ``_reread_grace`` is what a failed result sets; this snapshot is what
        :meth:`check` reads, so exactly the round after a failure gets the
        grace, and a round without a failure spends it.
        """
        self._reread_grace_active = self._reread_grace
        self._reread_grace = False

    def end_round(self) -> None:
        """Close the round.  Nothing else happens here.

        The send loop owns every judgement about whether the turn is advancing;
        the guard does not conclude that discovery has stopped.  The only state
        that survives a round boundary is the reread grace, which is snapshotted
        on the next :meth:`begin_round`.
        """
        return

    # ---- pre-execution gate ----------------------------------------------

    def check(
        self,
        name: str,
        args: Any,
        *,
        recovery_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Return a rejection payload for a call this turn should not make.

        ``None`` means the call may run.  Two rejections are possible, and both
        are recoverable — never terminal:

        * an unjustified exact repeat read whose original result is still in the
          request;
        * whatever :attr:`cross_segment_check` refuses, when a trajectory owner
          has wired that seam in.

        Broad discovery is never refused by a count; a turn that keeps returning
        genuinely new evidence is allowed to keep surveying for as long as the
        work needs.  Nothing else in production SINGLE bounds the turn.
        """
        if not self._is_observation(name):
            return None
        if self._reread_grace_active or recovery_pending:
            # A distinct failure, a stale-file notice, or a pending edit-recovery
            # step already told the model to read again. Neither boundary applies,
            # and neither does the cross-segment seam.
            return None

        fingerprint = read_fingerprint(name, args)
        previous = self.seen_reads.get(fingerprint, 0)
        if previous >= 1:
            if not self.is_rereadable(fingerprint):
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
            # The original result is no longer materially in the outbound view.
            # "You already have this" would be false, so as far as *this* rule is
            # concerned the reread is the model recovering context it lost.
            self.rereads_allowed_after_compaction += 1
        return self._cross_segment_rejection(name, args)

    def _cross_segment_rejection(
        self, name: str, args: Any
    ) -> dict[str, Any] | None:
        """Ask the wired-in trajectory owner about a call this rule cleared."""
        if self.cross_segment_check is None:
            return None
        rejection = self.cross_segment_check(name, args)
        if rejection is None:
            return None
        self.blocked_calls += 1
        return rejection

    def record(self, name: str, args: Any, call_id: str = "") -> None:
        """Record one accepted tool call for this round.

        Deliberately records no progress.  Intent is not evidence: a command
        that is about to fail, or never starts, must not reset any boundary
        before anyone has seen its result.

        ``call_id`` is the provider's tool-call id, which is what the outbound
        view keys residency on.  It is optional so that direct unit tests and
        replayed transcripts still work; without it the call's result is simply
        never known to have left the request, and the duplicate rule stays as
        strict as it was.
        """
        if not self._is_observation(name):
            return
        fingerprint = read_fingerprint(name, args)
        self.seen_reads[fingerprint] = self.seen_reads.get(fingerprint, 0) + 1
        if call_id:
            self.read_call_ids.setdefault(fingerprint, []).append(str(call_id))

    # ---- result accounting -----------------------------------------------

    def observe_result(self, name: str, ok: bool, payload: Any = None) -> None:
        """Fold one tool result into the guard's state.

        A mutation that explicitly applied flips ``write_applied`` — the one
        fact the send loop's completion truth reads.  A failure of any tool
        opens the one-round reread grace,
        because a failed read, write, patch, or validation is exactly when the
        model should be allowed to look again.
        """
        if not ok:
            self.note_failure(name, payload)
            return
        if self._is_mutation(name) and _payload_applied(payload):
            self.write_applied = True
            return
        if self._is_observation(name):
            fingerprint = evidence_fingerprint(name, payload)
            if fingerprint is not None:
                self.seen_evidence.add(fingerprint)

    def note_failure(self, name: str = "", payload: Any = None) -> None:
        """A tool failed: open the one-round reread grace.

        Kept as a stable API for callers that record a failure outside
        :meth:`observe_result`.  Repeating a failure renews the same single
        round of grace — grace is never latched open past the round boundary.
        """
        fingerprint = failure_fingerprint(name, payload)
        if fingerprint in self.seen_failures:
            self.repeated_failures += 1
        else:
            self.seen_failures.add(fingerprint)
        self._reread_grace = True

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
                self.read_call_ids.pop(fingerprint, None)


def _decode_payload(payload: Any) -> Any:
    """Return *payload* as structured data when it is JSON, else as-is."""
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return None
    return payload


def _payload_applied(payload: Any) -> bool:
    """Return whether a write tool's result *proves* the change actually landed.

    Fail-closed, matching the direct-write refresh contract in
    :func:`aura.conversation.manager_tool_round._result_payload_applied`: a
    mutation counts as applied only when its authoritative payload explicitly
    carries ``applied is True``.  A malformed payload, a non-dictionary payload,
    a payload with no ``applied`` field, and a truthy-but-not-``True`` value are
    all "not applied" — an ambiguous result must never be read as a landed edit.
    """
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return False
    if not isinstance(data, dict):
        return False
    return data.get("applied") is True


__all__ = [
    "COMMAND_TOOLS",
    "DIAGNOSTIC_TOOLS",
    "DISCOVERY_TOOLS",
    "DUPLICATE_READ_REASON",
    "NARROW_READ_TOOLS",
    "PROGRESS_TOOLS",
    "PreEditLoopGuard",
    "READ_ONLY_TOOLS",
    "evidence_fingerprint",
    "failure_fingerprint",
    "is_narrow_read",
    "read_fingerprint",
]
