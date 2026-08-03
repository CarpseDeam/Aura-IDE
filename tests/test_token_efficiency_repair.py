"""Focused tests for the token-efficiency repair.

The defect: a long coding turn re-sent every completed tool block of the
current request on every model round. The budget ladder only acted as an
emergency ceiling, so outbound requests grew toward it linearly with every
completed block.

The repair adds lifecycle retirement inside the outbound API view only:

* the active chain (the newest assistant tool-call block) stays verbatim —
  reasoning, calls, and every paired result, exactly as DeepSeek requires;
* working backward from it, completed observation blocks of the current real
  user turn stay verbatim while their cumulative replay cost fits the
  recent-evidence allowance (a fraction of the active model's working-set
  budget — a token budget, never a count of calls, files, rounds, or time);
* every block that is no longer active retires into *one* deterministic
  retired-evidence ledger with its own total token budget derived from the
  working-set budget: observations beyond the allowance, failures repaired by
  a later successful call on the same path, and command/validation runs
  superseded by a newer terminal block;
* recent retired evidence keeps its detail; older entries are deterministically
  trimmed, merged, and finally dropped oldest-first, so the request reaches a
  genuine plateau once the frontier fills;
* preserved verbatim are the newest completed command block (the current
  terminal output and validation chain), applied mutations (proof of an
  applied mutation), unresolved failures (the model may still be recovering),
  and unknown-effect calls (fail safe: no declaration, no retirement);
* block classes come from the authoritative tool-effect model, never a local
  name list — built-in, dynamic, MCP, and future observation tools all retire
  through the same lookup;
* production SINGLE freezes the Tier-1 system prompt for the whole real user
  turn; the next real user turn recomposes normally.

What is asserted here:

* outbound request growth reaches a genuinely bounded plateau (50+ sequential
  reads, and a mixed read/terminal/validation/repair trajectory), with exactly
  one ledger message and a bounded entry count;
* the newest active block stays intact and DeepSeek-valid on every round;
* ledger entries retain paths, hashes, ranges, statuses, failures, counts,
  bounded summaries, and write-staleness deterministically, ordered so the
  failure → diagnosis → repair → passing-validation sequence is never lost;
* unresolved failures stay verbatim; repaired and superseded failures become
  bounded ledger entries;
* dynamic and MCP observation tools retire according to their declared effect
  metadata; unknown-effect tools remain preserved;
* canonical history is byte-identical after every view build;
* the full production recovery loop through the real send loop keeps tool
  pairing and the system-prompt prefix stable;
* no read/round/retry/file-count ceilings were introduced.
"""

from __future__ import annotations

import hashlib
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt
from aura.conversation.api_view import (
    RECEIPT_MARKER,
    build_api_view,
    default_effect_lookup,
)
from aura.conversation.history import History
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.tools.effects import ToolEffect
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from tests.production_loop_harness import (
    IMPLEMENTATION_ROUTE,
    Recorder,
    ScriptedBackend,
    build_manager,
    final_round,
    run,
    tool_round,
)

FROZEN_PROMPT = "You are Aura's production coding agent."


# ── helpers ─────────────────────────────────────────────────────────────────


def fingerprint(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def assert_tool_pairing_valid(messages: list[dict[str, Any]]) -> None:
    """Every tool message answers a tool_call in the assistant right above it.

    Fails on an orphan tool message (no preceding assistant block) and on any
    assistant tool-call block whose results do not match its calls exactly.
    """
    i = 0
    seen: set[str] = set()
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "tool":
            raise AssertionError(
                f"tool message at {i} has no preceding assistant tool_calls block"
            )
        if msg.get("role") != "assistant" or not msg.get("tool_calls"):
            i += 1
            continue
        expected = [tc["id"] for tc in msg["tool_calls"]]
        j = i + 1
        answered: list[str] = []
        while j < len(messages) and messages[j].get("role") == "tool":
            call_id = messages[j].get("tool_call_id")
            assert call_id not in seen, f"duplicate tool result {call_id}"
            seen.add(call_id)
            answered.append(call_id)
            j += 1
        assert answered == expected, (
            f"assistant at {i} expected results {expected}, got {answered}"
        )
        i = j


def tool_block(
    call_id: str,
    name: str,
    args: dict[str, Any],
    result: Any,
    *,
    reasoning: str = "",
) -> list[dict[str, Any]]:
    """An assistant tool-call message plus its tool result."""
    assistant: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }
    if reasoning:
        assistant["reasoning_content"] = reasoning
    payload = result if isinstance(result, str) else json.dumps(result)
    return [assistant, {"role": "tool", "tool_call_id": call_id, "content": payload}]


def read_files_result(paths: list[str], chars_each: int) -> str:
    """A realistic read_files payload of a known size."""
    files: dict[str, Any] = {}
    for i, path in enumerate(paths):
        if path == "missing.py":
            files[path] = {
                "ok": False,
                "path": path,
                "status": "error",
                "reason": "file not found",
                "truncated": False,
                "continuation": "",
            }
            continue
        files[path] = {
            "ok": True,
            "path": path,
            "status": "complete",
            "reason": "file included in full",
            "file_size": chars_each,
            "content_hash": f"hash-{i}",
            "line_count": 3,
            "included_range": {"start_line": 1, "end_line": 3},
            "truncated": False,
            "content": f"# {path}\n" + ("x" * chars_each) + "\n",
            "continuation": "",
        }
    return json.dumps({"ok": True, "files": files, "requested_paths": paths})


