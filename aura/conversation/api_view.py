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

* every assistant message in the *active* tool-call chain keeps its
  ``reasoning_content`` — DeepSeek rejects a thinking-mode replay that drops it
  (see ``_strip_superseded_reasoning`` for where the chain begins);
* reasoning from completed batches inside the *same* real user turn is shed
  once a later batch opens: ``_mark_completed_step_boundary`` inserts a
  transient ``aura_internal`` user message at the active chain's start, which
  is what makes the strip provider-safe without touching canonical history;
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

from aura.conversation.tools.catalog import MUTATION_TOOL_NAMES

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
    "Use grep_search to anchor the symbol, then one bounded read_file "
    "(offset and limit) around that target."
)
_TERMINAL_HINT = "Re-run the command to see the full output."

# Read-only evidence probes. A completed block made up entirely of these is an
# "observation"; blocks containing anything else (mutations, terminal runs,
# diagnostics, bookkeeping) are never retired.
OBSERVATION_TOOLS: frozenset[str] = SOURCE_READ_TOOLS | frozenset({
    "glob",
    "list_directory",
    "code_intel_outline",
    "code_intel_references",
    "code_intel_dependents",
    "code_intel_audit",
    "git_status",
    "git_diff",
    "git_log",
    "git_show",
    "git_log_file",
    "git_branch_list",
    "git_stash_list",
    "git_stash_show",
    "get_workspace_snapshot",
    "inspect_godot_assets",
    "inspect_godot_api",
    "inspect_godot_editor",
})

# Tools whose results can be huge and are never retired; their *replay* in the
# outbound view is deterministically bounded (head+tail) so a big output does
# not sit verbatim in every later request of one turn.
TERMINAL_REPLAY_TOOLS: frozenset[str] = frozenset({
    "run_terminal_command",
    "run_and_watch",
    "run_diagnostic_command",
})

# The recent-evidence allowance: walking backward from the active chain, this
# many tokens of the working-set budget is spent replaying the most recent
# completed blocks verbatim; completed observations beyond it become receipts.
# A fraction of the budget, never a count of calls, files, or rounds.
RECENT_EVIDENCE_FRACTION: float = 0.25
MIN_RECENT_EVIDENCE_TOKENS: int = 8_000

# Deterministic replay caps for kept non-active results (never-retired blocks).
REPLAY_SOURCE_CHARS: int = 24_000
REPLAY_RESULT_CHARS: int = 16_000
REPLAY_TAIL_FRACTION: float = 0.25  # of the cap kept as the tail of a cut

# Bounds for one evidence receipt.
RECEIPT_MAX_CHARS: int = 1_800
RECEIPT_PREVIEW_CHARS: int = 300
RECEIPT_MATCH_PREVIEWS: int = 5

