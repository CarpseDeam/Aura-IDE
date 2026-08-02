"""Post-write context refresh ownership for production SINGLE mode.

After a successful write, production ``SINGLE`` refreshes the Tier 1 system
prompt / repo map *silently*: no ``Planner stale-read invalidation`` message,
no dependency notice, and no new user-turn boundary — the turn keeps exactly
one real user message. The legacy ``PLANNER`` path keeps its historical
notices.

What is asserted here:

* a SINGLE write round recomposes the system prompt without adding any user
  message or Planner text;
* the stale-file guard still sees the written paths (its note_stale_paths
  input is the combined post-write file list);
* the legacy PLANNER path still appends the stale-read notice;
* applied write paths are collected from write-tool results so the silent
  refresh actually fires.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aura.client import ToolResult
from aura.context_gearbox.models import RuntimeRole
from aura.context_gearbox.runtime import (
    PLANNER_SYSTEM_PROMPT,
    SINGLE_SYSTEM_PROMPT,
)
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import (
    ToolRoundRunner,
    _applied_write_paths,
    _combined_post_write_files,
)
from aura.conversation.planner_refresh import PlannerRefreshState
from aura.conversation.pre_edit_loop_guard import PreEditLoopGuard
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry

STALE = "stale system prompt"


def tool_call(call_id: str, name: str, args: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }


def _configured_refresh(tmp_path: Path, role: RuntimeRole) -> PlannerRefreshState:
    base = {
        RuntimeRole.SINGLE: SINGLE_SYSTEM_PROMPT,
        RuntimeRole.PLANNER: PLANNER_SYSTEM_PROMPT,
    }[role]
    refresh = PlannerRefreshState()
    refresh.configure(
        base_prompt=base,
        workspace_root=tmp_path,
        role=role,
        task_kind="bugfix",
        content="fix the cap",
        target_files=("app.py",),
    )
    return refresh


def _history_with_turn() -> History:
    history = History()
    history.set_system(STALE)
    history.append_user_text("Update app.py so the job pauses.")
    history.append_assistant(
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [tool_call("c1", "write_file", {"path": "app.py"})],
        }
    )
    history.append_tool_result(
        "c1", json.dumps({"ok": True, "applied": True, "path": "app.py"})
    )
    return history


def _history_blob(history: History) -> str:
    return json.dumps(history.messages) + (history.system_prompt or "")


# ── production SINGLE: silent refresh ───────────────────────────────────────


def test_single_post_write_refresh_appends_no_user_message(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    refresh = _configured_refresh(tmp_path, RuntimeRole.SINGLE)
    history = _history_with_turn()

    refresh.handle_post_write_notices(history, ["app.py"])

    users = [m for m in history.messages if m.get("role") == "user"]
    assert len(users) == 1, "a post-write refresh must not add a user-turn boundary"
    assert users[0]["content"] == "Update app.py so the job pauses."


def test_single_post_write_refresh_recomposes_system_prompt(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    refresh = _configured_refresh(tmp_path, RuntimeRole.SINGLE)
    history = _history_with_turn()

    refresh.handle_post_write_notices(history, ["app.py"])

    assert history.system_prompt != STALE, "the system prompt must be recomposed"
    assert "Core kernel:" in history.system_prompt
    assert "### Target files (manifest)" in history.system_prompt


def test_single_post_write_refresh_contains_no_planner_text(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    refresh = _configured_refresh(tmp_path, RuntimeRole.SINGLE)
    history = _history_with_turn()

    refresh.handle_post_write_notices(history, ["app.py"])

    blob = _history_blob(history)
    assert "Planner stale-read invalidation" not in blob
    assert "Planner dependency context" not in blob


def test_single_write_round_refreshes_context_silently(tmp_path: Path) -> None:
    """The production regression: one real SINGLE write through the tool round
    recomposes Tier 1 context without adding a user message or Planner text."""
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    history = History()
    history.set_system(STALE)
    history.append_user_text("Update app.py so the job pauses.")
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    runner = ToolRoundRunner(
        history=history,
        tools=tools,
        tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        planner_refresh=_configured_refresh(tmp_path, RuntimeRole.SINGLE),
    )
    state = _SendState(mode="single", research_policy=None)
    calls = [tool_call("c1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})]
    history.append_assistant(
        {"role": "assistant", "content": "", "tool_calls": calls}
    )
    events: list = []
    runner.run(
        tool_calls=calls,
        state=state,
        on_event=events.append,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda _cb: None,
    )

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    users = [m for m in history.messages if m.get("role") == "user"]
    assert len(users) == 1
    assert users[0]["content"] == "Update app.py so the job pauses."
    assert history.system_prompt != STALE
    assert "Core kernel:" in history.system_prompt
    blob = _history_blob(history)
    assert "Planner stale-read invalidation" not in blob
    assert "Planner dependency context" not in blob


def test_note_stale_paths_clears_read_fingerprints() -> None:
    """The stale-file recovery primitive: after a write the guard forgets reads
    of the written paths, so a post-write reread is not a duplicate-read loop."""
    guard = PreEditLoopGuard()
    guard.record("read_file", {"path": "app.py"})
    assert guard.check("read_file", {"path": "app.py"}) is not None

    guard.note_stale_paths(["app.py"])

    assert guard.check("read_file", {"path": "app.py"}) is None


def test_single_write_round_preserves_guard_state(tmp_path: Path) -> None:
    """The round feeds the written paths to the guard without breaking it: the
    guard still records, still applies writes, and a post-write reread stays
    allowed."""
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    history = History()
    history.set_system(STALE)
    history.append_user_text("Update app.py so the job pauses.")
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    runner = ToolRoundRunner(
        history=history,
        tools=tools,
        tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        planner_refresh=_configured_refresh(tmp_path, RuntimeRole.SINGLE),
    )
    state = _SendState(mode="single", research_policy=None)
    guard = state.pre_edit_guard
    assert guard is not None

    calls = [tool_call("c1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})]
    history.append_assistant(
        {"role": "assistant", "content": "", "tool_calls": calls}
    )
    runner.run(
        tool_calls=calls,
        state=state,
        on_event=lambda _e: None,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda _cb: None,
    )

    assert guard.write_applied
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    # A reread after the write is fresh evidence, not a loop.
    assert guard.check("read_file", {"path": "app.py"}) is None


# ── legacy PLANNER: notices preserved ───────────────────────────────────────


def test_legacy_planner_post_write_keeps_stale_read_notice(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    refresh = _configured_refresh(tmp_path, RuntimeRole.PLANNER)
    history = _history_with_turn()

    refresh.handle_post_write_notices(history, ["app.py"])

    users = [m for m in history.messages if m.get("role") == "user"]
    assert len(users) == 2, "legacy Planner keeps the stale-read user notice"
    assert users[-1]["content"].startswith("Planner stale-read invalidation:")
    assert "aura_internal" not in users[-1]
    assert history.system_prompt != STALE


# ── applied-write path collection ───────────────────────────────────────────


def test_applied_write_paths_collects_applied_writes_only() -> None:
    results = {
        "w1": {
            "result_payload": json.dumps({"ok": True, "applied": True, "path": "app.py"})
        },
        "w2": {
            "result_payload": json.dumps(
                {
                    "ok": False,
                    "applied": False,
                    "write_outcome": "not_applied_edit_mechanics_blocked",
                }
            )
        },
        "r1": {"result_payload": json.dumps({"ok": True, "path": "read.py"})},
    }
    tasks = [
        {"id": "w1", "name": "write_file", "args": {"path": "app.py"}},
        {"id": "w2", "name": "write_file", "args": {"path": "other.py"}},
        {"id": "r1", "name": "read_file", "args": {"path": "read.py"}},
    ]

    assert _applied_write_paths(tasks, results) == ["app.py"]


def test_applied_write_paths_includes_successful_deletes() -> None:
    results = {
        "d1": {
            "result_payload": json.dumps(
                {"ok": True, "applied": True, "deleted": True, "path": "old.py"}
            )
        }
    }
    tasks = [{"id": "d1", "name": "delete_file", "args": {"path": "old.py"}}]

    assert _applied_write_paths(tasks, results) == ["old.py"]


def test_combined_post_write_files_dedups_and_normalizes() -> None:
    combined = _combined_post_write_files(
        ["app.py", "a\\b.py", ""], ["app.py", "a/b.py", "c.py"]
    )

    assert combined == ["app.py", "a/b.py", "c.py"]


# ── fail-closed applied detection ──────────────────────────────────────────


def test_applied_write_paths_rejects_malformed_payload() -> None:
    """A result payload that is not valid JSON proves nothing and counts as
    nothing — the refresh must not fire on a write whose outcome is unknown."""
    results = {
        "w1": {"result_payload": "{this is not valid json", "event": None},
    }
    tasks = [{"id": "w1", "name": "write_file", "args": {"path": "app.py"}}]

    assert _applied_write_paths(tasks, results) == []


def test_applied_write_paths_rejects_missing_applied() -> None:
    """An ``ok: true`` payload without an explicit ``applied`` field is
    ambiguous, and an ambiguous write is not an applied write."""
    results = {
        "w1": {"result_payload": json.dumps({"ok": True, "path": "app.py"})},
    }
    tasks = [{"id": "w1", "name": "write_file", "args": {"path": "app.py"}}]

    assert _applied_write_paths(tasks, results) == []


def test_applied_write_paths_rejects_ok_false_with_applied_true() -> None:
    """The enclosing tool result's success outvotes the payload's own claim: an
    ``applied: true`` field cannot make a failed write count as landed."""
    results = {
        "w1": {
            "result_payload": json.dumps({"ok": True, "applied": True, "path": "app.py"}),
            "event": ToolResult(
                tool_call_id="w1", name="write_file", ok=False, result="boom"
            ),
        },
    }
    tasks = [{"id": "w1", "name": "write_file", "args": {"path": "app.py"}}]

    assert _applied_write_paths(tasks, results) == []


def test_applied_write_paths_explicit_write_success_with_confirming_result() -> None:
    """A successful write with an explicit ``applied: true`` and a confirming
    tool result is an applied write."""
    results = {
        "w1": {
            "result_payload": json.dumps({"ok": True, "applied": True, "path": "app.py"}),
            "event": ToolResult(
                tool_call_id="w1", name="write_file", ok=True, result="ok"
            ),
        },
    }
    tasks = [{"id": "w1", "name": "write_file", "args": {"path": "app.py"}}]

    assert _applied_write_paths(tasks, results) == ["app.py"]


def test_applied_write_paths_explicit_delete_success_with_confirming_result() -> None:
    """A successful delete carries ``applied: true`` and ``deleted: true``; the
    confirming result keeps it an applied write."""
    results = {
        "d1": {
            "result_payload": json.dumps(
                {"ok": True, "applied": True, "deleted": True, "path": "old.py"}
            ),
            "event": ToolResult(
                tool_call_id="d1", name="delete_file", ok=True, result="ok"
            ),
        },
    }
    tasks = [{"id": "d1", "name": "delete_file", "args": {"path": "old.py"}}]

    assert _applied_write_paths(tasks, results) == ["old.py"]


def test_rejected_write_causes_no_refresh_and_no_stale_invalidation(tmp_path: Path) -> None:
    """A write the user rejects lands nothing: the system prompt stays stale and
    the guard keeps its read fingerprints for the untouched path."""
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    history = History()
    history.set_system(STALE)
    history.append_user_text("Update app.py so the job pauses.")
    tools = ToolRegistry(workspace_root=tmp_path, mode="single")
    runner = ToolRoundRunner(
        history=history,
        tools=tools,
        tool_runner=ToolRunner(history=history, workspace_root=tmp_path),
        planner_refresh=_configured_refresh(tmp_path, RuntimeRole.SINGLE),
    )
    state = _SendState(mode="single", research_policy=None)
    guard = state.pre_edit_guard
    assert guard is not None
    guard.record("read_file", {"path": "app.py"})

    calls = [tool_call("c1", "write_file", {"path": "app.py", "content": "VALUE = 2\n"})]
    history.append_assistant(
        {"role": "assistant", "content": "", "tool_calls": calls}
    )
    runner.run(
        tool_calls=calls,
        state=state,
        on_event=lambda _e: None,
        approval_cb=lambda _req: ApprovalDecision(action="reject"),
        cancel_event=threading.Event(),
        dispatch_cb=None,
        cleanup_cancelled=lambda _cb: None,
    )

    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    # No refresh: the system prompt is still the stale sentinel.
    assert history.system_prompt == STALE
    # No stale-path invalidation: the path's read fingerprint was not cleared,
    # so once failure grace expires the reread is still a duplicate.
    assert any("app.py" in fingerprint for fingerprint in guard.seen_reads)
