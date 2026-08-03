"""Deterministic pre-edit loop guard for the SINGLE production runtime.

Three mechanical signals, all derived from state the send loop already keeps:

1. **Exact read fingerprints, checked against the outbound view.** Before the
   first applied write, the same read-only tool call with the same arguments is
   rejected the second time it is issued — *while its first result is still
   materially in front of the model*.  That qualifier is the whole rule.  The
   rejection's justification is "the result is already in the conversation
   above", and compaction can make that false: a read retired into an evidence
   receipt, truncated to a floor, bounded on replay, folded into a summary, or
   dropped from the working set is no longer answerable at the range and detail
   it originally had.  Rejecting the reread anyway told the model to use
   something it could no longer see, which is the one instruction it cannot
   follow.

   So the guard does not decide this from canonical history, which always
   remains exact.  It reads
   :class:`~aura.conversation.api_view.ResultResidency` — the authoritative
   record of what survived byte-identically into the request that is actually
   being sent — and rejects only when the original result is still there.  There
   is no second compaction system and no prediction of what compaction might do:
   the answer comes from the view the ladder already built.

2. **Stalled discovery → the focused action protocol.** A round that ran tools,
   observed their results, and produced *no new evidence* and *no progress* has
   stopped moving the turn forward.  That single stalled round is the protocol
   transition: :attr:`focused` becomes true, and the send loop answers the next
   model request with the action-serialization request — thinking off, mutation
   tools plus ``report_blocker``, exactly one tool call — instead of another
   ordinary reasoning stream.

   A round whose calls were *all* bookkeeping is exempt the first time it
   happens.  Publishing a TODO records state; it is not an attempt to move the
   turn, so its failure to move the turn proves nothing — and the capsule tells
   the agent to publish that checklist early, which made the very first round of
   an ordinary turn a "stall" that forced a blind edit before a single file had
   been read.  The exemption is not a free pass: the same bookkeeping round
   arriving twice is recognised through the round signatures rule 3 already
   keeps, and stalls normally.

3. **Short repeating cycles.** Rule 2 asks whether *this* round moved; a turn
   can still circle across rounds — read A, run B, read A, run B — where each
   round in isolation looks like it did something.  Every round is folded into
   one signature built from the fingerprints rules 1 and 2 already compute, and
   an ``A, B, A, B`` repeat of two *different* signatures is the same
   transition as a stall.  This is pattern equality over existing fingerprints,
   not a score.

There is deliberately **no counter of discovery calls, files, tokens, or
elapsed time**, and no "how many files should this task need" budget: discovery
is never refused by a count, and a turn that keeps returning genuinely new
evidence may keep surveying across as many ordinary requests as the work needs.
What ends discovery is evidence of circling, never arithmetic about how long
looking has taken.  The 300-call emergency brake in
:mod:`aura.conversation.tool_limits` remains the catastrophic backstop and is
not workflow control.

Evidence is judged by the *result*, never by the call arguments.  Each
successful read-only result is folded into a normalized fingerprint: a
genuinely new file, line range, search match, or result payload resets the
stall, while a result that is effectively identical to one already seen —
cosmetic argument changes included — does not.  Truncated reads return
genuinely new content, so a focused continuation after a truncation is normal
discovery and is never steered.  TODO updates and other bookkeeping are not
evidence.

A reread is legitimate, and is allowed, when the previous round had a tool
failure, when a stale-file notice invalidated that path, or while edit-recovery
state is pending.  Stale notices clear only the fingerprints for the paths they
name.

**Progress is owned by results, never by intent.**  Issuing a terminal or
diagnostic call proves nothing — the command may not even start.  Only an
*applied* write — one whose result payload explicitly says ``applied: True`` —
and a *successful* command count as forward progress and clear the failure
state.  A failed command leaves the round stalled, but a distinct failure opens
recovery: rereads are allowed while it is open, and the focused transition waits
so the agent can fix the command and recover.  Recovery is one round long, and
it is renewable by evidence, never by a count.  Progress or genuinely new
evidence resolves it; another distinct failure opens its own round; and a
granted round that ends with neither closes recovery outright — the reread grace
is spent *and* that same stalled round becomes the focused transition, because a
turn that cannot recover must still act rather than circle.  Re-running the same
command into the same failure is not a new distinct failure and renews nothing.
A corrected command is never blocked by this guard — commands are outside its
gate entirely — and when the correction succeeds, recovery closes like any other
progress.

There is deliberately no semantic classification of model output, no
planner/worker workflow, and no phase state machine here.  The 300-call
emergency brake in :mod:`aura.conversation.tool_limits` stays the final runaway
guard; this guard is the ordinary nudge that fires long before it.

The guard also owns the *only* failure-recovery ledger for the pre-write phase.
The focused action turn reuses it rather than keeping a retry manager of its
own: a focused mutation that failed distinctly opens the same recovery round any
other distinct failure opens (:attr:`recovery_open`), and a repeat of a failure
fingerprint already seen opens nothing.  There is no per-turn allowance of
recoveries — the send loop reads :attr:`last_round_advanced` and
:attr:`recovery_open` after every pre-write round and ends the turn only when a
round both failed to advance it and produced nothing the turn had not already
seen.
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
#: :meth:`PreEditLoopGuard._is_discovery`, which uses the effect metadata and
#: the bounded-window rule; kept for tests and back-compat.
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


#: Argument and result keys that name a file.  Used only to remember which
#: files are already known candidates, so narrow reads of them stay allowed.
_PATH_KEYS: frozenset[str] = frozenset({
    "path",
    "file",
    "file_path",
    "rel_path",
    "relative_path",
    "paths",
    "files",
})

#: Ceiling on remembered candidate paths — bookkeeping must not itself grow
#: without bound on a repository-wide glob.
_MAX_CANDIDATE_FILES: int = 2_000

#: Result-payload keys that describe call mechanics, not evidence content.
#: Stripped from evidence fingerprints so that two calls returning effectively
#: identical results (differing only in engine, search stats, echoed args, or
#: derived summaries) are recognised as the same evidence.
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
#: first line of the error match — so a retry of the identical broken command
#: is recognised as a repeat however much surrounding noise the payload carries.
_FAILURE_IDENTITY_KEYS: tuple[str, ...] = (
    "requested_command",
    "command",
    "failure_class",
    "reason",
    "exit_code",
    "path",
)

#: How many round signatures are kept.  A two-step cycle needs four, and the
#: check never looks further back, so this is bookkeeping depth, not a budget.
_MAX_ROUND_SIGNATURES: int = 8

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
    line of the error.  Two attempts at the same broken command share a
    fingerprint even when the payload differs in timing or truncation, so a
    retry cannot present itself as a new problem worth another recovery round.
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
    """Track read repetition, stalled discovery, and failure recovery pre-write.

    Protocol state, not a scoring engine: ``focused`` becomes true the first
    time a round runs tools, sees their results, and neither advanced the turn
    (an applied write or a successful command) nor gathered new evidence — or
    the first time the last four rounds form an ``A, B, A, B`` cycle over the
    same fingerprints.  Either transition hands the send loop the
    action-serialization request.  Rounds that keep returning genuinely new
    evidence are never bounded by a count and may continue as long as the work
    needs them.  A
    distinct failure opens recovery instead: rereads stay allowed while it is
    open and the focused transition waits, so the agent can correct the failing
    step rather than being pushed into a mutation the failure explains.
    """

    #: Authoritative effect classifier — the live registry's ``tool_effect``
    #: when one is wired in, else the built-in table plus the observation
    #: default.  The guard never re-derives intent from a tool's name.
    effect_lookup: Callable[[str], ToolEffect] = field(
        default=_default_effect_lookup, repr=False, compare=False
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
    #: Whether the round that just ended was an ordinary observation round: it
    #: ran tools and at least one of them was an observation. The send loop reads
    #: this to know a decision checkpoint is owed next.
    last_round_observed_evidence: bool = False
    _round_had_observation: bool = False

    #: Whether the loop has concluded that discovery is over and the next
    #: request must be the focused action request.  Set once, by the first
    #: stalled round or the first detected cycle; never reset by later evidence.
    focused: bool = False
    #: Whether the transition above was reached by cycle detection rather than
    #: by a stalled round.  Telemetry only; nothing branches on it.
    cycled: bool = False
    #: Whether the round that just ended advanced the turn — an applied write, a
    #: successful command, or genuinely new evidence.  The send loop reads this
    #: to judge whether a granted recovery round recovered anything.
    last_round_advanced: bool = False
    #: One normalized signature per completed round, newest last.  Built from
    #: the call and result fingerprints the other rules already compute; the
    #: only consumer is the ``A, B, A, B`` cycle check.
    round_signatures: list[str] = field(default_factory=list)
    #: Files already surfaced by this turn's discovery, from call arguments and
    #: from result payloads. Narrow reads of these stay allowed.
    candidate_files: set[str] = field(default_factory=set)
    #: Failure shapes already seen this turn. A repeat buys no further grace.
    seen_failures: set[str] = field(default_factory=set)
    #: How many times a failure this turn had already been seen — the signal
    #: that the agent is retrying the same broken command.
    repeated_failures: int = 0

    #: Whether an unresolved distinct failure blocks the focused transition.
    #: Set by a distinct failure and cleared either by recovery (new evidence or
    #: forward progress) or by the one granted recovery round ending without
    #: either.  While true, a stalled round is a recovery round, not a signal to
    #: force a mutation; it is never latched past that one round.
    _failure_active: bool = False
    #: Whether a reread is currently justified because the previous round
    #: opened a failure recovery.  Bounded: it is spent when the round after a
    #: distinct failure closes without a new distinct failure.
    _failure_pending: bool = False

    _round_had_tools: bool = False
    _round_made_progress: bool = False
    _round_new_evidence: bool = False
    _round_observed: bool = False
    _round_fresh_failure: bool = False
    #: Whether any call this round could have moved the turn — an observation, a
    #: mutation, or a command.  A round of pure bookkeeping leaves this false.
    _round_substantive: bool = False
    #: Whether this round's signature had already been recorded this turn.
    _round_signature_repeated: bool = False
    _round_call_parts: list[str] = field(default_factory=list)
    _round_result_parts: list[str] = field(default_factory=list)

    # ---- recovery ownership ----------------------------------------------

    @property
    def recovery_open(self) -> bool:
        """Whether a distinct failure has bought the next round as recovery.

        The single source of truth for "this failure earns one more ordinary
        round", shared by the guard's own reread grace and by the send loop's
        judgement of a focused mutation that did not apply.  It is true only
        after a *distinct* failure fingerprint; a repeat of a failure already
        seen this turn leaves it false, which is what stops a failure loop from
        renewing itself.  Read immediately after :meth:`end_round`, it answers
        "did the round that just ended produce a failure this turn had not seen
        before" — the signal that the diagnosis actually changed.
        """
        return self._failure_pending

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

    def _is_command(self, name: str) -> bool:
        """Whether the registry classifies *name* as an external command."""
        return self._effect(name) is ToolEffect.COMMAND

    def _is_discovery(self, name: str, args: Any) -> bool:
        """Whether the call opens new ground: an observation that is not a
        bounded narrow read of a file the turn already knows."""
        return self._is_observation(name) and not is_narrow_read(name, args)

    # ---- round lifecycle -------------------------------------------------

    def begin_round(self) -> None:
        self._round_had_tools = False
        self._round_made_progress = False
        self._round_new_evidence = False
        self._round_observed = False
        self._round_fresh_failure = False
        self._round_substantive = False
        self._round_signature_repeated = False
        self._round_had_observation = False
        self._round_call_parts = []
        self._round_result_parts = []

    def end_round(self) -> None:
        recovered = self._round_made_progress or self._round_new_evidence
        self.last_round_advanced = recovered
        self.last_round_observed_evidence = (
            self._round_had_tools and self._round_had_observation
        )
        self._record_round_signature()
        if recovered:
            # The failure is resolved: rereads no longer need grace and the
            # focused transition is unblocked.
            self._failure_active = False
            self._failure_pending = False
        elif self._round_fresh_failure:
            # A new distinct failure opens a recovery round for the one after
            # it, and keeps the focused transition waiting.
            self._failure_pending = True
        else:
            # The granted recovery round is over and it recovered nothing.
            # Recovery closes here — both the reread grace and the hold on the
            # focused transition — so this same stalled round is free to be the
            # transition below.  Recovery is opened only by a *distinct*
            # failure, so re-running one broken command into the failure it
            # already produced cannot keep re-granting this round forever.
            self._failure_pending = False
            self._failure_active = False

        if recovered or self._round_fresh_failure:
            return
        if not self._round_had_tools or not self._round_observed:
            return
        if not self._round_substantive and not self._round_signature_repeated:
            # Every call this round was bookkeeping — a TODO publication, a
            # memory write.  That round did not *try* to move the turn, so it is
            # no evidence the turn has stopped moving; treating it as a stall
            # forced the very first round of a turn that opens by publishing its
            # checklist straight into a blind edit.  A repeat of the same
            # bookkeeping round is a different matter, and falls through.
            return
        # A round that ran tools, saw their results, and neither advanced the
        # turn nor gathered evidence has stopped moving: hand the send loop the
        # focused action protocol.
        self.focused = True

    # ---- cycle detection --------------------------------------------------

    def _record_round_signature(self) -> None:
        """Fold the round into one signature and look for an ``A, B, A, B``.

        The signature is built only from fingerprints the guard already
        computes — the normalized call identities and the normalized result
        identities — so a round is "the same round" exactly when it made the
        same calls and got back the same results.  Order within the round is
        irrelevant, so a re-ordered batch is still recognised.

        The check is pattern equality on the last four signatures: two distinct
        alternating rounds, repeated once.  Every round in such a cycle can look
        individually productive to rule 2 — a fresh failure here, a novel-looking
        result there — which is precisely why the stalled-round rule alone
        cannot see it.  Nothing is scored, weighted, or thresholded.
        """
        signature = json.dumps(
            {
                "calls": sorted(self._round_call_parts),
                "results": sorted(self._round_result_parts),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        self._round_signature_repeated = signature in self.round_signatures
        self.round_signatures.append(signature)
        if len(self.round_signatures) > _MAX_ROUND_SIGNATURES:
            del self.round_signatures[:-_MAX_ROUND_SIGNATURES]
        if self.write_applied or len(self.round_signatures) < 4:
            return
        first, second, third, fourth = self.round_signatures[-4:]
        if first == third and second == fourth and first != second:
            self.cycled = True
            self.focused = True

    # ---- pre-execution gate ----------------------------------------------

    def check(
        self,
        name: str,
        args: Any,
        *,
        recovery_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Return a rejection payload for a call this turn should not make.

        ``None`` means the call may run.  One rejection is possible, recoverable
        and dormant once any write has applied — rereading to verify your own
        edit is normal work:

        * an unjustified exact repeat read.

        Broad discovery is never refused by a count; a turn that keeps returning
        genuinely new evidence is allowed to keep surveying, and the stalled
        round protocol plus the 300-call brake bound the rest.
        """
        if self.write_applied or not self._is_observation(name):
            return None
        if self._failure_pending or recovery_pending:
            # A distinct failure, a stale-file notice, or a pending edit-recovery
            # step already told the model to read again. Neither boundary applies.
            return None

        fingerprint = read_fingerprint(name, args)
        previous = self.seen_reads.get(fingerprint, 0)
        if previous >= 1:
            if self.is_rereadable(fingerprint):
                # The original result is no longer materially in the outbound
                # view. "You already have this" would be false, so the reread is
                # the model recovering context it lost, not circling.
                self.rereads_allowed_after_compaction += 1
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
        return None

    def record(self, name: str, args: Any, call_id: str = "") -> None:
        """Record one accepted tool call for this round.

        Deliberately records no progress.  Intent is not evidence: a command
        that is about to fail, or never starts, must not reset the stall
        counter before anyone has seen its result.  Progress is decided in
        :meth:`observe_result`.

        ``call_id`` is the provider's tool-call id, which is what the outbound
        view keys residency on.  It is optional so that direct unit tests and
        replayed transcripts still work; without it the call's result is simply
        never known to have left the request, and the duplicate rule stays as
        strict as it was.
        """
        self._round_had_tools = True
        self._round_call_parts.append(read_fingerprint(name, args))
        if self._effect(name) is not ToolEffect.BOOKKEEPING:
            # Observation, mutation, command: a call that could have moved the
            # turn. Whether it *did* is decided by its result, in
            # :meth:`observe_result` — this only records that the round tried.
            self._round_substantive = True
        if self._is_observation(name):
            self._round_had_observation = True
            fingerprint = read_fingerprint(name, args)
            self.seen_reads[fingerprint] = self.seen_reads.get(fingerprint, 0) + 1
            if call_id:
                self.read_call_ids.setdefault(fingerprint, []).append(str(call_id))
            self._note_candidates(args)

    # ---- evidence that justifies continued discovery ---------------------

    def observe_result(self, name: str, ok: bool, payload: Any = None) -> None:
        """Fold one tool result into the guard's state.

        This is the only place progress is granted.  A failure — of any tool,
        including a command — is never progress, so the round stays stalled and
        opens recovery instead.
        """
        self._round_observed = True
        if not ok:
            self._round_result_parts.append(failure_fingerprint(name, payload))
            self.note_failure(name, payload)
            return
        self._round_result_parts.append(
            evidence_fingerprint(name, payload) or f"{name}:<no-payload>"
        )
        if self._is_mutation(name) and _payload_applied(payload):
            self.write_applied = True
            self._round_made_progress = True
            return
        if self._is_command(name):
            # A command that actually ran and succeeded is real work: the
            # validation landed, so the turn moved.
            self._round_made_progress = True
            return
        if self._is_observation(name):
            self._note_candidates(_decode_payload(payload))
            fingerprint = evidence_fingerprint(name, payload)
            if fingerprint is not None and fingerprint not in self.seen_evidence:
                self.seen_evidence.add(fingerprint)
                self._round_new_evidence = True

    def note_failure(self, name: str = "", payload: Any = None) -> None:
        """A tool failed: a *distinct* failure opens recovery.

        Recovery opens reread grace for the round that follows and blocks the
        focused transition, so the agent can correct the failing step rather
        than being pushed into a mutation the failure explains.  Repeating a
        command into the failure it already produced is not new information, so
        it renews nothing: the grace already granted runs out on schedule.
        """
        fingerprint = failure_fingerprint(name, payload)
        if fingerprint in self.seen_failures:
            self.repeated_failures += 1
            return
        self.seen_failures.add(fingerprint)
        self._failure_active = True
        self._failure_pending = True
        self._round_fresh_failure = True

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

    # ---- candidate tracking ----------------------------------------------

    def is_known_candidate(self, path: Any) -> bool:
        """Return whether *path* names a file this turn has already surfaced."""
        normalized = _normalize_path(path)
        if not normalized:
            return False
        if normalized in self.candidate_files:
            return True
        # Accept an unambiguous suffix match so "aura/x.py" matches a candidate
        # recorded as "C:/repo/aura/x.py" and vice versa.
        return any(
            known.endswith("/" + normalized) or normalized.endswith("/" + known)
            for known in self.candidate_files
        )

    def _note_candidates(self, values: Any) -> None:
        for path in _iter_paths(values):
            if len(self.candidate_files) >= _MAX_CANDIDATE_FILES:
                return
            self.candidate_files.add(path)


