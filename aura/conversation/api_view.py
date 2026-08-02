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

* every retained assistant message keeps its ``reasoning_content`` — DeepSeek
  rejects a thinking-mode replay that drops it;
* an assistant message with ``tool_calls`` is always accompanied by exactly the
  tool messages for those ids, so compaction can never orphan a tool message;
* a tool result that started as valid JSON is still valid JSON afterwards.
"""

from __future__ import annotations

import copy
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

_CONTINUE_HINT = (
    "Use read_file_outline or grep_search to anchor the symbol, then one narrow "
    "read_file_range around that target."
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
    source_result_chars_generated: int = 0
    source_result_chars_retained: int = 0
    compacted_results: int = 0
    dropped_blocks: int = 0
    # Identity of each tool message already counted, so a result squeezed at
    # several floors is reported once rather than once per pass.
    _counted: set[int] = field(default_factory=set, repr=False, compare=False)
    repaired_messages: int = 0
    reasoning_chars_replayed: int = 0
    over_budget: bool = False


@dataclass
class ApiView:
    """The outbound messages plus what it cost to get them there."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    stats: CompactionStats = field(default_factory=CompactionStats)


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


def _shrink_strings(obj: Any, cap: int) -> Any:
    """Return `obj` with every string *value* longer than `cap` shortened.

    Keys, numbers, booleans and structure are untouched, so an envelope such as
    read_files' per-path metadata survives intact while only bulk content is
    reduced.
    """
    if isinstance(obj, str):
        if len(obj) <= cap:
            return obj
        return (
            f"{obj[:cap]}\n[... aura compacted: {len(obj)} -> {cap} chars. {_CONTINUE_HINT}]"
        )
    if isinstance(obj, dict):
        return {k: _shrink_strings(v, cap) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_shrink_strings(v, cap) for v in obj]
    return obj


def _compact_text(content: str, budget_chars: int, tool_name: str | None) -> str:
    """Fallback for results that were never JSON — a marked prefix cut."""
    keep = max(MIN_LEAF_CHARS, budget_chars)
    return (
        f"{content[:keep]}\n\n"
        f"[... result truncated: {len(content)} chars -> {keep} chars "
        f"(tool: {tool_name or 'unknown'}). {_CONTINUE_HINT} ...]"
    )


def compact_result_content(
    content: str,
    budget_chars: int,
    tool_name: str | None = None,
) -> tuple[str, bool]:
    """Shrink one tool-result string to roughly `budget_chars`.

    Returns ``(new_content, was_compacted)``. JSON input always yields JSON
    output: the largest string leaves are shortened until the serialised form
    fits, and if even the minimum leaf size does not fit, the structurally
    complete (still valid, still parseable) form is returned rather than a
    truncated byte prefix.
    """
    if len(content) <= budget_chars:
        return content, False

    try:
        parsed = json.loads(content)
    except (ValueError, TypeError):
        return _compact_text(content, budget_chars, tool_name), True

    if not isinstance(parsed, (dict, list)):
        return _compact_text(content, budget_chars, tool_name), True

    def rendered(cap: int) -> str:
        return json.dumps(_shrink_strings(parsed, cap), ensure_ascii=False)

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


def _is_source_result(msg: dict[str, Any], names: dict[str, str]) -> bool:
    return names.get(msg.get("tool_call_id", "")) in SOURCE_READ_TOOLS


def _source_result_chars(messages: list[dict[str, Any]], names: dict[str, str]) -> int:
    total = 0
    for msg in messages:
        if msg.get("role") != "tool" or not _is_source_result(msg, names):
            continue
        content = msg.get("content")
        if isinstance(content, str):
            total += len(content)
    return total


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
    return msg.get("role") == "user" and not msg.get("aura_internal")


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
) -> None:
    """Compact tool results in messages[start:end] in place (on the copy)."""
    for i in range(start, min(end, len(messages))):
        msg = messages[i]
        if msg.get("role") != "tool":
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


def build_api_view(
    system_prompt: str | None,
    messages: list[dict[str, Any]],
    budget_tokens: int,
    keep_last_n_turns: int = 5,
) -> ApiView:
    """Build the outbound message list without touching `messages`.

    `messages` is deep-copied first; the caller's canonical history is never
    modified, whatever the budget forces us to do here.
    """
    working = copy.deepcopy(messages)
    stats = CompactionStats(
        budget_tokens=budget_tokens,
        system_prompt_chars=len(system_prompt or ""),
        messages_before=len(messages),
    )

    stats.repaired_messages = repair_tool_call_blocks(working)

    names = _tool_name_map(working)
    stats.source_result_chars_generated = _source_result_chars(working, names)
    stats.tokens_before = estimate_tokens(working, system_prompt)

    _compact_to_budget(working, system_prompt, budget_tokens, names, stats, keep_last_n_turns)

    stats.tokens_after = estimate_tokens(working, system_prompt)
    stats.source_result_chars_retained = _source_result_chars(working, names)
    stats.over_budget = stats.tokens_after > budget_tokens

    out = _render(system_prompt, working, stats)
    stats.messages_after = len(out)
    return ApiView(messages=out, stats=stats)


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
        )
        if fits():
            return

    # --- 5. Last resort: drop completed blocks inside the current turn too ---
    while not fits():
        blocks = _completed_blocks(working)
        if len(blocks) <= 1:
            # Never drop the only remaining block — the model would lose the
            # evidence for the step it is in the middle of.
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
        # THE TRAP: reasoning_content must be replayed on every assistant
        # message that has it, with or without tool_calls.
        rc = msg.get("reasoning_content")
        if rc:
            api_msg["reasoning_content"] = rc
            if isinstance(rc, str):
                stats.reasoning_chars_replayed += len(rc)
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            api_msg["tool_calls"] = tool_calls
        out.append(api_msg)

    return out
