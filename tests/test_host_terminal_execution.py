from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from aura.bridge.event_relay import WorkerEventRelay
from aura.client import TerminalOutput, ToolCallArgsDelta, ToolCallEnd, ToolCallStart
from aura.conversation.history import History
from aura.conversation.tool_runner import ToolRunner
from aura.events import (
    WORKER_COMMAND_STARTED,
    WORKER_TOOL_STARTED,
    WORKER_VALIDATION_STARTED,
    EventBus,
)
from aura.sandbox import SandboxExecutor

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows host-launch contract")


@pytest.fixture(autouse=True)
def _host_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "aura.conversation.tool_runner.load_settings",
        lambda: SimpleNamespace(sandbox_mode="host"),
    )


def _system_executable(name: str) -> Path:
    return Path(os.environ["SystemRoot"]) / "System32" / name


def _run_tool(
    root: Path,
    command: str,
    *,
    cwd: str = "",
    timeout: int = 5,
    cancel_event: threading.Event | None = None,
) -> tuple[dict, list[object]]:
    events: list[object] = []
    args: dict[str, object] = {"command": command, "timeout": timeout}
    if cwd:
        args["cwd"] = cwd
    result = ToolRunner(History(), root).handle_terminal_command(
        "terminal-test",
        args,
        events.append,
        cancel_event or threading.Event(),
        "single",
    )
    assert result is not None
    return result["_terminal_payload"], events


def test_absolute_windows_executable_and_arguments_use_real_tool_runner(tmp_path: Path) -> None:
    command = f"{_system_executable('where.exe')} cmd.exe"

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert "cmd.exe" in payload["output"].lower()


def test_quoted_executable_path_and_arguments_with_spaces(tmp_path: Path) -> None:
    spaced_dir = tmp_path / "Tools With Spaces"
    spaced_dir.mkdir()
    executable = spaced_dir / "where.exe"
    shutil.copy2(_system_executable("where.exe"), executable)
    command = f'"{executable}" "cmd.exe"'

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert "cmd.exe" in payload["output"].lower()


def test_cmd_builtins_chaining_pipe_and_redirect(tmp_path: Path) -> None:
    command = (
        "echo argument with spaces && "
        "(echo piped value|findstr /c:\"piped value\") > terminal-output.txt "
        "&& type terminal-output.txt"
    )

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["output"].splitlines() == ["argument with spaces ", "piped value"]
    assert (tmp_path / "terminal-output.txt").read_text().strip() == "piped value"


def test_structured_relative_cwd_is_subprocess_cwd(tmp_path: Path) -> None:
    (tmp_path / "subdirectory").mkdir()

    payload, _ = _run_tool(tmp_path, "echo %CD%", cwd="subdirectory")

    assert payload["ok"] is True
    assert payload["working_directory"] == "subdirectory"
    assert Path(payload["output"].strip()) == tmp_path / "subdirectory"


def test_cd_d_relative_normalizes_to_structured_cwd(tmp_path: Path) -> None:
    (tmp_path / "subdirectory").mkdir()

    payload, _ = _run_tool(tmp_path, "cd /d subdirectory && echo normalized")

    assert payload["ok"] is True
    assert payload["command"] == "echo normalized"
    assert payload["working_directory"] == "subdirectory"
    assert payload["output"].strip() == "normalized"


@pytest.mark.parametrize("cwd", [r"C:\Windows", r"..\outside"])
def test_cd_d_absolute_or_escaping_path_is_rejected(tmp_path: Path, cwd: str) -> None:
    payload, _ = _run_tool(tmp_path, f"cd /d {cwd} && echo should-not-run")

    assert payload["ok"] is False
    assert payload["exit_code"] is None
    assert payload["failure_class"] == "validation_command_unrunnable"
    assert "cwd must" in payload["error"]


@pytest.mark.parametrize("code, expected_ok", [(0, True), (1, False)])
def test_real_exit_code_controls_ok(tmp_path: Path, code: int, expected_ok: bool) -> None:
    payload, _ = _run_tool(tmp_path, f"exit /b {code}")

    assert payload["ok"] is expected_ok
    assert payload["exit_code"] == code


def test_missing_executable_is_execution_failure(tmp_path: Path) -> None:
    payload, _ = _run_tool(tmp_path, "aura_definitely_missing_executable_7f31")

    assert payload["ok"] is False
    assert payload["exit_code"] != 0
    assert payload["failure_class"] == "execution_failed"
    assert payload["terminal_classification"] == "execution_failed"
    assert "not recognized" in payload["output"]