def _normalize_path(value: Any) -> str:
    """Return a comparable path string, or ``\"\"`` when *value* is not one."""
    if not isinstance(value, str):
        return ""
    return value.replace("\\", "/").strip().strip("/")


def _decode_payload(payload: Any) -> Any:
    """Return *payload* as structured data when it is JSON, else as-is."""
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except (TypeError, ValueError):
            return None
    return payload


def _iter_paths(value: Any, _depth: int = 0):
    """Yield normalized file paths named anywhere inside *value*.

    Walks nested dicts and lists looking for the handful of keys tools use to
    name files. Purely bookkeeping for "which files does this turn already know
    about" — it never decides whether a call is allowed on its own.
    """
    if _depth > 6:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _PATH_KEYS:
                if isinstance(item, str):
                    normalized = _normalize_path(item)
                    if normalized:
                        yield normalized
                elif isinstance(item, list):
                    for entry in item:
                        normalized = _normalize_path(entry)
                        if normalized:
                            yield normalized
            if isinstance(item, (dict, list)):
                yield from _iter_paths(item, _depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                yield from _iter_paths(item, _depth + 1)


def _payload_applied(payload: Any) -> bool:
    """Return whether a write tool's result *proves* the change actually landed.

    Fail-closed, matching the direct-write refresh contract in
    :func:`aura.conversation.manager_tool_round._result_payload_applied`: a
    mutation counts as applied only when its authoritative payload explicitly
    carries ``applied is True``.  A malformed payload, a non-dictionary payload,
    a payload with no ``applied`` field, and a truthy-but-not-``True`` value are
    all "not applied" — an ambiguous result must never be read as a landed edit,
    because that would clear the guard's pre-write gates on no evidence.
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