# Marker that identifies a receipt message in the outbound view.
RECEIPT_MARKER: str = "aura_evidence_receipt"


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
    reasoning_chars_dropped: int = 0
    boundary_messages_inserted: int = 0
    # Lifecycle retirement of completed observation blocks (see
    # ``_retire_completed_observations``).
    retired_observation_blocks: int = 0
    receipt_chars_retained: int = 0
    active_chain_chars_retained: int = 0
    recent_evidence_tokens: int = 0
    bounded_replays: int = 0
    system_prompt_fingerprint: str = ""
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

    The transient completed-step boundary is emitted without its ``aura_internal``
    marker (``_render`` strips internal flags before the provider sees them), so it
    is identified here by content as well — a fake user request that must never
    count as a turn start.

    This is the single definition of "the real user turn" — turn-boundary
    detection here, and rewind/retry in ``History``, share it.
    """
    return (
        msg.get("role") == "user"
        and not msg.get("aura_internal")
        and not is_step_boundary_message(msg)
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


# The transient user message that marks the completed-step boundary. It is
# emitted to the provider (DeepSeek's replay rule keys on the last ``role=user``
# message) but is ``aura_internal``, so it is never persisted, never shown in
# the transcript, and never counted as a real user turn.
_STEP_BOUNDARY_TEXT = (
    "[Aura internal: completed-step boundary. Continue the active step.]"
)


def is_step_boundary_message(msg: dict[str, Any]) -> bool:
    """True for the transient completed-step boundary in an outbound view.

    Identifies the boundary after ``_render`` has dropped the ``aura_internal``
    marker, so a caller that persists a rendered view (``prune_for_context``)
    can keep the fake user request out of storage.
    """
    return (
        msg.get("role") == "user"
        and msg.get("content") == _STEP_BOUNDARY_TEXT
    )


def _active_chain_start(messages: list[dict[str, Any]]) -> int | None:
    """Index of the last assistant message carrying ``tool_calls``, or None.

    In Aura's mid-loop shape the conversation ends on a tool result, so this is
    the assistant whose results the model is about to continue from — the only
    reasoning DeepSeek's thinking-mode replay still requires.
    """
    for i in range(len(messages) - 1, -1, -1):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            return i
    return None


def _completed_step_boundary_index(
    messages: list[dict[str, Any]],
) -> int | None:
    """Index at which to insert the completed-step boundary, or None.

    Within one real user turn a tool loop runs many rounds. Reasoning on every
    finished batch is dead weight that the provider nevertheless forces us to
    replay, because it only recognises one boundary: the last ``role=user``
    message. This returns the start of the *active* chain when completed steps
    sit behind it, so ``_mark_completed_step_boundary`` can insert an
    ``aura_internal`` user message there. That message is exactly what makes the
    strip provider-safe: assistant messages before it (the completed steps) may
    shed reasoning while the active chain after it keeps its own.

    Returns ``None`` when there is nothing to shed — no open chain, no completed
    step ahead of it, no reasoning in the completed span, or a user message
    already bounding the chain.
    """
    if not messages or messages[-1].get("role") != "tool":
        # Only the mid-loop shape has an open chain the provider will continue.
        return None
    active = _active_chain_start(messages)
    if active is None:
        return None

    last_user = -1
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            last_user = i
            break
    if active <= last_user:
        # A real turn or internal steering already bounds the chain.
        return None

    # A completed step (assistant tool-call block) must sit between the last
    # user message and the active chain, and it must carry reasoning worth
    # shedding — otherwise the boundary buys nothing but a visible marker.
    for i in range(last_user + 1, active):
        msg = messages[i]
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            continue
        if isinstance(msg.get("reasoning_content"), str) and msg.get(
            "reasoning_content"
        ):
            return active
    return None


def _mark_completed_step_boundary(
    working: list[dict[str, Any]], stats: CompactionStats
) -> None:
    """Insert the completed-step boundary when reasoning is there to shed.

    The boundary is an ``aura_internal`` user message: transient (this is a deep
    copy, never persisted), invisible to the user (the transcript and persistence
    skip ``aura_internal``), and never a real user turn (so routing, skill
    selection, research policy, and rewind are untouched). The provider sees it
    as the last user message, which is exactly what makes
    ``_strip_superseded_reasoning`` legal for the completed steps before it.
    """
    idx = _completed_step_boundary_index(working)
    if idx is None:
        return
    working.insert(
        idx,
        {
            "role": "user",
            "content": _STEP_BOUNDARY_TEXT,
            "aura_internal": True,
        },
    )
    stats.boundary_messages_inserted += 1


def _strip_superseded_reasoning(
    working: list[dict[str, Any]], stats: CompactionStats
) -> None:
    """Drop ``reasoning_content`` from assistant messages of finished turns.

    DeepSeek's thinking-mode rule is narrower than "never strip reasoning".
    Probed directly against the API, with ``thinking`` enabled and the array
    ending on a tool result (Aura's mid-loop shape):

    * stripping any assistant message that sits *after* the last user message
      is rejected -- 400 "The `reasoning_content` in the thinking mode must be
      passed back to the API";
    * stripping assistant messages *before* the last user message is accepted.

    So the requirement binds the active tool-call chain, not the transcript.
    Thinking from turns the user has already moved past is dead weight: it is
    replayed on every subsequent round, and in measured production turns it
    accounted for 46-98% of all replayed reasoning -- crowding out tool
    results that then had to be dropped and re-read.

    The last user message is the boundary the provider itself sees, which
    includes Aura's internal steering messages (they are sent as ``role:
    user``) and, inside one real user turn, the completed-step boundary that
    ``_mark_completed_step_boundary`` inserts at the active chain's start.
    Canonical history is untouched; this only shapes one request.
    """
    boundary = -1
    for index, msg in enumerate(working):
        if msg.get("role") == "user":
            boundary = index
    if boundary < 0:
        return

    for msg in working[:boundary]:
        if msg.get("role") != "assistant":
            continue
        rc = msg.pop("reasoning_content", None)
        if isinstance(rc, str):
            stats.reasoning_chars_dropped += len(rc)


# ---- lifecycle retirement of completed observation blocks -------------------
#
# A long coding turn replays every completed tool block of the current request
# on every model round until the budget ladder forces a cut. Below the budget
# the request grows without bound: each round re-sends an ever larger pile of
# already-consumed evidence. Retirement fixes the growth curve instead of the
# ceiling: once a newer tool batch has opened, a completed block is either
# recent enough to be worth replaying verbatim, or old enough that a compact
# deterministic receipt is the honest representation of it.
#
# The rule, applied to a deep copy inside ``build_api_view`` only:
#
# * the active chain (the newest assistant tool-call block) is never touched —
#   DeepSeek requires its reasoning and pairing exactly as it is;
# * working backward from it, completed blocks of the *current real user turn*
#   stay verbatim while their cumulative replay cost fits the recent-evidence
#   allowance, a fixed fraction of the model's working-set budget — a token
#   budget, never a count of calls, files, rounds, or time;
# * completed blocks made up entirely of read-only observation calls that sit
#   beyond that allowance are retired: the whole block (assistant call,
#   reasoning, results) is replaced by one deterministic evidence receipt;
# * blocks containing any mutation, terminal, diagnostic, or other
#   non-observation call, and blocks with any failed result, are never retired
#   — they are preserved regardless of the allowance and only *bounded* on
#   replay, so failed writes and validation output stay available through the
#   repair and rerun;
# * canonical history is never touched; this shapes one outbound request.


def _fingerprint(text: str | None) -> str:
    """Short deterministic fingerprint of the system prompt prefix."""
    return hashlib.sha1((text or "").encode("utf-8", "replace")).hexdigest()[:12]


def _normalize_receipt_path(raw: Any) -> str:
    """Normalize a path for receipt comparison (slash form, no ``./``)."""
    text = str(raw).strip()
    text = text.replace("\\", "/")
    if text.startswith("./"):
        text = text[2:]
    while "//" in text:
        text = text.replace("//", "/")
    return text.strip()


def _args_paths(args: dict[str, Any]) -> list[str]:
    """Normalized path-like values from one tool call's arguments."""
    out: list[str] = []
    for key in (
        "path", "paths", "file", "files", "old_path", "new_path",
        "target_paths", "target_files", "scene_path",
    ):
        value = args.get(key)
        if isinstance(value, str):
            values = [value]
        elif isinstance(value, list):
            values = [v for v in value if isinstance(v, str)]
        else:
            continue
        for v in values:
            normalized = _normalize_receipt_path(v)
            if normalized and normalized not in out:
                out.append(normalized)
    return out


