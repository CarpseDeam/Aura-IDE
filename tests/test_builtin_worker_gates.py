from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from aura.bridge.dispatch import _DispatchProxy
from aura.conversation.history import History
from aura.conversation.manager_send_state import _SendState
from aura.conversation.manager_tool_round import ToolRoundRunner
from aura.conversation.tools._types import ToolExecResult
from aura.conversation.tools.registry import ToolRegistry
from aura.events import EventBus, WORKER_PRE_TOOL_GATE_DECIDED
from aura.lifecycle import LifecycleHooks
from aura.lifecycle.builtin_worker_gates import register_builtin_worker_gates


def _state() -> _SendState:
    return _SendState(mode="worker", research_policy=None)


def _runner(
    workspace_root: Path,
    lifecycle: LifecycleHooks,
    *,
    event_bus: EventBus | None = None,
) -> tuple[ToolRoundRunner, MagicMock]:
    tools = MagicMock(spec=ToolRegistry)
    tools.workspace_root = str(workspace_root)
    tools.execute.return_value = ToolExecResult(
        ok=True,
        payload={"ok": True, "value": "executed"},
    )
    loop_detector = MagicMock()
    loop_detector.observe.return_value = MagicMock(
        content='{"ok": true, "value": "executed"}',
        info=None,
    )
    return (
        ToolRoundRunner(
            history=History(),
            tools=tools,  # type: ignore[arg-type]
            tool_runner=MagicMock(),
            loop_detector=loop_detector,
            planner_refresh=MagicMock(),
            lifecycle=lifecycle,
            event_bus=event_bus,
        ),
        tools,
    )


def _builtin_runner(workspace_root: Path) -> tuple[ToolRoundRunner, _SendState, MagicMock]:
    lifecycle = LifecycleHooks()
    register_builtin_worker_gates(lifecycle)
    runner, tools = _runner(workspace_root, lifecycle)
    return runner, _state(), tools


def _run_gate(
    runner: ToolRoundRunner,
    state: _SendState,
    *,
    name: str,
    args: dict,
) -> dict | None:
    return runner._run_worker_pre_tool_gate(
        tool_call_id="tc_gate",
        name=name,
        args=args,
        state=state,
    )


def _blocked_payload(result: dict | None) -> dict:
    assert result is not None
    assert result["blocked"] is True
    return result["blocked_payload"]


def test_dispatch_proxy_registers_builtin_worker_gates(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("VALUE = 1\n", encoding="utf-8")
    proxy = _DispatchProxy(
        parent_widget=None,
        registry_factory=MagicMock(),
        approval_proxy=MagicMock(),
        workspace_root=tmp_path,
    )
    runner, _tools = _runner(tmp_path, proxy.lifecycle_hooks())
    payload = _blocked_payload(
        _run_gate(
            runner,
            _state(),
            name="write_file",
            args={"path": "app.py", "content": "VALUE = 2\n"},
        )
    )
    assert payload["failure_class"] == "worker_existing_file_not_read"


def test_existing_patch_file_without_read_or_context_evidence_blocks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)

    payload = _blocked_payload(
        _run_gate(
            runner,
            state,
            name="patch_file",
            args={"path": "app.py", "edits": [{"old": "1", "new": "2"}]},
        )
    )

    assert payload["failure_class"] == "worker_existing_file_not_read"


@pytest.mark.parametrize("tool_name", ["write_file", "delete_file"])
def test_existing_write_tools_without_read_or_context_evidence_block(
    tmp_path: Path,
    tool_name: str,
) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    args = {"path": "app.py", "content": "VALUE = 2\n"}

    payload = _blocked_payload(_run_gate(runner, state, name=tool_name, args=args))

    assert payload["failure_class"] == "worker_existing_file_not_read"
    assert payload["suggested_next_tool"] == "read_file"


def test_new_file_write_is_allowed(tmp_path: Path) -> None:
    runner, state, _tools = _builtin_runner(tmp_path)

    result = _run_gate(
        runner,
        state,
        name="write_file",
        args={"path": "new.py", "content": "VALUE = 1\n"},
    )

    assert result is None


def test_existing_write_with_worker_file_state_evidence_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    state.worker_file_state["app.py"] = {
        "content_hash": "hash1",
        "file_size": 10,
        "fresh_for_patch": True,
        "last_read_tool": "read_file",
    }

    result = _run_gate(
        runner,
        state,
        name="write_file",
        args={"path": "app.py", "content": "VALUE = 2\n"},
    )

    assert result is None


