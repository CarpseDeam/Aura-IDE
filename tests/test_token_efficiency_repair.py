"""Focused tests for the token-efficiency repair.

The defect: a long coding turn re-sent every completed tool block of the
current request on every model round. The budget ladder only acted as an
emergency ceiling, so outbound requests grew toward it linearly with every
completed observation block.

The repair adds lifecycle retirement inside the outbound API view only:

* the active chain (the newest assistant tool-call block) stays verbatim —
  reasoning, calls, and every paired result, exactly as DeepSeek requires;
* working backward from it, completed blocks of the current real user turn
  stay verbatim while their cumulative replay cost fits the recent-evidence
  allowance (a fraction of the model's working-set budget — a token budget,
  never a count of calls, files, rounds, or time);
* completed blocks beyond that allowance are retired to deterministic
  evidence receipts when they are pure read-only observations;
* blocks containing mutations, terminal/diagnostic runs, or any failed result
  are never retired — they are preserved regardless of the allowance and only
  bounded on replay;
* production SINGLE freezes the Tier-1 system prompt for the whole real user
  turn; the next real user turn recomposes normally.

What is asserted here:

* outbound request growth plateaus once the recent-evidence allowance is
  saturated, while the raw replay would keep growing linearly;
* the newest active block stays intact and DeepSeek-valid on every round;
* receipts retain paths, hashes, ranges, statuses, failures, counts, bounded
  summaries, and write-staleness deterministically;
* huge never-retired outputs (terminal/diagnostic) get a bounded,
  structure-preserving replay, not a permanent verbatim replay;
* the full production recovery loop — discovery rounds, write, failed
  validation, repair, corrected write, passing validation, truthful
  completion — and multiple distinct failed writes before success both keep
  failed evidence available and the system-prompt prefix stable;
* no read/round/retry/file-count ceilings were introduced.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import compose_system_prompt
from aura.conversation.api_view import RECEIPT_MARKER
from aura.conversation.history import History
from aura.conversation.planner_refresh import PlannerRefreshState
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


def is_receipt(msg: dict[str, Any]) -> bool:
    if msg.get("role") != "assistant":
        return False
    try:
        parsed = json.loads(msg.get("content") or "")
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get(RECEIPT_MARKER) is True


def receipts_in(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        json.loads(m["content"])
        for m in messages
        if is_receipt(m)
    ]


def big_workspace(root: Path, *, modules: int = 8) -> Path:
    """Modules big enough that one read saturates the recent-evidence allowance.

    One ~26K-char read costs ~6.5K tokens — inside the 8K-token floor of the
    allowance, so the newest completed read stays verbatim while older ones
    retire.
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


# ── 1: outbound request growth plateaus ─────────────────────────────────────