def _mutation_paths(working: list[dict[str, Any]]) -> frozenset[str]:
    """Normalized paths written by mutation calls anywhere in the view."""
    written: set[str] = set()
    for msg in working:
        if msg.get("role") != "assistant":
            continue
        for tc in (msg.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            name = str(fn.get("name")) if isinstance(fn, dict) and fn.get("name") else ""
            if name not in MUTATION_TOOL_NAMES:
                continue
            args: dict[str, Any] = {}
            if isinstance(fn, dict):
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except (TypeError, ValueError):
                    args = {}
            if isinstance(args, dict):
                written.update(_args_paths(args))
    return frozenset(written)


def _build_evidence_receipt(
    call: dict[str, Any],
    result_msg: dict[str, Any] | None,
    tool_name: str,
    stale_paths: frozenset[str],
) -> dict[str, Any]:
    """Deterministic, bounded evidence receipt for one retired tool call.

    Pure local extraction — no model summarization. Every fact the next
    decision might need that is cheap to keep is kept: tool name, normalized
    paths, content hashes, line/range metadata, size and truncation state,
    matched-symbol or result counts, success/failure, failure reason, a short
    bounded summary, and whether a later write made the evidence stale.
    """
    receipt: dict[str, Any] = {RECEIPT_MARKER: True, "tool": tool_name}

    args: dict[str, Any] = {}
    fn = call.get("function")
    if isinstance(fn, dict):
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (TypeError, ValueError):
            args = {}
    if not isinstance(args, dict):
        args = {}

    paths = _args_paths(args)
    if paths:
        receipt["paths"] = paths

    parsed: Any = None
    if result_msg is not None:
        content = result_msg.get("content")
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except (TypeError, ValueError):
                parsed = None
    if isinstance(parsed, dict):
        ok = parsed.get("ok")
        if ok is not None:
            receipt["ok"] = bool(ok)
        for flag in ("recoverable", "batch_rejected"):
            if parsed.get(flag) is True:
                receipt[flag] = True
        status = parsed.get("status")
        if isinstance(status, str) and status and status != "complete":
            receipt["status"] = status
            reason = parsed.get("reason")
            if isinstance(reason, str) and reason:
                receipt["reason"] = str(reason)[:RECEIPT_PREVIEW_CHARS]

    files = parsed.get("files") if isinstance(parsed, dict) else None
    if isinstance(files, dict):
        entries: dict[str, Any] = {}
        for path, entry in files.items():
            if not isinstance(entry, dict):
                continue
            facts: dict[str, Any] = {}
            status = entry.get("status")
            if status:
                facts["status"] = str(status)
            if entry.get("content_hash"):
                facts["hash"] = str(entry["content_hash"])[:24]
            rng = entry.get("included_range")
            if isinstance(rng, dict) and rng.get("start_line") is not None:
                facts["range"] = f"{rng['start_line']}-{rng.get('end_line', '?')}"
            if entry.get("file_size"):
                facts["size"] = int(entry["file_size"])
            if entry.get("truncated") is not None:
                facts["truncated"] = bool(entry["truncated"])
            if entry.get("line_count"):
                facts["lines"] = int(entry["line_count"])
            if status in ("error", "omitted"):
                reason = entry.get("reason")
                if reason:
                    facts["reason"] = str(reason)[:RECEIPT_PREVIEW_CHARS]
            entries[str(path)] = facts
        if entries:
            receipt["files"] = entries
            if "ok" not in receipt:
                receipt["ok"] = all(
                    f.get("status") in (None, "complete") for f in entries.values()
                )
    elif (
        isinstance(parsed, dict)
        and tool_name in ("read_file", "read_file_range", "read_file_outline")
    ):
        for key, alias in (
            ("status", "status"),
            ("content_hash", "hash"),
            ("file_size", "size"),
            ("truncated", "truncated"),
            ("line_count", "lines"),
        ):
            value = parsed.get(key)
            if value is not None:
                receipt[alias] = bool(value) if key in ("truncated",) else value
        rng = parsed.get("included_range")
        if isinstance(rng, dict) and rng.get("start_line") is not None:
            receipt["range"] = f"{rng['start_line']}-{rng.get('end_line', '?')}"
        if parsed.get("status") in ("error", "omitted"):
            reason = parsed.get("reason")
            if reason:
                receipt["reason"] = str(reason)[:RECEIPT_PREVIEW_CHARS]
        content = parsed.get("content")
        if isinstance(content, str) and content:
            receipt["summary"] = content[:RECEIPT_PREVIEW_CHARS]

    if isinstance(parsed, dict):
        for key in ("pattern", "scope", "query", "symbol", "name"):
            value = parsed.get(key) or args.get(key)
            if value is not None:
                receipt["search"] = str(value)[:RECEIPT_PREVIEW_CHARS]
                break
        count = parsed.get("count")
        if count is not None:
            receipt["count"] = int(count)
        matches = parsed.get("matches")
        if isinstance(matches, list) and matches:
            previews: list[str] = []
            for m in matches[:RECEIPT_MATCH_PREVIEWS]:
                if isinstance(m, dict):
                    loc = m.get("path") or m.get("file") or m.get("location")
                    line = m.get("line")
                    previews.append(
                        f"{loc}:{line}" if loc is not None else str(loc or m)[:80]
                    )
                else:
                    previews.append(str(m)[:80])
            receipt["matches"] = previews

    if "summary" not in receipt and "files" not in receipt:
        if isinstance(parsed, (dict, list)):
            summary = json.dumps(
                _shrink_strings(parsed, RECEIPT_PREVIEW_CHARS), ensure_ascii=False
            )
        elif isinstance(parsed, str) and parsed:
            summary = parsed[:RECEIPT_PREVIEW_CHARS]
        else:
            summary = ""
        if summary:
            receipt["summary"] = summary

    if paths:
        stale = [p for p in paths if p in stale_paths]
        receipt["stale_after_writes"] = stale
        receipt["note"] = "Re-read any file you still need."

    return receipt


def _retire_block_to_receipt(
    working: list[dict[str, Any]],
    start: int,
    end: int,
    names: dict[str, str],
    stale_paths: frozenset[str],
    stats: CompactionStats,
) -> None:
    """Replace one completed observation block with its evidence receipt."""
    assistant_msg = working[start]
    receipts: list[dict[str, Any]] = []
    for tc in (assistant_msg.get("tool_calls") or []):
        if not isinstance(tc, dict):
            continue
        call_id = tc.get("id")
        result_msg = next(
            (
                m
                for m in working[start + 1:end]
                if m.get("role") == "tool" and m.get("tool_call_id") == call_id
            ),
            None,
        )
        receipts.append(
            _build_evidence_receipt(
                tc, result_msg, names.get(str(call_id), ""), stale_paths
            )
        )
    if len(receipts) == 1:
        payload: Any = receipts[0]
    else:
        payload = {RECEIPT_MARKER: True, "calls": receipts}
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    if len(text) > RECEIPT_MAX_CHARS:
        text = json.dumps(
            _shrink_strings(payload, RECEIPT_PREVIEW_CHARS),
            sort_keys=True,
            ensure_ascii=False,
        )
    working[start:end] = [{"role": "assistant", "content": text}]
    stats.retired_observation_blocks += 1
    stats.receipt_chars_retained += len(text)


def _block_has_failed_result(
    working: list[dict[str, Any]], start: int, end: int
) -> bool:
    """Whether any tool result of a block reports a failure.

    A failed result means the model may still be recovering from it, so the
    whole block stays: failed observations, guard rejections, batch rejections,
    per-path errors and omitted reads all protect the block.
    """
    for i in range(start + 1, end):
        msg = working[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return True  # unparseable result is treated as a failure
        if not isinstance(parsed, dict):
            continue
        if parsed.get("ok") is False:
            return True
        if parsed.get("recoverable") is True or parsed.get("batch_rejected") is True:
            return True
        if parsed.get("status") in ("error", "omitted"):
            return True
        files = parsed.get("files")
        if isinstance(files, dict) and any(
            isinstance(entry, dict) and entry.get("status") in ("error", "omitted")
            for entry in files.values()
        ):
            return True
    return False


def _bound_replayed_results(
    working: list[dict[str, Any]],
    start: int,
    end: int,
    names: dict[str, str],
    stats: CompactionStats,
) -> None:
    """Deterministic replay bound for kept non-active results.

    A block that is never retired (a mutation, a terminal run, a validation
    failure, a mixed batch) replays on every round of the turn. Its bulk
    leaves get a bounded, structurally valid representation here — regardless
    of budget pressure — so a 200K-char test run does not sit verbatim in the
    request forever. The envelope (paths, statuses, failure class, exit code,
    continuation guidance) always survives.
    """
    for i in range(start + 1, end):
        msg = working[i]
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str):
            continue
        tool_name = names.get(msg.get("tool_call_id", ""))
        if tool_name in SOURCE_READ_TOOLS:
            cap, head_tail, hint = REPLAY_SOURCE_CHARS, False, _CONTINUE_HINT
        elif tool_name in TERMINAL_REPLAY_TOOLS:
            cap, head_tail, hint = REPLAY_RESULT_CHARS, True, _TERMINAL_HINT
        else:
            cap, head_tail, hint = REPLAY_RESULT_CHARS, False, _CONTINUE_HINT
        new_content, changed = compact_result_content(
            content, cap, tool_name, head_tail=head_tail, hint=hint
        )
        if changed:
            msg["content"] = new_content
            stats.bounded_replays += 1


def _retire_completed_observations(
    working: list[dict[str, Any]],
    budget_tokens: int,
    names: dict[str, str],
    stats: CompactionStats,
) -> None:
    """Retire older completed observation blocks; keep recent evidence verbatim.

    See the section docstring for the lifecycle rule. Runs on the deep copy
    after reasoning shedding, so canonical history is untouched and the
    completed-step boundary still governs which reasoning may be replayed.
    """
    active = _active_chain_start(working)
    if active is None:
        return

    # Blocks before the last genuine user request belong to older turns; the
    # budget ladder owns them. Only the current real user turn is in scope.
    current_start = 0
    for i, msg in enumerate(working):
        if is_real_user_message(msg):
            current_start = i + 1

    blocks = [
        (s, e) for (s, e) in _completed_blocks(working)
        if s >= current_start and e <= active
    ]
    if not blocks:
        return

    allowance = max(
        MIN_RECENT_EVIDENCE_TOKENS, int(budget_tokens * RECENT_EVIDENCE_FRACTION)
    )
    stats.recent_evidence_tokens = allowance
    stale_paths = _mutation_paths(working)

    for start, end in reversed(blocks):
        call_names = [
            str(tc.get("function", {}).get("name", ""))
            for tc in (working[start].get("tool_calls") or [])
            if isinstance(tc, dict) and isinstance(tc.get("function"), dict)
        ]
        is_observation = bool(call_names) and all(
            name in OBSERVATION_TOOLS for name in call_names
        )
        if not is_observation or _block_has_failed_result(working, start, end):
            # Never retired: mutations, terminal/diagnostic runs, bookkeeping,
            # and failures the turn may still be recovering from. Bounded only.
            _bound_replayed_results(working, start, end, names, stats)
            continue

        cost = estimate_tokens(working[start:end])
        if cost <= allowance:
            allowance -= cost
            continue

        _retire_block_to_receipt(working, start, end, names, stale_paths, stats)


def _active_chain_chars(working: list[dict[str, Any]]) -> int:
    """Characters retained by the newest assistant tool-call chain."""
    active = _active_chain_start(working)
    if active is None:
        return 0
    total = 0
    for i in range(active, len(working)):
        msg = working[i]
        if msg.get("role") == "assistant":
            total += len(msg.get("content") or "")
            total += len(msg.get("reasoning_content") or "")
            total += len(json.dumps(msg.get("tool_calls") or [], ensure_ascii=False))
            continue
        if msg.get("role") == "tool":
            total += len(msg.get("content") or "")
            continue
        break
    return total


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
    stats.system_prompt_fingerprint = _fingerprint(system_prompt)

    stats.repaired_messages = repair_tool_call_blocks(working)

    names = _tool_name_map(working)
    stats.source_result_chars_generated = _source_result_chars(working, names)
    stats.tokens_before = estimate_tokens(working, system_prompt)

    # Before compaction, so the space this frees is space the ladder does not
    # have to take out of tool results. The completed-step boundary first:
    # within one real user turn it makes reasoning from finished batches
    # provider-safe to shed while the active chain keeps its own.
    _mark_completed_step_boundary(working, stats)
    _strip_superseded_reasoning(working, stats)

    # Lifecycle retirement next: completed observation blocks beyond the
    # recent-evidence allowance become deterministic receipts, so the request
    # stops growing with every completed block. Kept blocks are bounded on
    # replay. The ladder only then handles whatever still does not fit.
    _retire_completed_observations(working, budget_tokens, names, stats)
    stats.active_chain_chars_retained = _active_chain_chars(working)

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