def grep_result(pattern: str, count: int) -> str:
    return json.dumps({
        "ok": True,
        "pattern": pattern,
        "scope": "src",
        "count": count,
        "matches": [
            {"path": "src/b.py", "line": 12},
            {"path": "src/c.py", "line": 34},
            {"path": "src/d.py", "line": 56},
        ][:count],
    })


def big_grep_result(pattern: str, count: int, chars: int) -> str:
    """A search result whose bulk content dominates the payload."""
    return json.dumps({
        "ok": True,
        "pattern": pattern,
        "scope": "src",
        "count": count,
        "matches": [
            {"path": "src/b.py", "line": 12, "snippet": "x" * chars},
            {"path": "src/c.py", "line": 34, "snippet": "y" * chars},
        ][:count],
    })


def terminal_result(ok: bool, exit_code: int, chars: int = 2_000) -> str:
    return json.dumps({
        "ok": ok,
        "exit_code": exit_code,
        "command": "pytest -q",
        "cwd": ".",
        "failure_class": "execution_failed" if not ok else None,
        "output": (
            ("trace start\n" + ("x" * chars) + "\ntraceback: FAILED at end")
            if not ok
            else "3 passed"
        ),
    })


def is_receipt(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant":
        return False
    try:
        parsed = json.loads(msg.get("content") or "")
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get(RECEIPT_MARKER) is True


def ledgers_in(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The retired-evidence ledger payloads (exactly one per view, once full)."""
    return [
        json.loads(m["content"])
        for m in messages
        if is_receipt(m)
    ]


def entries_in(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every ledger entry across all ledger messages, in view order."""
    out: list[dict[str, Any]] = []
    for ledger in ledgers_in(messages):
        out.extend(ledger.get("entries") or [])
    return out


def big_workspace(root: Path, *, modules: int = 8) -> Path:
    """Modules big enough that one read saturates the recent-evidence allowance.

    One ~26K-char read costs ~6.5K tokens — inside the allowance of the
    harness budget (~8.4K tokens), so the newest completed read stays verbatim
    while older ones retire.
    """
    root.mkdir(parents=True, exist_ok=True)
    for i in range(modules):
        body = (
            f"# module {i:02d}\n"
            + "".join(f"VAL_{j:04d} = {j}\n" for j in range(1_600))
        )
        (root / f"mod_{i:02d}.py").write_text(body, encoding="utf-8")
    return root


def stall_round(call_id: str, path: str) -> list:
    return tool_round([(call_id, "list_directory", {"path": path})])


def compile_round(call_id: str, path: str = "mod_00.py") -> list:
    command = f'"{sys.executable}" -m py_compile {path}'
    return tool_round([(call_id, "run_terminal_command", {"command": command})])


def write_round(call_id: str, path: str, content: str) -> list:
    return tool_round([(call_id, "write_file", {"path": path, "content": content})])


def run_with_frozen_prefix(
    workspace: Path,
    user_text: str,
    backend: ScriptedBackend,
    recorder: Recorder,
) -> None:
    manager = build_manager(workspace, user_text)
    manager.configure_runtime_context(
        FROZEN_PROMPT,
        workspace,
        RuntimeRole.SINGLE,
        task_kind="bugfix",
        content=user_text,
    )
    run(manager, recorder, route=IMPLEMENTATION_ROUTE)


def append_block(history: History, block: list[dict[str, Any]]) -> None:
    history.messages.extend(block)


# ── 1: outbound request growth plateaus ─────────────────────────────────────


class TestContextPlateau:
    """One synthetic user turn with many sequential observation rounds.

    Each round produces meaningful new evidence. Once the recent-evidence
    allowance is saturated, every additional completed block folds into the
    single evidence ledger instead of a full replay, so the request stops
    growing linearly with the evidence.
    """

    BUDGET = 10_000
    RESULT_CHARS = 6_000
    # allowance = 25% of the working-set budget — the 8K unconditional floor is
    # gone, so a small budget never spends most of itself on recent evidence.
    ALLOWANCE = int(BUDGET * 0.25)

    def _sizes(self, rounds: int) -> tuple[History, list[float]]:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the retry cap so the job pauses.")
        sizes: list[float] = []
        for i in range(rounds):
            append_block(
                history,
                tool_block(
                    f"r{i}", "read_files", {"paths": [f"f{i}.py"]},
                    read_files_result([f"f{i}.py"], self.RESULT_CHARS),
                    reasoning=f"round {i} reasoning\n",
                ),
            )
            view = history.build_api_payload(self.BUDGET)
            sizes.append(view.stats.tokens_after)
        return history, sizes

    def test_growth_plateaus_while_evidence_keeps_growing(self) -> None:
        history, sizes = self._sizes(8)

        # While the allowance fills, completed evidence is replayed verbatim:
        # the request grows by roughly one full block per round.
        assert sizes[1] - sizes[0] >= 1_200, (
            f"early growth {sizes[1] - sizes[0]:.0f} was not evidence-sized; "
            "the allowance is not being filled"
        )
        # Once the allowance is saturated, each new block must cost only its
        # ledger entry: per-round growth collapses to a small constant instead
        # of one ~1.5K-token block per round.
        deltas = [sizes[i] - sizes[i - 1] for i in range(4, len(sizes))]
        assert all(delta <= 700 for delta in deltas), (
            f"post-saturation deltas {[round(d, 1) for d in deltas]} are not "
            "bounded; the request still grows with every completed block"
        )
        # Structural plateau relative to the generated evidence.
        assert sizes[-1] - sizes[3] <= 1_500, (
            f"plateau drifted {sizes[-1] - sizes[3]:.0f} tokens over four "
            "evidence rounds"
        )
        # The view never approaches the full replay size.
        assert sizes[-1] < history.build_api_payload(
            self.BUDGET
        ).stats.tokens_before

    def test_completed_blocks_become_one_deterministic_ledger(self) -> None:
        history, _ = self._sizes(8)
        view = history.build_api_payload(self.BUDGET)

        assert view.stats.retired_blocks >= 2, (
            "no completed block was retired"
        )
        assert view.stats.ledger_chars_retained > 0
        assert view.stats.recent_evidence_tokens == self.ALLOWANCE, (
            "the recent-evidence allowance must be the budget fraction, "
            "not an unconditional 8K floor"
        )
        ledgers = ledgers_in(view.messages)
        assert len(ledgers) == 1, (
            "all retired blocks must fold into exactly one ledger message"
        )
        entries = ledgers[0]["entries"]
        assert len(entries) >= 2
        for entry in entries:
            assert len(json.dumps(entry)) <= 2_000, (
                "a ledger entry must stay compact"
            )
            assert entry["tool"] == "read_files"
            assert "f" in entry["paths"][0]
        # Deterministic: building the same view twice retires identically.
        again = history.build_api_payload(self.BUDGET)
        assert again.messages == view.messages
        assert again.stats.retired_blocks == view.stats.retired_blocks

    def test_the_newest_active_block_stays_intact_and_valid(self) -> None:
        history, _ = self._sizes(8)
        view = history.build_api_payload(self.BUDGET)

        last_assistant = next(
            m for m in reversed(view.messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert last_assistant["tool_calls"][0]["id"] == "r7"
        assert last_assistant["reasoning_content"] == "round 7 reasoning\n", (
            "the active chain lost its reasoning — DeepSeek rejects this with a 400"
        )
        tool_messages = [m for m in view.messages if m.get("role") == "tool"]
        assert len(tool_messages) <= 3, (
            "eight evidence blocks reduced to a bounded replay window "
            f"({len(tool_messages)} tool results retained)"
        )
        expected_last = read_files_result(["f7.py"], self.RESULT_CHARS)
        assert tool_messages[-1]["content"] == expected_last, (
            "the newest active result was altered"
        )
        assert_tool_pairing_valid(view.messages)

    def test_completed_reasoning_is_shed_and_only_the_active_chain_replays(
        self,
    ) -> None:
        history, _ = self._sizes(8)
        view = history.build_api_payload(self.BUDGET)

        replayed = [
            m.get("reasoning_content")
            for m in view.messages
            if m.get("role") == "assistant" and m.get("reasoning_content")
        ]
        assert replayed == ["round 7 reasoning\n"], (
            f"completed reasoning grew back into the request: {replayed!r}"
        )
        assert view.stats.reasoning_chars_dropped > 0
        assert_tool_pairing_valid(view.messages)


# ── 2: the retired-evidence ledger is truly bounded ─────────────────────────


class TestEvidenceLedger:
    """The ledger replaces the unbounded sequence of individual receipts."""

    BUDGET = 10_000
    RESULT_CHARS = 6_000

    @staticmethod
    def _history_with_reads(count: int) -> History:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the retry cap so the job pauses.")
        for i in range(count):
            append_block(
                history,
                tool_block(
                    f"r{i}", "read_files", {"paths": [f"f{i}.py"]},
                    read_files_result([f"f{i}.py"], TestEvidenceLedger.RESULT_CHARS),
                    reasoning=f"round {i} reasoning\n",
                ),
            )
        return history

    def test_fifty_completed_reads_reach_a_bounded_plateau(self) -> None:
        history = self._history_with_reads(60)
        sizes: list[float] = []

        for i in range(60):
            history_trunc = History(
                system_prompt=history.system_prompt,
                messages=history.messages[: (1 + 2 * (i + 1))],
            )
            view = history_trunc.build_api_payload(self.BUDGET)
            sizes.append(view.stats.tokens_after)

        # After the ledger budget fills, thirty more evidence rounds must not
        # move the request: a genuine plateau, not a gentler slope.
        tail = [sizes[i] - sizes[i - 1] for i in range(31, 60)]
        assert all(delta <= 100 for delta in tail), (
            f"post-fill deltas {[round(d, 1) for d in tail]} are not a plateau; "
            "the ledger still grows with every completed block"
        )
        assert abs(sizes[-1] - sizes[30]) <= 200, (
            f"rounds 31-60 drifted {sizes[-1] - sizes[30]:.0f} tokens; "
            "the request is not bounded after the frontier fills"
        )
        # The plateau sits far below the raw replay, and the view is a single
        # ledger message holding a bounded entry count.
        view = history.build_api_payload(self.BUDGET)
        assert view.stats.tokens_after < history.build_api_payload(
            self.BUDGET
        ).stats.tokens_before
        assert len(ledgers_in(view.messages)) == 1
        assert len(entries_in(view.messages)) <= 40, (
            "fifty retired blocks must not add one receipt per round forever"
        )

    def test_mixed_trajectory_also_plateaus(self) -> None:
        """Reads, failing validation, failed write, repair, passing validation,
        then thirty more reads: the request must plateau with the failure
        sequence preserved in order."""
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the failing test.")
        for i in range(6):
            append_block(
                history,
                tool_block(
                    f"rd{i}", "read_files", {"paths": ["a.py"]},
                    read_files_result(["a.py"], self.RESULT_CHARS),
                    reasoning=f"r{i}\n",
                ),
            )
        append_block(
            history,
            tool_block(
                "t1", "run_terminal_command", {"command": "pytest -q"},
                terminal_result(False, 1),
                reasoning="run\n",
            ),
        )
        append_block(
            history,
            tool_block(
                "rd6", "read_files", {"paths": ["a.py"]},
                read_files_result(["a.py"], self.RESULT_CHARS),
                reasoning="r6\n",
            ),
        )
        append_block(
            history,
            tool_block(
                "w1", "write_file", {"path": "a.py", "content": "x ="},
                {"ok": False, "path": "a.py", "failure_class": "syntax_invalid",
                 "reason": "invalid python"},
                reasoning="write\n",
            ),
        )
        append_block(
            history,
            tool_block(
                "rd7", "read_files", {"paths": ["a.py"]},
                read_files_result(["a.py"], self.RESULT_CHARS),
                reasoning="r7\n",
            ),
        )
        append_block(
            history,
            tool_block(
                "w2", "write_file", {"path": "a.py", "content": "x = 2"},
                {"ok": True, "applied": True, "path": "a.py"},
                reasoning="write2\n",
            ),
        )
        append_block(
            history,
            tool_block(
                "t2", "run_terminal_command", {"command": "pytest -q"},
                terminal_result(True, 0),
                reasoning="run2\n",
            ),
        )
        for i in range(30):
            append_block(
                history,
                tool_block(
                    f"rd{i + 8}", "read_files", {"paths": ["a.py"]},
                    read_files_result(["a.py"], self.RESULT_CHARS),
                    reasoning=f"r{i + 8}\n",
                ),
            )

        sizes: list[float] = []
        for i in range(len(history.messages) // 2):
            view = History(
                system_prompt=history.system_prompt,
                messages=history.messages[: 1 + 2 * (i + 1)],
            ).build_api_payload(self.BUDGET)
            sizes.append(view.stats.tokens_after)
        tail = [sizes[i] - sizes[i - 1] for i in range(len(sizes) - 10, len(sizes))]
        assert all(delta <= 100 for delta in tail), (
            f"mixed-trajectory tail deltas {[round(d, 1) for d in tail]} are "
            "not a plateau"
        )

        view = history.build_api_payload(self.BUDGET)
        assert len(ledgers_in(view.messages)) == 1
        entries = entries_in(view.messages)
        tools = [e.get("tool") for e in entries]

        # The failing validation was superseded by the passing run -> ledger.
        assert "run_terminal_command" in tools
        t1_entry = next(e for e in entries if e.get("tool") == "run_terminal_command")
        assert t1_entry.get("ok") is False
        # The failed write was repaired by w2 -> ledger, with the failure fact.
        w1_entry = next(e for e in entries if e.get("tool") == "write_file")
        assert w1_entry.get("ok") is False
        assert w1_entry.get("paths") == ["a.py"]

        # The repair and the passing validation stay verbatim and ordered after
        # the ledger: failure -> diagnosis -> repair -> passing validation.
        answered = [m["tool_call_id"] for m in view.messages if m.get("role") == "tool"]
        assert "w2" in answered, "the applied mutation must stay verbatim"
        assert "t2" in answered, "the latest validation chain must stay verbatim"
        ledger_index = next(
            i for i, m in enumerate(view.messages) if is_receipt(m)
        )
        w2_index = next(
            i for i, m in enumerate(view.messages)
            if m.get("role") == "assistant" and any(
                tc.get("id") == "w2" for tc in (m.get("tool_calls") or [])
            )
        )
        t2_index = next(
            i for i, m in enumerate(view.messages)
            if m.get("role") == "assistant" and any(
                tc.get("id") == "t2" for tc in (m.get("tool_calls") or [])
            )
        )
        assert ledger_index < w2_index < t2_index, (
            "the failure sequence must read: ledger, repair, passing validation"
        )
        assert_tool_pairing_valid(view.messages)

    def test_unknown_effect_tools_remain_preserved(self) -> None:
        """A call with no known effect metadata fails safe: never retired."""
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Probe the workspace.")
        for i in range(8):
            append_block(
                history,
                tool_block(
                    f"p{i}", "custom_probe", {"path": f"p{i}.txt"},
                    {"ok": True, "result": "x" * self.RESULT_CHARS},
                    reasoning=f"probe {i}\n",
                ),
            )

        # default_effect_lookup knows no custom_probe -> preserved.
        view = history.build_api_payload(self.BUDGET)
        answered = {m["tool_call_id"] for m in view.messages if m.get("role") == "tool"}
        assert len(answered) == 8, "unknown-effect blocks were retired"
        assert view.stats.retired_blocks == 0
        assert ledgers_in(view.messages) == []
        assert_tool_pairing_valid(view.messages)

    def test_dynamic_and_mcp_observation_tools_retire_by_declared_effect(
        self,
    ) -> None:
        """Retirement follows the authoritative effect metadata, not a name list.

        The same tool names retire when the lookup declares them observations
        and are preserved when nothing declares them.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Inspect the environment."},
        ]
        for i in range(10):
            messages.extend(
                tool_block(
                    f"m{i}", "mcp_lookup", {"key": f"k{i}"},
                    {"ok": True, "value": "x" * 6_000},
                    reasoning=f"mcp {i}\n",
                )
            )
        for i in range(10):
            messages.extend(
                tool_block(
                    f"d{i}", "dynamic_probe", {"path": f"d{i}.py"},
                    {"ok": True, "content": "x" * 6_000},
                    reasoning=f"dyn {i}\n",
                )
            )

        declared_observation = {
            "mcp_lookup": ToolEffect.OBSERVATION,
            "dynamic_probe": ToolEffect.OBSERVATION,
        }

        def lookup(name: str) -> ToolEffect | None:
            return declared_observation.get(name)

        view = build_api_view(
            "system prompt", messages, self.BUDGET, effect_for=lookup
        )
        ledgers = ledgers_in(view.messages)
        assert len(ledgers) == 1
        tools = {e.get("tool") for e in entries_in(view.messages)}
        assert {"mcp_lookup", "dynamic_probe"} <= tools, (
            "declared observation tools must retire like any observation"
        )
        answered = {m["tool_call_id"] for m in view.messages if m.get("role") == "tool"}
        assert len(answered) <= 3, "declared observations beyond the allowance stayed"

        # Without a declaration, the same calls are unknown -> the lifecycle
        # never retires them. (Under budget pressure the emergency ladder may
        # still drop over-budget material — that is its documented last
        # resort, not retirement — so the small-history case proves survival
        # where the ladder never engages.)
        unknown = build_api_view(
            "system prompt", messages, self.BUDGET,
            effect_for=lambda name: None,
        )
        assert unknown.stats.retired_blocks == 0, (
            "undeclared dynamic/MCP calls must fail safe and remain unretired"
        )
        small_messages: list[dict[str, Any]] = [
            {"role": "user", "content": "Inspect the environment."},
        ]
        for i in range(10):
            small_messages.extend(
                tool_block(
                    f"m{i}", "mcp_lookup", {"key": f"k{i}"},
                    {"ok": True, "value": "v"},
                    reasoning=f"mcp {i}\n",
                )
            )
        for i in range(10):
            small_messages.extend(
                tool_block(
                    f"d{i}", "dynamic_probe", {"path": f"d{i}.py"},
                    {"ok": True, "content": "c"},
                    reasoning=f"dyn {i}\n",
                )
            )
        unknown_small = build_api_view(
            "system prompt", small_messages, self.BUDGET,
            effect_for=lambda name: None,
        )
        answered_unknown = {
            m["tool_call_id"] for m in unknown_small.messages if m.get("role") == "tool"
        }
        assert len(answered_unknown) == 20, (
            "undeclared dynamic/MCP calls must fail safe and remain unretired"
        )
        assert unknown_small.stats.retired_blocks == 0
        assert unknown_small.stats.dropped_blocks == 0

    def test_canonical_history_remains_unchanged(self) -> None:
        history = self._history_with_reads(40)
        snapshot = deepcopy(history.messages)
        for _ in range(3):
            history.build_api_payload(self.BUDGET)
        assert history.messages == snapshot, (
            "the outbound view must never mutate canonical history"
        )


# ── 3: evidence receipts preserve the facts ─────────────────────────────────


class TestEvidenceReceipts:
    """Retired observations keep every fact the next decision might need."""

    def _history(self) -> History:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the retry cap so the job pauses.")
        # A: a completed observation well beyond the recent-evidence allowance.
        append_block(
            history,
            tool_block(
                "a1", "read_files", {"paths": ["a.py"]},
                read_files_result(["a.py"], 60_000),
                reasoning="reading the target\n",
            ),
        )
        # B: a second completed observation with a huge bulk payload.
        append_block(
            history,
            tool_block(
                "b1", "grep_search", {"pattern": "def foo", "scope": "src"},
                big_grep_result("def foo", 2, 40_000),
                reasoning="locating the call sites\n",
            ),
        )
        # F: a failed read — unresolved, so it stays however old it gets.
        append_block(
            history,
            tool_block(
                "f1", "read_files", {"paths": ["missing.py"]},
                read_files_result(["missing.py"], 0),
                reasoning="checking a missing file\n",
            ),
        )
        # A later write makes a.py stale; the receipt must say so.
        append_block(
            history,
            tool_block(
                "w1", "write_file", {"path": "a.py", "content": "x = 2\n"},
                {"ok": True, "applied": True, "path": "a.py"},
            ),
        )
        append_block(
            history,
            tool_block(
                "c1", "read_file_range", {"path": "a.py", "offset": 1, "limit": 40},
                {"ok": True, "path": "a.py", "status": "complete",
                 "content": "x = 2\n", "content_hash": "h-latest",
                 "included_range": {"start_line": 1, "end_line": 40}},
                reasoning="verifying\n",
            ),
        )
        return history

    def test_receipts_retain_paths_hashes_ranges_statuses_and_counts(
        self,
    ) -> None:
        history = self._history()
        view = history.build_api_payload(8_000)
        ledgers = ledgers_in(view.messages)
        assert len(ledgers) == 1, "both completed observations fold into one ledger"
        entries = entries_in(view.messages)
        assert len(entries) == 2, "both completed observations must be retired"
        by_tool = {e["tool"]: e for e in entries}
        read_receipt = by_tool["read_files"]
        assert read_receipt["paths"] == ["a.py"]
        files = read_receipt["files"]
        assert files["a.py"]["status"] == "complete"
        assert files["a.py"]["hash"] == "hash-0"
        assert files["a.py"]["range"] == "1-3"
        assert files["a.py"]["size"] == 60_000
        assert files["a.py"]["truncated"] is False
        assert read_receipt["stale_after_writes"] == ["a.py"], (
            "the receipt must record that a later write made a.py stale"
        )

        grep_receipt = by_tool["grep_search"]
        assert grep_receipt["search"] == "def foo"
        assert grep_receipt["count"] == 2
        assert any("b.py:12" in m for m in grep_receipt["matches"])

    def test_receipts_are_bounded_and_parseable(self) -> None:
        history = self._history()
        view = history.build_api_payload(8_000)

        for entry in entries_in(view.messages):
            assert len(json.dumps(entry)) <= 2_000
            json.loads(json.dumps(entry))  # round-trips

    def test_the_active_block_the_write_and_failed_reads_are_never_retired(
        self,
    ) -> None:
        history = self._history()
        view = history.build_api_payload(8_000)

        answered = {
            m["tool_call_id"] for m in view.messages if m.get("role") == "tool"
        }
        assert "w1" in answered, "a mutation result was retired"
        assert "c1" in answered, "the active chain was retired"
        assert "f1" in answered, (
            "an unresolved failed observation was retired — it may still be "
            "under recovery"
        )
        assert not {"a1", "b1"} & answered, "a completed observation survived"
        active = next(
            m for m in reversed(view.messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert active["tool_calls"][0]["id"] == "c1"
        assert active["reasoning_content"] == "verifying\n"
        assert_tool_pairing_valid(view.messages)


# ── 4: terminal output lifecycle ────────────────────────────────────────────


class TestTerminalLifecycle:
    """Terminal/diagnostic blocks stay while relevant, retire once superseded."""

    @staticmethod
    def _terminal_result(chars: int) -> str:
        return json.dumps({
            "ok": False,
            "exit_code": 1,
            "command": "pytest -q",
            "cwd": ".",
            "failure_class": "execution_failed",
            "output": "trace start\n" + ("x" * chars) + "\ntraceback: FAILED at end",
        })

    def test_the_newest_completed_terminal_run_stays_verbatim(self) -> None:
        """The current validation failure the model is diagnosing is preserved
        exactly, byte for byte, until a newer command supersedes it."""
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the failing test.")
        big = self._terminal_result(200_000)
        append_block(
            history,
            tool_block(
                "t1", "run_terminal_command", {"command": "pytest -q"}, big
            ),
        )
        append_block(
            history,
            tool_block(
                "r1", "read_file", {"path": "a.py"},
                {"ok": True, "content": "x = 1\n"},
                reasoning="inspecting\n",
            ),
        )

        # Generous budget: no ladder pressure at all. The lifecycle rule alone
        # decides what stays.
        view = history.build_api_payload(10_000_000)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")

        assert tool_msg["content"] == big, (
            "the currently relevant terminal output must stay exactly as produced"
        )
        assert view.stats.retired_blocks == 0
        # The active chain is untouched.
        last = next(
            m for m in reversed(view.messages)
            if m.get("role") == "tool"
        )
        assert last["tool_call_id"] == "r1"
        assert_tool_pairing_valid(view.messages)

    def test_a_superseded_terminal_run_retires_into_the_ledger(self) -> None:
        """Once a newer command block opens, the older terminal run becomes a
        bounded ledger entry — its failure facts survive, its bulk does not."""
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the failing test.")
        big = self._terminal_result(200_000)
        append_block(
            history,
            tool_block(
                "t1", "run_terminal_command", {"command": "pytest -q"}, big
            ),
        )
        append_block(
            history,
            tool_block(
                "t2", "run_terminal_command", {"command": "pytest -q"},
                json.dumps({"ok": True, "exit_code": 0, "command": "pytest -q",
                            "output": "3 passed"}),
            ),
        )
        append_block(
            history,
            tool_block(
                "r1", "read_file", {"path": "a.py"},
                {"ok": True, "content": "x = 1\n"},
                reasoning="inspecting\n",
            ),
        )

        view = history.build_api_payload(10_000_000)
        answered = {
            m["tool_call_id"] for m in view.messages if m.get("role") == "tool"
        }
        assert "t1" not in answered, "the superseded terminal run stayed verbatim"
        assert "t2" in answered, "the newest terminal run must stay available"
        entries = entries_in(view.messages)
        assert len(entries) == 1
        assert entries[0]["tool"] == "run_terminal_command"
        assert entries[0]["ok"] is False
        # The current passing validation output stays exactly as produced.
        t2_msg = next(
            m for m in view.messages
            if m.get("role") == "tool" and m.get("tool_call_id") == "t2"
        )
        assert t2_msg["content"] == json.dumps({
            "ok": True, "exit_code": 0, "command": "pytest -q", "output": "3 passed"
        })
        assert_tool_pairing_valid(view.messages)

    def test_the_active_chain_terminal_result_stays_verbatim(self) -> None:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Run the tests.")
        big = self._terminal_result(120_000)
        append_block(
            history,
            tool_block(
                "t1", "run_terminal_command", {"command": "pytest -q"}, big,
                reasoning="running\n",
            ),
        )

        view = history.build_api_payload(10_000_000)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")
        assert tool_msg["content"] == big, (
            "active terminal output must stay exactly as produced"
        )
        assert view.stats.bounded_replays == 0


# ── 5: the production recovery loop preserves evidence and prefix ───────────


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


def _assert_every_request_valid(backend: ScriptedBackend) -> None:
    for call in backend.calls:
        messages = call["messages"]
        assert_tool_pairing_valid(messages)
        texts = [
            str(m.get("content", ""))
            for m in messages
            if m.get("role") == "system"
        ]
        assert texts and all(t == FROZEN_PROMPT for t in texts), (
            "the system-prompt prefix changed inside one real user turn"
        )


class TestRecoveryPreservation:
    """Discovery → write → failed validation → repair → corrected write →
    passing validation → truthful completion, through the real send loop.

    Also: multiple distinct failed write attempts before a successful one.
    Every request is asserted to keep tool pairing, the frozen prefix, and the
    evidence the turn is still working from.
    """

    def test_validation_repair_keeps_evidence_and_the_frozen_prefix(
        self, tmp_path, isolated_streams
    ) -> None:
        workspace = big_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            # four legitimate discovery rounds (invariant: never bounded by a count)
            tool_round([("r0", "read_file", {"path": "mod_04.py"})]),
            tool_round([("r1", "read_file", {"path": "mod_05.py"})]),
            tool_round([("r2", "read_file", {"path": "mod_06.py"})]),
            tool_round([("r3", "read_file", {"path": "mod_07.py"})]),
            stall_round("d1", "."),
            stall_round("d2", "./"),
            # write attempt that fails the write's own validation (the written
            # body is invalid Python: the write tool rejects it offline)
            write_round("w1", "mod_00.py", "x =\n"),
            # diagnostic/read repair round
            tool_round([("r4", "read_file", {"path": "mod_00.py"})]),
            # corrected write
            write_round("w2", "mod_00.py", "x = 2\n"),
            # passing validation
            compile_round("v2"),
            final_round("Validation passes: mod_00.py compiles."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run_with_frozen_prefix(
            workspace,
            "Update mod_00.py so it compiles.",
            backend,
            recorder,
        )

        writes = recorder.results_named("write_file")
        assert [w.ok for w in writes] == [False, True], (
            "non-vacuous: the first write really failed validation and the "
            "corrected write applied"
        )
        failed = json.loads(writes[0].result)
        assert failed["failure_class"] == "syntax_invalid", (
            "the write's own validation must be the offline failure"
        )
        compiles = recorder.results_named("run_terminal_command")
        assert [c.ok for c in compiles] == [True], (
            "non-vacuous: the rerun validation really passed"
        )
        assert "Validation passes" in recorder.chat_text, (
            "the turn completed truthfully"
        )
        assert backend.request_shapes() == [
            False, False, False, False, False, False,
            True, False, True, False, False,
        ], "discovery → focused write → repair → corrected write → pass → final"

        _assert_every_request_valid(backend)

        # Older completed observations beyond the allowance became a ledger,
        # while the newest read and the stall rounds stayed.
        w1_request = backend.calls[6]["messages"]
        ledgers = ledgers_in(w1_request)
        assert len(ledgers) == 1, "no completed observation was retired"
        assert len(entries_in(w1_request)) >= 2
        answered = {
            m.get("tool_call_id") for m in w1_request if m.get("role") == "tool"
        }
        assert "r3" in answered, "the newest completed read was retired"
        assert "d1" in answered and "d2" in answered

        # The failed write stays verbatim while it is still unresolved — in
        # the diagnosis request and the corrected-write request, the repair is
        # not yet visible in history.
        for index in (7, 8):
            answered = {
                m.get("tool_call_id")
                for m in backend.calls[index]["messages"]
                if m.get("role") == "tool"
            }
            assert "w1" in answered, (
                "an unresolved failed write must stay verbatim"
            )

        # Once the repair is visible (passing-validation request and final
        # request), the failure retires into the ledger with its facts, and
        # the sequence ledger → repair write → passing validation is kept.
        for index in (9, 10):
            call = backend.calls[index]["messages"]
            answered = {
                m.get("tool_call_id") for m in call if m.get("role") == "tool"
            }
            assert "w1" not in answered, (
                "a repaired failure must retire instead of replaying forever"
            )
            entries = entries_in(call)
            w1_entry = next(
                (e for e in entries if e.get("tool") == "write_file"), None
            )
            assert w1_entry is not None, "the repaired failure lost its receipt"
            assert w1_entry["ok"] is False
            assert "mod_00.py" in w1_entry["paths"]
            ledger_index = next(i for i, m in enumerate(call) if is_receipt(m))
            w2_index = next(
                i for i, m in enumerate(call)
                if m.get("role") == "assistant" and any(
                    tc.get("id") == "w2" for tc in (m.get("tool_calls") or [])
                )
            )
            assert ledger_index < w2_index, (
                "the repaired failure's receipt must precede the repair write"
            )
            assert "w2" in answered, (
                "the applied mutation must stay verbatim"
            )
            if index == 10:
                # The final request also replays the passing validation.
                v2_index = next(
                    i for i, m in enumerate(call)
                    if m.get("role") == "assistant" and any(
                        tc.get("id") == "v2" for tc in (m.get("tool_calls") or [])
                    )
                )
                assert w2_index < v2_index, (
                    "repair must precede the passing validation"
                )
                assert "v2" in answered, (
                    "the latest validation chain must stay verbatim"
                )

    def test_two_distinct_failed_writes_still_reach_a_successful_third(
        self, tmp_path, isolated_streams
    ) -> None:
        from tests.production_loop_harness import make_workspace, read_round

        workspace = make_workspace(tmp_path / "proj")
        backend = ScriptedBackend([
            stall_round("d1", "."),
            stall_round("d2", "./"),
            # failure A: the path escapes the workspace
            tool_round([("w1", "write_file", {"path": "../outside.md",
                                              "content": "nope"})]),
            read_round("e1", 7),
            # failure B: a delete of a file that is not there
            tool_round([("w2", "delete_file", {"path": "gone.md"})]),
            read_round("e2", 8),
            # the third act applies
            write_round("w3", "mod_00.py", "value = 99\n"),
            final_round("Applied on the third approach."),
        ])
        isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)
        recorder = Recorder()

        run_with_frozen_prefix(
            workspace,
            "Fix the loader in mod_00.py.",
            backend,
            recorder,
        )

        writes = recorder.results_named("write_file")
        assert [w.ok for w in writes] == [False, True], (
            "non-vacuous: the first write really failed and the third applied"
        )
        deletes = recorder.results_named("delete_file")
        assert len(deletes) == 1 and not deletes[0].ok
        assert "Applied on the third approach." in recorder.chat_text
        assert backend.request_shapes() == [
            False, False, True, False, True, False, True, False,
        ]

        _assert_every_request_valid(backend)

        # Invariant: unresolved failed mutation evidence stays available until
        # the turn ends — present in every request after each failure. Neither
        # failure is ever repaired on its own path, so neither may retire.
        for call in backend.calls[3:]:
            answered = {
                m.get("tool_call_id")
                for m in call["messages"]
                if m.get("role") == "tool"
            }
            assert "w1" in answered, "a later request lost the failed write"
        for call in backend.calls[5:]:
            answered = {
                m.get("tool_call_id")
                for m in call["messages"]
                if m.get("role") == "tool"
            }
            assert "w2" in answered, "a later request lost the failed delete"


# ── 6: the Tier-1 prefix is frozen inside one user turn ─────────────────────


class TestPrefixStability:
    """Production SINGLE keeps one system-prompt fingerprint per user turn."""

    def test_frozen_prefix_survives_the_write_refresh(self, tmp_path: Path) -> None:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        refresh = PlannerRefreshState()
        refresh.configure(
            "base prompt",
            tmp_path,
            RuntimeRole.SINGLE,
            task_kind="bugfix",
            content="update app.py",
        )
        history = History()
        history.set_system(FROZEN_PROMPT)
        history.append_user_text("Update app.py.")

        refresh.handle_post_write_notices(history, ["app.py"])

        assert history.system_prompt == FROZEN_PROMPT
        assert fingerprint(history.system_prompt) == fingerprint(FROZEN_PROMPT)
        users = [m for m in history.messages if m.get("role") == "user"]
        assert len(users) == 1, "the frozen refresh added a user-turn boundary"

    def test_the_next_real_user_turn_may_recompose_tier1(
        self, tmp_path: Path,
    ) -> None:
        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        refresh = PlannerRefreshState()
        refresh.configure(
            "base prompt",
            tmp_path,
            RuntimeRole.SINGLE,
            task_kind="bugfix",
        )
        history = History()
        history.set_system(FROZEN_PROMPT)
        history.append_user_text("Update app.py.")

        # Turn 1: the write lands, the prefix stays frozen.
        refresh.handle_post_write_notices(history, ["app.py"])
        assert history.system_prompt == FROZEN_PROMPT

        # Turn 2 (a new real user request): Tier 1 is recomposed normally and
        # nothing about the freeze inhibits the fresh prompt.
        composed = compose_system_prompt(
            RuntimeRole.SINGLE,
            "Always prefer the Qt signal path.",
            tmp_path,
            task_kind="bugfix",
            target_files=("app.py",),
            content="update app.py",
        )
        assert fingerprint(composed.system_prompt) != fingerprint(FROZEN_PROMPT)
        assert "### Custom Instructions" in composed.system_prompt
        history.set_system(composed.system_prompt)
        assert history.system_prompt == composed.system_prompt

    def test_legacy_planner_still_recomposes_after_writes(self, tmp_path: Path) -> None:
        from aura.context_gearbox.runtime import PLANNER_SYSTEM_PROMPT

        (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
        refresh = PlannerRefreshState()
        refresh.configure(
            PLANNER_SYSTEM_PROMPT,
            tmp_path,
            RuntimeRole.PLANNER,
            task_kind="bugfix",
        )
        history = History()
        history.set_system(FROZEN_PROMPT)
        history.append_user_text("Update app.py.")

        refresh.handle_post_write_notices(history, ["app.py"])

        assert history.system_prompt != FROZEN_PROMPT, (
            "the legacy Planner path must keep recomposing Tier 1"
        )
        assert fingerprint(history.system_prompt) != fingerprint(FROZEN_PROMPT)
