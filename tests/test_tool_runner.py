from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from aura.client.events import (
    TerminalOutput,
    ToolCallStart,
    ToolResult,
    WorkerDispatchRequested,
)
from aura.conversation.dispatch import WorkerDispatchResult
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.tool_runner import ToolRunner
from aura.conversation.verification_progress import VerificationProgressTracker
from aura.sandbox import SandboxResult


def _make_runner(tmp_path: Path) -> ToolRunner:
    return ToolRunner(History(), tmp_path, LoopDetector(), VerificationProgressTracker())


def test_terminal_command_default_timeout_is_300_seconds(tmp_path: Path):
    runner = _make_runner(tmp_path)
    events = []
    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=True,
        stdout="ok\n",
        stderr="",
        exit_code=0,
    )

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-1",
            args={"command": "pytest tests/test_sandbox.py -q"},
            on_event=events.append,
            cancel_event=threading.Event(),
            mode="single",
        )

    sandbox.run_terminal_command.assert_called_once()
    assert sandbox.run_terminal_command.call_args.kwargs["timeout"] == 300
    assert any(isinstance(ev, ToolCallStart) for ev in events)
    assert any(isinstance(ev, ToolResult) for ev in events)


def test_py_compile_default_timeout_is_30_seconds(tmp_path: Path):
    runner = _make_runner(tmp_path)
    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=True,
        stdout="compiled\n",
        stderr="",
        exit_code=0,
    )

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-2",
            args={"command": "python -m py_compile aura/sandbox.py"},
            on_event=lambda ev: None,
            cancel_event=threading.Event(),
            mode="worker",
        )

    assert sandbox.run_terminal_command.call_args.kwargs["timeout"] == 30


def test_terminal_timeout_is_clamped_to_300_seconds(tmp_path: Path):
    runner = _make_runner(tmp_path)
    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=True,
        stdout="ok\n",
        stderr="",
        exit_code=0,
    )

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-3",
            args={"command": "pytest -q", "timeout": 9999},
            on_event=lambda ev: None,
            cancel_event=threading.Event(),
            mode="single",
        )

    assert sandbox.run_terminal_command.call_args.kwargs["timeout"] == 300


def test_terminal_command_runs_from_workspace_relative_cwd(tmp_path: Path):
    runner = _make_runner(tmp_path)
    (tmp_path / "companion-web").mkdir()
    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=True,
        stdout="built\n",
        stderr="",
        exit_code=0,
    )

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-cwd",
            args={"command": "npm run build", "cwd": "companion-web"},
            on_event=lambda ev: None,
            cancel_event=threading.Event(),
            mode="single",
        )

    call_kwargs = sandbox.run_terminal_command.call_args.kwargs
    assert call_kwargs["command"] == "npm run build"
    assert call_kwargs["working_directory"] == (tmp_path / "companion-web").resolve()


def test_terminal_command_normalizes_cd_wrapper_to_cwd(tmp_path: Path):
    runner = _make_runner(tmp_path)
    (tmp_path / "companion-web").mkdir()
    events = []
    sandbox = MagicMock()
    sandbox.run_terminal_command.return_value = SandboxResult(
        ok=True,
        stdout="built\n",
        stderr="",
        exit_code=0,
    )

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-cd",
            args={"command": "cd companion-web && npm run build"},
            on_event=events.append,
            cancel_event=threading.Event(),
            mode="single",
        )

    call_kwargs = sandbox.run_terminal_command.call_args.kwargs
    assert call_kwargs["command"] == "npm run build"
    assert call_kwargs["working_directory"] == (tmp_path / "companion-web").resolve()
    payload = json.loads([ev for ev in events if isinstance(ev, ToolResult)][-1].result)
    assert payload["cwd"] == "companion-web"
    assert payload["validation_command_normalized"] is True


def test_terminal_command_rejects_cwd_outside_workspace(tmp_path: Path):
    runner = _make_runner(tmp_path)
    events = []
    sandbox = MagicMock()

    with patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox):
        runner.handle_terminal_command(
            tool_call_id="term-escape",
            args={"command": "npm run build", "cwd": "../outside"},
            on_event=events.append,
            cancel_event=threading.Event(),
            mode="worker",
        )

    sandbox.run_terminal_command.assert_not_called()
    payload = json.loads([ev for ev in events if isinstance(ev, ToolResult)][-1].result)
    assert payload["ok"] is False
    assert payload["failure_class"] == "validation_command_unrunnable"


