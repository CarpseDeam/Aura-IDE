from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from aura.bridge.qt_bridge import ConversationBridge
from aura.client import TerminalCommandStarted, TerminalOutput, ToolResult
from aura.conversation.history import History
from aura.conversation.shell_tool import ShellTool
from aura.shell.powershell_session import PowerShellSession, resolve_powershell


@pytest.fixture
def powershell_available() -> None:
    try:
        resolve_powershell()
    except FileNotFoundError:
        pytest.skip("PowerShell is not installed")


def test_persistent_state_streaming_and_nonzero_exit(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    session = PowerShellSession(tmp_path)
    chunks: list[str] = []
    try:
        first = session.execute(
            "Set-Location nested; $env:AURA_PHASE2_ENV='persisted'; "
            "$AURA_PHASE2_VAR='same'; Write-Output 'first'",
            timeout=10,
            on_output=chunks.append,
        )
        second = session.execute(
            "Write-Output ($env:AURA_PHASE2_ENV + '|' + $AURA_PHASE2_VAR + '|' + (Get-Location).Path)",
            timeout=10,
            on_output=chunks.append,
        )
        failed = session.execute("cmd.exe /c exit 7", timeout=10)
    finally:
        session.close()

    assert first.ok and second.ok
    assert "persisted|same|" in second.output
    assert second.cwd == str(nested)
    assert "first" in "".join(chunks)
    assert "__AURA_PS_DONE_" not in "".join(chunks)
    assert failed.ok is False
    assert failed.exit_code == 7
    assert failed.session_reset is False


def test_timeout_and_cancellation_reset_session(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    session = PowerShellSession(tmp_path)
    try:
        timed_out = session.execute("Start-Sleep -Seconds 5", timeout=1)
        after_timeout = session.execute("Write-Output fresh", timeout=10)

        cancel_event = threading.Event()
        result_box: dict[str, object] = {}

        def run() -> None:
            result_box["result"] = session.execute(
                "Start-Sleep -Seconds 5",
                timeout=30,
                cancel_event=cancel_event,
            )

        worker = threading.Thread(target=run)
        worker.start()
        time.sleep(0.2)
        cancel_event.set()
        worker.join(timeout=10)
        cancelled = result_box["result"]
        after_cancel = session.execute("Write-Output clean", timeout=10)
    finally:
        session.close()

    assert timed_out.timed_out and timed_out.session_reset
    assert "session reset" in timed_out.output
    assert after_timeout.ok
    assert cancelled.cancelled and cancelled.session_reset  # type: ignore[union-attr]
    assert after_cancel.ok
    assert after_cancel.session_id != after_timeout.session_id


def test_shell_tool_emits_authoritative_start_and_persists_history(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    history = History()
    tool = ShellTool(history, tmp_path)
    events: list[object] = []
    try:
        payload = tool.handle_terminal_command(
            "call-1",
            {"command": "Write-Output phase2"},
            events.append,
            threading.Event(),
        )
    finally:
        tool.close()

    assert payload is not None
    assert isinstance(events[0], TerminalCommandStarted)
    assert events[0].tool_call_id == "call-1"
    assert isinstance(events[1], TerminalOutput)
    assert isinstance(events[-1], ToolResult)
    assert (json_payload := payload["_terminal_payload"])
    assert json_payload["session_identity"]
    assert json_payload["session_reset"] is False
    assert len(history.messages) == 1


def test_bridge_reset_workspace_change_and_shutdown_close_session(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    bridge = ConversationBridge(None)
    tool_runner = bridge._manager._tool_runner
    assert tool_runner.shell_tool.session.is_running is False
    tool_runner.handle_terminal_command(
        "call-1", {"command": "Write-Output live"}, lambda _event: None, threading.Event()
    )
    old_session = tool_runner.shell_tool.session
    assert old_session.is_running

    bridge.reset_history()
    assert old_session.is_running is False
    new_root = tmp_path / "new-workspace"
    new_root.mkdir()
    bridge.set_workspace_root(new_root)
    assert tool_runner.shell_tool.session.is_running is False
    bridge.shutdown()
