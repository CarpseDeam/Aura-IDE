"""Non-destructive, block-aware construction of the API message view.

Two rules drive this module.

1. **Canonical history is never edited to fit a context window.** Everything
   here operates on a deep copy. What the user (and the transcript, and a
   later re-read) sees stays exact and durable; only the outbound view shrinks.

2. **Compaction preserves structure.** Tool results are JSON strings. The old
   path cut them with a raw character prefix (`content[:8000]`), which emits
   syntactically invalid JSON — the model then had to guess at a shredded
   payload, re-read the same file, and the turn spiralled. Here a result is
   parsed and its *string leaves* are shrunk, then re-serialised, so the
   envelope (every key, every path, every hash) survives and the result is
   always parseable.

Invariants held by `build_api_view`:

* every assistant message inside the *current real user turn* keeps its
  ``content`` and its ``reasoning_content`` — that is the model's working state
  for the request it is still executing, and dropping it is what made the model
  restate its plan after every tool round (see ``_strip_superseded_reasoning``
  for where the turn begins);
* reasoning from completed batches inside that same turn is replayed verbatim
  once a later batch opens: the provider's context cache is an exact prefix
  match, so stripping a batch's reasoning on a later round rewrites an
  already-sent prefix and turns the whole suffix into a cache miss (DeepSeek
  Flash input is 50x more expensive on a miss than on a hit). Replayed
  reasoning is replayed at cache-hit prices; a smaller unstable prefix is not
  cheaper;
* reasoning is shed only *across* real user turns. Whether the wire format then
  carries that reasoning as provider-native ``reasoning_content`` or as
  Anthropic ``thinking`` blocks is a transport decision, made in
  ``aura.client.anthropic_stream``, not here;
* an assistant message with ``tool_calls`` is always accompanied by exactly the
  tool messages for those ids, so compaction can never orphan a tool message;
* a tool result that started as valid JSON is still valid JSON afterwards.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


# Tools whose results carry source code the model needs in order to act.
# These get a much higher floor than incidental results before anything is cut.
SOURCE_READ_TOOLS: frozenset[str] = frozenset({
    "read_file",
    "read_files",
    "read_file_range",
    "grep_search",
    "find_usages",
    "read_file_outline",
    "search_codebase",
})

# Tools whose completed blocks are *pinned turn context* for the rest of the
# current real user turn: they must replay byte-identically on every round —
# never duplicated, never folded into a retired-evidence receipt, never
# shredded into fragments — so the provider prefix cache sees a stable block
# and the model keeps what it was told to work from.
#
# ``load_skills`` — the exact activated skill bodies, the procedure the model
# is following.  Pinning is scoped to the current real user turn: a previous
# turn's activated bodies are not this turn's context, and the ordinary ladder
# owns them like any other older evidence.
PINNED_INSTRUCTION_TOOLS: frozenset[str] = frozenset({
    "load_skills",
})

# Per-phase character allowances for tool results.
OLD_TURN_RESULT_CHARS: int = 1_200
PRESERVED_TURN_RESULT_CHARS: int = 3_000
PRESERVED_TURN_SOURCE_CHARS: int = 8_000
CURRENT_TURN_RESULT_CHARS: int = 4_000
# Descending floors applied to the *current* turn's source evidence, only ever
# reached after older material has already been compacted and dropped.
CURRENT_TURN_SOURCE_FLOORS: tuple[int, ...] = (24_000, 16_000, 8_000, 4_000)

# A shrunk string never drops below this; under it a snippet carries no meaning.
MIN_LEAF_CHARS: int = 160

# Of a head+tail cut, the fraction of the cap kept as the tail.
REPLAY_TAIL_FRACTION: float = 0.25

_CONTINUE_HINT = (
    "Use grep_search to anchor the symbol, then one bounded read_file "
    "(offset and limit) around that target."
)


@dataclass
class CompactionStats:
    """Per-round diagnostics for one API view build."""

    budget_tokens: int = 0
    tokens_before: int = 0
    tokens_after: int = 0
    messages_before: int = 0
    messages_after: int = 0
    system_prompt_chars: int = 0
    compacted_results: int = 0
    dropped_blocks: int = 0
    # Identity of each tool message already counted, so a result squeezed at
    # several floors is reported once rather than once per pass.
    _counted: set[int] = field(default_factory=set, repr=False, compare=False)
    repaired_messages: int = 0
    reasoning_chars_replayed: int = 0
    reasoning_chars_dropped: int = 0
    system_prompt_fingerprint: str = ""
    over_budget: bool = False


@dataclass(frozen=True)
class ResultResidency:
    """Which canonical tool results are still *materially* in the outbound view.

    The single authoritative answer to "can the model still read that result?",
    and deliberately not a second compaction system: it is read off the view
    this build actually produced, after every retirement, bound, receipt, and
    budget pass has run.  Nothing here re-derives what compaction *would* do.

    A result is resident only when it survived byte-identically.  Anything the
    ladder did to it — truncating it or dropping it from the working set
    entirely — leaves the model unable to answer from it at the range and
    detail it originally had, so it is not resident.  ``PreEditLoopGuard`` reads
    exactly this to decide whether rejecting a reread as a duplicate is still
    telling the truth.
    """

    resident_call_ids: frozenset[str] = frozenset()

    def is_resident(self, call_id: str) -> bool:
        """Whether that call's original result is still fully available."""
        return bool(call_id) and call_id in self.resident_call_ids