class TestContextPlateau:
    """One synthetic user turn with many sequential observation rounds.

    Each round produces meaningful new evidence. Once the recent-evidence
    allowance is saturated, every additional completed block becomes a
    deterministic receipt instead of a full replay, so the request stops
    growing linearly with the evidence.
    """

    BUDGET = 10_000
    RESULT_CHARS = 12_000

    def _sizes(self, rounds: int) -> tuple[History, list[float]]:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the retry cap so the job pauses.")
        sizes: list[float] = []
        for i in range(rounds):
            for msg in tool_block(
                f"r{i}", "read_files", {"paths": [f"f{i}.py"]},
                read_files_result([f"f{i}.py"], self.RESULT_CHARS),
                reasoning=f"round {i} reasoning\n",
            ):
                history.messages.append(msg)
            view = history.build_api_payload(self.BUDGET)
            sizes.append(view.stats.tokens_after)
        return history, sizes

    def test_growth_plateaus_while_evidence_keeps_growing(self) -> None:
        history, sizes = self._sizes(8)

        # While the allowance fills, completed evidence is replayed verbatim:
        # the request grows by roughly one full block per round.
        assert sizes[2] - sizes[1] >= 2_500, (
            f"early growth {sizes[2] - sizes[1]:.0f} was not evidence-sized; "
            "the allowance is not being filled"
        )
        # Once the allowance is saturated, each new block must cost only its
        # receipt: per-round growth collapses to a small constant instead of
        # one ~3K-token block per round.
        deltas = [sizes[i] - sizes[i - 1] for i in range(5, len(sizes))]
        assert all(delta <= 700 for delta in deltas), (
            f"post-saturation deltas {[round(d, 1) for d in deltas]} are not "
            "bounded; the request still grows with every completed block"
        )
        # Structural plateau relative to the generated evidence: three more
        # rounds of ~12K-char evidence (≈9K tokens) move the request by less
        # than one receipt's worth.
        assert sizes[-1] - sizes[3] <= 1_500, (
            f"plateau drifted {sizes[-1] - sizes[3]:.0f} tokens over four "
            "evidence rounds"
        )
        # The view never approaches the full replay size.
        assert sizes[-1] < history.build_api_payload(
            self.BUDGET
        ).stats.tokens_before

    def test_completed_blocks_become_deterministic_receipts(self) -> None:
        history, _ = self._sizes(8)
        view = history.build_api_payload(self.BUDGET)

        assert view.stats.retired_observation_blocks >= 2, (
            "no completed observation block was retired"
        )
        assert view.stats.receipt_chars_retained > 0
        assert view.stats.recent_evidence_tokens == 8_000
        receipts = receipts_in(view.messages)
        assert len(receipts) >= 2
        for receipt in receipts:
            assert receipt[RECEIPT_MARKER] is True
            assert len(json.dumps(receipt)) <= 2_000, (
                "a receipt must stay compact"
            )
            assert receipt["tool"] == "read_files"
            assert "f" in receipt["paths"][0]
        # Deterministic: building the same view twice retires identically.
        again = history.build_api_payload(self.BUDGET)
        assert again.messages == view.messages
        assert again.stats.retired_observation_blocks == (
            view.stats.retired_observation_blocks
        )

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


# ── 2: evidence receipts preserve the facts ─────────────────────────────────


class TestEvidenceReceipts:
    """Retired observations keep every fact the next decision might need."""

    def _history(self) -> History:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the retry cap so the job pauses.")
        # A: a completed observation well beyond the recent-evidence allowance.
        for msg in tool_block(
            "a1", "read_files", {"paths": ["a.py"]},
            read_files_result(["a.py"], 60_000),
            reasoning="reading the target\n",
        ):
            history.messages.append(msg)
        # B: a second completed observation with a huge bulk payload.
        for msg in tool_block(
            "b1", "grep_search", {"pattern": "def foo", "scope": "src"},
            big_grep_result("def foo", 2, 40_000),
            reasoning="locating the call sites\n",
        ):
            history.messages.append(msg)
        # F: a failed read — never retired, however old it gets.
        for msg in tool_block(
            "f1", "read_files", {"paths": ["missing.py"]},
            read_files_result(["missing.py"], 0),
            reasoning="checking a missing file\n",
        ):
            history.messages.append(msg)
        # A later write makes a.py stale; the receipt must say so.
        for msg in tool_block(
            "w1", "write_file", {"path": "a.py", "content": "x = 2\n"},
            {"ok": True, "applied": True, "path": "a.py"},
        ):
            history.messages.append(msg)
        for msg in tool_block(
            "c1", "read_file_range", {"path": "a.py", "offset": 1, "limit": 40},
            {"ok": True, "path": "a.py", "status": "complete",
             "content": "x = 2\n", "content_hash": "h-latest",
             "included_range": {"start_line": 1, "end_line": 40}},
            reasoning="verifying\n",
        ):
            history.messages.append(msg)
        return history

    def test_receipts_retain_paths_hashes_ranges_statuses_and_counts(
        self,
    ) -> None:
        history = self._history()
        view = history.build_api_payload(8_000)
        receipts = receipts_in(view.messages)

        assert len(receipts) == 2, "both completed observations must be retired"
        by_tool = {r["tool"]: r for r in receipts}
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

        for receipt in receipts_in(view.messages):
            assert len(json.dumps(receipt)) <= 2_000
            json.loads(json.dumps(receipt))  # round-trips

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
            "a failed observation was retired — it may still be under recovery"
        )
        assert not {"a1", "b1"} & answered, "a completed observation survived"
        active = next(
            m for m in reversed(view.messages)
            if m.get("role") == "assistant" and m.get("tool_calls")
        )
        assert active["tool_calls"][0]["id"] == "c1"
        assert active["reasoning_content"] == "verifying\n"
        assert_tool_pairing_valid(view.messages)


