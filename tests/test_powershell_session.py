from __future__ import annotations

import queue
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


def test_lifecycle_flags_and_ordinary_cd_persist(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    session = PowerShellSession(tmp_path)
    try:
        first = session.execute("cd nested", timeout=10)
        second = session.execute("$AURA_VALUE='kept'; Write-Output (Get-Location).Path", timeout=10)
        third = session.execute("Write-Output $AURA_VALUE", timeout=10)
    finally:
        session.close()

    assert first.ok and first.cwd == str(nested)
    assert first.session_started is True
    assert first.session_restarted is False
    assert second.session_started is False
    assert second.session_restarted is False
    assert third.session_id == first.session_id == second.session_id
    assert "kept" in third.output


def test_timeout_and_cancellation_reset_session(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    session = PowerShellSession(tmp_path)
    try:
        setup = session.execute(
            "cd nested; $env:AURA_RESET_PROOF='timeout-state'",
            timeout=10,
        )
        timed_out = session.execute("Start-Sleep -Seconds 5", timeout=1)
        after_timeout = session.execute(
            "Write-Output ((Get-Location).Path + '|' + $env:AURA_RESET_PROOF)",
            timeout=10,
        )
        session.execute("cd nested; $env:AURA_RESET_PROOF='cancel-state'", timeout=10)

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
        after_cancel = session.execute(
            "Write-Output ((Get-Location).Path + '|' + $env:AURA_RESET_PROOF)",
            timeout=10,
        )
    finally:
        session.close()

    assert setup.ok and setup.cwd == str(nested)
    assert timed_out.timed_out and timed_out.session_reset
    assert timed_out.cwd == str(tmp_path)
    assert "session reset" in timed_out.output
    assert after_timeout.ok
    assert after_timeout.cwd == str(tmp_path)
    assert after_timeout.session_started is False
    assert after_timeout.session_restarted is True
    assert after_timeout.session_id != timed_out.session_id
    assert "timeout-state" not in after_timeout.output
    assert cancelled.cancelled and cancelled.session_reset  # type: ignore[union-attr]
    assert cancelled.cwd == str(tmp_path)  # type: ignore[union-attr]
    assert after_cancel.ok
    assert after_cancel.cwd == str(tmp_path)
    assert after_cancel.session_restarted is True
    assert after_cancel.session_id != after_timeout.session_id
    assert "cancel-state" not in after_cancel.output


def test_unexpected_exit_and_explicit_close_reset_process_owned_state(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    session = PowerShellSession(tmp_path)
    try:
        setup = session.execute("cd nested; $AURA_EXIT_PROOF='old'", timeout=10)
        exited = session.execute("exit 23", timeout=10)
        after_exit = session.execute(
            "Write-Output ((Get-Location).Path + '|' + $AURA_EXIT_PROOF)",
            timeout=10,
        )
        identity_after_exit = after_exit.session_id
        session.execute("cd nested; $AURA_EXIT_PROOF='closed'", timeout=10)
        session.close()
        assert session.current_cwd == tmp_path.resolve()
        after_close = session.execute(
            "Write-Output ((Get-Location).Path + '|' + $AURA_EXIT_PROOF)",
            timeout=10,
        )
    finally:
        session.close()

    assert setup.ok
    assert exited.session_reset is True
    assert exited.failure_class == "shell_exited"
    assert exited.cwd == str(tmp_path)
    assert after_exit.session_restarted is True
    assert after_exit.session_id != setup.session_id
    assert after_exit.cwd == str(tmp_path)
    assert "old" not in after_exit.output
    assert after_close.session_restarted is True
    assert after_close.session_id != identity_after_exit
    assert after_close.cwd == str(tmp_path)
    assert "closed" not in after_close.output


def test_stdout_and_stderr_boundaries_do_not_bleed(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    session = PowerShellSession(tmp_path)
    try:
        first = session.execute(
            "1..400 | ForEach-Object { "
            '[Console]::Out.WriteLine("OUT_A_$($_)"); '
            '[Console]::Error.WriteLine("ERR_A_$($_)") }',
            timeout=20,
        )
        second = session.execute(
            "[Console]::Out.WriteLine('OUT_B'); [Console]::Error.WriteLine('ERR_B')",
            timeout=10,
        )
    finally:
        session.close()

    assert first.ok and second.ok
    assert "OUT_A_1" in first.stdout and "OUT_A_400" in first.stdout
    assert "ERR_A_1" in first.stderr and "ERR_A_400" in first.stderr
    assert "OUT_B" not in first.output and "ERR_B" not in first.output
    assert "OUT_A_" not in second.output and "ERR_A_" not in second.output
    assert "OUT_B" in second.stdout and "ERR_B" in second.stderr
    assert "__AURA_PS_" not in first.output + second.output


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
    assert json_payload["session_started"] is True
    assert json_payload["session_restarted"] is False
    assert json_payload["session_reset"] is False
    assert json_payload["starting_cwd"] == str(tmp_path)
    assert json_payload["actual_cwd"] == str(tmp_path)
    assert len(history.messages) == 1


def test_run_terminal_command_accepts_and_persists_ordinary_cd(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    structured = tmp_path / "structured"
    structured.mkdir()
    tool = ShellTool(History(), tmp_path)
    try:
        changed = tool.handle_terminal_command(
            "cd",
            {"command": "cd nested"},
            lambda _event: None,
            threading.Event(),
        )
        observed = tool.handle_terminal_command(
            "pwd",
            {"command": "Write-Output (Get-Location).Path"},
            lambda _event: None,
            threading.Event(),
        )
        structured_change = tool.handle_terminal_command(
            "structured",
            {"command": "Write-Output (Get-Location).Path", "cwd": "structured"},
            lambda _event: None,
            threading.Event(),
        )
        structured_followup = tool.handle_terminal_command(
            "structured-followup",
            {"command": "Write-Output (Get-Location).Path"},
            lambda _event: None,
            threading.Event(),
        )
    finally:
        tool.close()

    assert changed is not None and observed is not None
    assert structured_change is not None and structured_followup is not None
    changed_payload = changed["_terminal_payload"]
    observed_payload = observed["_terminal_payload"]
    assert changed_payload["ok"] is True
    assert changed_payload["actual_cwd"] == str(nested)
    assert observed_payload["starting_cwd"] == str(nested)
    assert observed_payload["actual_cwd"] == str(nested)
    assert observed_payload["session_identity"] == changed_payload["session_identity"]
    assert structured_change["_terminal_payload"]["starting_cwd"] == str(structured)
    assert structured_change["_terminal_payload"]["actual_cwd"] == str(structured)
    assert structured_followup["_terminal_payload"]["actual_cwd"] == str(structured)


def test_preexisting_cancellation_resets_without_start_event(
    tmp_path: Path,
    powershell_available: None,
) -> None:
    history = History()
    tool = ShellTool(history, tmp_path)
    try:
        tool.handle_terminal_command(
            "setup",
            {"command": "$AURA_VALUE='old'; cd ."},
            lambda _event: None,
            threading.Event(),
        )
        old_identity = tool.session.session_id
        cancelled = threading.Event()
        cancelled.set()
        events: list[object] = []
        payload = tool.handle_terminal_command(
            "cancelled",
            {"command": "Write-Output should-not-run"},
            events.append,
            cancelled,
        )
        after = tool.handle_terminal_command(
            "after",
            {"command": "Write-Output ((Get-Location).Path + '|' + $AURA_VALUE)"},
            lambda _event: None,
            threading.Event(),
        )
    finally:
        tool.close()

    assert payload is not None and after is not None
    assert not any(isinstance(event, TerminalCommandStarted) for event in events)
    assert len(events) == 1 and isinstance(events[0], ToolResult)
    cancelled_payload = payload["_terminal_payload"]
    assert cancelled_payload["session_reset"] is True
    assert cancelled_payload["session_identity"] == old_identity
    assert cancelled_payload["actual_cwd"] == str(tmp_path)
    after_payload = after["_terminal_payload"]
    assert after_payload["session_restarted"] is True
    assert after_payload["session_identity"] != old_identity
    assert after_payload["starting_cwd"] == str(tmp_path)
    assert after_payload["actual_cwd"] == str(tmp_path)
    assert "old" not in after_payload["output"]


def test_resolution_failure_returns_result_without_start_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aura.shell.powershell_session.resolve_powershell",
        lambda: (_ for _ in ()).throw(FileNotFoundError("missing PowerShell")),
    )
    tool = ShellTool(History(), tmp_path)
    events: list[object] = []
    payload = tool.handle_terminal_command(
        "missing-shell",
        {"command": "Write-Output nope"},
        events.append,
        threading.Event(),
    )

    assert payload is not None
    assert len(events) == 1 and isinstance(events[0], ToolResult)
    result = payload["_terminal_payload"]
    assert result["failure_class"] == "execution_failed"
    assert result["session_reset"] is True
    assert result["session_started"] is False
    assert result["session_restarted"] is False
    assert result["actual_cwd"] == str(tmp_path)
    assert result["submitted"] is False
    assert result["executed_command"] == ""


def test_stdin_write_failure_does_not_report_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenStdin:
        def write(self, _payload: bytes) -> None:
            raise BrokenPipeError("closed")

    class FakeProcess:
        stdin = BrokenStdin()

        @staticmethod
        def poll():
            return None

    session = PowerShellSession(tmp_path)

    def pretend_started() -> bool:
        session._process = FakeProcess()  # type: ignore[assignment]
        session._queue = queue.Queue()
        session._session_id = "ps-write-failure"
        return True

    monkeypatch.setattr(session, "_ensure_started", pretend_started)
    monkeypatch.setattr(session, "_terminate_locked", lambda: None)
    submissions: list[bool] = []

    result = session.execute(
        "Write-Output nope",
        timeout=10,
        on_submitted=lambda: submissions.append(True),
    )

    assert submissions == []
    assert result.submitted is False
    assert result.session_reset is True
    assert result.cwd == str(tmp_path)


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
    old_identity = old_session.session_id
    assert old_session.is_running

    bridge.reset_history()
    assert old_session.is_running is False
    reset_result = tool_runner.handle_terminal_command(
        "call-2", {"command": "Write-Output reset"}, lambda _event: None, threading.Event()
    )
    assert reset_result is not None
    assert reset_result["_terminal_payload"]["session_started"] is True
    assert reset_result["_terminal_payload"]["session_restarted"] is False
    assert reset_result["_terminal_payload"]["session_identity"] != old_identity
    new_root = tmp_path / "new-workspace"
    new_root.mkdir()
    reset_session = tool_runner.shell_tool.session
    bridge.set_workspace_root(new_root)
    assert reset_session.is_running is False
    workspace_result = tool_runner.handle_terminal_command(
        "call-3", {"command": "Write-Output (Get-Location).Path"}, lambda _event: None, threading.Event()
    )
    assert workspace_result is not None
    workspace_payload = workspace_result["_terminal_payload"]
    assert workspace_payload["session_started"] is True
    assert workspace_payload["session_restarted"] is False
    assert workspace_payload["starting_cwd"] == str(new_root)
    assert workspace_payload["actual_cwd"] == str(new_root)
    bridge.shutdown()
