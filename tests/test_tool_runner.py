from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from aura.client.events import TerminalOutput, ToolCallStart, ToolResult
from aura.conversation.history import History
from aura.conversation.loop_detection import LoopDetector
from aura.conversation.tool_runner import ToolRunner
from aura.sandbox import SandboxResult


def _make_runner(tmp_path: Path) -> ToolRunner:
    return ToolRunner(History(), tmp_path, LoopDetector())


def test_terminal_command_default_timeout_is_45_seconds(tmp_path: Path):
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
    assert sandbox.run_terminal_command.call_args.kwargs["timeout"] == 45
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