def test_shell_launch_failure_is_execution_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_to_resolve(_command: str):
        raise FileNotFoundError("cmd.exe unavailable")

    monkeypatch.setattr("aura.sandbox._host_shell_invocation", fail_to_resolve)

    result = SandboxExecutor(mode="host", workspace_root=tmp_path).run_terminal_command("echo never-runs")

    assert result.ok is False
    assert result.exit_code == -1
    assert result.failure_class == "execution_failed"
    assert "FileNotFoundError" in result.stdout


def test_streaming_output_is_ordered_and_complete(tmp_path: Path) -> None:
    command = "echo first && ping -n 2 127.0.0.1 >nul && echo second"

    payload, events = _run_tool(tmp_path, command)
    streamed = "".join(event.text for event in events if isinstance(event, TerminalOutput))

    assert payload["ok"] is True
    assert streamed == payload["output"]
    assert streamed.splitlines() == ["first ", "second"]


def test_timeout_and_cancellation_remain_distinct(tmp_path: Path) -> None:
    command = "ping -n 6 127.0.0.1 >nul"
    timed_out, _ = _run_tool(tmp_path, command, timeout=1)
    cancel_event = threading.Event()
    timer = threading.Timer(0.2, cancel_event.set)
    timer.start()
    try:
        cancelled, _ = _run_tool(tmp_path, command, cancel_event=cancel_event)
    finally:
        timer.cancel()

    assert timed_out["ok"] is False
    assert timed_out["exit_code"] == 124
    assert timed_out["failure_class"] == "timeout"
    assert timed_out["timed_out"] is True
    assert timed_out["cancelled"] is False
    assert cancelled["ok"] is False
    assert cancelled["exit_code"] == -1
    assert cancelled["failure_class"] == "cancelled"
    assert cancelled["cancelled"] is True
    assert cancelled["terminal_classification"] == "cancelled"


def test_stdin_support_is_preserved(tmp_path: Path) -> None:
    sandbox = SandboxExecutor(mode="host", workspace_root=tmp_path)

    result = sandbox.run_terminal_command("findstr needle", input_data="needle value\n")

    assert result.ok is True
    assert result.exit_code == 0
    assert result.stdout == "needle value\n"


def test_run_and_watch_uses_same_host_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    original = SandboxExecutor._launch_host_command

    def recording_launcher(self, command, **kwargs):
        calls.append(command)
        return original(self, command, **kwargs)

    monkeypatch.setattr(SandboxExecutor, "_launch_host_command", recording_launcher)
    sandbox = SandboxExecutor(mode="host", workspace_root=tmp_path)

    terminal = sandbox.run_terminal_command("echo terminal")
    watch = sandbox.run_and_watch("echo watched", window_seconds=2)

    assert terminal.ok is True
    assert watch.ok is True
    assert watch.exit_code == 0
    assert calls == ["echo terminal", "echo watched"]


def test_one_lifecycle_start_projection_for_one_terminal_tool_call(tmp_path: Path) -> None:
    bus = EventBus()
    seen = []
    for topic in (WORKER_TOOL_STARTED, WORKER_COMMAND_STARTED, WORKER_VALIDATION_STARTED):
        bus.subscribe(topic, seen.append)
    relay = WorkerEventRelay(MagicMock(), bus)

    def forward(event: object) -> None:
        relay.relay("dispatch-test", event)

    command = "echo lifecycle"
    forward(ToolCallStart(index=0, id="terminal-lifecycle", name="run_terminal_command"))
    forward(ToolCallArgsDelta(index=0, args_chunk=json.dumps({"command": command})))
    forward(ToolCallEnd(index=0))
    ToolRunner(History(), tmp_path).handle_terminal_command(
        "terminal-lifecycle",
        {"command": command},
        forward,
        threading.Event(),
        "single",
    )

    assert sum(event.topic == WORKER_TOOL_STARTED for event in seen) == 1
    assert sum(event.topic == WORKER_COMMAND_STARTED for event in seen) == 1
    assert sum(event.topic == WORKER_VALIDATION_STARTED for event in seen) == 0


