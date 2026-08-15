from __future__ import annotations

import codecs
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aura.config import get_subprocess_kwargs
from aura.shell.process_tree import terminate_process_tree

_log = logging.getLogger(__name__)
_POLL_SECONDS = 0.05


@dataclass(frozen=True)
class PowerShellCommandResult:
    ok: bool
    stdout: str
    stderr: str
    output: str
    exit_code: int | None
    cwd: str
    timed_out: bool = False
    cancelled: bool = False
    failure_class: str | None = None
    session_id: str = ""
    session_reset: bool = False


QueueItem = tuple[str, str | None]
OutputCallback = Callable[[str], None] | None


def resolve_powershell() -> str:
    candidates = [shutil.which("pwsh")]
    if os.name == "nt":
        candidates.extend(
            [
                os.path.join(os.environ.get("ProgramFiles", ""), "PowerShell", "7", "pwsh.exe"),
                os.path.join(
                    os.environ.get("SystemRoot", r"C:\Windows"),
                    "System32",
                    "WindowsPowerShell",
                    "v1.0",
                    "powershell.exe",
                ),
                shutil.which("powershell"),
            ]
        )
    else:
        candidates.append(shutil.which("powershell"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise FileNotFoundError("Unable to resolve pwsh or Windows PowerShell")


class PowerShellSession:
    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = Path(workspace_root).resolve()
        self._current_cwd = self._workspace_root
        self._process: subprocess.Popen[bytes] | None = None
        self._job: Any | None = None
        self._shell_path = ""
        self._session_id = ""
        self._queue: queue.Queue[QueueItem] | None = None
        self._reader_threads: list[threading.Thread] = []
        self._lock = threading.Lock()

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def current_cwd(self) -> Path:
        return self._current_cwd

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def is_running(self) -> bool:
        process = self._process
        return process is not None and process.poll() is None

    def execute(
        self,
        command: str,
        *,
        timeout: int,
        cancel_event: Any = None,
        on_output: OutputCallback = None,
    ) -> PowerShellCommandResult:
        with self._lock:
            return self._execute_locked(
                command,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output,
            )

    def close(self) -> None:
        with self._lock:
            self._terminate_locked()
            self._reset_process_state()

    def _execute_locked(
        self,
        command: str,
        *,
        timeout: int,
        cancel_event: Any,
        on_output: OutputCallback,
    ) -> PowerShellCommandResult:
        if cancel_event is not None and cancel_event.is_set():
            return self._reset_result(
                output="\n[CANCELLED: PowerShell session reset]\n",
                on_output=on_output,
                cancelled=True,
                failure_class="cancelled",
            )
        try:
            self._ensure_started()
            process = self._process
            output_queue = self._queue
            if process is None or output_queue is None or process.stdin is None:
                raise RuntimeError("PowerShell session did not start")
            marker = f"__AURA_PS_DONE_{uuid.uuid4().hex}__"
            payload = self._protocol_command(command, marker)
            process.stdin.write(payload.encode("utf-8"))
            process.stdin.flush()
            return self._read_until_marker(
                process,
                output_queue,
                marker,
                timeout=timeout,
                cancel_event=cancel_event,
                on_output=on_output,
            )
        except (BrokenPipeError, ConnectionError, OSError, RuntimeError) as exc:
            _log.warning("PowerShell session failed: %s", exc)
            self._terminate_locked()
            message = f"\n[ERROR: PowerShell session failed: {exc}; session reset]\n"
            return self._reset_result(
                output=message,
                on_output=on_output,
                exit_code=-1,
                failure_class="execution_failed",
            )

    def _ensure_started(self) -> None:
        if self.is_running:
            return
        if self._process is not None:
            self._terminate_locked()
            self._reset_process_state()
        shell_path = resolve_powershell()
        kwargs: dict[str, Any] = {
            "args": [
                shell_path,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "-",
            ],
            "cwd": str(self._workspace_root),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "shell": False,
            "text": False,
            "bufsize": 0,
        }
        kwargs.update(get_subprocess_kwargs())
        if os.name == "nt":
            kwargs["creationflags"] = int(kwargs.get("creationflags", 0)) | int(subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(**kwargs)
        self._process = process
        self._shell_path = shell_path
        self._session_id = f"ps-{uuid.uuid4().hex[:12]}"
        self._queue = queue.Queue()
        self._reader_threads = []
        if os.name == "nt":
            from aura.win_job import WindowsJob

            self._job = WindowsJob.try_assign(process)
        for stream, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
            if pipe is not None:
                reader = threading.Thread(
                    target=self._read_pipe,
                    args=(stream, pipe, self._queue),
                    name=f"aura-powershell-{stream}",
                    daemon=True,
                )
                reader.start()
                self._reader_threads.append(reader)
        self._write_startup_encoding(process)

    def _write_startup_encoding(self, process: subprocess.Popen[bytes]) -> None:
        if process.stdin is None:
            return
        startup = (
            "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)\n"
        )
        process.stdin.write(startup.encode("utf-8"))
        process.stdin.flush()

    @staticmethod
    def _protocol_command(command: str, marker: str) -> str:
        return (
            "$global:LASTEXITCODE = 0\n"
            "$__aura_success = $true\n"
            "try {\n"
            f"{command}\n"
            "$__aura_success = $?\n"
            "} catch {\n"
            "  $_ | Out-String | Write-Error\n"
            "  $__aura_success = $false\n"
            "}\n"
            "$__aura_native_exit = $global:LASTEXITCODE\n"
            "$__aura_exit = if ($__aura_success) { "
            "if ($null -eq $__aura_native_exit) { 0 } "
            "else { [int]$__aura_native_exit } "
            "} elseif ($null -ne $__aura_native_exit -and $__aura_native_exit -ne 0) { "
            "[int]$__aura_native_exit } else { 1 }\n"
            "$__aura_cwd = (Get-Location).Path\n"
            "$__aura_record = @{ exit_code = $__aura_exit; cwd = $__aura_cwd } "
            "| ConvertTo-Json -Compress\n"
            f"[Console]::Out.WriteLine('{marker}' + $__aura_record)\n\n"
        )

    @staticmethod
    def _read_pipe(
        stream: str,
        pipe: Any,
        output_queue: queue.Queue[QueueItem],
    ) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        try:
            while True:
                chunk = pipe.read(4096)
                if not chunk:
                    break
                output_queue.put((stream, decoder.decode(chunk)))
            tail = decoder.decode(b"", final=True)
            if tail:
                output_queue.put((stream, tail))
        except (OSError, ValueError):
            pass
        finally:
            output_queue.put((stream, None))

    def _read_until_marker(
        self,
        process: subprocess.Popen[bytes],
        output_queue: queue.Queue[QueueItem],
        marker: str,
        *,
        timeout: int,
        cancel_event: Any,
        on_output: OutputCallback,
    ) -> PowerShellCommandResult:
        started = time.monotonic()
        stdout_buffer = ""
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        output_parts: list[str] = []
        record: dict[str, Any] | None = None

        def emit(stream: str, text: str) -> None:
            if not text:
                return
            output_parts.append(text)
            if stream == "stdout":
                stdout_parts.append(text)
            else:
                stderr_parts.append(text)
            if on_output is not None:
                on_output(text)

        def consume(stream: str, text: str | None) -> None:
            nonlocal stdout_buffer, record
            if text is None:
                return
            if stream != "stdout":
                emit(stream, text)
                return
            if record is not None:
                return
            stdout_buffer += text
            marker_index = stdout_buffer.find(marker)
            if marker_index < 0:
                safe_length = max(0, len(stdout_buffer) - len(marker) + 1)
                if safe_length:
                    emit("stdout", stdout_buffer[:safe_length])
                    stdout_buffer = stdout_buffer[safe_length:]
                return
            before = stdout_buffer[:marker_index]
            if before:
                emit("stdout", before)
            record_end = stdout_buffer.find("\n", marker_index + len(marker))
            if record_end < 0:
                stdout_buffer = stdout_buffer[marker_index:]
                return
            raw_record = stdout_buffer[marker_index + len(marker) : record_end].strip()
            try:
                parsed = json.loads(raw_record)
            except json.JSONDecodeError:
                emit("stdout", stdout_buffer[marker_index : record_end + 1])
                stdout_buffer = stdout_buffer[record_end + 1 :]
                return
            if isinstance(parsed, dict):
                record = parsed
            stdout_buffer = ""

        def flush_stdout_buffer() -> None:
            nonlocal stdout_buffer
            if stdout_buffer:
                emit("stdout", stdout_buffer)
                stdout_buffer = ""

        while record is None:
            if cancel_event is not None and cancel_event.is_set():
                self._terminate_locked()
                self._drain_queue(output_queue, consume)
                flush_stdout_buffer()
                return self._reset_result(
                    output="\n[CANCELLED: PowerShell session reset]\n",
                    stdout="".join(stdout_parts),
                    stderr="".join(stderr_parts),
                    on_output=on_output,
                    exit_code=-1,
                    cancelled=True,
                    failure_class="cancelled",
                )
            if time.monotonic() - started >= max(1, timeout):
                self._terminate_locked()
                self._drain_queue(output_queue, consume)
                flush_stdout_buffer()
                return self._reset_result(
                    output=(f"\n[ERROR: Command timed out after {timeout} seconds; PowerShell session reset]\n"),
                    stdout="".join(stdout_parts),
                    stderr="".join(stderr_parts),
                    on_output=on_output,
                    exit_code=124,
                    timed_out=True,
                    failure_class="timeout",
                )
            try:
                stream, text = output_queue.get(timeout=_POLL_SECONDS)
                consume(stream, text)
            except queue.Empty:
                if process.poll() is not None:
                    self._drain_queue(output_queue, consume)
                    flush_stdout_buffer()
                    return self._unexpected_exit(
                        process,
                        stdout="".join(stdout_parts),
                        stderr="".join(stderr_parts),
                        output="".join(output_parts),
                        on_output=on_output,
                    )
        self._drain_queue(output_queue, consume)
        cwd = str(record.get("cwd") or self._current_cwd)
        try:
            self._current_cwd = Path(cwd).resolve()
        except OSError:
            pass
        exit_code = _as_int(record.get("exit_code"), default=1)
        return PowerShellCommandResult(
            ok=exit_code == 0,
            stdout="".join(stdout_parts),
            stderr="".join(stderr_parts),
            output="".join(output_parts),
            exit_code=exit_code,
            cwd=cwd,
            session_id=self._session_id,
        )

    def _unexpected_exit(
        self,
        process: subprocess.Popen[bytes],
        *,
        stdout: str,
        stderr: str,
        output: str,
        on_output: OutputCallback,
    ) -> PowerShellCommandResult:
        self._terminate_locked()
        message = "\n[ERROR: PowerShell session exited unexpectedly; session reset]\n"
        if on_output is not None:
            on_output(message)
        result = PowerShellCommandResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            output=output + message,
            exit_code=process.returncode if process.returncode is not None else -1,
            cwd=str(self._current_cwd),
            failure_class="shell_exited",
            session_id=self._session_id,
            session_reset=True,
        )
        self._reset_process_state()
        return result

    def _reset_result(
        self,
        *,
        output: str,
        on_output: OutputCallback,
        stdout: str = "",
        stderr: str = "",
        exit_code: int | None = None,
        timed_out: bool = False,
        cancelled: bool = False,
        failure_class: str | None = None,
    ) -> PowerShellCommandResult:
        if on_output is not None:
            on_output(output)
        result = PowerShellCommandResult(
            ok=False,
            stdout=stdout,
            stderr=stderr,
            output=stdout + stderr + output,
            exit_code=exit_code,
            cwd=str(self._current_cwd),
            timed_out=timed_out,
            cancelled=cancelled,
            failure_class=failure_class,
            session_id=self._session_id,
            session_reset=True,
        )
        self._reset_process_state()
        return result

    def _drain_queue(self, output_queue: queue.Queue[QueueItem], consume: Callable[..., None]) -> None:
        while True:
            try:
                stream, text = output_queue.get_nowait()
            except queue.Empty:
                return
            consume(stream, text)

    def _terminate_locked(self) -> None:
        process = self._process
        if process is None:
            return
        job = self._job
        self._job = None
        terminate_process_tree(process, job, self._reader_threads)

    def _reset_process_state(self) -> None:
        self._process = None
        self._queue = None
        self._reader_threads = []
        self._job = None
        self._shell_path = ""
        self._session_id = ""


def _as_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


__all__ = ["PowerShellCommandResult", "PowerShellSession", "resolve_powershell"]
