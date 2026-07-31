"""Smoke: one production turn runs straight through without circling.

The consolidated production contract (``aura/roles/bundled/single.md``) asks for
one pass — orientation, focused reads, one decision, edit, validation, receipt —
and forbids reopening a settled decision or restating the implementation before
editing.  This smoke drives a disciplined transcript through the real production
execution machinery and asserts that shape, then proves the circularity detector
actually bites by running the pre-fix behaviour through it.
"""

from __future__ import annotations

import difflib
import json
import re

import pytest

from aura.bridge.production_execution import ProductionExecutionSession
from aura.bridge.production_receipt import STATUS_COMPLETED
from aura.client import (
    ContentDelta,
    Done,
    ReasoningDelta,
    ToolCallStart,
    ToolResult,
)
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


# ── circularity detector ────────────────────────────────────────────────────

_DELIBERATION = re.compile(
    r"\b(option [abc]\b|let me think|reconsider|to summari[sz]e|"
    r"acceptance criteria|double-check the decision)",
    re.IGNORECASE,
)
_CODE_FENCE = re.compile(r"```")


def circularity_report(prose: list[str], first_edit_index: int) -> dict[str, list[str]]:
    """Return the circling a transcript exhibits, as {finding: evidence}.

    An empty report means the turn moved forward: no restated decision, no
    proposed patch before the first edit, no deliberation narration.
    """
    findings: dict[str, list[str]] = {}

    restated = [
        f"{a[:48]}... ~ {b[:48]}..."
        for i, a in enumerate(prose)
        for b in prose[i + 1:]
        if difflib.SequenceMatcher(None, a, b).ratio() > 0.55
    ]
    if restated:
        findings["restated_decision"] = restated

    narrated = [line[:64] for line in prose if _DELIBERATION.search(line)]
    if narrated:
        findings["narrated_deliberation"] = narrated

    patch_first = [
        line[:64]
        for line in prose[:first_edit_index]
        if _CODE_FENCE.search(line)
    ]
    if patch_first:
        findings["patch_before_edit"] = patch_first

    return findings


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


def test_disciplined_transcript_shows_no_circling() -> None:
    first_edit = DISCIPLINED_PROSE.index("Editing `aura/work_artifact/runner.py`.")
    assert circularity_report(DISCIPLINED_PROSE, first_edit) == {}


def test_detector_catches_the_pre_fix_circular_transcript() -> None:
    """The detector is not vacuous: the old behaviour trips every finding."""
    first_edit = CIRCULAR_PROSE.index("Editing `aura/work_artifact/runner.py`.")
    report = circularity_report(CIRCULAR_PROSE, first_edit)

    assert set(report) == {
        "restated_decision",
        "narrated_deliberation",
        "patch_before_edit",
    }
