"""Cancellation integrity: a cancel mid-turn never erases completed work.

When cancellation interrupts an assistant tool-call block, history ends at an
assistant message whose calls have no results.  The manager must repair — not
rewind — the current real-user turn:

* every completed assistant/tool-result block is preserved byte-for-byte;
* only the newest incomplete assistant tool-call block is touched;
* each call whose authoritative result never arrived receives exactly one
  structured synthetic cancellation result, in call order, so the provider's
  tool-call pairing stays valid;
* synthetic results are fail-closed — never ``applied``, never successful;
* repair is idempotent;
* the whole real-user turn is never rewound, and a malformed newest block is
  removed on its own.

These drive the real :class:`ConversationManager` and its real
:class:`~aura.conversation.history.History`; only the history is hand-seeded so
each repair scenario can be asserted exactly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from aura.client import ApiError
from aura.conversation.api_view import repair_tool_call_blocks
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.tools.registry import ToolRegistry

APPLIED_WRITE = json.dumps(
    {"ok": True, "applied": True, "path": "notes.md", "write_outcome": "applied"}
)
FAILED_VALIDATION = json.dumps(
    {
        "ok": False,
        "command": "python -m pytest",
        "exit_code": 1,
        "validation_ok": False,
        "counts_as_validation": True,
        "counts_as_product_failure": True,
    }
)
PASSED_VALIDATION = json.dumps(
    {
        "ok": True,
        "command": "python -m pytest",
        "exit_code": 0,
        "validation_ok": True,
        "counts_as_validation": True,
        "counts_as_product_failure": False,
    }
)
READ_RESULT = json.dumps({"ok": True, "path": "src/app.py", "truncated": False})


def asst(content=None, reasoning=None, tool_calls=None) -> dict:
    msg: dict = {"role": "assistant"}
    if content is not None:
        msg["content"] = content
    if reasoning is not None:
        msg["reasoning_content"] = reasoning
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return msg


def calls(*pairs: tuple[str, str]) -> list[dict]:
    """Assistant tool-call entries in call order: ``(call_id, tool_name)``."""
    return [
        {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": "{}"},
        }
        for call_id, name in pairs
    ]


def tool_result(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def make_manager(history: History) -> ConversationManager:
    return ConversationManager(
        history, ToolRegistry(workspace_root=Path.cwd(), mode="single")
    )


class _Events:
    def __init__(self) -> None:
        self.events: list = []

    def __call__(self, ev) -> None:
        self.events.append(ev)

    def api_errors(self) -> list[ApiError]:
        return [e for e in self.events if isinstance(e, ApiError)]


def _cleanup(history: History):
    recorder = _Events()
    make_manager(history)._cleanup_cancelled(recorder)
    return recorder


def _synthetic_payload(message: dict) -> dict:
    return json.loads(message["content"])


def _assert_cancellation_payload(payload: dict, tool: str) -> None:
    """The exact structured contract a synthetic result must honour."""
    assert payload["ok"] is False
    assert payload["cancelled"] is True
    assert payload["recoverable"] is False
    assert payload["failure_class"] == "cancelled"
    assert payload["execution_status"] == "interrupted_before_authoritative_result"
    assert payload["tool"] == tool
    assert "applied" not in payload, "a synthetic result must never claim applied"
    assert payload.get("applied") is not True
    assert "no workspace changes" not in payload.get("message", ""), (
        "a synthetic result must not claim the tool made no changes"
    )


# ── 1. cancellation before assistant output ─────────────────────────────────


class TestCancelBeforeAssistantOutput:
    def test_empty_history_still_emits_cancelled(self) -> None:
        history = History()
        recorder = _cleanup(history)
        assert [e.message for e in recorder.api_errors()] == ["Cancelled."]

    def test_turn_with_only_the_user_message_is_untouched(self) -> None:
        history = History()
        history.append_user_text("fix the bug")
        before = copy.deepcopy(history.messages)
        recorder = _cleanup(history)
        assert history.messages == before, "a cancelled-before-output turn must not change"
        assert [e.message for e in recorder.api_errors()] == ["Cancelled."]

    def test_prior_completed_turns_are_not_rewound(self) -> None:
        """Cancel fires before this turn's assistant output; the earlier
        completed turn stays intact."""
        history = History()
        history.append_user_text("first task")
        history.append_assistant(asst(tool_calls=calls(("r1", "read_file"))))
        history.append_tool_result("r1", READ_RESULT)
        history.append_user_text("second task")
        before = copy.deepcopy(history.messages)
        _cleanup(history)
        assert history.messages == before


# ── 2. cancellation during reasoning or prose ───────────────────────────────


class TestCancelDuringProse:
    def test_partial_prose_block_is_preserved_byte_for_byte(self) -> None:
        prose = asst(
            content="Let me check the ownership model before editing.",
            reasoning="the factory is the right place to look",
        )
        history = History()
        history.append_user_text("refactor the loader")
        history.append_assistant(prose)
        _cleanup(history)
        assert history.messages == [
            {"role": "user", "content": "refactor the loader"},
            prose,
        ]

    def test_partial_stream_content_survives_alongside_earlier_blocks(self) -> None:
        history = History()
        history.append_user_text("add validation")
        history.append_assistant(asst(tool_calls=calls(("r1", "read_file"))))
        history.append_tool_result("r1", READ_RESULT)
        history.append_assistant(asst(content="I found the call site — "))
        before_prose = history.messages[-1]
        _cleanup(history)
        assert history.messages[-1] is before_prose
        assert history.messages[1] == asst(tool_calls=calls(("r1", "read_file")))
        assert history.messages[2] == tool_result("r1", READ_RESULT)

    def test_empty_assistant_block_is_still_stripped(self) -> None:
        history = History()
        history.append_user_text("update notes")
        history.append_assistant(asst())
        _cleanup(history)
        assert history.messages == [{"role": "user", "content": "update notes"}]


# ── 3. completed blocks remain unchanged ────────────────────────────────────


class TestCompletedBlocksUnchanged:
    def test_a_fully_complete_turn_is_untouched(self) -> None:
        history = History()
        history.append_user_text("add validation")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=calls(("v1", "run_terminal_command"))))
        history.append_tool_result("v1", PASSED_VALIDATION)
        before = copy.deepcopy(history.messages)
        _cleanup(history)
        assert history.messages == before

    def test_completed_blocks_stay_byte_for_byte_while_newest_is_repaired(self) -> None:
        history = History()
        history.append_user_text("fix and verify")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=calls(("v1", "run_terminal_command"))))
        history.append_tool_result("v1", PASSED_VALIDATION)
        history.append_assistant(asst(tool_calls=calls(("w2", "write_file"))))

        completed = copy.deepcopy(history.messages[:5])
        _cleanup(history)

        assert history.messages[:5] == completed
        assert len(history.messages) == 7
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[6]), "write_file"
        )
        assert history.messages[6]["tool_call_id"] == "w2"


# ── 4. partial multi-call batches ───────────────────────────────────────────


class TestPartialMultiCallBatches:
    def test_missing_calls_get_one_synthetic_result_in_call_order(self) -> None:
        history = History()
        history.append_user_text("batch it")
        history.append_assistant(asst(tool_calls=calls(
            ("a1", "read_file"), ("b2", "write_file"), ("c3", "run_terminal_command")
        )))
        history.append_tool_result("a1", READ_RESULT)

        _cleanup(history)

        assert [m["role"] for m in history.messages] == [
            "user", "assistant", "tool", "tool", "tool",
        ]
        # The authoritative result that arrived is preserved verbatim.
        assert history.messages[2] == tool_result("a1", READ_RESULT)
        # The missing two are paired in call order with fail-closed payloads.
        assert history.messages[3]["tool_call_id"] == "b2"
        assert history.messages[4]["tool_call_id"] == "c3"
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[3]), "write_file"
        )
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[4]), "run_terminal_command"
        )

    def test_synthetic_results_never_claim_applied(self) -> None:
        history = History()
        history.append_user_text("batch it")
        history.append_assistant(asst(tool_calls=calls(
            ("m1", "edit_file"), ("m2", "write_file")
        )))
        _cleanup(history)
        for message in history.messages[-2:]:
            payload = _synthetic_payload(message)
            assert payload.get("applied") is not True
            assert "applied" not in payload


# ── 5 & 6 & 7. cancellation after real work (write / failure / repair) ──────


class TestCancelAfterWork:
    def test_cancel_after_an_applied_write_preserves_the_write(self) -> None:
        history = History()
        history.append_user_text("fix it")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=calls(("w2", "write_file"))))

        _cleanup(history)

        assert history.messages[:3] == [
            {"role": "user", "content": "fix it"},
            asst(tool_calls=calls(("w1", "write_file"))),
            tool_result("w1", APPLIED_WRITE),
        ]
        assert len(history.messages) == 5
        assert history.messages[4]["tool_call_id"] == "w2"

    def test_cancel_after_a_validation_failure_preserves_the_failure(self) -> None:
        history = History()
        history.append_user_text("run the tests")
        history.append_assistant(asst(tool_calls=calls(("v1", "run_terminal_command"))))
        history.append_tool_result("v1", FAILED_VALIDATION)
        history.append_assistant(asst(tool_calls=calls(("v2", "run_terminal_command"))))

        _cleanup(history)

        assert history.messages[:3] == [
            {"role": "user", "content": "run the tests"},
            asst(tool_calls=calls(("v1", "run_terminal_command"))),
            tool_result("v1", FAILED_VALIDATION),
        ]
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[4]), "run_terminal_command"
        )

    def test_cancel_after_a_repair_and_passing_rerun_preserves_everything(self) -> None:
        history = History()
        history.append_user_text("make the tests pass")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=calls(("v1", "run_terminal_command"))))
        history.append_tool_result("v1", FAILED_VALIDATION)
        history.append_assistant(asst(tool_calls=calls(("w2", "write_file"))))
        history.append_tool_result("w2", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=calls(("v2", "run_terminal_command"))))
        history.append_tool_result("v2", PASSED_VALIDATION)
        history.append_assistant(asst(tool_calls=calls(("w3", "write_file"))))

        completed = copy.deepcopy(history.messages[:9])
        _cleanup(history)

        assert history.messages[:9] == completed
        assert len(history.messages) == 11
        assert history.messages[10]["tool_call_id"] == "w3"


# ── 8. focused-action cancellation ──────────────────────────────────────────


class TestFocusedActionCancellation:
    def test_a_single_focused_call_is_paired_with_a_synthetic_result(self) -> None:
        history = History()
        history.append_user_text("land the edit")
        history.append_assistant(asst(tool_calls=calls(("fa1", "write_file"))))
        _cleanup(history)
        assert len(history.messages) == 3
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[2]), "write_file"
        )

    def test_a_focused_blocker_interrupted_mid_call_is_paired(self) -> None:
        history = History()
        history.append_user_text("land the edit")
        history.append_assistant(asst(tool_calls=calls(("fb1", "report_blocker"))))
        _cleanup(history)
        _assert_cancellation_payload(
            _synthetic_payload(history.messages[2]), "report_blocker"
        )


# ── 9. provider replay / tool-result pairing ────────────────────────────────


class TestProviderPairing:
    def test_repaired_history_is_fully_replayable(self) -> None:
        """A later API view must not need to repair the repaired history."""
        history = History()
        history.append_user_text("batch it")
        history.append_assistant(asst(tool_calls=calls(
            ("a1", "read_file"), ("b2", "write_file"), ("c3", "run_terminal_command")
        )))
        history.append_tool_result("a1", READ_RESULT)
        _cleanup(history)

        view = copy.deepcopy(history.messages)
        removed = repair_tool_call_blocks(view)
        assert removed == 0, "a repaired block must pair cleanly for replay"

    def test_only_the_newest_incomplete_block_is_repaired(self) -> None:
        history = History()
        history.append_user_text("work")
        history.append_assistant(asst(tool_calls=calls(("a1", "read_file"))))
        history.append_tool_result("a1", READ_RESULT)
        history.append_assistant(asst(tool_calls=calls(("b2", "write_file"))))
        _cleanup(history)

        # The earlier read block keeps exactly its one authoritative result.
        assert history.messages[2] == tool_result("a1", READ_RESULT)
        # The newest block gains one synthetic pairing and nothing else.
        assert len(history.messages) == 5
        assert history.messages[4]["tool_call_id"] == "b2"
        assert history.messages[3] == asst(tool_calls=calls(("b2", "write_file")))


# ── 10. idempotence ─────────────────────────────────────────────────────────


class TestIdempotence:
    def test_second_cleanup_changes_nothing(self) -> None:
        history = History()
        history.append_user_text("batch it")
        history.append_assistant(asst(tool_calls=calls(
            ("a1", "read_file"), ("b2", "write_file"), ("c3", "run_terminal_command")
        )))
        history.append_tool_result("a1", READ_RESULT)

        _cleanup(history)
        after_first = copy.deepcopy(history.messages)
        _cleanup(history)

        assert history.messages == after_first

    def test_second_cleanup_after_no_incomplete_block_changes_nothing(self) -> None:
        history = History()
        history.append_user_text("run the tests")
        history.append_assistant(asst(tool_calls=calls(("v1", "run_terminal_command"))))
        history.append_tool_result("v1", PASSED_VALIDATION)

        _cleanup(history)
        after_first = copy.deepcopy(history.messages)
        _cleanup(history)

        assert history.messages == after_first


# ── 11. explicit Retry regression ───────────────────────────────────────────


class TestRetryRegression:
    def test_retry_rewinds_to_the_real_user_message_after_cleanup(self) -> None:
        """A repaired turn still rewinds cleanly for an explicit Retry: the
        user request is kept, the whole (repaired) response is dropped."""
        history = History()
        history.append_user_text("batch it")
        history.append_assistant(asst(tool_calls=calls(
            ("a1", "read_file"), ("b2", "write_file")
        )))
        _cleanup(history)

        assert history.rewind_to_last_user_turn() is True
        assert history.messages == [{"role": "user", "content": "batch it"}]

    def test_rewind_drops_internal_steering_but_keeps_the_real_request(self) -> None:
        history = History()
        history.append_user_text("batch it")
        history.append_internal_user_text("Aura steering: reread first.")
        history.append_assistant(asst(tool_calls=calls(("a1", "read_file"))))
        _cleanup(history)

        assert history.rewind_to_last_user_turn() is True
        assert history.messages == [{"role": "user", "content": "batch it"}]


# ── malformed newest block ──────────────────────────────────────────────────


class TestMalformedNewestBlock:
    def test_malformed_newest_block_is_removed_alone(self) -> None:
        history = History()
        history.append_user_text("fix it")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls=[{"type": "function", "function": {"name": "write_file"}}]))

        _cleanup(history)

        # The completed write block survives; only the malformed block is gone.
        assert history.messages == [
            {"role": "user", "content": "fix it"},
            asst(tool_calls=calls(("w1", "write_file"))),
            tool_result("w1", APPLIED_WRITE),
        ]

    def test_non_list_tool_calls_removes_only_the_newest_block(self) -> None:
        history = History()
        history.append_user_text("fix it")
        history.append_assistant(asst(tool_calls=calls(("w1", "write_file"))))
        history.append_tool_result("w1", APPLIED_WRITE)
        history.append_assistant(asst(tool_calls={"oops": "not a list"}))

        _cleanup(history)

        assert history.messages == [
            {"role": "user", "content": "fix it"},
            asst(tool_calls=calls(("w1", "write_file"))),
            tool_result("w1", APPLIED_WRITE),
        ]