@dataclass
class ApiView:
    """The outbound messages plus what it cost to get them there."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    stats: CompactionStats = field(default_factory=CompactionStats)
    residency: ResultResidency = field(default_factory=ResultResidency)


# ---- estimation -------------------------------------------------------------


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Rough token estimate for one message (len/4, BPE-close enough)."""
    tokens = 0
    content = msg.get("content")
    if isinstance(content, str):
        tokens += len(content) // 4
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                tokens += len(part.get("text", "")) // 4
    rc = msg.get("reasoning_content")
    if isinstance(rc, str):
        tokens += len(rc) // 4
    for tc in (msg.get("tool_calls") or []):
        tokens += len(json.dumps(tc, ensure_ascii=False)) // 4
    return tokens


def estimate_tokens(messages: list[dict[str, Any]], system_prompt: str | None = None) -> int:
    total = len(system_prompt) // 4 if system_prompt else 0
    for msg in messages:
        total += estimate_message_tokens(msg)
    return total


# ---- structural repair ------------------------------------------------------


def repair_tool_call_blocks(messages: list[dict[str, Any]]) -> int:
    """Remove tool-call blocks that cannot be replayed. Mutates `messages`.

    Chat APIs require every assistant message carrying ``tool_calls`` to be
    followed by tool messages for exactly those ids. An interrupted turn can
    leave a call with no result — that block poisons every later request until
    it is gone. Returns the number of messages removed.
    """
    removed = 0
    i = 0
    while i < len(messages):
        msg = messages[i]

        if msg.get("role") == "tool":
            del messages[i]
            removed += 1
            continue

        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue

        tool_calls = msg.get("tool_calls") or []
        expected_ids = [
            tc.get("id") for tc in tool_calls if isinstance(tc, dict) and tc.get("id")
        ]
        expected = set(expected_ids)
        seen: set[str] = set()
        valid_block = bool(expected) and len(expected) == len(expected_ids)

        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            tool_call_id = messages[j].get("tool_call_id")
            if tool_call_id not in expected or tool_call_id in seen:
                valid_block = False
            else:
                seen.add(tool_call_id)
            j += 1

        if valid_block and seen == expected:
            i = j
            continue

        removed += j - i
        del messages[i:j]

    return removed


# ---- structure-preserving compaction ----------------------------------------


