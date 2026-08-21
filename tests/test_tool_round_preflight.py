"""Effect-aware batch preflight for ``ToolRoundRunner``.

Phase 2C.1: a schema-invalid ``update_task_checklist`` call must not veto
valid ``read_file`` siblings in the same round — the checklist is a passive
display-only fact, not an execution gate. But anything that could touch the
workspace or run a command (``apply_patch``, ``shell``, any other
MUTATION/COMMAND-effect tool) keeps the strict all-or-nothing contract:
one bad sibling still vetoes the whole batch, and nothing partially
executes. These tests drive the real ``ToolRoundRunner``/``ToolRegistry``
pair against a temp workspace, not a fake.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from aura.client import Event, ToolResult
from aura.conversation.history import History
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.tools._types import ApprovalDecision
from aura.conversation.tools.registry import ToolRegistry


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}


def _make_runner(tmp_path: Path) -> tuple[ToolRoundRunner, ToolRegistry, History]:
    history = History()
    tools = ToolRegistry(workspace_root=tmp_path)
    tool_runner = ToolRunner(history, tmp_path)
    runner = ToolRoundRunner(history=history, tools=tools, tool_runner=tool_runner)
    return runner, tools, history


def _run(runner: ToolRoundRunner, tool_calls: list[dict]) -> list[Event]:
    events: list[Event] = []
    runner.run(
        tool_calls=tool_calls,
        on_event=events.append,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda _cb: None,
    )
    return events


def _tool_results(events: list[Event]) -> list[ToolResult]:
    return [e for e in events if isinstance(e, ToolResult)]


def _invalid_checklist_call(call_id: str) -> dict:
    # Missing required "status" on the item makes this schema-invalid.
    return _tool_call(
        call_id,
        "update_task_checklist",
        {"items": [{"id": "step-1", "text": "Do the thing"}]},
    )


# ── observation/bookkeeping-only batches isolate a schema failure ──────────


def test_invalid_checklist_call_does_not_block_a_valid_sibling_read(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_bytes(b"x = 1\n")
    runner, _tools, history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _tool_call("call-1", "read_file", {"path": "a.py"}),
            _invalid_checklist_call("call-2"),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert set(results) == {"call-1", "call-2"}
    assert results["call-1"].ok is True
    assert results["call-2"].ok is False
    assert results["call-2"].extras.get("failure_class") == "tool_call_schema_violation"
    # The valid sibling's history entry proves it actually ran, not just that
    # a ToolResult event with ok=True was synthesized.
    read_payload = json.loads(results["call-1"].result)
    assert read_payload["ok"] is True
    assert read_payload["content"] == "x = 1\n"


def test_exactly_one_result_per_call_in_original_order(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _invalid_checklist_call("call-1"),
            _tool_call("call-2", "read_file", {"path": "a.py"}),
            _tool_call("call-3", "read_file", {"path": "b.py"}),
        ],
    )

    results = _tool_results(events)
    assert [r.tool_call_id for r in results] == ["call-1", "call-2", "call-3"]
    assert [r.ok for r in results] == [False, True, True]


def test_multiple_invalid_calls_in_an_isolatable_batch_each_get_their_own_failure(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _invalid_checklist_call("call-1"),
            _tool_call("call-2", "read_file", {"path": "a.py"}),
            _invalid_checklist_call("call-3"),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is True
    assert results["call-3"].ok is False


# ── batches with a mutation or command stay strictly all-or-nothing ────────


def test_schema_failure_alongside_apply_patch_blocks_the_whole_batch(tmp_path: Path) -> None:
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _invalid_checklist_call("call-1"),
            _tool_call(
                "call-2",
                "apply_patch",
                {"operation": "create", "path": "new.py", "content": "x = 1\n"},
            ),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is False
    payload = json.loads(results["call-2"].result)
    assert payload.get("batch_rejected") is True
    # Nothing partially executed: the file was never created.
    assert not (tmp_path / "new.py").exists()


def test_schema_failure_alongside_shell_blocks_the_whole_batch(tmp_path: Path) -> None:
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _invalid_checklist_call("call-1"),
            _tool_call("call-2", "shell", {"command": "echo hi"}),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is False
    payload = json.loads(results["call-2"].result)
    assert payload.get("batch_rejected") is True


# ── malformed / unexposed calls always stay all-or-nothing ─────────────────


def test_unexposed_tool_name_blocks_the_whole_batch_even_when_siblings_are_observations(
    tmp_path: Path,
) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    events = _run(
        runner,
        [
            _tool_call("call-1", "read_file", {"path": "a.py"}),
            _tool_call("call-2", "totally_unknown_tool", {}),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is False
    payload1 = json.loads(results["call-1"].result)
    assert payload1.get("batch_rejected") is True


# ── Phase 3A: the exact request catalog is the sole callable authority ─────


def test_withheld_observation_is_rejected_and_its_handler_never_runs(
    tmp_path: Path, monkeypatch,
) -> None:
    """``git_log`` is a registered OBSERVATION handler that production never
    advertises. A call to it must be rejected as ``tool_call_not_exposed``
    and its handler must never execute, even though the same handler stays
    reachable via direct internal ``ToolRegistry.execute()`` calls."""
    import aura.conversation.tools.registry as registry_module

    called = False

    def _spy(owner, args, approval_cb, reject_all):
        nonlocal called
        called = True
        raise AssertionError("withheld handler must not execute")

    monkeypatch.setitem(registry_module.TOOL_HANDLERS, "git_log", _spy)

    runner, tools, _history = _make_runner(tmp_path)
    assert "git_log" not in {
        d["function"]["name"] for d in tools.tool_defs()
    }

    events = _run(runner, [_tool_call("call-1", "git_log", {})])

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-1"].extras.get("failure_class") == "tool_call_not_exposed"
    assert called is False


def test_batch_with_one_advertised_and_one_withheld_call_executes_neither(
    tmp_path: Path,
) -> None:
    """A registered-but-unadvertised call vetoes the whole batch: the valid,
    advertised sibling must not execute ahead of it."""
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    runner, tools, _history = _make_runner(tmp_path)
    assert "read_file" in {d["function"]["name"] for d in tools.tool_defs()}
    assert "git_log" not in {d["function"]["name"] for d in tools.tool_defs()}

    events = _run(
        runner,
        [
            _tool_call("call-1", "read_file", {"path": "a.py"}),
            _tool_call("call-2", "git_log", {}),
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is False
    payload1 = json.loads(results["call-1"].result)
    assert payload1.get("batch_rejected") is True
    payload2 = json.loads(results["call-2"].result)
    assert payload2.get("failure_class") == "tool_call_not_exposed"


def test_optional_tool_succeeds_when_present_in_the_supplied_request_catalog(
    tmp_path: Path,
) -> None:
    """A conditional tool (here, an extra def passed as this request's exact
    ``tool_defs``) is callable precisely because it was included in the
    request that produced the call — nothing more, nothing less."""
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    runner, tools, _history = _make_runner(tmp_path)
    base_defs = tools.tool_defs()
    assert "get_workspace_snapshot" not in {d["function"]["name"] for d in base_defs}

    from aura.conversation.tools.schemas import WORKSPACE_SNAPSHOT_TOOL_DEF

    extended_defs = base_defs + [dict(WORKSPACE_SNAPSHOT_TOOL_DEF)]

    events: list[Event] = []
    runner.run(
        tool_calls=[_tool_call("call-1", "get_workspace_snapshot", {})],
        on_event=events.append,
        approval_cb=lambda _req: ApprovalDecision(action="approve"),
        cancel_event=threading.Event(),
        cleanup_cancelled=lambda _cb: None,
        tool_defs=extended_defs,
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].extras.get("failure_class") != "tool_call_not_exposed"
    assert results["call-1"].ok is True


def test_malformed_call_blocks_the_whole_batch(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    runner, _tools, _history = _make_runner(tmp_path)

    malformed = {"id": "call-2", "function": {"name": "read_file", "arguments": "not json"}}
    events = _run(
        runner,
        [
            _tool_call("call-1", "read_file", {"path": "a.py"}),
            malformed,
        ],
    )

    results = {r.tool_call_id: r for r in _tool_results(events)}
    assert results["call-1"].ok is False
    assert results["call-2"].ok is False