# ── 3: never-retired outputs get bounded, structured replays ────────────────


class TestBoundedReplays:
    """Terminal/diagnostic outputs replay forever — but never verbatim."""

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

    def test_a_completed_terminal_run_is_bounded_with_its_envelope(self) -> None:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Fix the failing test.")
        big = self._terminal_result(200_000)
        for msg in tool_block(
            "t1", "run_terminal_command", {"command": "pytest -q"}, big
        ):
            history.messages.append(msg)
        for msg in tool_block(
            "r1", "read_file", {"path": "a.py"},
            {"ok": True, "content": "x = 1\n"},
            reasoning="inspecting\n",
        ):
            history.messages.append(msg)

        # Generous budget: no ladder pressure at all. The bound is the
        # lifecycle replay cap, not budget pressure.
        view = history.build_api_payload(10_000_000)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")

        assert view.stats.bounded_replays >= 1
        assert len(tool_msg["content"]) < len(big), "the output was not bounded"
        assert len(tool_msg["content"]) <= 17_000
        parsed = json.loads(tool_msg["content"])  # envelope survives
        assert parsed["ok"] is False
        assert parsed["exit_code"] == 1
        assert parsed["failure_class"] == "execution_failed"
        assert "aura compacted head+tail" in parsed["output"]
        assert parsed["output"].endswith("traceback: FAILED at end"), (
            "the tail — where the failure lives — must survive the cut"
        )
        # The active chain is untouched.
        last = next(
            m for m in reversed(view.messages)
            if m.get("role") == "tool"
        )
        assert last["tool_call_id"] == "r1"
        assert_tool_pairing_valid(view.messages)

    def test_the_active_chain_terminal_result_stays_verbatim(self) -> None:
        history = History()
        history.set_system("system prompt")
        history.append_user_text("Run the tests.")
        big = self._terminal_result(120_000)
        for msg in tool_block(
            "t1", "run_terminal_command", {"command": "pytest -q"}, big,
            reasoning="running\n",
        ):
            history.messages.append(msg)

        view = history.build_api_payload(10_000_000)
        tool_msg = next(m for m in view.messages if m.get("role") == "tool")
        assert tool_msg["content"] == big, (
            "active terminal output must stay exactly as produced"
        )
        assert view.stats.bounded_replays == 0


# ── 4: the production recovery loop preserves evidence and prefix ───────────


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
    failed evidence the turn is still recovering from.
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

        # Invariant: failed-validation output stays available through the
        # repair and rerun — present in every request after the failure.
        w1_id = "w1"
        for call in backend.calls[7:]:
            answered = {
                m.get("tool_call_id")
                for m in call["messages"]
                if m.get("role") == "tool"
            }
            assert w1_id in answered, (
                "a later request lost the failed-validation output"
            )

        # Older completed observations beyond the allowance became receipts,
        # while the newest read, the stall rounds, and both writes stayed.
        w1_request = backend.calls[6]["messages"]
        receipts = receipts_in(w1_request)
        assert len(receipts) >= 2, "no completed observation was retired"
        answered = {
            m.get("tool_call_id") for m in w1_request if m.get("role") == "tool"
        }
        assert "r3" in answered, "the newest completed read was retired"
        assert "d1" in answered and "d2" in answered

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

        # Invariant: failed mutation evidence stays available until the turn
        # ends — present in every request after each failure.
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


# ── 5: the Tier-1 prefix is frozen inside one user turn ─────────────────────


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
