"""Deterministic pre-edit loop guard for the SINGLE production runtime.

Three mechanical signals, all derived from state the send loop already keeps:

1. **Exact read fingerprints.** Before the first applied write, the same
   read-only tool call with the same arguments is rejected the second time it
   is issued.  The result is already in the conversation above; repeating it
   only feeds the model its own transcript again.
2. **Stagnant discovery rounds.** After
   :data:`MAX_STAGNANT_ROUNDS_BEFORE_STEER` consecutive rounds that produced
   *no new evidence* — no new file, range, search result, or result payload,
   no applied write, and no terminal command — one concise internal steering
   message is injected, once per turn, telling the agent to use the evidence
   it has and implement.
3. **Cumulative discovery budget.** Signal 2 only fires when discovery *stops*
   producing evidence.  A turn can also burn an entire context window while
   every single call returns something genuinely new — a wide codebase always
   has one more file to open.  So the accepted discovery calls made before the
   first applied write are simply counted.  Past
   :data:`MAX_DISCOVERY_CALLS_BEFORE_FOCUS` one focus instruction is injected;
   past a further :data:`MAX_DISCOVERY_CALLS_AFTER_FOCUS` targeted calls,
   continued *broad* discovery is rejected with a structured recoverable
   payload.

   These two numbers are mechanical safety budgets on spend, not a judgement
   about the task.  Nothing here classifies task complexity, scores difficulty,
   or decides that a task "should" need fewer files — it only says that a turn
   which has opened this much and still written nothing must now narrow down.
   Narrow reads (ranges and outlines) of files already discovered stay
   available afterwards, so narrowing down is always possible.

Evidence is judged by the *result*, never by the call arguments.  Each
successful read-only result is folded into a normalized fingerprint: a
genuinely new file, line range, search match, or result payload resets the
stall counter, while a result that is effectively identical to one already
seen — cosmetic argument changes included — does not.  Truncated reads return
genuinely new content, so a focused continuation after a truncation is normal
discovery and is never steered.  TODO updates and other bookkeeping are not
evidence.

A reread is legitimate, and is allowed, when the previous round had a tool
failure, when a stale-file notice invalidated that path, or while edit-recovery
state is pending.  Stale notices clear only the fingerprints for the paths they
name.

**Progress is owned by results, never by intent.**  Issuing a terminal or
diagnostic call proves nothing — the command may not even start.  Only an
*applied* write and a *successful* command count as forward progress and reset
the stall counter.  A failed command leaves the round stagnant, so steering
still fires on a turn that is only failing.  One *distinct* failure shape buys
one recovery round of reread grace; re-running the same command into the same
failure buys nothing further, and is not new evidence.  A corrected command is
never blocked by this guard — commands are outside its gate entirely — and when
the correction succeeds, stagnation resets like any other progress.

There is deliberately no semantic classification of model output, no
planner/worker workflow, and no phase state machine here.  The 300-call
emergency brake in :mod:`aura.conversation.tool_limits` stays the final runaway
guard; this guard is the ordinary nudge that fires long before it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from aura.conversation.tool_limits import TERMINAL_TOOLS, WRITE_TOOLS
from aura.conversation.tools.effects import BUILTIN_TOOL_EFFECTS, ToolEffect

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
    """Classify without a live registry: the built-in table, else observation.

    Matches the registry's own default for dynamic, MCP, drone, and workspace
    tools that declare no effect metadata.
    """
    return BUILTIN_TOOL_EFFECTS.get(name, ToolEffect.OBSERVATION)

#: Rounds of tools-without-new-evidence tolerated before one steering message.
MAX_STAGNANT_ROUNDS_BEFORE_STEER: int = 2

#: Accepted discovery calls tolerated before the first applied write, after
#: which one focus instruction is injected.  A mechanical spend budget — not a
#: statement about how many files the task "should" need.
MAX_DISCOVERY_CALLS_BEFORE_FOCUS: int = 12

#: Further discovery calls allowed after the focus instruction, so the agent
#: can confirm the one or two files it named before committing to an edit.
MAX_DISCOVERY_CALLS_AFTER_FOCUS: int = 4

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

#: Rounds of reread grace bought by one distinct failure.  Spent by the round
#: that observed the failure and the one after it, so exactly one round may
#: reread while recovering.
FAILURE_GRACE_ROUNDS: int = 2

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

DUPLICATE_READ_REASON = "duplicate_read_before_first_edit"
DISCOVERY_EXHAUSTED_REASON = "discovery_budget_exhausted_before_first_edit"

#: Named in the rejection payload so the recovery path is explicit rather than
#: something the model has to guess at.  ``read_file`` stays available only as
#: a bounded offset/limit window of a file the turn already knows.
_STILL_AVAILABLE_TOOLS: tuple[str, ...] = (
    "read_file",
    "write_file",
    "patch_file",
)

_DUPLICATE_READ_MESSAGE = (
    "You already ran this exact call earlier in this turn and its result is "
    "still in the conversation above. Reading it again returns the same bytes "
    "and adds no evidence. Use what you already have and make the edit. "
    "Rereads after a failed tool call, a stale-file notice, or a pending "
    "edit-recovery step are allowed and are not blocked by this guard."
)

_STEERING_MESSAGE = (
    "Loop guard: {rounds} consecutive rounds produced no new evidence and no "
    "edit applied. You have the evidence you need. Do not re-read, restate "
    "the plan, or re-derive the decision — make the change now with "
    "write_file or patch_file, then validate it. If something genuinely "
    "blocks the edit, name that blocker in one sentence and stop."
)

_FOCUS_MESSAGE = (
    "Loop guard: {calls} discovery calls have run in this turn and nothing has "
    "been written yet. Stop surveying and commit to a target. Name the "
    "specific file(s) you are about to change, then confirm only what you "
    "still need with a bounded read_file (offset and limit) on those files and "
    "make the edit. You have about {remaining} more broad discovery calls "
    "before they are refused; narrow reads of files you have already found "
    "stay available either way. If you genuinely cannot identify a target, say "
    "so in one sentence and stop."
)

_DISCOVERY_EXHAUSTED_MESSAGE = (
    "This turn has spent its broad-discovery budget ({calls} discovery calls) "
    "without applying a single edit, so further exploration is refused. This "
    "is not a failure of the tool and not a reason to try a different search: "
    "read_file with a bounded offset/limit window on the files you have "
    "already found still works, and so do write_file and patch_file. Narrow to "
    "a target file, confirm the lines you need, and make the change. Once any "
    "edit applies, this limit is lifted for the rest of the turn. If you truly "
    "cannot proceed, name the blocker in one sentence and stop."
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
    """Track read repetition, stagnant rounds, and discovery spend pre-write."""

    #: Authoritative effect classifier — the live registry's ``tool_effect``
    #: when one is wired in, else the built-in table plus the observation
    #: default.  The guard never re-derives intent from a tool's name.
    effect_lookup: Callable[[str], ToolEffect] = field(
        default=_default_effect_lookup, repr=False, compare=False
    )

    seen_reads: dict[str, int] = field(default_factory=dict)
    seen_evidence: set[str] = field(default_factory=set)
    stagnant_rounds: int = 0
    write_applied: bool = False
    steered: bool = False
    blocked_calls: int = 0

    #: Accepted discovery calls made before the first applied write.
    discovery_calls: int = 0
    #: Whether the one focus instruction has been handed to the send loop.
    focused: bool = False
    #: Files already surfaced by this turn's discovery, from call arguments and
    #: from result payloads. Narrow reads of these stay allowed after the
    #: budget is spent.
    candidate_files: set[str] = field(default_factory=set)
    #: Failure shapes already seen this turn. A repeat buys no further grace.
    seen_failures: set[str] = field(default_factory=set)
    #: How many times a failure this turn had already been seen — the signal
    #: that the agent is retrying the same broken command.
    repeated_failures: int = 0

    # Grace budget in rounds; >0 means a reread is currently justified.
    _grace_rounds: int = 0
    _round_had_tools: bool = False
    _round_made_progress: bool = False
    _round_new_evidence: bool = False

    # ---- discovery budget ------------------------------------------------

    @property
    def discovery_limit(self) -> int:
        """Total discovery calls allowed right now, focus allowance included."""
        limit = MAX_DISCOVERY_CALLS_BEFORE_FOCUS
        if self.focused:
            limit += MAX_DISCOVERY_CALLS_AFTER_FOCUS
        return limit

    @property
    def discovery_exhausted(self) -> bool:
        """Whether broad discovery is spent (focus issued and allowance used)."""
        return self.focused and self.discovery_calls >= self.discovery_limit

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

    def end_round(self) -> None:
        if self._grace_rounds > 0:
            self._grace_rounds -= 1
        if self._round_made_progress or self._round_new_evidence:
            self.stagnant_rounds = 0
        elif self._round_had_tools:
            self.stagnant_rounds += 1

    # ---- pre-execution gate ----------------------------------------------

    def check(
        self,
        name: str,
        args: Any,
        *,
        recovery_pending: bool = False,
    ) -> dict[str, Any] | None:
        """Return a rejection payload for a call this turn should not make.

        ``None`` means the call may run.  Two rejections are possible, both
        recoverable and both dormant once any write has applied — rereading to
        verify your own edit is normal work:

        * an unjustified exact repeat read, and
        * broad discovery after the cumulative budget is spent.
        """
        if self.write_applied or not self._is_observation(name):
            return None
        if self._grace_rounds > 0 or recovery_pending:
            # A failure, a stale-file notice, or a pending edit-recovery step
            # already told the model to read again. Neither boundary applies.
            return None

        fingerprint = read_fingerprint(name, args)
        previous = self.seen_reads.get(fingerprint, 0)
        if previous >= 1:
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

        # Narrow reads — a bounded read_file window, plus the historical range
        # and outline tools kept for replay — stay available past this point,
        # so the turn can always narrow down to an edit. They remain subject
        # to the duplicate check above, since an exact repeat still returns the
        # same bytes.
        if self._is_discovery(name, args) and self.discovery_exhausted:
            self.blocked_calls += 1
            return {
                "ok": False,
                "loop_guard": True,
                "recoverable": True,
                "reason": DISCOVERY_EXHAUSTED_REASON,
                "tool": name,
                "discovery_calls": self.discovery_calls,
                "discovery_limit": self.discovery_limit,
                "still_available": _STILL_AVAILABLE_TOOLS,
                "known_candidate_files": sorted(self.candidate_files)[:20],
                "message": _DISCOVERY_EXHAUSTED_MESSAGE.format(
                    calls=self.discovery_calls
                ),
            }
        return None

    def record(self, name: str, args: Any) -> None:
        """Record one accepted tool call for this round.

        Deliberately records no progress.  Intent is not evidence: a command
        that is about to fail, or never starts, must not reset the stall
        counter before anyone has seen its result.  Progress is decided in
        :meth:`observe_result`.
        """
        self._round_had_tools = True
        if self._is_observation(name):
            fingerprint = read_fingerprint(name, args)
            self.seen_reads[fingerprint] = self.seen_reads.get(fingerprint, 0) + 1
            self._note_candidates(args)
        if self._is_discovery(name, args) and not self.write_applied:
            # Counted per accepted call, regardless of whether the result was
            # new: a turn can survey forever while every call returns something.
            self.discovery_calls += 1

    # ---- evidence that justifies continued discovery ---------------------

    def observe_result(self, name: str, ok: bool, payload: Any = None) -> None:
        """Fold one tool result into the guard's state.

        This is the only place progress is granted.  A failure — of any tool,
        including a command — is never progress, so the round stays stagnant
        and the ordinary steering still fires on a turn that is only failing.
        """
        if not ok:
            self.note_failure(name, payload)
            return
        if self._is_mutation(name) and _payload_applied(payload):
            self.write_applied = True
            self.stagnant_rounds = 0
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
        """A tool failed: one *distinct* failure buys one round of reread grace.

        Repeating a command into the failure it already produced is not new
        information, so it renews nothing: the grace already granted runs out
        on schedule and the round still counts as stagnant.  That is what stops
        a broken command from reopening pre-edit planning indefinitely.
        """
        fingerprint = failure_fingerprint(name, payload)
        if fingerprint in self.seen_failures:
            self.repeated_failures += 1
            return
        self.seen_failures.add(fingerprint)
        self._grace_rounds = FAILURE_GRACE_ROUNDS

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

    # ---- steering --------------------------------------------------------

    def take_steering_message(self) -> str:
        """Return the one steering message when it is due, else ``\"\"``."""
        if self.write_applied or self.steered:
            return ""
        if self.stagnant_rounds < MAX_STAGNANT_ROUNDS_BEFORE_STEER:
            return ""
        self.steered = True
        return _STEERING_MESSAGE.format(rounds=self.stagnant_rounds)

    def take_focus_message(self) -> str:
        """Return the one discovery-focus instruction when due, else ``\"\"``.

        Fires at most once per turn, and never after a write has applied.
        Issuing it opens the small post-focus allowance.
        """
        if self.write_applied or self.focused:
            return ""
        if self.discovery_calls < MAX_DISCOVERY_CALLS_BEFORE_FOCUS:
            return ""
        self.focused = True
        return _FOCUS_MESSAGE.format(
            calls=self.discovery_calls,
            remaining=MAX_DISCOVERY_CALLS_AFTER_FOCUS,
        )

    def take_internal_messages(self) -> list[str]:
        """Return every internal instruction due after this round, in order.

        The send loop appends these as ``aura_internal`` user messages. They
        are Aura's own steering, not the user's turn, and must never be treated
        as a new user turn boundary.
        """
        messages = [self.take_focus_message(), self.take_steering_message()]
        return [message for message in messages if message]


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
    """Return whether a write tool's result claims the change actually landed."""
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except (TypeError, ValueError):
            return True
    if not isinstance(data, dict):
        return True
    if "applied" in data:
        return bool(data["applied"])
    return True


__all__ = [
    "COMMAND_TOOLS",
    "DIAGNOSTIC_TOOLS",
    "DISCOVERY_EXHAUSTED_REASON",
    "DISCOVERY_TOOLS",
    "DUPLICATE_READ_REASON",
    "FAILURE_GRACE_ROUNDS",
    "failure_fingerprint",
    "MAX_DISCOVERY_CALLS_AFTER_FOCUS",
    "MAX_DISCOVERY_CALLS_BEFORE_FOCUS",
    "MAX_STAGNANT_ROUNDS_BEFORE_STEER",
    "NARROW_READ_TOOLS",
    "PROGRESS_TOOLS",
    "PreEditLoopGuard",
    "READ_ONLY_TOOLS",
    "evidence_fingerprint",
    "read_fingerprint",
]