def test_terminal_output_events_stream_chunks(tmp_path: Path):
    runner = _make_runner(tmp_path)
    events = []

    def run_terminal_command(**kwargs):
        kwargs["on_output"]("first line\n")
        kwargs["on_output"]("[still running: 5s / timeout 45s]\n")
        return SandboxResult(
            ok=True,
            stdout="first line\n[still running: 5s / timeout 45s]\n",
            stderr="",
            exit_code=0,
        )

    sandbox = MagicMock()
    sandbox.run_terminal_command.side_effect = run_terminal_command

    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        runner.handle_terminal_command(
            tool_call_id="term-4",
            args={"command": "python -m py_compile aura/sandbox.py"},
            on_event=events.append,
            cancel_event=threading.Event(),
            mode="single",
        )

    terminal_output_events = [ev for ev in events if isinstance(ev, TerminalOutput)]
    assert [ev.text for ev in terminal_output_events] == [
        "first line\n",
        "[still running: 5s / timeout 45s]\n",
    ]

    tool_results = [ev for ev in events if isinstance(ev, ToolResult)]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0].result)
    assert payload["ok"] is True


def test_worker_validation_stall_trips_on_repeated_failure_fingerprint(tmp_path: Path):
    runner = _make_runner(tmp_path)
    sandbox = MagicMock()
    sandbox.run_terminal_command.side_effect = [
        SandboxResult(
            ok=False,
            stdout="FAILED tests/test_example.py::test_widget - AssertionError\n1 failed in 0.11s\n",
            stderr="",
            exit_code=1,
        ),
        SandboxResult(
            ok=False,
            stdout="FAILED tests/test_example.py::test_widget - AssertionError\n1 failed in 0.22s\n",
            stderr="",
            exit_code=1,
        ),
        SandboxResult(
            ok=False,
            stdout="FAILED tests/test_example.py::test_widget - AssertionError\n1 failed in 0.33s\n",
            stderr="",
            exit_code=1,
        ),
    ]

    loop_infos = []
    with (
        patch("aura.conversation.tool_runner.SandboxExecutor", return_value=sandbox),
        patch("aura.conversation.tool_runner.load_settings") as load_settings,
    ):
        load_settings.return_value.sandbox_mode = "host"
        for index in range(3):
            loop_infos.append(
                runner.handle_terminal_command(
                    tool_call_id=f"term-{index}",
                    args={"command": "pytest tests/test_example.py -q"},
                    on_event=lambda ev: None,
                    cancel_event=threading.Event(),
                    mode="worker",
                )
            )

    assert not loop_infos[0].get("phase_boundary")
    assert not loop_infos[1].get("phase_boundary")
    assert loop_infos[2]["phase_boundary"] is True
    assert loop_infos[2]["recoverable"] is True
    assert loop_infos[2]["reason"] == "verification_not_converging"
    assert loop_infos[2]["verification_stall"] == {
        "fingerprint": ["tests/test_example.py::test_widget"],
        "repeated": 3,
        "threshold": 3,
    }