def test_existing_write_with_loaded_target_file_evidence_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    state.loaded_target_files = ["app.py"]

    result = _run_gate(
        runner,
        state,
        name="write_file",
        args={"path": "app.py", "content": "VALUE = 2\n"},
    )

    assert result is None


def test_patch_with_read_evidence_missing_expected_hash_blocks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    state.worker_file_state["app.py"] = {
        "content_hash": "hash1",
        "file_size": 10,
        "fresh_for_patch": True,
        "last_read_tool": "read_file",
    }

    payload = _blocked_payload(
        _run_gate(
            runner,
            state,
            name="patch_file",
            args={"path": "app.py", "edits": [{"old": "1", "new": "2"}]},
        )
    )

    assert payload["failure_class"] == "patch_file_missing_expected_hash"
    assert payload["suggested_next_tool"] == "read_file"


def test_patch_with_stale_expected_hash_blocks(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    state.worker_file_state["app.py"] = {
        "content_hash": "fresh_hash",
        "file_size": 10,
        "fresh_for_patch": True,
        "last_read_tool": "read_file",
    }

    payload = _blocked_payload(
        _run_gate(
            runner,
            state,
            name="patch_file",
            args={
                "path": "app.py",
                "expected_file_hash": "stale_hash",
                "edits": [{"old": "1", "new": "2"}],
            },
        )
    )

    assert payload["failure_class"] == "patch_file_hash_mismatch"
    assert payload["latest_read_content_hash"] == "fresh_hash"


def test_patch_with_matching_expected_hash_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    runner, state, _tools = _builtin_runner(tmp_path)
    state.worker_file_state["app.py"] = {
        "content_hash": "fresh_hash",
        "file_size": 10,
        "fresh_for_patch": True,
        "last_read_tool": "read_file",
    }

    result = _run_gate(
        runner,
        state,
        name="patch_file",
        args={
            "path": "app.py",
            "expected_file_hash": "fresh_hash",
            "edits": [{"old": "1", "new": "2"}],
        },
    )

    assert result is None


def test_planner_mode_skips_worker_gates(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    lifecycle = LifecycleHooks()
    register_builtin_worker_gates(lifecycle)
    runner, tools = _runner(tmp_path, lifecycle)

    runner._process_task(
        task={
            "id": "tc_01",
            "name": "write_file",
            "args": {"path": "app.py", "content": "VALUE = 2\n"},
        },
        state=_SendState(mode="planner", research_policy=None),
        on_event=MagicMock(),
        approval_cb=MagicMock(),
        cancel_event=MagicMock(),
        dispatch_cb=None,
        workflow_state_cb=None,
        explicit_validation_commands=None,
        declared_run_command=None,
    )

    tools.execute.assert_called_once()


def test_workspace_escaping_write_path_blocks(tmp_path: Path) -> None:
    runner, state, _tools = _builtin_runner(tmp_path)

    payload = _blocked_payload(
        _run_gate(
            runner,
            state,
            name="write_file",
            args={"path": "../outside.py", "content": "VALUE = 1\n"},
        )
    )

    assert payload["failure_class"] == "worker_workspace_path_escape"
    assert payload["recoverable"] is False


def test_pre_tool_gate_decision_event_emits_builtin_block_status(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    lifecycle = LifecycleHooks()
    register_builtin_worker_gates(lifecycle)
    bus = EventBus()
    events = []
    bus.subscribe(WORKER_PRE_TOOL_GATE_DECIDED, events.append)
    runner, tools = _runner(tmp_path, lifecycle, event_bus=bus)

    result = runner._process_task(
        task={
            "id": "tc_01",
            "name": "write_file",
            "args": {"path": "app.py", "content": "VALUE = 2\n"},
        },
        state=_state(),
        on_event=MagicMock(),
        approval_cb=MagicMock(),
        cancel_event=MagicMock(),
        dispatch_cb=None,
        workflow_state_cb=None,
        explicit_validation_commands=None,
        declared_run_command=None,
    )

    tools.execute.assert_not_called()
    payload = json.loads(result["result_payload"])
    assert payload["failure_class"] == "worker_existing_file_not_read"
    assert events
    assert events[0].payload["allowed"] is False
    assert events[0].payload["blocked"] is True
    assert events[0].payload["reason"] == "worker_existing_file_not_read"


def test_old_worker_patch_state_policy_owner_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("aura.conversation.worker_patch_state_policy")
