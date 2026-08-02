"""Internal Aura messages must not redefine the current conversation turn.

Aura injects its own ``role="user"`` messages — steering nudges, loop guards,
recovery notices — marked ``aura_internal``. The API view used to treat every
``role="user"`` message as the start of a new turn, so one long production tool
loop got sliced into artificial turns. The consequence was concrete and bad:
evidence the model had just gathered *for the request it is still working on*
fell behind the synthetic boundary, became "an old turn", and was handed to
old-turn compaction and whole-block dropping while genuinely stale turns from
earlier requests were still sitting in the view.

What is asserted here:

* an ``aura_internal`` user message inside a tool loop is not a turn start;
* under real budget pressure, current-request source evidence that sits *before*
  an internal message outlives genuinely older turns;
* internal messages still reach the model, just without the internal marker;
* genuine user messages still start turns, and pairing / non-destructiveness
  survive the change.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from aura.conversation.api_view import _turn_starts, build_api_view
from aura.conversation.history import History

# ── helpers ─────────────────────────────────────────────────────────────────


def assert_tool_pairing_valid(messages: list[dict[str, Any]]) -> None:
    """Every tool message answers a tool_call in the assistant right above it."""
    i = 0
    seen_tool_ids: set[str] = set()
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
            assert call_id not in seen_tool_ids, f"duplicate tool result {call_id}"
            seen_tool_ids.add(call_id)
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
    result: str,
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
    return [assistant, {"role": "tool", "tool_call_id": call_id, "content": result}]


def source_result(path: str, chars: int) -> str:
    """A realistic read_files payload of a known size."""
    return json.dumps({
        "ok": True,
        "requested_paths": [path],
        "files": {
            path: {
                "ok": True,
                "path": path,
                "status": "complete",
                "reason": "file included in full",
                "file_size": chars,
                "content_hash": f"hash-{path}",
                "line_count": 3,
                "included_range": {"start_line": 1, "end_line": 3},
                "content": f"# {path}\n" + ("x" * chars) + "\n",
                "continuation": "",
            }
        },
    })


def answered_call_ids(messages: list[dict[str, Any]]) -> set[str]:
    return {m["tool_call_id"] for m in messages if m.get("role") == "tool"}


def long_loop_history(*, old_turns: int = 6, chars: int = 12_000) -> History:
    """Old requests, then one current request whose tool loop is interrupted.

    The current request looks like production: read some source, get steered by
    an internal Aura message, keep working. ``cur-a`` is the evidence that sits
    behind the internal message and used to be treated as belonging to an old
    turn.
    """
    h = History()
    h.set_system("You are Aura's production coding agent.")

    for turn in range(old_turns):
        h.append_user_text(f"old request {turn}")
        for msg in tool_block(
            f"old-{turn}", "read_files", {"paths": [f"old_{turn}.py"]},
            source_result(f"old_{turn}.py", chars),
            reasoning=f"thinking about old {turn}\n",
        ):
            h.messages.append(msg)

    h.append_user_text("Fix the retry cap so the job pauses.")
    for msg in tool_block(
        "cur-a", "read_files", {"paths": ["current_a.py"]},
        source_result("current_a.py", chars),
        reasoning="reading the file under repair\n",
    ):
        h.messages.append(msg)
    h.append_internal_user_text(
        "Loop guard: you have re-read the same file twice. Make an edit."
    )
    for msg in tool_block(
        "cur-b", "read_files", {"paths": ["current_b.py"]},
        source_result("current_b.py", chars),
        reasoning="reading the caller\n",
    ):
        h.messages.append(msg)
    return h


# ── 1: an internal message is not a turn boundary ───────────────────────────


class TestInternalMessagesAreNotTurnStarts:

    def test_internal_user_message_inside_a_tool_loop_starts_no_turn(self) -> None:
        h = long_loop_history()
        starts = _turn_starts(h.messages)

        internal_indices = [
            i for i, m in enumerate(h.messages) if m.get("aura_internal")
        ]
        assert internal_indices, "the fixture stopped injecting an internal message"
        for idx in internal_indices:
            assert idx not in starts, (
                f"the internal message at {idx} was counted as a new user turn"
            )

    def test_the_current_turn_starts_at_the_genuine_request(self) -> None:
        h = long_loop_history(old_turns=6)
        starts = _turn_starts(h.messages)

        assert len(starts) == 7, "one turn per genuine user message, no more"
        current_start = starts[-1]
        assert h.messages[current_start]["content"].startswith("Fix the retry cap")
        # cur-a's evidence is inside the current turn, not behind its boundary.
        cur_a = next(
            i for i, m in enumerate(h.messages) if m.get("tool_call_id") == "cur-a"
        )
        assert cur_a > current_start

    def test_a_run_of_internal_messages_still_starts_no_turn(self) -> None:
        h = History()
        h.set_system("s")
        h.append_user_text("real request")
        for _ in range(3):
            h.append_internal_user_text("internal notice")

        assert _turn_starts(h.messages) == [0]

    def test_genuine_user_messages_still_start_turns(self) -> None:
        h = History()
        h.set_system("s")
        h.append_user_text("first")
        h.append_internal_user_text("steering")
        h.append_user_text("second")

        assert _turn_starts(h.messages) == [0, 2], (
            "turn detection was disabled rather than corrected"
        )

    def test_multimodal_user_turns_are_still_boundaries(self) -> None:
        h = History()
        h.set_system("s")
        h.append_user_text("first")
        h.append_user_multimodal([{"type": "text", "text": "look at this"}])

        assert _turn_starts(h.messages) == [0, 1]


# ── 2: current-request evidence outlives genuinely older turns ──────────────


class TestCurrentRequestEvidenceSurvivesInternalMessages:
    """The defect that made this worth fixing, exercised end to end."""

    # Tight enough that every droppable old block must go, loose enough that the
    # current request's own two blocks can be kept (compacted) rather than cut.
    BUDGET = 4_000

    def test_evidence_before_an_internal_message_outlives_old_turns(self) -> None:
        h = long_loop_history()
        view = h.build_api_payload(self.BUDGET)
        answered = answered_call_ids(view.messages)

        assert view.stats.dropped_blocks > 0, (
            "no block was dropped, so this budget proves nothing about priority"
        )
        assert not any(cid.startswith("old-") for cid in answered), (
            "older turns survived, so the pressure never reached the interesting case"
        )
        assert "cur-a" in answered, (
            "current-request evidence sitting before an internal Aura message was "
            "dropped as if it belonged to an old turn"
        )
        assert "cur-b" in answered, "the newest evidence was dropped"

    def test_that_evidence_is_compacted_rather_than_discarded(self) -> None:
        h = long_loop_history()
        view = h.build_api_payload(self.BUDGET)

        cur_a = next(m for m in view.messages if m.get("tool_call_id") == "cur-a")
        parsed = json.loads(cur_a["content"])
        assert set(parsed["files"]) == {"current_a.py"}
        assert parsed["files"]["current_a.py"]["content_hash"]
        assert parsed["files"]["current_a.py"]["status"] == "complete"

    def test_an_internal_message_does_not_shield_older_turns(self) -> None:
        """Turn *preservation* is still counted in genuine turns, not injections."""
        h = long_loop_history(old_turns=10)
        view = h.build_api_payload(self.BUDGET)
        answered = answered_call_ids(view.messages)

        assert answered <= {"cur-a", "cur-b"}
        assert "cur-a" in answered

    @pytest.mark.parametrize("budget", [200_000, 40_000, 12_000, 4_000, 800])
    def test_pairing_holds_at_every_budget(self, budget) -> None:
        h = long_loop_history()
        view = h.build_api_payload(budget)
        assert_tool_pairing_valid(view.messages)

    def test_an_uncompacted_view_is_unaffected(self) -> None:
        h = long_loop_history()
        view = h.build_api_payload(10_000_000)

        assert view.stats.compacted_results == 0
        assert view.stats.dropped_blocks == 0
        assert answered_call_ids(view.messages) == (
            {f"old-{i}" for i in range(6)} | {"cur-a", "cur-b"}
        )


# ── 3: internal messages still reach the model, history stays untouched ─────


class TestInternalMessagesStillReachTheModel:

    def test_the_internal_message_is_present_without_its_marker(self) -> None:
        h = long_loop_history()
        view = h.build_api_payload(10_000_000)

        internal = [
            m for m in view.messages
            if m.get("role") == "user" and "Loop guard" in str(m.get("content"))
        ]
        assert len(internal) == 1, "the internal message was dropped from the view"
        assert "aura_internal" not in internal[0], (
            "the internal marker leaked into the outbound payload"
        )

    def test_it_survives_heavy_compaction(self) -> None:
        h = long_loop_history()
        view = h.build_api_payload(800)

        assert any(
            m.get("role") == "user" and "Loop guard" in str(m.get("content"))
            for m in view.messages
        ), "compaction removed the steering the model still needs to obey"

    def test_storage_is_never_edited(self) -> None:
        h = long_loop_history()
        before = json.dumps(h.messages, sort_keys=True)

        view = h.build_api_payload(800)

        assert json.dumps(h.messages, sort_keys=True) == before, (
            "the canonical log was edited to fit the context window"
        )
        assert h.messages[-3].get("aura_internal") is True, (
            "the stored internal marker was stripped from canonical history"
        )
        assert view.stats.tokens_after < view.stats.tokens_before

    def test_repeated_builds_are_stable(self) -> None:
        h = long_loop_history()
        first = h.build_api_payload(4_000).messages
        second = h.build_api_payload(4_000).messages
        assert first == second


# ── 4: a history of nothing but internal messages degrades safely ───────────


class TestOnlyInternalUserMessages:
    """No genuine turn exists — compaction must still terminate and stay valid."""

    def _history(self) -> History:
        h = History()
        h.set_system("s")
        h.append_internal_user_text("recovery notice")
        for turn in range(4):
            for msg in tool_block(
                f"i{turn}", "read_files", {"paths": [f"f{turn}.py"]},
                source_result(f"f{turn}.py", 12_000),
                reasoning="thinking\n",
            ):
                h.messages.append(msg)
        return h

    def test_no_genuine_turn_still_compacts_and_pairs(self) -> None:
        h = self._history()
        assert _turn_starts(h.messages) == []

        view = h.build_api_payload(1_000)

        assert_tool_pairing_valid(view.messages)
        assert view.stats.tokens_after < view.stats.tokens_before
        for msg in view.messages:
            if msg.get("role") == "tool":
                json.loads(msg["content"])  # must not raise

    def test_build_api_view_handles_it_directly(self) -> None:
        h = self._history()
        view = build_api_view(h.system_prompt, h.messages, 1_000)
        assert view.messages
        assert_tool_pairing_valid(view.messages)