def test_recoverable_dispatch_result_emits_nonfailed_tool_event(tmp_path: Path):
    runner = _make_runner(tmp_path)
    events = []

    result = runner.handle_dispatch(
        tool_call_id="dispatch-1",
        args={
            "goal": "Fix bug",
            "files": ["a.py"],
            "spec": "Change the implementation in a.py.",
            "acceptance": "Run python -m py_compile a.py.",
        },
        on_event=events.append,
        dispatch_cb=lambda _tool_call_id, _req: WorkerDispatchResult(
            ok=False,
            summary="Worker needs one more pass.",
            needs_followup=True,
            recoverable=True,
        ),
    )

    assert result is not None
    assert result.ok is False
    tool_results = [ev for ev in events if isinstance(ev, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].ok is True
    payload = json.loads(tool_results[0].result)
    assert payload["ok"] is False
    assert payload["recoverable"] is True


def test_handle_dispatch_parses_steps_from_tool_args(tmp_path: Path):
    runner = _make_runner(tmp_path)
    captured = {}
    events = []

    def dispatch_cb(tool_call_id, req):
        captured["tool_call_id"] = tool_call_id
        captured["request"] = req
        return WorkerDispatchResult(ok=True, summary="done")

    result = runner.handle_dispatch(
        tool_call_id="dispatch-steps",
        args={
            "goal": "Complete campaign",
            "files": ["shared.py"],
            "spec": "Use the listed steps to complete the campaign.",
            "acceptance": "Run python -m py_compile shared.py.",
            "summary": "Campaign",
            "steps": [
                {"id": "one", "title": "One", "goal": "First step", "files": ["one.py"]},
                {"id": "two", "title": "Two", "goal": "Second step", "files": ["two.py"]},
                {"id": "three", "title": "Three", "goal": "Third step", "files": ["three.py"]},
            ],
        },
        on_event=events.append,
        dispatch_cb=dispatch_cb,
    )

    assert result is not None
    assert result.ok is True
    assert captured["tool_call_id"] == "dispatch-steps"
    req = captured["request"]
    assert [step.id for step in req.steps] == ["one", "two", "three"]
    assert [step.goal for step in req.steps] == ["First step", "Second step", "Third step"]
    assert req.files == ["shared.py"]
    assert req.spec == "Use the listed steps to complete the campaign."
    dispatch_events = [ev for ev in events if isinstance(ev, WorkerDispatchRequested)]
    assert len(dispatch_events) == 1
    assert [step["id"] for step in dispatch_events[0].steps] == ["one", "two", "three"]


def test_handle_dispatch_without_steps_keeps_flat_request_empty_steps(tmp_path: Path):
    runner = _make_runner(tmp_path)
    captured = {}
    events = []

    def dispatch_cb(_tool_call_id, req):
        captured["request"] = req
        return WorkerDispatchResult(ok=True, summary="done")

    result = runner.handle_dispatch(
        tool_call_id="dispatch-flat",
        args={
            "goal": "Fix bug",
            "files": ["a.py"],
            "spec": "Change the implementation in a.py.",
            "acceptance": "Run python -m py_compile a.py.",
            "summary": "Flat fix",
        },
        on_event=events.append,
        dispatch_cb=dispatch_cb,
    )

    assert result is not None
    assert result.ok is True
    req = captured["request"]
    assert req.steps == []
    assert req.goal == "Fix bug"
    assert req.files == ["a.py"]
    assert req.spec == "Change the implementation in a.py."
    assert req.acceptance == "Run python -m py_compile a.py."
    assert req.summary == "Flat fix"
    dispatch_events = [ev for ev in events if isinstance(ev, WorkerDispatchRequested)]
    assert len(dispatch_events) == 1
    assert dispatch_events[0].steps == []


def test_handle_dispatch_broad_without_steps_proceeds_via_compatibility_plan(tmp_path: Path):
    runner = _make_runner(tmp_path)
    captured = {}
    events = []

    def dispatch_cb(tool_call_id, req):
        captured["tool_call_id"] = tool_call_id
        captured["request"] = req
        return WorkerDispatchResult(ok=True, summary="done")

    result = runner.handle_dispatch(
        tool_call_id="dispatch-broad-flat",
        args={
            "goal": "Build behavioral verification rung",
            "files": ["a.py", "b.py", "c.py"],
            "spec": "Add a validation rung across the dispatch subsystem.",
            "acceptance": "Run compileall and focused dispatch tests.",
            "summary": "Build behavioral verification rung",
        },
        on_event=events.append,
        dispatch_cb=dispatch_cb,
    )

    assert result is not None
    assert result.ok is True
    assert captured["tool_call_id"] == "dispatch-broad-flat"
    assert captured["request"].steps == []
    dispatch_events = [ev for ev in events if isinstance(ev, WorkerDispatchRequested)]
    assert len(dispatch_events) == 1
    assert dispatch_events[0].steps == []
    tool_results = [ev for ev in events if isinstance(ev, ToolResult)]
    assert len(tool_results) == 1
    payload = json.loads(tool_results[0].result)
    assert payload["ok"] is True
    assert "dispatch_campaign_rejected" not in payload.get("extras", {})


def test_recoverable_dispatch_spec_rejection_emits_nonfailed_tool_event(tmp_path: Path):
    runner = _make_runner(tmp_path)
    events = []

    result = runner.handle_dispatch(
        tool_call_id="dispatch-1",
        args={
            "goal": "Fix bug",
            "files": ["a.py"],
            "spec": "Change the implementation in a.py.",
            "acceptance": "",
        },
        on_event=events.append,
        dispatch_cb=lambda _tool_call_id, _req: WorkerDispatchResult(ok=True, summary="unused"),
    )

    assert result is not None
    assert result.ok is False
    assert result.recoverable is True
    tool_results = [ev for ev in events if isinstance(ev, ToolResult)]
    assert len(tool_results) == 1
    assert tool_results[0].ok is True
    assert tool_results[0].extras["dispatch_spec_rejected"] is True
    assert tool_results[0].extras["recoverable"] is True
    payload = json.loads(tool_results[0].result)
    assert payload["ok"] is False
    assert payload["extras"]["dispatch_spec_rejected"] is True