def test_validation_start_only_for_genuine_validation(tmp_path: Path) -> None:
    bus = EventBus()
    seen = []
    bus.subscribe(WORKER_VALIDATION_STARTED, seen.append)
    relay = WorkerEventRelay(MagicMock(), bus)
    (tmp_path / "check.py").write_text("value = 1\n", encoding="utf-8")
    command = f'"{sys.executable}" -m py_compile check.py'

    relay.relay("dispatch-test", ToolCallStart(index=0, id="validation", name="run_terminal_command"))
    relay.relay("dispatch-test", ToolCallArgsDelta(index=0, args_chunk=json.dumps({"command": command})))
    relay.relay("dispatch-test", ToolCallEnd(index=0))
    ToolRunner(History(), tmp_path).handle_terminal_command(
        "validation",
        {"command": command},
        lambda event: relay.relay("dispatch-test", event),
        threading.Event(),
        "single",
    )

    assert len(seen) == 1
    assert seen[0].payload["command"] == command


def test_godot_style_absolute_command_reaches_launcher_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []
    original = SandboxExecutor._launch_host_command

    def recording_launcher(self, command, **kwargs):
        seen.append(command)
        return original(self, command, **kwargs)

    monkeypatch.setattr(SandboxExecutor, "_launch_host_command", recording_launcher)
    command = r"C:\Godot_v4.7.1-stable_win64.exe --version"

    payload, _ = _run_tool(tmp_path, command)

    assert seen == [command]
    assert payload["command"] == command
    assert payload["ok"] is False
    assert payload["failure_class"] == "execution_failed"


# ── nested quoting and multiline/JSON python probes ─────────────────────────


def test_python_dash_c_with_escaped_double_quotes(tmp_path: Path) -> None:
    command = r'python -c "print(\"escaped double\")"'

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["output"].strip() == "escaped double"


def test_python_dash_c_with_embedded_json_single_line(tmp_path: Path) -> None:
    command = "python -c \"import json; print(json.dumps({'a': [1, 'x']}))\""

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["output"].strip() == '{"a": [1, "x"]}'


def test_python_dash_c_then_chain(tmp_path: Path) -> None:
    command = 'python -c "print(1)" && echo after'

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["output"].splitlines() == ["1", "after"]


def test_quoted_argument_containing_shell_characters(tmp_path: Path) -> None:
    command = 'echo "quoted & ampersand"'

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert "quoted & ampersand" in payload["output"]


def test_multiline_python_dash_c_json_probe(tmp_path: Path) -> None:
    """cmd.exe cannot carry a newline inside a quoted argument; the launcher
    must run a plain multiline argv directly so the probe keeps its newline."""
    command = "python -c \"import json\nprint(json.dumps({'a': 1}))\""

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True, payload["output"]
    assert payload["output"].strip() == '{"a": 1}'


def test_multiline_python_dash_c_loop_keeps_indentation(tmp_path: Path) -> None:
    command = "python -c \"for i in range(3):\n    print(i)\""

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True, payload["output"]
    assert payload["output"].splitlines() == ["0", "1", "2"]


def test_multiline_command_without_quotes_still_runs(tmp_path: Path) -> None:
    """A multiline command with no quoted-argument newline keeps the CMD
    contract (builtin fallback) and runs line by line."""
    command = "echo line-one\necho line-two"

    payload, _ = _run_tool(tmp_path, command)

    assert payload["ok"] is True
    assert payload["output"].splitlines() == ["line-one", "line-two"]


# ── descendant process tree termination ─────────────────────────────────────


def test_cancellation_kills_descendants_before_they_write_the_marker(
    tmp_path: Path,
) -> None:
    """A delayed marker write from a grandchild of the wrapper shell must
    never land when the terminal call is cancelled."""
    marker = tmp_path / "marker.txt"
    command = (
        "python -c \"import time; time.sleep(30); "
        "open('marker.txt', 'w').write('boom')\""
    )

    cancel_event = threading.Event()
    timer = threading.Timer(1.0, cancel_event.set)
    timer.start()
    try:
        payload, _ = _run_tool(tmp_path, command, cancel_event=cancel_event)
    finally:
        timer.cancel()

    assert payload["ok"] is False
    assert payload["cancelled"] is True
    assert not marker.exists(), (
        "a cancelled terminal call must terminate the complete descendant "
        "tree, not only the wrapper shell"
    )


def test_timeout_kills_descendants_before_they_write_the_marker(tmp_path: Path) -> None:
    marker = tmp_path / "marker.txt"
    command = (
        "python -c \"import time; time.sleep(30); "
        "open('marker.txt', 'w').write('boom')\""
    )

    payload, _ = _run_tool(tmp_path, command, timeout=1)

    assert payload["timed_out"] is True
    assert not marker.exists()