def _shrink_strings(obj: Any, cap: int, *, head_tail: bool = False, hint: str = _CONTINUE_HINT) -> Any:
    """Return `obj` with every string *value* longer than `cap` shortened.

    Keys, numbers, booleans and structure are untouched, so an envelope such as
    read_files' per-path metadata survives intact while only bulk content is
    reduced.  With ``head_tail`` the first ``cap``-tail-fraction characters are
    kept alongside the head, so a traceback at the end of a long terminal
    output survives the cut.
    """
    if isinstance(obj, str):
        if len(obj) <= cap:
            return obj
        if head_tail:
            tail_cap = max(MIN_LEAF_CHARS, int(cap * REPLAY_TAIL_FRACTION))
            head_cap = max(MIN_LEAF_CHARS, cap - tail_cap)
            return (
                f"{obj[:head_cap]}\n[... aura compacted head+tail: {len(obj)} -> "
                f"{head_cap}+{tail_cap} chars. {hint}]\n{obj[-tail_cap:]}"
            )
        return (
            f"{obj[:cap]}\n[... aura compacted: {len(obj)} -> {cap} chars. {hint}]"
        )
    if isinstance(obj, dict):
        return {k: _shrink_strings(v, cap, head_tail=head_tail, hint=hint) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink_strings(v, cap, head_tail=head_tail, hint=hint) for v in obj]
    return obj


def _compact_text(
    content: str,
    budget_chars: int,
    tool_name: str | None,
    *,
    head_tail: bool = False,
    hint: str = _CONTINUE_HINT,
) -> str:
    """Fallback for results that were never JSON — a marked prefix cut."""
    keep = max(MIN_LEAF_CHARS, budget_chars)
    if head_tail and len(content) > keep:
        tail_cap = max(MIN_LEAF_CHARS, int(keep * REPLAY_TAIL_FRACTION))
        head_cap = max(MIN_LEAF_CHARS, keep - tail_cap)
        return (
            f"{content[:head_cap]}\n\n"
            f"[... result truncated head+tail: {len(content)} chars -> "
            f"{head_cap}+{tail_cap} chars (tool: {tool_name or 'unknown'}). "
            f"{hint} ...]\n\n{content[-tail_cap:]}"
        )
    return (
        f"{content[:keep]}\n\n"
        f"[... result truncated: {len(content)} chars -> {keep} chars "
        f"(tool: {tool_name or 'unknown'}). {hint} ...]"
    )


def compact_result_content(
    content: str,
    budget_chars: int,
    tool_name: str | None = None,
    *,
    head_tail: bool = False,
    hint: str = _CONTINUE_HINT,
) -> tuple[str, bool]:
    """Shrink one tool-result string to roughly `budget_chars`.

    Returns ``(new_content, was_compacted)``. JSON input always yields JSON
    output: the largest string leaves are shortened until the serialised form
    fits, and if even the minimum leaf size does not fit, the structurally
    complete (still valid, still parseable) form is returned rather than a
    truncated byte prefix.  ``head_tail`` keeps the end of a cut leaf as well
    as its start (see ``_shrink_strings``).
    """
    if len(content) <= budget_chars:
        return content, False

    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return _compact_text(content, budget_chars, tool_name, head_tail=head_tail, hint=hint), True

    if not isinstance(parsed, (dict, list)):
        return _compact_text(content, budget_chars, tool_name, head_tail=head_tail, hint=hint), True

    def rendered(cap: int) -> str:
        return json.dumps(
            _shrink_strings(parsed, cap, head_tail=head_tail, hint=hint),
            ensure_ascii=False,
        )

    floor_render = rendered(MIN_LEAF_CHARS)
    if len(floor_render) >= budget_chars:
        # Cannot reach the budget without destroying the envelope. Keeping the
        # envelope wins: every path/key stays represented and parseable.
        return floor_render, True

    # Largest per-leaf cap that still fits the budget.
    low, high = MIN_LEAF_CHARS, len(content)
    best = floor_render
    while low <= high:
        mid = (low + high) // 2
        candidate = rendered(mid)
        if len(candidate) <= budget_chars:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1

    return best, len(best) < len(content)


