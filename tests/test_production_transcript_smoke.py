"""Smoke: one production turn runs straight through without circling.

The consolidated production contract (``aura/roles/bundled/single.md``) asks for
one pass — orientation, focused reads, one decision, edit, validation, receipt.
Two different things are checked here, and only together are they evidence:

* **Projection** (``ProductionExecutionSession``): a disciplined transcript
  lands in the workspace in the right order with one receipt.  This is a
  projection test and nothing more — feeding it an ideal transcript proves
  nothing about whether the agent circles.
* **Control** (``ConversationManager``): the circular transcript is streamed
  through the *real* send loop, and the loop is what stops it — the pre-tool
  essay never reaches chat or history, and the second identical read is
  rejected.  The manager owns real streamed rounds; the projection layer never
  sees the prose at all.

Deeper coverage of the buffering contract and the loop guard lives in
``tests/test_single_pre_tool_narration.py``.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.bridge.production_execution import ProductionExecutionSession
from aura.bridge.production_receipt import STATUS_COMPLETED
from aura.client import (
    ContentDelta,
    Done,
    Event,
    ReasoningDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    ToolResult,
)
from aura.conversation.history import History
from aura.conversation.manager import ConversationManager
from aura.conversation.pre_edit_loop_guard import DUPLICATE_READ_REASON
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry
from aura.model_streams import PRODUCTION_STREAM_HOOK, ModelStreamRegistry
from aura.worker_todo import UPDATE_WORKER_TODO_TOOL

pytest.importorskip("PySide6.QtWidgets")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


class _ApprovalProxyStub:
    def __init__(self) -> None:
        self.events: list[dict] = []

    def queue_event(self, rel_path: str, old: str, new: str, is_new_file: bool) -> None:
        self.events.append({
            "rel_path": rel_path,
            "old_content": old,
            "new_content": new,
            "is_new_file": is_new_file,
        })

    def consume_last_event(self):
        return self.events.pop(0) if self.events else None


@pytest.fixture
def approval_proxy() -> _ApprovalProxyStub:
    return _ApprovalProxyStub()


@pytest.fixture
def session(qapp, approval_proxy) -> ProductionExecutionSession:
    return ProductionExecutionSession(approval_proxy=approval_proxy)


# ── transcript fixtures ─────────────────────────────────────────────────────

# The turn the contract asks for: orient, read the two files that own the
# behaviour, decide once, edit, validate, report.
DISCIPLINED_PROSE = [
    "Orienting: locating the owner of the retry cap.",
    "Read `aura/work_artifact/runner.py` and `tests/test_work_artifact_runner.py`. "
    "`_ARTIFACT_ITEM_RETRY_CAP` is enforced in `_run_item`, which breaks the inner "
    "loop instead of returning.",
    "Decision: return a paused job from `_run_item` when the cap is hit.",
    "Editing `aura/work_artifact/runner.py`.",
    "Running the focused test.",
    "Changed `aura/work_artifact/runner.py`: the retry cap now returns a paused "
    "job. Verified by `python -m pytest tests/test_work_artifact_runner.py` — "
    "4 passed.",
]

# The pre-fix behaviour: the same decision restated four times, a full patch
# narrated before any edit, acceptance criteria invented mid-turn, and a second
# round of reads that answers no new question.
CIRCULAR_PROSE = [
    "Orienting: locating the owner of the retry cap.",
    "Read `aura/work_artifact/runner.py`. The cap is enforced in `_run_item`.",
    "Let me think about the options. Option A: return a paused job from "
    "`_run_item`. Option B: raise and catch upstream. Option C: flag the job and "
    "let the caller decide. I lean toward Option A.",
    "Acceptance criteria: the job is paused; the receipt names the cap; existing "
    "callers keep working; no behaviour change below the cap.",
    "Here is the proposed patch before I apply anything:\n"
    "```python\n"
    "if attempts >= _ARTIFACT_ITEM_RETRY_CAP:\n"
    "    return _paused(job)\n"
    "```",
    "Re-reading `aura/work_artifact/runner.py` to double-check the decision.",
    "Reconsidering: Option A returns a paused job from `_run_item`. Option B "
    "raises and catches upstream. I still lean toward Option A.",
    "To summarize the plan again: return a paused job from `_run_item` when the "
    "retry cap is reached, and keep the existing callers working.",
    "Editing `aura/work_artifact/runner.py`.",
]


def _write_result(tool_id: str, path: str) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_id,
        name="write_file",
        ok=True,
        result=json.dumps({
            "ok": True, "applied": True, "path": path,
            "rel_path": path, "is_new_file": False,
        }),
        extras={"approval": "approve", "rel_path": path},
    )


def _read_result(tool_id: str, path: str) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_id,
        name="read_file",
        ok=True,
        result=json.dumps({"ok": True, "path": path}),
        extras={},
    )


def _validation_result(tool_id: str, command: str) -> ToolResult:
    return ToolResult(
        tool_call_id=tool_id,
        name="run_terminal_command",
        ok=True,
        result=json.dumps({
            "ok": True, "command": command, "exit_code": 0,
            "output": "4 passed", "counts_as_validation": True,
            "validation_classification": "passed",
            "counts_as_product_failure": False,
        }),
        extras={},
    )


def _todo_result(tool_id: str) -> ToolResult:
    items = [
        {"text": "Find the retry cap owner", "status": "done"},
        {"text": "Return a paused job at the cap", "status": "active"},
        {"text": "Run the focused test", "status": "pending"},
    ]
    return ToolResult(
        tool_call_id=tool_id,
        name=UPDATE_WORKER_TODO_TOOL,
        ok=True,
        result=json.dumps({"ok": True, "items": items}),
        extras={},
    )


def _run_disciplined_turn(session, approval_proxy) -> None:
    """One production turn: orient → read → decide → edit → validate → report."""
    session.begin(model="prod-model")

    # Orientation
    session.handle_event(ReasoningDelta(text="orienting"))
    session.handle_event(ContentDelta(text=DISCIPLINED_PROSE[0]))
    session.handle_event(ToolCallStart(index=0, id="g1", name="grep_search"))
    session.handle_event(ToolResult(
        tool_call_id="g1", name="grep_search", ok=True,
        result=json.dumps({"ok": True, "matches": 2}), extras={},
    ))
    session.handle_event(ToolCallStart(index=1, id="todo", name=UPDATE_WORKER_TODO_TOOL))
    session.handle_event(_todo_result("todo"))

    # Focused reads, batched
    session.handle_event(ToolCallStart(index=2, id="r1", name="read_file"))
    session.handle_event(ToolCallStart(index=3, id="r2", name="read_file"))
    session.handle_event(_read_result("r1", "aura/work_artifact/runner.py"))
    session.handle_event(_read_result("r2", "tests/test_work_artifact_runner.py"))
    session.handle_event(ContentDelta(text=DISCIPLINED_PROSE[1]))

    # One decision, then the edit
    session.handle_event(ContentDelta(text=DISCIPLINED_PROSE[2]))
    session.handle_event(ContentDelta(text=DISCIPLINED_PROSE[3]))
    session.handle_event(ToolCallStart(index=4, id="w1", name="write_file"))
    approval_proxy.queue_event(
        "aura/work_artifact/runner.py", "old", "new", False
    )
    session.handle_event(_write_result("w1", "aura/work_artifact/runner.py"))

    # Focused validation
    session.handle_event(ContentDelta(text=DISCIPLINED_PROSE[4]))
    session.handle_event(ToolCallStart(index=5, id="c1", name="run_terminal_command"))
    session.handle_event(_validation_result(
        "c1", "python -m pytest tests/test_work_artifact_runner.py"
    ))

    # Receipt
    session.handle_event(Done(
        finish_reason="stop",
        full_message={"role": "assistant", "content": DISCIPLINED_PROSE[5]},
    ))


# ── the real send loop, driven with the circular transcript ─────────────────


def _scripted_tool_round(
    prose: str, calls: list[tuple[str, str, dict]]
) -> list[Event]:
    """One streamed round: prose, then tool calls, then Done."""
    events: list[Event] = [ContentDelta(text=prose)]
    tool_calls = []
    for index, (call_id, name, args) in enumerate(calls):
        arguments = json.dumps(args)
        events.append(ToolCallStart(index=index, id=call_id, name=name))
        events.append(ToolCallArgsDelta(index=index, args_chunk=arguments))
        events.append(ToolCallEnd(index=index))
        tool_calls.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": arguments},
        })
    events.append(Done(
        finish_reason="tool_calls",
        full_message={
            "role": "assistant", "content": prose, "tool_calls": tool_calls,
        },
    ))
    return events


def _scripted_final_round(prose: str) -> list[Event]:
    return [
        ContentDelta(text=prose),
        Done(
            finish_reason="stop",
            full_message={"role": "assistant", "content": prose},
        ),
    ]


class _ScriptedProductionBackend:
    def __init__(self, rounds: list[list[Event]]) -> None:
        self._rounds = rounds
        self.calls: list[dict] = []

    def stream(self, **kwargs):
        index = len(self.calls)
        self.calls.append(kwargs)
        if index < len(self._rounds):
            return iter(self._rounds[index])
        return iter(_scripted_final_round("(script exhausted)"))


@pytest.fixture
def isolated_streams(monkeypatch) -> ModelStreamRegistry:
    registry = ModelStreamRegistry()
    import aura.conversation.manager as manager_module

    monkeypatch.setattr(manager_module, "model_streams", registry)
    return registry


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "aura" / "work_artifact").mkdir(parents=True)
    (root / "aura" / "work_artifact" / "runner.py").write_text(
        "CAP = 10\n\n\ndef _run_item(job):\n    return job\n", encoding="utf-8"
    )
    return root


def _run_circular_transcript(repo: Path, isolated_streams):
    """Stream the pre-fix circular transcript through the real send loop."""
    runner = "aura/work_artifact/runner.py"
    backend = _ScriptedProductionBackend([
        _scripted_tool_round(
            CIRCULAR_PROSE[0], [("g1", "grep_search", {"pattern": "CAP"})]
        ),
        _scripted_tool_round(
            CIRCULAR_PROSE[1], [("r1", "read_file", {"path": runner})]
        ),
        _scripted_tool_round(
            f"{CIRCULAR_PROSE[2]}\n{CIRCULAR_PROSE[3]}\n{CIRCULAR_PROSE[4]}",
            [("r2", "read_file", {"path": runner})],
        ),
        _scripted_tool_round(
            f"{CIRCULAR_PROSE[5]}\n{CIRCULAR_PROSE[6]}\n{CIRCULAR_PROSE[7]}",
            [("r3", "read_file", {"path": runner})],
        ),
        # The circling reads were all rejected as duplicates; the loop then
        # asks again on the same ordinary catalog and the model finally acts.
        _scripted_tool_round(
            "Editing the runner.",
            [("w1", "write_file", {"path": runner, "content": "CAP = 10\n\n"
               "def _run_item(job):\n    return job\n"})],
        ),
        _scripted_final_round("Stopped."),
    ])
    isolated_streams.register(PRODUCTION_STREAM_HOOK, backend.stream)

    history = History()
    history.set_system("You are Aura's production coding agent.")
    history.append_user_text("Make the retry cap pause the job.")
    manager = ConversationManager(
        history, ToolRegistry(workspace_root=repo, mode="single")
    )
    events: list[Event] = []
    manager.send(
        on_event=events.append,
        approval_cb=lambda _r: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        model="scripted-production-model",
        thinking="off",
        hook_name=PRODUCTION_STREAM_HOOK,
        max_tool_rounds=12,
    )
    return manager, events, backend


# ── the smoke ───────────────────────────────────────────────────────────────


def test_production_turn_runs_orientation_to_receipt_once(
    session, approval_proxy
) -> None:
    _run_disciplined_turn(session, approval_proxy)
    receipt = session.finish()
    evidence = session.evidence()

    # One decision, one edit, one focused validation, one receipt.
    assert [w["path"] for w in evidence.write_results] == [
        "aura/work_artifact/runner.py"
    ]
    assert [v["command"] for v in evidence.validation_results] == [
        "python -m pytest tests/test_work_artifact_runner.py"
    ]
    assert receipt is not None
    assert receipt.ok is True
    assert receipt.status == STATUS_COMPLETED
    assert session.finish() is None
    assert session.is_active() is False


def test_disciplined_turn_reads_before_it_edits_and_validates_after(
    session, approval_proxy
) -> None:
    _run_disciplined_turn(session, approval_proxy)
    session.finish()

    order = [record["name"] for record in session.relay.tool_results]
    read_at = order.index("read_file")
    write_at = order.index("write_file")
    validate_at = order.index("run_terminal_command")

    assert order.index("grep_search") < read_at, "orientation precedes focused reads"
    assert read_at < write_at, "focused reads precede the edit"
    assert write_at < validate_at, "validation follows the edit"
    # Reads do not resume after the edit: no second round of inspection.
    assert order[write_at:].count("read_file") == 0
    assert order.count("write_file") == 1


# ── the manager, not the projection layer, is what stops the circling ───────


class TestManagerControlsRealStreamedRounds:
    """The circular transcript is streamed at the manager; it never gets out."""

    def test_the_circling_prose_never_reaches_chat(
        self, repo, isolated_streams
    ) -> None:
        _manager, events, _backend = _run_circular_transcript(repo, isolated_streams)

        chat = "".join(
            e.text for e in events if isinstance(e, ContentDelta)
        )
        for line in CIRCULAR_PROSE[:8]:
            assert line not in chat, f"pre-tool prose leaked into chat: {line!r}"
        assert chat == "Stopped.", (
            "only the final no-tool answer is chat-owned prose"
        )

    def test_the_circling_prose_is_never_stored_in_history(
        self, repo, isolated_streams
    ) -> None:
        manager, _events, _backend = _run_circular_transcript(repo, isolated_streams)

        stored = json.dumps(manager.history.messages)
        assert "Option A" not in stored
        assert "Acceptance criteria" not in stored
        assert "Reconsidering" not in stored
        assert "To summarize the plan again" not in stored
        assert "return _paused(job)" not in stored, (
            "the narrated patch must not be replayed back to the model"
        )

    def test_the_prompt_replayed_to_the_provider_stays_clean(
        self, repo, isolated_streams
    ) -> None:
        """The loop closes through the next round's prompt — prove it is open."""
        _manager, _events, backend = _run_circular_transcript(repo, isolated_streams)

        assert len(backend.calls) >= 3
        for call in backend.calls[1:]:
            replayed = json.dumps(call["messages"])
            assert "Option A" not in replayed
            assert "Acceptance criteria" not in replayed

    def test_the_repeated_read_is_rejected_before_the_first_edit(
        self, repo, isolated_streams
    ) -> None:
        _manager, events, _backend = _run_circular_transcript(repo, isolated_streams)

        results = {
            e.tool_call_id: e for e in events if isinstance(e, ToolResult)
        }
        assert results["r1"].ok is True, "the first read is real work"
        assert results["r2"].ok is False, "the identical reread adds nothing"
        assert json.loads(results["r2"].result)["reason"] == DUPLICATE_READ_REASON
        assert results["r3"].ok is False

    def test_tool_calls_still_execute_and_project(
        self, repo, isolated_streams
    ) -> None:
        """Suppressing prose must not disturb tool execution or projection."""
        _manager, events, _backend = _run_circular_transcript(repo, isolated_streams)

        started = [e.name for e in events if isinstance(e, ToolCallStart)]
        assert started == [
            "grep_search", "read_file", "read_file", "read_file", "write_file",
        ]
        assert len([e for e in events if isinstance(e, ToolResult)]) == 5