# ---- view construction ------------------------------------------------------


def _tool_name_map(messages: list[dict[str, Any]]) -> dict[str, str]:
    """Map tool_call_id -> tool name using the assistant messages present."""
    names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            call_id = tc.get("id")
            fn = tc.get("function")
            if call_id and isinstance(fn, dict) and fn.get("name"):
                names[call_id] = str(fn["name"])
    return names


def _pinned_call_ids(names: dict[str, str]) -> frozenset[str]:
    """Tool-call ids that belong to pinned instructional blocks."""
    return frozenset(
        call_id
        for call_id, name in names.items()
        if name in PINNED_INSTRUCTION_TOOLS
    )


def _block_is_pinned(working: list[dict[str, Any]], start: int) -> bool:
    """Whether a completed block's assistant message calls a pinned tool.

    A block that mixes a pinned tool with ordinary calls is pinned as a whole:
    the activated bodies must stay with their call, so the pair is preserved
    together rather than split.
    """
    for tc in (working[start].get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function")
        name = str(fn.get("name")) if isinstance(fn, dict) and fn.get("name") else ""
        if name in PINNED_INSTRUCTION_TOOLS:
            return True
    return False


def _is_source_result(msg: dict[str, Any], names: dict[str, str]) -> bool:
    return names.get(msg.get("tool_call_id", "")) in SOURCE_READ_TOOLS


def is_real_user_message(msg: dict[str, Any]) -> bool:
    """True only for a user message that actually starts a new request.

    Aura injects its own ``role="user"`` messages (steering nudges, recovery
    notices, loop guards) marked ``aura_internal``. They belong in the outbound
    view — the model must see them — but they are *not* new user requests. Counting
    them as turns splits one long tool loop into artificial turns, which makes the
    current request's own fresh evidence look like an old turn and hands it to
    old-turn compaction and block dropping.

    This is the single definition of "the real user turn" — turn-boundary
    detection here, and rewind/retry in ``History``, share it.
    """
    return (
        msg.get("role") == "user"
        and not msg.get("aura_internal")
    )


def user_message_text(msg: dict[str, Any]) -> str:
    """Plain text of a user message, flattening multimodal text parts."""
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _turn_starts(messages: list[dict[str, Any]]) -> list[int]:
    return [i for i, m in enumerate(messages) if is_real_user_message(m)]


def _compact_range(
    messages: list[dict[str, Any]],
    start: int,
    end: int,
    max_chars: int,
    names: dict[str, str],
    source_min_chars: int = 0,
    stats: CompactionStats | None = None,
    pinned_call_ids: frozenset[str] = frozenset(),
) -> None:
    """Compact tool results in messages[start:end] in place (on the copy).

    Results belonging to pinned instructional blocks are skipped: an activated
    skill body must stay byte-identical, never shredded into fragments.
    """
    for i in range(start, min(end, len(messages))):
        msg = messages[i]
        if msg.get("role") != "tool":
            continue
        if msg.get("tool_call_id") in pinned_call_ids:
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue

        allowance = max_chars
        if source_min_chars > max_chars and _is_source_result(msg, names):
            allowance = source_min_chars

        new_content, changed = compact_result_content(
            content, allowance, names.get(msg.get("tool_call_id", ""))
        )
        if changed:
            msg["content"] = new_content
            if stats is not None and id(msg) not in stats._counted:
                stats._counted.add(id(msg))
                stats.compacted_results += 1


def _completed_blocks(messages: list[dict[str, Any]]) -> list[tuple[int, int]]:
    """Return (start, end) spans of assistant-with-tool_calls plus its results.

    Only complete blocks are returned, so removing a span can never leave a
    tool message without its assistant message or vice versa.
    """
    blocks: list[tuple[int, int]] = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        j = i + 1
        while j < len(messages) and messages[j].get("role") == "tool":
            j += 1
        if j > i + 1:
            blocks.append((i, j))
        i = j
    return blocks


def _drop_block(messages: list[dict[str, Any]], start: int, end: int) -> None:
    """Replace a completed tool block with a one-line assistant note.

    The note keeps the model aware that work happened here without replaying
    the evidence. Dropping the assistant message along with its results is what
    keeps tool pairing valid; the reasoning-replay rule only binds assistant
    messages we *keep*.
    """
    call_names: list[str] = []
    for tc in (messages[start].get("tool_calls") or []):
        fn = tc.get("function") if isinstance(tc, dict) else None
        if isinstance(fn, dict) and fn.get("name"):
            call_names.append(str(fn["name"]))
    label = ", ".join(dict.fromkeys(call_names)) or "tool calls"
    note = (
        f"[Aura compacted an earlier completed step ({label}) to stay within the "
        f"context budget. Re-read any file you still need.]"
    )

    prev = messages[start - 1] if start > 0 else None
    if prev is not None and prev.get("role") == "assistant" and not prev.get("tool_calls"):
        # Merge into the adjacent assistant turn rather than stacking notes.
        existing = prev.get("content") or ""
        prev["content"] = f"{existing}\n{note}" if existing else note
        del messages[start:end]
        return

    messages[start:end] = [{"role": "assistant", "content": note}]


def _strip_superseded_reasoning(
    working: list[dict[str, Any]],
    stats: CompactionStats,
) -> None:
    """Drop reasoning that belongs to *completed* real user turns.

    One policy, for every transport. Reasoning produced inside the request the
    model is still working on is its working state: it is what the model decided
    before it called the tool whose result it is now reading. Removing it is what
    forced the model to rebuild its plan on every round. Reasoning from turns the
    user has already moved past is dead weight -- it is replayed on every
    subsequent round, and in measured production turns it accounted for 46-98%
    of all replayed reasoning, crowding out tool results that then had to be
    dropped and re-read.

    So the boundary is the *latest genuine user request*
    (:func:`is_real_user_message`): assistant messages before it shed their
    reasoning, assistant messages at or after it keep it verbatim.

    Two properties follow, and both matter:

    * DeepSeek's OpenAI-compatible thinking mode rejects a replay that drops
      ``reasoning_content`` from any assistant message after the *last* user
      message ("The `reasoning_content` in the thinking mode must be passed back
      to the API"). The real-user boundary is never later than that one, so this
      rule always keeps at least what that transport demands.
    * Aura's own continuation messages are ``role="user"`` with
      ``aura_internal``. Treating them as boundaries would move the strip point
      *inside* a live turn: appending one would retroactively delete reasoning
      already sent on an earlier round, rewriting the request prefix the
      provider's context cache matches against (DeepSeek Flash input is 50x more
      expensive on a miss than a hit). The real-user boundary does not move
      until the user actually asks for something else.

    Canonical history is untouched; this only shapes one request.
    """
    boundary = -1
    for index, msg in enumerate(working):
        if is_real_user_message(msg):
            boundary = index
    if boundary < 0:
        return

    for msg in working[:boundary]:
        if msg.get("role") != "assistant":
            continue
        rc = msg.pop("reasoning_content", None)
        if isinstance(rc, str):
            stats.reasoning_chars_dropped += len(rc)


# * canonical history is never touched; this shapes one outbound request.


def _fingerprint(text: str | None) -> str:
    """Short deterministic fingerprint of the system prompt prefix."""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _compute_residency(
    canonical: list[dict[str, Any]], outbound: list[dict[str, Any]]
) -> ResultResidency:
    """Which canonical tool results survived into *outbound* byte-identically.

    Derived from the two message lists and nothing else, which is what makes it
    authoritative: whatever the ladder did this round — retire, bound, receipt,
    summarise, drop — shows up here as a content mismatch or an absence, without
    this function knowing anything about how compaction works.

    Equality is exact on purpose.  A result the model can only see the first
    1,200 characters of cannot answer the request that produced it, so "present
    but shortened" is not residency; it is precisely the state that used to make
    the duplicate-read rejection a lie.
    """
    canonical_content: dict[str, Any] = {}
    for msg in canonical:
        if msg.get("role") != "tool":
            continue
        call_id = msg.get("tool_call_id")
        if isinstance(call_id, str) and call_id:
            canonical_content[call_id] = msg.get("content")

    resident: set[str] = set()
    for msg in outbound:
        if msg.get("role") != "tool":
            continue
        call_id = msg.get("tool_call_id")
        if not isinstance(call_id, str) or call_id not in canonical_content:
            continue
        if msg.get("content") == canonical_content[call_id]:
            resident.add(call_id)
    return ResultResidency(resident_call_ids=frozenset(resident))


def build_api_view(
    system_prompt: str | None,
    messages: list[dict[str, Any]],
    budget_tokens: int,
    keep_last_n_turns: int = 5,
) -> ApiView:
    """Build the outbound message list without touching `messages`.

    `messages` is deep-copied first; the caller's canonical history is never
    modified, whatever the budget forces us to do here.

    Reasoning follows one rule for every transport: the current real user turn
    keeps it, completed turns shed it (see ``_strip_superseded_reasoning``).
    """

    working = copy.deepcopy(messages)
    stats = CompactionStats(
        budget_tokens=budget_tokens,
        system_prompt_chars=len(system_prompt or ""),
        messages_before=len(messages),
    )
    stats.system_prompt_fingerprint = _fingerprint(system_prompt)

    stats.repaired_messages = repair_tool_call_blocks(working)

    names = _tool_name_map(working)
    stats.tokens_before = estimate_tokens(working, system_prompt)

    # Before compaction, so the space this frees is space the ladder does not
    # have to take out of tool results. Reasoning is shed only across a real
    # user-turn boundary — stripping a finished batch's reasoning inside the
    # *same* turn would both delete the model's working state and rewrite an
    # already-sent request prefix on the next round, turning the whole suffix
    # into a DeepSeek cache miss (50x the input price of a hit).
    _strip_superseded_reasoning(working, stats)

    # If the copy still exceeds the working-set budget, the compaction ladder
    # shrinks it cheapest-loss-first, never touching canonical history.


    _compact_to_budget(
        working, system_prompt, budget_tokens, names, stats, keep_last_n_turns,
    )

    stats.tokens_after = estimate_tokens(working, system_prompt)
    stats.over_budget = stats.tokens_after > budget_tokens

    out = _render(system_prompt, working, stats)
    stats.messages_after = len(out)
    return ApiView(
        messages=out,
        stats=stats,
        residency=_compute_residency(messages, out),
    )


def _compact_to_budget(
    working: list[dict[str, Any]],
    system_prompt: str | None,
    budget_tokens: int,
    names: dict[str, str],
    stats: CompactionStats,
    keep_last_n_turns: int,
) -> None:
    """Run the compaction ladder until the copy fits, cheapest loss first."""

    def fits() -> bool:
        return estimate_tokens(working, system_prompt) <= budget_tokens

    if fits():
        return

    starts = _turn_starts(working)
    if not starts:
        _compact_range(working, 0, len(working), OLD_TURN_RESULT_CHARS, names, stats=stats)
        return

    def turn_bounds(t: int, all_starts: list[int]) -> tuple[int, int]:
        s = all_starts[t]
        e = all_starts[t + 1] if t + 1 < len(all_starts) else len(working)
        return s, e

    def preserved_turns(all_starts: list[int]) -> set[int]:
        n = len(all_starts)
        keep = {0}
        keep.update(range(max(0, n - keep_last_n_turns), n))
        return keep

    # Tool-call ids of pinned instructional blocks (activated skill bodies),
    # scoped to the current real user turn: only the current turn's
    # activated-skill blocks are protected from compaction and dropping; older
    # turns keep the ordinary ladder.
    pinned = _pinned_call_ids(names)

    # --- 1. Compact tool results in old, non-preserved turns ---
    preserved = preserved_turns(starts)
    for t in range(len(starts)):
        if t in preserved:
            continue
        s, e = turn_bounds(t, starts)
        _compact_range(working, s, e, OLD_TURN_RESULT_CHARS, names, stats=stats)

    if fits():
        return

    # --- 2. Drop whole completed tool blocks outside the current turn ---
    # Oldest first, and always the assistant message together with its results.
    while not fits():
        starts = _turn_starts(working)
        current_start = starts[-1] if starts else 0
        blocks = [b for b in _completed_blocks(working) if b[0] < current_start]
        if not blocks:
            break
        start, end = blocks[0]
        _drop_block(working, start, end)
        stats.dropped_blocks += 1

    if fits():
        return

    # --- 3. Compact preserved-but-not-current turns ---
    starts = _turn_starts(working)
    preserved = preserved_turns(starts)
    current_turn = len(starts) - 1
    for t in range(len(starts)):
        if t not in preserved or t == current_turn:
            continue
        s, e = turn_bounds(t, starts)
        _compact_range(
            working, s, e,
            PRESERVED_TURN_RESULT_CHARS, names,
            source_min_chars=PRESERVED_TURN_SOURCE_CHARS,
            stats=stats,
        )

    if fits():
        return

    # --- 4. Only now touch the current turn's own evidence, floor by floor ---
    for floor in CURRENT_TURN_SOURCE_FLOORS:
        starts = _turn_starts(working)
        s = starts[-1] if starts else 0
        _compact_range(
            working, s, len(working),
            CURRENT_TURN_RESULT_CHARS, names,
            source_min_chars=floor,
            stats=stats,
            pinned_call_ids=pinned,
        )
        if fits():
            return

    # --- 5. Last resort: drop completed blocks inside the current turn too ---
    while not fits():
        blocks = [
            b for b in _completed_blocks(working)
            if not _block_is_pinned(working, b[0])
        ]
        if len(blocks) <= 1:
            # Never drop the only remaining block — the model would lose the
            # evidence for the step it is in the middle of (and a pinned
            # activated-skill block is never dropped at all).
            break
        start, end = blocks[0]
        _drop_block(working, start, end)
        stats.dropped_blocks += 1


def _render(
    system_prompt: str | None,
    working: list[dict[str, Any]],
    stats: CompactionStats,
) -> list[dict[str, Any]]:
    """Serialise the compacted copy into the outbound message array."""
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    for msg in working:
        if msg.get("role") != "assistant":
            api_msg = dict(msg)
            api_msg.pop("aura_internal", None)
            out.append(api_msg)
            continue

        api_msg = {"role": "assistant", "content": msg.get("content")}
        # Reasoning is already shaped in ``working``: the current real user
        # turn kept it, completed turns shed it before ``_render`` runs, so this
        # only ever re-serialises what the policy left in the copy. The
        # provider's own signature for that reasoning travels with it —
        # transports that verify replayed thinking blocks need the pair, and a
        # signature without its reasoning is meaningless.
        rc = msg.get("reasoning_content")
        if rc:
            api_msg["reasoning_content"] = rc
            if isinstance(rc, str):
                stats.reasoning_chars_replayed += len(rc)
            signature = msg.get("reasoning_signature")
            if signature:
                api_msg["reasoning_signature"] = signature
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            api_msg["tool_calls"] = tool_calls
        out.append(api_msg)

    return out
